"""GitHub deep transform: keys, GPG UIDs, plus the existing github_deep harvest.

Three layers:

1. **`github.com/<u>.keys`** — every public SSH key the user has uploaded.
   These are stamped on the Account node as `ssh_keys` (with fingerprint
   hashes) and a `ssh_key_count` summary. Not directly identifying but
   useful for cross-referencing other accounts that publish the same key
   (a real OSINT pivot — same SSH key on GitLab/SourceHut = same person).

2. **`github.com/<u>.gpg`** — armored ASCII GPG keys, which include UID
   blocks like `Alice <alice@example.com>`. Extracts those emails into
   Email nodes (often the *real* email behind the noreply commit address).

3. **`github_deep.enrich_one`** — runs the existing rich harvest
   (orgs, starred repos, commit-email leak, verified social accounts)
   and maps each result into graph nodes.

Auth-free. No API key required. Subject to GitHub's 60/hr unauth rate
limit, so per-process concurrency is kept low.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import sys
from typing import Any

import aiohttp

from graph.model import Graph, Node
from graph.transforms import transform


_USER_AGENT = "Phantom-OSINT/2.0 (public-data enrichment)"
_TIMEOUT = 12.0

# GPG UID regex - armored .gpg responses include UIDs in their header
# comments after `:uid:::::::N::Name <email>:`. Regex out `<email@host>`.
_GPG_EMAIL_RE = re.compile(
    r"<([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})>"
)


@transform(input="Account", produces=("Email", "Url"))
async def github_account_deep(node: Node, g: Graph) -> None:
    """For GitHub accounts only: harvest .keys / .gpg / deep-API data."""
    if (node.attrs.get("site") or "").lower() != "github":
        return
    handle = (node.attrs.get("handle") or "").strip()
    if not handle:
        return

    timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
    async with aiohttp.ClientSession(
        timeout=timeout,
        headers={"User-Agent": _USER_AGENT},
    ) as session:
        keys_task = asyncio.create_task(_fetch_ssh_keys(session, handle))
        gpg_task = asyncio.create_task(_fetch_gpg_emails(session, handle))
        deep_task = asyncio.create_task(_run_existing_deep(handle))
        ssh_keys, gpg_emails, deep = await asyncio.gather(
            keys_task, gpg_task, deep_task,
            return_exceptions=True,
        )

    # SSH keys → Account.attrs.
    enriched = False
    if isinstance(ssh_keys, list) and ssh_keys:
        node.attrs.setdefault("ssh_keys", ssh_keys)
        node.attrs.setdefault("ssh_key_count", len(ssh_keys))
        enriched = True

    # GPG UID emails → Email nodes.
    if isinstance(gpg_emails, list) and gpg_emails:
        for addr in gpg_emails:
            email_node = g.add_node("Email", source="github_keys", address=addr)
            g.add_edge(node.id, email_node.id, "has_email", source="github_gpg")
        enriched = True

    # Deep API enrichment.
    if isinstance(deep, dict) and deep:
        _absorb_deep(node, deep, g)
        enriched = True

    if enriched and "github_keys" not in node.sources:
        node.sources.append("github_keys")


async def _fetch_ssh_keys(session: aiohttp.ClientSession, handle: str) -> list[dict]:
    """Return a list of {type, fingerprint_sha256, length} dicts."""
    try:
        async with session.get(f"https://github.com/{handle}.keys") as resp:
            if resp.status != 200:
                return []
            body = await resp.text(errors="replace")
    except (aiohttp.ClientError, OSError):
        return []
    out: list[dict] = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        ktype, b64 = parts[0], parts[1]
        try:
            import base64
            raw = base64.b64decode(b64)
            fp = hashlib.sha256(raw).digest()
            import base64 as _b
            fp_b64 = _b.b64encode(fp).decode("ascii").rstrip("=")
            out.append({
                "type": ktype,
                "fingerprint_sha256": f"SHA256:{fp_b64}",
            })
        except Exception:
            out.append({"type": ktype, "fingerprint_sha256": None})
    return out


async def _fetch_gpg_emails(session: aiohttp.ClientSession, handle: str) -> list[str]:
    """Return distinct emails found in the user's published GPG keys."""
    try:
        async with session.get(f"https://github.com/{handle}.gpg") as resp:
            if resp.status != 200:
                return []
            body = await resp.text(errors="replace")
    except (aiohttp.ClientError, OSError):
        return []
    found = set()
    for m in _GPG_EMAIL_RE.finditer(body):
        addr = m.group(1).strip().lower()
        # Skip GitHub noreply addresses - non-contactable.
        if addr.endswith("@users.noreply.github.com"):
            continue
        if addr.endswith("@noreply.github.com"):
            continue
        found.add(addr)
    return sorted(found)


async def _run_existing_deep(handle: str) -> dict:
    """Reuse github_deep.enrich_one for orgs / starred / commit-email / social."""
    try:
        import github_deep
        return await github_deep.enrich_one(handle)
    except Exception as e:
        print(f"github_keys: deep enrich failed for {handle}: {e}", file=sys.stderr)
        return {}


def _absorb_deep(node: Node, deep: dict, g: Graph) -> None:
    """Map github_deep.enrich_one output into graph nodes."""
    # Commit email - promote to a top-level Email node.
    commit_email = deep.get("commit_email")
    if isinstance(commit_email, str) and "@" in commit_email:
        email_node = g.add_node(
            "Email",
            source="github_keys",
            address=commit_email.strip().lower(),
            via="commit_patch",
            source_url=deep.get("commit_email_source"),
        )
        g.add_edge(node.id, email_node.id, "has_email", source="github_commit")

    # Verified social accounts → Url nodes.
    socials = deep.get("verified_social_accounts") or []
    if isinstance(socials, list):
        for s in socials:
            if not isinstance(s, dict):
                continue
            url = s.get("url")
            if isinstance(url, str) and url.strip():
                u = g.add_node(
                    "Url",
                    source="github_keys",
                    url=url.strip(),
                    via="github_verified_social",
                    provider=s.get("provider"),
                )
                g.add_edge(node.id, u.id, "linked", role="verified_social")

    # Stash heavier collections on the Account node itself for downstream UI.
    for key in ("organizations", "recent_starred"):
        if deep.get(key) and key not in node.attrs:
            node.attrs[key] = deep[key]

    if deep.get("commit_email_note") and "commit_email_note" not in node.attrs:
        node.attrs["commit_email_note"] = deep["commit_email_note"]
