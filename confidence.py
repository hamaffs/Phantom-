"""
Confidence scoring for Phantom FOUND results.

Each found profile receives a score from 0–100 based on cross-platform
signals that distinguish the genuine subject from impostors and squatters.

Tiers
-----
  verified_identity  (70+)  — highly likely the real person
  likely_match       (40–69) — probably the same person, some uncertainty
  possible_impostor  (0–39)  — likely a different person or squatter
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from checker import CheckResult

TIER_VERIFIED = "verified_identity"
TIER_LIKELY   = "likely_match"
TIER_IMPOSTOR = "possible_impostor"

# ---------------------------------------------------------------------------
# Default / placeholder avatar heuristics
# ---------------------------------------------------------------------------

_DEFAULT_AVATAR_FRAGMENTS = (
    "default_profile",
    "default_avatar",
    "default-th",
    "ghost.png",
    "anonymous.png",
    "placeholder",
    "/i/default_profile",
    "identicon",
    "wavatar",
    "monsterid",
    "/img/f-default",
    "gravatar.com/avatar/00000000",
    "s.gravatar.com/avatar",          # bare gravatar (no hash after /avatar/)
    "cdn.jsdelivr.net/gh/primer",     # GitHub default user / org avatar
    "avatars.githubusercontent.com/u/0",
)


def _is_default_avatar(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return any(f in u for f in _DEFAULT_AVATAR_FRAGMENTS)


# ---------------------------------------------------------------------------
# Impostor-affix detection
# ---------------------------------------------------------------------------

_IMPOSTOR_PREFIXES = frozenset({
    "official", "real", "the", "its", "iam", "i_am", "iamthe",
    "thisis", "this_is",
})
_IMPOSTOR_SUFFIXES = frozenset({
    "official", "real",
})


def _has_impostor_affix(variant: str) -> bool:
    """Return True if the variant starts/ends with a known impostor affix."""
    v = variant.lower().strip("-_.")
    for pfx in _IMPOSTOR_PREFIXES:
        if v.startswith(pfx) and len(v) > len(pfx):
            return True
    for sfx in _IMPOSTOR_SUFFIXES:
        clean = v.rstrip("_")
        if clean.endswith(sfx) and len(clean) > len(sfx):
            return True
    return False


# ---------------------------------------------------------------------------
# Joined-date parser (no third-party deps)
# ---------------------------------------------------------------------------

def _joined_days_ago(joined_raw: str) -> Optional[int]:
    """Return approximate days since the account joined, or None."""
    if not joined_raw:
        return None
    s = joined_raw.strip()
    now = datetime.now(timezone.utc)
    # ISO-ish prefixes: "2015-12-27T04:02:17Z", "2015-12-27T04:02:17", "2015-12-27"
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:len(fmt)], fmt).replace(tzinfo=timezone.utc)
            return max(0, (now - dt).days)
        except (ValueError, IndexError):
            pass
    # "Jan 2020", "January 2020"
    for fmt in ("%b %Y", "%B %Y"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return max(0, (now - dt).days)
        except ValueError:
            pass
    # "2019" (year only)
    m = re.match(r'^(20\d\d)$', s)
    if m:
        try:
            dt = datetime(int(m.group(1)), 1, 1, tzinfo=timezone.utc)
            return max(0, (now - dt).days)
        except (ValueError, OverflowError):
            pass
    return None


# ---------------------------------------------------------------------------
# Core scorer
# ---------------------------------------------------------------------------

def tier_from_score(score: int) -> str:
    if score >= 55:
        return TIER_VERIFIED
    if score >= 20:
        return TIER_LIKELY
    return TIER_IMPOSTOR


def score_result(
    r: "CheckResult",
    all_found: list["CheckResult"],
    clusters: list,
    subject_name: str,
    input_username: str,
) -> int:
    """Return a 0–100 confidence score for one FOUND result."""
    p = r.profile or {}
    score = 0

    # ------------------------------------------------------------------
    # Pre-compute shared data that multiple signals need
    # ------------------------------------------------------------------

    # All photo URLs that appear in clusters with 2+ members.
    multi_cluster_photo_urls: set[str] = set()
    has_any_multi_cluster = False
    for c in (clusters or []):
        if len(c.member_indexes) > 1:
            has_any_multi_cluster = True
            for photo in (c.photos or []):
                if photo:
                    multi_cluster_photo_urls.add(photo.rstrip("/"))

    this_photo = (p.get("photo") or "").rstrip("/")

    # Domains / bare hostnames of all found profile URLs.
    found_domains: set[str] = set()
    for other in all_found:
        m = re.search(r'https?://([^/]+)', other.url or "")
        if m:
            found_domains.add(m.group(1).lower())

    # All follower counts > 0 across found results.
    all_fc: list[float] = [
        float((rr.profile or {}).get("followers"))
        for rr in all_found
        if (rr.profile or {}).get("followers") is not None
        and float((rr.profile or {}).get("followers")) > 0
    ]
    this_fc = p.get("followers")

    # ------------------------------------------------------------------
    # Positive signals
    # ------------------------------------------------------------------

    # +50  verified badge on the platform itself
    if p.get("verified") is True:
        score += 50

    # +30  photo perceptually matches another FOUND account (photo cluster)
    if this_photo and this_photo in multi_cluster_photo_urls:
        score += 30

    # +25  bio or linked website cross-references another confirmed account
    bio_website = (
        (p.get("bio") or "") + " " + (p.get("website") or "")
    ).lower()
    if found_domains and any(d in bio_website for d in found_domains):
        score += 25

    # +20  follower count is consistent with the rest of the found set.
    #       "Consistent" = within two orders of magnitude of the median of
    #       accounts that have follower data. Requires at least 2 data points
    #       so a single-account scan doesn't trigger the penalty branch below.
    if all_fc and this_fc is not None and this_fc > 0 and len(all_fc) >= 2:
        try:
            med = statistics.median(all_fc)
            if med > 0:
                ratio = max(float(this_fc), med) / min(float(this_fc), med)
                if ratio <= 100:   # two orders of magnitude
                    score += 20
        except statistics.StatisticsError:
            pass

    # +15  display name matches inferred subject name (case-insensitive,
    #       substring-fuzzy so "Pewdiepie" matches "PewDiePie" etc.)
    if subject_name:
        def _norm(s: str) -> str:
            return re.sub(r"[^a-z0-9]", "", s.lower())
        sn = _norm(subject_name)
        dn = _norm(p.get("display_name") or "")
        if sn and dn and (sn in dn or dn in sn):
            score += 15

    # +10  account shows signs of genuine activity
    has_posts = (p.get("posts") or 0) > 0
    days_ago = _joined_days_ago(p.get("joined") or "")
    has_old_join = days_ago is not None and days_ago > 180
    if has_posts or has_old_join:
        score += 10

    # +20  variant is the bare input username (no separators, no numbers).
    #      An exact handle match is a strong anchor: impostors almost always
    #      use a modified variant (suffix, number, prefix).
    if r.variant and r.variant.lower() == input_username.lower():
        score += 20

    # ------------------------------------------------------------------
    # Negative signals
    # ------------------------------------------------------------------

    # -25  zero posts AND zero followers (parked / placeholder account)
    fc_val = p.get("followers")
    posts_val = p.get("posts")
    if (fc_val is not None and int(fc_val) == 0
            and posts_val is not None and int(posts_val) == 0):
        score -= 25

    # -20  default or placeholder profile photo
    if _is_default_avatar(this_photo):
        score -= 20

    # -15  has a non-default photo that is not shared with any other
    #       found account (only penalise when photo-matching is working)
    if (this_photo
            and not _is_default_avatar(this_photo)
            and has_any_multi_cluster
            and this_photo not in multi_cluster_photo_urls):
        score -= 15

    # -15  variant has an impostor affix AND the plain variant was also
    #       found on the same platform (the plain one is more likely real)
    if _has_impostor_affix(r.variant or ""):
        plain_on_same_site = any(
            other.site == r.site
            and other is not r
            and not _has_impostor_affix(other.variant or "")
            for other in all_found
        )
        if plain_on_same_site:
            score -= 15

    # -10  very low follower count while the subject clearly has a large
    #       audience elsewhere (huge audience-size mismatch)
    max_fc_elsewhere = 0
    if len(all_found) > 1:
        others = [
            int((rr.profile or {}).get("followers") or 0)
            for rr in all_found
            if rr is not r
        ]
        if others:
            max_fc_elsewhere = max(others)
    if (fc_val is not None
            and int(fc_val) < 10
            and max_fc_elsewhere > 100_000):
        score -= 10

    # -20  variant has a number suffix (e.g. pewdiepie123, pewdiepie99)
    #      AND the bare variant (e.g. pewdiepie) was also FOUND on the same
    #      platform.  Co-existence of both strongly suggests the numbered
    #      account is an impostor of the real one.  Only fires when both are
    #      on the same site so 'hamaffs1' on a platform where 'hamaffs' was
    #      never found is not penalised.
    if r.variant and re.search(r'\d+$', r.variant):
        bare = re.sub(r'\d+$', '', r.variant).rstrip('-_.')
        if bare and any(
            other.site == r.site
            and other is not r
            and (other.variant or "").lower() == bare.lower()
            for other in all_found
        ):
            score -= 20

    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# Batch scorer — mutates CheckResult objects in-place
# ---------------------------------------------------------------------------

def score_all(
    found: list["CheckResult"],
    clusters: list,
    subject_name: str,
    input_username: str,
) -> None:
    """Attach .score and .tier to every result in *found* (in-place).

    Idempotent: safe to call multiple times; rescores every time.
    """
    for r in found:
        s = score_result(r, found, clusters, subject_name, input_username)
        r.score = s
        r.tier = tier_from_score(s)
