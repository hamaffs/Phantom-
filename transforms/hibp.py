"""HIBP transform: Email → Breach[].

Calls Have I Been Pwned v3 (`/breachedaccount/{email}?truncateResponse=false`).
Requires a paid API key (configure with `phantom --api add hibp KEY`).
Without a key, this transform is silently skipped by the runner.

Emits one `Breach` node per breach the email appears in, with an
`appeared_in` edge from the Email node. HIBP also surfaces metadata
(BreachDate, DataClasses, PwnCount) which we attach to the Breach node
for downstream display.

Free-tier alternative (Pastes endpoint) is not used here — the v3
breaches endpoint is the one most analysts care about.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

import aiohttp

import apis
from graph.model import Graph, Node
from graph.transforms import transform


_HIBP_URL = "https://haveibeenpwned.com/api/v3/breachedaccount/{email}"
_USER_AGENT = "Phantom-OSINT"
_TIMEOUT = 15.0


@transform(input="Email", produces=("Breach",), needs_key="hibp")
async def query_hibp(node: Node, g: Graph) -> None:
    email = (node.attrs.get("address") or "").strip().lower()
    if not email or "@" not in email:
        return
    key = apis.get("hibp")
    if not key:  # Defensive — runner already filters, but make it standalone-safe.
        return

    headers = {
        "hibp-api-key": key,
        "user-agent": _USER_AGENT,
        "accept": "application/json",
    }
    url = _HIBP_URL.format(email=email) + "?truncateResponse=false"
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 404:
                    # HIBP returns 404 when the email has no breach hits.
                    return
                if resp.status == 401:
                    print("hibp: invalid API key", file=sys.stderr)
                    return
                if resp.status == 429:
                    # Rate-limited - back off briefly and retry once.
                    retry_after = float(resp.headers.get("retry-after", "2"))
                    await asyncio.sleep(min(retry_after, 10.0))
                    async with session.get(url, headers=headers) as r2:
                        if r2.status != 200:
                            return
                        data = await r2.json(content_type=None)
                elif resp.status != 200:
                    return
                else:
                    data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(f"hibp: request failed for {email}: {e}", file=sys.stderr)
            return

    if not isinstance(data, list):
        return

    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("Name") or entry.get("Title")
        if not isinstance(name, str) or not name:
            continue
        attrs: dict[str, Any] = {
            "name": name,
            "title": entry.get("Title"),
            "breach_date": entry.get("BreachDate"),
            "added_date": entry.get("AddedDate"),
            "pwn_count": entry.get("PwnCount"),
            "data_classes": entry.get("DataClasses"),
            "description": entry.get("Description"),
            "is_verified": entry.get("IsVerified"),
            "is_sensitive": entry.get("IsSensitive"),
            "domain": entry.get("Domain"),
        }
        breach = g.add_node("Breach", source="hibp", **attrs)
        g.add_edge(node.id, breach.id, "appeared_in", source="hibp")
