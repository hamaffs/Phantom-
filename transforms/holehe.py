"""Email-registration probes (holehe-style).

The holehe ecosystem has decayed badly. Of 12 platforms tested only
Twitter/X still has a working email-availability endpoint; the rest
require auth, CSRF tokens, or no longer distinguish in their response.

Framework left in place so new probes drop in as `@dataclass` entries
in `_PROBES`. The companion `gravatar.py` transform covers a different
angle (Email → public profile) and remains highly effective.

For now, this transform produces real signal only for emails registered
on Twitter/X.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Awaitable, Callable, Optional

import aiohttp

from graph.model import Graph, Node
from graph.transforms import transform


_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_TIMEOUT = 10.0
_CONCURRENCY = 4


Probe = Callable[[aiohttp.ClientSession, str], "Awaitable[str]"]


async def _twitter(session: aiohttp.ClientSession, email: str) -> str:
    """Twitter / X — the one probe still standing in 2026.

    GET /i/users/email_available.json?email={email} returns JSON like
    `{"valid":false,"msg":"E-mailadres reeds in gebruik.","taken":true}`.
    `taken:true` = registered. `taken:false` = not.
    """
    url = "https://api.twitter.com/i/users/email_available.json"
    try:
        async with session.get(url, params={"email": email}) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                if isinstance(data, dict) and data.get("taken") is True:
                    return "registered"
                if isinstance(data, dict) and data.get("taken") is False:
                    return "not_found"
            return "error"
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return "error"


_PROBES: list[tuple[str, str, Probe]] = [
    ("twitter", "https://twitter.com/", _twitter),
]


@transform(input="Email", produces=("Url",))
async def query_holehe(node: Node, g: Graph) -> None:
    """For each Email node, probe known platforms for registration.

    Emits a `registered_at` edge from the Email node to a Url node when
    a platform confirms registration. Silent on inconclusive results.
    """
    email = (node.attrs.get("address") or "").strip().lower()
    if not email or "@" not in email:
        return

    sem = asyncio.Semaphore(_CONCURRENCY)
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT)

    async def _one(label: str, site_url: str, probe: Probe, session) -> Optional[tuple[str, str]]:
        async with sem:
            try:
                verdict = await probe(session, email)
            except Exception as e:
                print(
                    f"holehe[{label}]: probe error for {email}: "
                    f"{type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                return None
        if verdict == "registered":
            return (label, site_url)
        return None

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers={"User-Agent": _USER_AGENT},
    ) as session:
        results = await asyncio.gather(
            *(_one(label, url, fn, session) for label, url, fn in _PROBES),
            return_exceptions=True,
        )

    hits = False
    for hit in results:
        if not isinstance(hit, tuple):
            continue
        label, site_url = hit
        u = g.add_node(
            "Url", source="holehe",
            url=site_url, via="holehe", site=label,
        )
        g.add_edge(node.id, u.id, "registered_at", source="holehe", site=label)
        hits = True
    if hits and "holehe" not in node.sources:
        node.sources.append("holehe")
