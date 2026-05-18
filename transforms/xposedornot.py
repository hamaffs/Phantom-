"""XposedOrNot transform — Email → list of public data breaches.

Free, no auth. XposedOrNot is an independent breach aggregator
maintained as a free public service (alternative to HIBP). Two
endpoints we use:

  - `/v1/check-email/{email}` → fast "is the email in any breach?" check,
    returns just a list of breach IDs the email appears in.
  - `/v1/breaches?email={email}` → full breach details: breach date,
    domain, industry, password risk, exposed data classes, breach
    description.

Per breach we get back, we emit one `Breach` node attached to the
Email via `appeared_in`. Differences from HudsonRock (infostealer logs)
and ProxyNova (combo lists):

  - XposedOrNot tracks publicly-known **breaches** (companies that
    disclosed they got hacked) — same lane HIBP plays in.
  - Has descriptive metadata HIBP charges for: industry, risk_score,
    password strength buckets.

Note: the schema's `passwordRisk` field can be "easytocrack",
"strongHash", "plainText", "unknown" — useful signal we surface as a
Breach attr.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

import aiohttp

from graph.model import Graph, Node
from graph.transforms import transform


_USER_AGENT = "Phantom-OSINT"
_TIMEOUT = 15.0
_BASE = "https://api.xposedornot.com/v1"


@transform(input="Email", produces=("Breach",))
async def query_xposedornot(node: Node, g: Graph) -> None:
    address = (node.attrs.get("address") or "").strip().lower()
    if not address or "@" not in address:
        return

    timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        # Step 1: quick check.
        try:
            async with session.get(f"{_BASE}/check-email/{address}") as resp:
                if resp.status == 404:
                    return  # email not in any indexed breach
                if resp.status != 200:
                    return
                check_data = await resp.json(content_type=None)
        except (aiohttp.ClientError, OSError, ValueError) as e:
            print(f"xposedornot: check-email failed for {address}: {e}", file=sys.stderr)
            return

        # XposedOrNot returns {"breaches": [[<flat list of breach names>]]}
        breaches_list = check_data.get("breaches") if isinstance(check_data, dict) else None
        if not isinstance(breaches_list, list) or not breaches_list:
            return
        # Flatten the doubly-nested list.
        breach_names = []
        for inner in breaches_list:
            if isinstance(inner, list):
                breach_names.extend(b for b in inner if isinstance(b, str))
            elif isinstance(inner, str):
                breach_names.append(inner)
        if not breach_names:
            return

        # Step 2: full details.
        try:
            async with session.get(f"{_BASE}/breaches", params={"email": address}) as resp:
                if resp.status != 200:
                    # Fall back to bare names if details fetch fails.
                    full_data = None
                else:
                    full_data = await resp.json(content_type=None)
        except (aiohttp.ClientError, OSError, ValueError):
            full_data = None

    # Build a {breach_id: full_record} index.
    detail_by_name: dict[str, dict] = {}
    if isinstance(full_data, dict):
        for b in full_data.get("exposedBreaches") or []:
            if isinstance(b, dict) and b.get("breachID"):
                detail_by_name[b["breachID"]] = b

    # Stamp summary on the Email itself.
    node.attrs["xposedornot_breach_count"] = len(breach_names)
    if "xposedornot" not in node.sources:
        node.sources.append("xposedornot")

    # Optionally, the /v1/breach-analytics endpoint returns a risk_score
    # can extract via a separate fetch - skipped to keep latency tight.
    # Emit Breach nodes.
    for name in breach_names:
        detail = detail_by_name.get(name) or {}
        attrs: dict[str, Any] = {
            "name": name,
            "title": detail.get("breachID") or name,
            "breach_date": detail.get("breachedDate"),
            "added_date": detail.get("addedDate"),
            "domain": detail.get("domain"),
            "industry": detail.get("industry"),
            "password_risk": detail.get("passwordRisk"),
            "is_verified": detail.get("verified"),
            "is_sensitive": detail.get("sensitive"),
            "exposed_records": detail.get("exposedRecords"),
            "exposed_data": detail.get("exposedData"),
            "description": (detail.get("exposureDescription") or "")[:500] or None,
            "via": "xposedornot",
        }
        attrs = {k: v for k, v in attrs.items() if v not in (None, "", [])}
        breach = g.add_node("Breach", source="xposedornot", **attrs)
        g.add_edge(node.id, breach.id, "appeared_in", source="xposedornot")
