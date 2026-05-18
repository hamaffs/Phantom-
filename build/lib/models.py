"""Phantom data model + detection logic.

Site, CheckResult, evaluate(), bot-wall detection, body streaming. Pure
functions and dataclasses — no I/O beyond load_sites() reading sites.json.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "DNT": "1",
}

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

# Title fragments + body fingerprints that indicate a Cloudflare / WAF /
# captcha challenge - the body cannot be trusted as a real response.
#
# Each entry is (substring_match, vendor_label). Substring matches are
# done against the lowercased <title> first, then the full body as
# fallback. The vendor label is surfaced on CheckResult.blocked_by so
# users (and the LLM analyst) can see *what* blocked us, not just that
# were blocked. Phase 5 OPSEC hardening relies on this signal.
BOT_TITLE_HINTS = (
    ("just a moment", "cloudflare"),
    ("attention required", "cloudflare"),
    ("checking your browser", "cloudflare"),
    ("ddos protection", "cloudflare"),
    ("please wait", "generic_challenge"),
    ("verify you are human", "generic_challenge"),
    ("robot check", "generic_challenge"),
    ("access denied", "generic_block"),
    ("client challenge", "datadome"),
    ("ng guard", "datadome"),
    ("prove your humanity", "hcaptcha"),
    ("captcha", "captcha_generic"),
    ("imperva incapsula", "imperva"),
    ("are you a human", "perimeterx"),
)

# Body-level fingerprints - match against full lowercased body, not title.
# These catch challenges that don't put their name in <title>.
BOT_BODY_HINTS = (
    ("cf-challenge-running", "cloudflare"),
    ("__cf_chl_", "cloudflare"),
    ("cf_chl_opt", "cloudflare"),
    ("/cdn-cgi/challenge-platform/", "cloudflare"),
    ("turnstile.cloudflare", "cloudflare_turnstile"),
    ("h-captcha", "hcaptcha"),
    ("hcaptcha.com/captcha", "hcaptcha"),
    ("g-recaptcha", "recaptcha"),
    ("recaptcha/api.js", "recaptcha"),
    ("datadome", "datadome"),
    ("perimeterx.net", "perimeterx"),
    ("_pxhd", "perimeterx"),
    ("incapsula incident", "imperva"),
    ("_incap_", "imperva"),
    ("akamai bot manager", "akamai"),
    ("ak-bmsc", "akamai"),
    ("sucuri webfirewall", "sucuri"),
)

# Login-wall fingerprints - sites that gate profile pages behind auth.
# When matched, the result must be UNKNOWN, not False (different from a
# "user doesn't exist" verdict). These often appear *with* a 200 status
# and a body that superficially looks like a real page, which is why
# the bug existed before Phase 5+1 - Facebook absence_text used to
# include "rsrcTags" which is on every login wall, so Phantom told
# users every Facebook profile didn't exist.
#
# Each entry is (substring, vendor_label). Vendor labels are surfaced
# as CheckResult.blocked_by ("login:facebook" etc.) so reports can
# distinguish "don't know - login required" from "don't know -
# Cloudflare blocked us".
LOGIN_WALL_BODY_HINTS = (
    # Facebook - `rsrcTags` is on every Facebook page including logged-out
    # walls; the more specific signal is the login form action + the
    # logged-out canvas markup.    ('id="login_form"', "facebook"),
    ('action="/login/device-based/regular/login/"', "facebook"),
    ("you must log in to continue", "facebook"),
    ('"requires_login_to_view"', "facebook"),
    # Instagram API endpoint - known canned response from 2024+    ('"require_login":true', "instagram"),
    ('"please wait a few minutes before you try again"', "instagram"),
    ("login • instagram", "instagram"),
    # TikTok - desktop login wall is interstitial JSON    ('"login.signupTitle"', "tiktok"),
    ('"webapp.user-detail-redirect"', "tiktok"),
    # LinkedIn - auth wall    ("authwall", "linkedin"),
    ("join linkedin to see", "linkedin"),
    # X / Twitter logged-out shell    ('"need to log in"', "twitter"),
    # Discord    ("you need to be logged in to view this content", "discord"),
)

_TITLE_RE = re.compile(r"<title[^>]*>([^<]*)</title>", re.IGNORECASE)

# CDN domains that send `cross-origin-resource-policy: same-origin`, blocking
# browsers from loading the image in cross-origin contexts (file:// reports).
# For these embed the bytes as a base64 data URI instead of a URL.
_CORP_RESTRICTED_CDNS = (
    "fbcdn.net/", "cdninstagram.com/",
    # TikTok's CDNs Referer-gate every image - clicking the URL in a
    # browser tab returns "Access Denied" because the browser sends no
    # Referer. already fetched the bytes via _fetch_image (which
    # adds the right Referer), so inline them as a data URI instead.    "tiktokcdn.com/", "tiktokcdn-us.com/",
)

def _photo_to_data_uri(url: str, photo_bytes_map: Optional[dict]) -> Optional[str]:
    """Return a base64 data URI for `url` when it comes from a CORP-restricted CDN."""
    if not url or not photo_bytes_map:
        return None
    if not any(p in url for p in _CORP_RESTRICTED_CDNS):
        return None
    data = photo_bytes_map.get(url)
    if not data:
        return None
    ct = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
    return f"data:{ct};base64,{base64.b64encode(data).decode('ascii')}"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Site:
    name: str
    category: str
    url: str
    method: str                              # "status" | "message"
    reliability: int
    valid_status: list[int] = field(default_factory=list)
    invalid_status: list[int] = field(default_factory=list)
    presence_text: list[str] = field(default_factory=list)
    absence_text: list[str] = field(default_factory=list)
    protection: list[str] = field(default_factory=list)
    headers: dict = field(default_factory=dict)
    request_method: str = "GET"              # "GET" | "POST"
    request_body: Optional[str] = None       # raw body template (with {username})
    profile_url: Optional[str] = None        # public profile URL when probe URL is an API endpoint
    # Optional richer metadata for filtering. None = "any / unspecified".
    country: Optional[str] = None            # ISO 3166-1 alpha-2 (lowercase), or "global"
    language: Optional[str] = None           # ISO 639-1 (lowercase), or "global"
    content_type: Optional[str] = None       # photo | text | code | audio | video | links | mixed
    disabled: bool = False                   # auto-disable flag for known-broken sites

    def url_for(self, username: str) -> str:
        return self.url.replace("{username}", username)

    def display_url_for(self, username: str) -> str:
        """Public-facing profile URL used in reports. Falls back to the probe URL."""
        base = self.profile_url or self.url
        return base.replace("{username}", username)

    def body_for(self, username: str) -> Optional[str]:
        return self.request_body.replace("{username}", username) if self.request_body else None

    @property
    def needs_impersonation(self) -> bool:
        return "tls_fingerprint" in self.protection

    @property
    def needs_js_render(self) -> bool:
        return "js_challenge" in self.protection


@dataclass
class CheckResult:
    site: str
    category: str
    url: str
    exists: Optional[bool]      # True = found, False = absent, None = unknown
    reliability: int
    status: Optional[int] = None
    error: Optional[str] = None
    elapsed_ms: int = 0
    reason: Optional[str] = None    # human label for why we decided as we did
    final_url: Optional[str] = None # set when redirects landed elsewhere
    backend: str = "aiohttp"        # which HTTP client handled the request
    profile: dict = field(default_factory=dict)  # display name, bio, photo, follower counts… if FOUND
    variant: Optional[str] = None   # which generated variant produced this
    score: Optional[int] = None     # confidence score 0–100 (set by confidence.py after scan)
    tier: Optional[str] = None      # 'verified_identity' | 'likely_match' | 'possible_impostor'
    identity_id: Optional[int] = None        # cluster ID assigned by disambiguation.py
    is_primary_identity: Optional[bool] = None  # True iff this account's cluster is the primary
    signals: list = field(default_factory=list)  # confidence trace: [{"label": str, "weight": int}, ...]
    blocked_by: Optional[str] = None  # Phase 5: vendor of the bot-wall / challenge that blocked us (e.g. "cloudflare", "datadome", "hcaptcha"). None when no challenge detected.


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_sites(path: Path) -> list[Site]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    sites = []
    for entry in raw:
        if entry.get("disabled"):
            continue
        sites.append(Site(
            name=entry["name"],
            category=entry["category"],
            url=entry["url"],
            method=entry["method"],
            reliability=int(entry["reliability"]),
            valid_status=entry.get("valid_status", []),
            invalid_status=entry.get("invalid_status", []),
            presence_text=entry.get("presence_text", []),
            absence_text=entry.get("absence_text", []),
            protection=entry.get("protection", []),
            headers=entry.get("headers", {}),
            request_method=entry.get("request_method", "GET"),
            request_body=entry.get("request_body"),
            profile_url=entry.get("profile_url"),
            country=entry.get("country"),
            language=entry.get("language"),
            content_type=entry.get("content_type"),
            disabled=False,  # filtered above, never propagates
        ))
    return sites
# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def _expand(patterns: list[str], username: str) -> list[str]:
    """Substitute {username} in detection patterns."""
    return [p.replace("{username}", username) for p in patterns]


def _detect_bot_wall(body: str) -> Optional[str]:
    """Return the vendor label ('cloudflare', 'datadome', 'hcaptcha', ...)
    when the body looks like a challenge page, else None.

    Title hints take precedence over body hints because <title> is more
    discriminating (a real profile page rarely contains "verify you are
    human" in <title>). Body fingerprints catch CDN tags / script srcs
    that show up even when the page renders an OK title.
    """
    if not body:
        return None
    m = _TITLE_RE.search(body)
    if m:
        title = m.group(1).strip().lower()
        for fragment, vendor in BOT_TITLE_HINTS:
            if fragment in title:
                return vendor
    lowered = body.lower() if len(body) < 200_000 else body[:200_000].lower()
    for fragment, vendor in BOT_BODY_HINTS:
        if fragment in lowered:
            return vendor
    return None


def _is_bot_wall(body: str) -> bool:
    """Back-compat shim — returns True/False without the vendor label."""
    return _detect_bot_wall(body) is not None


def _detect_login_wall(body: str) -> Optional[str]:
    """Return the platform label when the body is a login wall, else None.

    Login walls are NOT the same as bot walls (Cloudflare). A login wall
    is a logged-out version of a real platform — the page exists but the
    profile data is hidden behind auth. The verdict for a login-walled
    response is UNKNOWN, not MISSING — Phantom previously misclassified
    these as "user doesn't exist" because their absence_text rules
    matched the login wall's own markup (e.g. Facebook's 'rsrcTags').
    """
    if not body:
        return None
    # Body-level only - titles are usually generic ("Facebook", "Instagram")
    # and not discriminating.
    lowered = body.lower() if len(body) < 200_000 else body[:200_000].lower()
    for fragment, vendor in LOGIN_WALL_BODY_HINTS:
        if fragment in lowered:
            return vendor
    return None


def evaluate(site: Site, status: int, body: str, username: str) -> tuple[Optional[bool], str]:
    """Return (exists, reason).

    `exists` is True/False (decision), or None for inconclusive.
    `reason` is a short tag suitable for output: e.g. "200", "404",
    "presence", "absence", "no-presence", "bot-wall".

    Decision order (hard rules first, soft signals last):

    1. If the status is in `invalid_status` → MISSING. Status codes are the
       single most reliable signal when the site has clean ones.
    2. **Login-wall detection runs BEFORE absence-text matching** because
       login walls (Facebook 'rsrcTags', Instagram 'require_login:true',
       TikTok login overlay) often contain strings that look like absence
       markers but actually mean "we couldn't read the profile, auth
       required." Order matters: absence-rule first would falsely
       report every Facebook user as MISSING from a logged-out scan.
    3. If the body matches an `absence_text` pattern → MISSING. A site
       saying "user not found" beats a misleading 200.
    4. Bot-wall detection (Cloudflare/captcha title) → UNKNOWN. Body is
       untrustworthy.
    5. Method-specific positive checks (status_code or presence_text).
    6. Anything else → UNKNOWN.
    """
    presence = _expand(site.presence_text, username)
    absence = _expand(site.absence_text, username)

    body_has_presence = any(p in body for p in presence) if presence else False
    body_has_absence = any(p in body for p in absence) if absence else False

    # 1. Hard MISSING by status code (always trustworthy - 404 is 404).
    if status in site.invalid_status:
        return False, f"{status}"

    # 1.5. Presence wins over login-wall: if the body genuinely contains
    # the presence pattern (e.g. an unauthenticated Instagram API
    # response that DID return data), trust the presence. Only fall
    # through to login-wall classification when couldn't confirm the
    # account exists.
    if body_has_presence:
        # Fall through - let the per-method block below register a True.
        pass
    else:
        # 2. Login-wall detection (NEW): runs before absence so the
        # `rsrcTags` / `require_login:true` strings on login pages don't
        # masquerade as "user doesn't exist".
        login_vendor = _detect_login_wall(body)
        if login_vendor is not None:
            return None, f"login-wall:{login_vendor}"

    # 3. Body-level absence.
    if body_has_absence:
        return False, "absence"

    # 4. Bot-wall - body unreliable, status was likely 200 from the WAF.
    vendor = _detect_bot_wall(body)
    if vendor is not None:
        return None, f"bot-wall:{vendor}"

    # 3. Method-specific positive decision.
    if site.method == "status":
        if status in site.valid_status:
            if presence and not body_has_presence:
                return None, "no-presence"
            return True, f"{status}"
        return None, f"unexpected-{status}"

    if site.method == "message":
        if presence:
            if body_has_presence:
                return True, "presence"
            return None, "no-presence"
        # No presence patterns defined - last resort: trust the status.
        if status >= 400:
            return None, f"{status}"
        return True, "no-absence"

    return None, "unknown-method"
# ---------------------------------------------------------------------------
# HTTP backends
# ---------------------------------------------------------------------------
async def _drain(stream, max_body: int) -> bytes:
    """Read until EOF or max_body, in chunks."""
    chunks: list[bytes] = []
    total = 0
    while total < max_body:
        piece = await stream.read(min(64 * 1024, max_body - total))
        if not piece:
            break
        chunks.append(piece)
        total += len(piece)
    return b"".join(chunks)
