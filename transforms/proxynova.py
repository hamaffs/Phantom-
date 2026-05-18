"""ProxyNova ComB transform — public combo-list password lookup.

Free, no auth. ProxyNova hosts a search index over the "Compilation of
Many Breaches" (COMB) and similar public credential dumps. Querying an
email returns the raw `email:password` lines where that email appeared.

⚠️ **Passwords are stored in plaintext** on the Email node attrs. The
user opted into this explicitly for personal research / self-OSINT —
do not enable this transform on shared infrastructure or pipe its
output to anywhere uncontrolled.

We store:
  - `leaked_passwords` — list of distinct password strings
  - `leaked_password_count` — convenience integer
  - `leaked_password_sample` — first 5 (for compact display)
"""
from __future__ import annotations

import sys

import aiohttp

from graph.model import Graph, Node
from graph.transforms import transform


_URL = "https://api.proxynova.com/comb"
_USER_AGENT = "Phantom-OSINT"
_TIMEOUT = 12.0
_MAX_RESULTS = 100  # ProxyNova API caps at 100 per request (HTTP 400 above 100).


@transform(input="Email", produces=())
async def query_proxynova(node: Node, g: Graph) -> None:
    address = (node.attrs.get("address") or "").strip().lower()
    if not address or "@" not in address:
        return

    timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
    params = {"query": address, "start": "0", "limit": str(_MAX_RESULTS)}
    try:
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT},
        ) as session:
            async with session.get(_URL, params=params) as resp:
                if resp.status != 200:
                    return
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, OSError, ValueError) as e:
        print(f"proxynova: request failed for {address}: {e}", file=sys.stderr)
        return

    if not isinstance(data, dict):
        return
    lines = data.get("lines") or []
    if not isinstance(lines, list) or not lines:
        return

    # Extract distinct passwords. Lines look like "email@x:password" but
    # passwords can contain `:` themselves, so split only on the FIRST one.
    passwords: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if not isinstance(line, str) or ":" not in line:
            continue
        email_part, _, pw = line.partition(":")
        if email_part.strip().lower() != address:
            # ProxyNova does substring matching, so it returns hits for
            # similar emails too (e.g. test+x@gmail.com). Filter to
            # exact matches only.
            continue
        if not pw or pw in seen:
            continue
        seen.add(pw)
        passwords.append(pw)

    if not passwords:
        return

    node.attrs["leaked_password_count"] = len(passwords)
    node.attrs["leaked_passwords"] = passwords
    node.attrs["leaked_password_sample"] = passwords[:5]
    if "proxynova" not in node.sources:
        node.sources.append("proxynova")
