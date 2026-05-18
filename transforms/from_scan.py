"""Boundary adapter: scan results → typed graph nodes.

This is NOT a `@transform` — it's the entry point that converts the
existing `list[CheckResult]` from `scanner.Phantom.run_many` into a
graph. Once the graph exists, every other transform fires off node
kinds rather than off CheckResults.

Mapping:
    CheckResult(exists=True, site, url, profile, variant)
        → Account(site, handle=variant, url, ...profile attrs)
    profile["photo"]  → Photo(url) + has_photo edge
    profile["bio"]    → Bio(text)  + has_bio edge
    profile["location"] → Location(label) + located edge
    profile["linked_accounts"][*] → Url + linked edge
    profile email-like fields → Email + has_email edge (Account→Email goes
        via the eventual Identity, but for Phase 1 we attach to Account
        directly with has_email; correlate_photo promotes to Identity later.)
"""
from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import urlsplit

from graph.model import Graph, Node


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Profile dict keys that often carry an email value verbatim.
_EMAIL_KEYS = ("email", "public_email", "commit_email", "contact_email")


def adapt(grouped: list[tuple[str, list[Any]]] | list[Any], source: str = "scan") -> Graph:
    """Build a fresh graph from scan results.

    Accepts either the `run_many` shape `[(variant, [CheckResult])]` or a
    flat `list[CheckResult]`. Only `exists is True` rows are projected
    into the graph — UNKNOWN / MISSING entries do not become Accounts.
    """
    g = Graph()
    for r in _iter_results(grouped):
        if getattr(r, "exists", None) is not True:
            continue
        _add_result(g, r, source)
    return g


def _iter_results(obj) -> Iterable[Any]:
    if isinstance(obj, list) and obj and isinstance(obj[0], tuple):
        for _, rs in obj:
            for r in rs:
                yield r
    elif isinstance(obj, list):
        yield from obj
    else:
        # Defensive: a generator or other iterable.
        yield from obj  # type: ignore[misc]


def _add_result(g: Graph, r: Any, source: str) -> Node:
    profile: dict = getattr(r, "profile", None) or {}
    site = getattr(r, "site", "") or ""
    handle = getattr(r, "variant", None) or ""
    url = getattr(r, "url", "") or ""

    # The Account is the root entity for this check.
    account_attrs = {
        "site": site,
        "handle": handle,
        "url": url,
        "category": getattr(r, "category", None),
        "reliability": getattr(r, "reliability", None),
        "tier": getattr(r, "tier", None),
        "score": getattr(r, "score", None),
        # Surface profile fields directly for convenience in the UI.        "display_name": profile.get("display_name"),
        "follower_count": profile.get("follower_count"),
        "following_count": profile.get("following_count"),
        "post_count": profile.get("post_count"),
        "joined_date": profile.get("joined_date") or profile.get("created_at"),
        "verified": profile.get("verified"),
        "language": profile.get("language"),
        "language_label": profile.get("language_label"),
        # Numeric user_id - required by transforms/snowflake.py to
        # decode account-creation timestamps. Pulled by enrich.py's
        # Twitter and Threads extractors.        "user_id": profile.get("user_id"),
    }
    account = g.add_node("Account", source=source, **account_attrs)

    # Also stash a Username node so cross-platform same-handle correlation
    # (transforms/correlate_handle.py) has something to fire on.
    if handle:
        uname = g.add_node("Username", source=source, handle=handle)
        g.add_edge(account.id, uname.id, "linked", role="username")

    # Domain of the account URL.
    if url:
        host = (urlsplit(url).netloc or "").lower()
        if host:
            dom = g.add_node("Domain", source=source, host=host)
            g.add_edge(account.id, dom.id, "linked", role="hosted_on")

    # Photo.
    photo_url = profile.get("photo")
    if photo_url:
        photo = g.add_node("Photo", source=source, url=photo_url)
        g.add_edge(account.id, photo.id, "has_photo")

    # Bio.
    bio_text = profile.get("bio")
    if isinstance(bio_text, str) and bio_text.strip():
        bio = g.add_node(
            "Bio", source=source,
            text=bio_text.strip(),
            language=profile.get("language"),
        )
        g.add_edge(account.id, bio.id, "has_bio")

    # Location.
    loc = profile.get("location")
    if isinstance(loc, str) and loc.strip():
        location = g.add_node(
            "Location", source=source,
            label=loc.strip(),
            country=profile.get("country"),
        )
        g.add_edge(account.id, location.id, "located")

    # Linked accounts (outbound URLs from bio / linked_accounts field).
    linked = profile.get("linked_accounts") or []
    if isinstance(linked, list):
        for link in linked:
            if isinstance(link, str) and link.strip():
                u = g.add_node("Url", source=source, url=link.strip())
                g.add_edge(account.id, u.id, "linked")
            elif isinstance(link, dict) and link.get("url"):
                u = g.add_node("Url", source=source, url=link["url"], **{k: v for k, v in link.items() if k != "url"})
                g.add_edge(account.id, u.id, "linked")

    # Emails - explicit fields plus bio harvest.
    for key in _EMAIL_KEYS:
        val = profile.get(key)
        if isinstance(val, str) and "@" in val:
            for m in _EMAIL_RE.findall(val):
                _attach_email(g, account, m, source)
    if isinstance(bio_text, str):
        for m in _EMAIL_RE.findall(bio_text):
            _attach_email(g, account, m, source)
    # Hunter.io / emails.py attaches a list under profile["emails"].
    emails_field = profile.get("emails")
    if isinstance(emails_field, list):
        for item in emails_field:
            addr = item.get("value") if isinstance(item, dict) else item
            if isinstance(addr, str):
                _attach_email(g, account, addr, source)

    return account


def _attach_email(g: Graph, account: Node, address: str, source: str) -> None:
    addr = address.strip().lower()
    if not _EMAIL_RE.fullmatch(addr):
        return
    email = g.add_node("Email", source=source, address=addr)
    g.add_edge(account.id, email.id, "has_email")
