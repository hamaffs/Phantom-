"""Facebook public name search.

Hits facebook.com/public/{First}-{Last} to find profiles by display
name. Catches the /people/.../pfbid... accounts that don't have a
vanity URL — they're invisible to the regular facebook.com/{handle}
scan but show up in this public listing.

Name-mode only (input must have whitespace).
"""
from __future__ import annotations

import asyncio
import html as html_lib
import re
import time
from typing import Optional

from models import CheckResult


_RESULT_ANCHOR_RE = re.compile(
    r'<a\s+title="([^"]+)"[^>]*'
    r'href="(https://www\.facebook\.com/people/[^"?]+/pfbid[A-Za-z0-9]+/?)"',
    re.IGNORECASE,
)

_SEARCH_TIMEOUT = 12.0
_MAX_PROFILES_TO_FETCH = 5


def _norm(s: str) -> str:
    # Strip diacritics + non-alnum for accent-insensitive name matching.
    if not s:
        return ""
    # Decompose Unicode then drop combining marks
    import unicodedata
    decomposed = unicodedata.normalize("NFKD", s)
    no_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", no_marks.lower())


async def search_public(
    first: str,
    last: str,
    session,
) -> list[tuple[str, str]]:
    """Returns [(display_name, url), ...] for profiles matching first+last."""
    first_n = _norm(first)
    last_n = _norm(last)
    if not first_n or not last_n:
        return []

    slug = f"{first}-{last}".replace(" ", "-")
    url = f"https://www.facebook.com/public/{slug}"

    try:
        resp = await session.get(url, timeout=_SEARCH_TIMEOUT, allow_redirects=True)
    except Exception:
        return []
    if resp.status_code != 200:
        return []

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _RESULT_ANCHOR_RE.finditer(resp.text):
        name = html_lib.unescape(m.group(1)).strip()
        profile_url = m.group(2)
        if profile_url in seen:
            continue
        seen.add(profile_url)
        # Both first AND last must be in the display name; otherwise
        # Facebook's fuzzy search returns reordered near-misses.
        name_norm = _norm(name)
        if first_n in name_norm and last_n in name_norm:
            out.append((name, profile_url))
    return out


async def _fetch_profile(
    name: str, profile_url: str, query_handle: str, session,
) -> Optional[CheckResult]:
    from enrich import extract_facebook
    start = time.monotonic()
    try:
        resp = await session.get(profile_url, timeout=_SEARCH_TIMEOUT, allow_redirects=True)
    except Exception:
        return None
    if resp.status_code != 200 or not resp.text:
        return None
    profile = extract_facebook(resp.text, query_handle) or {}
    if not profile.get("display_name"):
        profile["display_name"] = name

    return CheckResult(
        site="Facebook",
        category="social",
        url=profile_url,
        variant=_pfbid_short(profile_url),
        reliability=80,
        exists=True,
        status=200,
        elapsed_ms=int((time.monotonic() - start) * 1000),
        reason="presence:fb-public-search",
        backend="curl_cffi",
        profile=profile,
    )


def _pfbid_short(url: str) -> str:
    m = re.search(r"pfbid([A-Za-z0-9]{8,})", url)
    if m:
        return f"pfbid:{m.group(1)[:12]}"
    return "fb-public-search"


async def discover_facebook_profiles(
    raw_name: str,
    session,
) -> list[CheckResult]:
    """Returns a list of FOUND CheckResults for the public-search matches."""
    parts = raw_name.split()
    if len(parts) < 2:
        return []
    first, last = parts[0], parts[-1]

    matches = await search_public(first, last, session)
    if not matches:
        return []
    capped = matches[:_MAX_PROFILES_TO_FETCH]

    tasks = [
        _fetch_profile(name, url, last.lower(), session)
        for name, url in capped
    ]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in raw_results if isinstance(r, CheckResult)]
