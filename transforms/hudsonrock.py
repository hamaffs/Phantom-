"""HudsonRock Cavalier transform — infostealer victim lookup.

Free, no auth. HudsonRock aggregates **infostealer malware logs** from
the wild — when a Windows PC is infected by RedLine, Vidar, Raccoon,
etc., every credential saved in the browser ends up in a log that gets
sold on darknet markets. HudsonRock buys those logs and indexes them.

When an email appears in their data, it means that computer was
infected and the saved passwords for that email are loose. Different
class of leak than HIBP/XposedOrNot (which only see *site-side*
breaches) — infostealer logs catch credentials no public breach has
seen.

Per stealer-victim record we get:
  - date_compromised, computer_name, operating_system, malware_path, ip
  - total_corporate_services + total_user_services exposed
  - top_passwords (partially masked by HudsonRock, e.g. "H************#")
  - top_logins (partially masked emails)

Each entry becomes a `Breach` node attached to the Email via `appeared_in`.
"""
from __future__ import annotations

import sys
from typing import Any

import aiohttp

from graph.model import Graph, Node
from graph.transforms import transform


_URL = "https://cavalier.hudsonrock.com/api/json/v2/osint-tools/search-by-email"
_USER_AGENT = "Phantom-OSINT"
_TIMEOUT = 15.0


@transform(input="Email", produces=("Breach",))
async def query_hudsonrock(node: Node, g: Graph) -> None:
    address = (node.attrs.get("address") or "").strip().lower()
    if not address or "@" not in address:
        return

    timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(_URL, params={"email": address}) as resp:
                if resp.status != 200:
                    return
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, OSError, ValueError) as e:
        print(f"hudsonrock: request failed for {address}: {e}", file=sys.stderr)
        return

    if not isinstance(data, dict):
        return
    stealers = data.get("stealers") or []
    if not isinstance(stealers, list) or not stealers:
        return

    # Mark the email itself.
    node.attrs["infostealer_victim"] = True
    node.attrs["infostealer_log_count"] = len(stealers)
    if "hudsonrock" not in node.sources:
        node.sources.append("hudsonrock")

    for entry in stealers:
        if not isinstance(entry, dict):
            continue
        date = entry.get("date_compromised")
        if not isinstance(date, str):
            continue
        # Compose a stable breach name so re-runs dedupe.
        comp_name = entry.get("computer_name") or "unknown"
        # Use date + computer_name as the dedup key - same victim won't
        # produce two Breach nodes per HudsonRock entry across re-scans.
        breach_name = f"hudsonrock:{date}:{comp_name}"

        attrs: dict[str, Any] = {
            "name": breach_name,
            "title": f"Infostealer log — {comp_name}",
            "breach_date": date,
            "via": "hudsonrock",
            "breach_category": "infostealer",
            "computer_name": comp_name,
            "operating_system": entry.get("operating_system"),
            "malware_path": entry.get("malware_path"),
            "antiviruses": entry.get("antiviruses"),
            "ip": entry.get("ip"),
            "total_corporate_services": entry.get("total_corporate_services"),
            "total_user_services": entry.get("total_user_services"),
            "top_passwords_masked": entry.get("top_passwords"),
            "top_logins_masked": entry.get("top_logins"),
        }
        # Drop None / "Not Found" sentinel values for cleaner graphs.
        attrs = {
            k: v for k, v in attrs.items()
            if v not in (None, "", "Not Found", []) and not (isinstance(v, str) and v.strip() == "Not Found")
        }
        breach = g.add_node("Breach", source="hudsonrock", **attrs)
        g.add_edge(node.id, breach.id, "appeared_in", source="hudsonrock")
