"""Cross-link expansion.

Phantom extracts public profile data during its initial scan (display name,
photos, bios — and crucially, *links to other accounts*). This module
harvests those linked accounts and produces a list of fresh handles to
test, turning a single-username scan into a two-hop graph walk.

Data sources we mine, in order of reliability:

  1. Keybase `proofs` — cryptographic identity proofs. Bullet-proof: the
     handle is verified to belong to the same person.
  2. JSON-LD `sameAs` (Dev.to, About.me, Linktree-style profile JSON) —
     the platform explicitly declares "this user is also @x on Twitter".
  3. Linktree `links` — bio-link URLs the user manually curated.
  4. Site-specific fields (GitHub `x_handle`, GitHub `blog`, Twitter
     `website`) — the user pointed at it themselves.

Each candidate URL is parsed against a known-platform regex to recover
the bare handle. URLs that don't match any pattern are dropped — we
don't try to scrape arbitrary websites for handles, that's a different
tool entirely.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional
from urllib.parse import urlparse

from models import CheckResult


# Per-platform handle extractors. Each entry maps a host (without "www.")
# to a regex applied against the URL path. The first capture group is
# the handle.
#
# Patterns are written tightly to avoid greedy matches over multi-segment
# paths like /users/123/posts/456 — we want the username segment, not a
# random path component.
_HANDLE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("twitter.com",      re.compile(r"^/@?([A-Za-z0-9_]{1,30})/?$")),
    ("x.com",            re.compile(r"^/@?([A-Za-z0-9_]{1,30})/?$")),
    ("instagram.com",    re.compile(r"^/@?([A-Za-z0-9_.]{1,32})/?$")),
    ("tiktok.com",       re.compile(r"^/@([A-Za-z0-9_.]{1,32})/?$")),
    ("threads.net",      re.compile(r"^/@([A-Za-z0-9_.]{1,32})/?$")),
    ("threads.com",      re.compile(r"^/@([A-Za-z0-9_.]{1,32})/?$")),
    ("youtube.com",      re.compile(r"^/@([A-Za-z0-9_.-]{1,64})/?")),
    ("github.com",       re.compile(r"^/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38})?)/?$")),
    ("gitlab.com",       re.compile(r"^/([A-Za-z0-9][A-Za-z0-9_.-]{0,38})/?$")),
    ("reddit.com",       re.compile(r"^/(?:u(?:ser)?|r)/([A-Za-z0-9_-]{1,20})/?")),
    ("old.reddit.com",   re.compile(r"^/(?:u(?:ser)?|r)/([A-Za-z0-9_-]{1,20})/?")),
    ("twitch.tv",        re.compile(r"^/([A-Za-z0-9_]{1,25})/?$")),
    ("m.twitch.tv",      re.compile(r"^/([A-Za-z0-9_]{1,25})/?$")),
    ("linkedin.com",     re.compile(r"^/in/([A-Za-z0-9_-]{1,100})/?")),
    ("facebook.com",     re.compile(r"^/([A-Za-z0-9.][A-Za-z0-9._-]{0,49})/?$")),
    ("pinterest.com",    re.compile(r"^/([A-Za-z0-9_]{1,30})/?$")),
    ("soundcloud.com",   re.compile(r"^/([A-Za-z0-9_-]{1,40})/?$")),
    ("bandcamp.com",     re.compile(r"^/?$")),  # subdomain form, handled below
    ("medium.com",       re.compile(r"^/@?([A-Za-z0-9_.-]{1,40})/?$")),
    ("dev.to",           re.compile(r"^/([A-Za-z0-9_]{1,40})/?$")),
    ("vimeo.com",        re.compile(r"^/([A-Za-z0-9_-]{1,40})/?$")),
    ("mixcloud.com",     re.compile(r"^/([A-Za-z0-9_-]{1,40})/?$")),
    ("dribbble.com",     re.compile(r"^/([A-Za-z0-9_-]{1,40})/?$")),
    ("behance.net",      re.compile(r"^/([A-Za-z0-9_-]{1,40})/?")),
    ("keybase.io",       re.compile(r"^/([A-Za-z0-9_]{1,16})/?$")),
    ("lichess.org",      re.compile(r"^/@/([A-Za-z0-9_-]{1,40})/?")),
    ("last.fm",          re.compile(r"^/user/([A-Za-z0-9_-]{1,40})/?")),
    ("about.me",         re.compile(r"^/([A-Za-z0-9_-]{1,40})/?$")),
    ("linktr.ee",        re.compile(r"^/([A-Za-z0-9_-]{1,40})/?$")),
    ("beacons.ai",       re.compile(r"^/([A-Za-z0-9_-]{1,40})/?$")),
    ("bio.link",         re.compile(r"^/([A-Za-z0-9_.-]{1,40})/?$")),
]

# Subdomain-style platforms (handle is the subdomain, not a path segment).
_SUBDOMAIN_PLATFORMS = {
    "bandcamp.com",
    "carrd.co",
}


def _normalize_host(host: str) -> str:
    """Drop leading 'www.' for matching."""
    host = host.lower().strip()
    if host.startswith("www."):
        host = host[4:]
    return host


def _extract_one(url: str) -> Optional[str]:
    """Return the bare handle for a URL, or None if no known platform
    matches it."""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
    except Exception:
        return None
    host = _normalize_host(parsed.netloc)
    if not host:
        return None

    # Subdomain-style platforms: handle is the leftmost subdomain.
    for base in _SUBDOMAIN_PLATFORMS:
        if host.endswith("." + base) and host != base:
            sub = host[: -(len(base) + 1)]
            if sub and sub != "www":
                return sub
        if host == base:
            return None  # bare bandcamp.com isn't a profile

    path = parsed.path or "/"
    for known_host, rx in _HANDLE_PATTERNS:
        if host == known_host or host.endswith("." + known_host):
            m = rx.search(path)
            if m and m.groups():
                return m.group(1)
            return None
    return None


def _candidate_urls(profile: dict) -> list[str]:
    """Pull every URL that might point at another social profile, deduped
    while preserving order. Strings only; nested dicts (linktree links)
    are walked one level deep."""
    if not profile:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def push(value):
        if isinstance(value, str) and value:
            v = value.strip()
            if v and v not in seen:
                seen.add(v)
                out.append(v)
        elif isinstance(value, dict):
            # link entries: {"title": ..., "url": ...}
            for k in ("url", "link", "href"):
                push(value.get(k))
        elif isinstance(value, list):
            for item in value:
                push(item)

    for key in (
        "linked_accounts",
        "links",
        "proofs",
        "website",
    ):
        push(profile.get(key))

    # GitHub's free-form "blog" field is the user's own pointer.
    push(profile.get("blog"))
    # GitHub also exposes a Twitter handle directly (not a URL).
    gh_x = profile.get("x_handle") or profile.get("twitter")
    if isinstance(gh_x, str) and gh_x.strip():
        push(f"https://x.com/{gh_x.strip().lstrip('@')}")

    return out


def discover_new_handles(
    found: Iterable[CheckResult],
    already_tested: set[str],
) -> list[str]:
    """Return a list of new handles to scan, drawn from the linked-account
    data on every FOUND profile. Handles in `already_tested` are filtered
    out (case-insensitive). Order is stable for reproducibility.
    """
    tested_lower = {h.lower() for h in already_tested}
    out: list[str] = []
    seen: set[str] = set()
    for r in found:
        for url in _candidate_urls(r.profile or {}):
            handle = _extract_one(url)
            if not handle:
                continue
            key = handle.lower()
            if key in tested_lower or key in seen:
                continue
            seen.add(key)
            out.append(handle)
    return out
