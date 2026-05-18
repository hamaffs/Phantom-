"""Snowflake / numeric-ID dating.

Several platforms encode account creation time inside their numeric
user IDs. Given the ID, you can recover the millisecond-precision
creation date with zero network requests.

- **Twitter / X**: pre-2010 IDs are sequential integers (<8e9); IDs from
  2010-11-04 onward are Twitter Snowflakes (epoch 2010-11-04 01:42:54.657 UTC,
  timestamp = (id >> 22) + epoch_ms).
- **Discord**: epoch 2015-01-01 00:00:00 UTC, same bit layout.
- **Instagram**: epoch 2011-08-24 21:07:01.721 UTC, timestamp = (id >> 23) + epoch_ms.
- **Reddit**: t2_ ID is base36; doesn't carry a timestamp directly, but the
  `created_utc` field of the /about.json endpoint does. We don't fetch
  here — Reddit handled by the enrich step already.

This transform reads `attrs.user_id` on each Account node and writes
`attrs.created_at` (ISO 8601 UTC). No new nodes are produced.

When the Account doesn't have a `user_id` populated, we silently skip.
The from_scan adapter doesn't currently pull user_id, but enrich.py's
extract_twitter / extract_instagram / extract_tiktok do populate it
into `profile.user_id` — adapt would surface those via Account.attrs.
We re-read both spelling variants for safety.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from graph.model import Graph, Node
from graph.transforms import transform


# Epoch milliseconds for each platform's snowflake scheme.
_TWITTER_EPOCH_MS = 1288834974657
# 2010-11-04 01:42:54.657 UTC
_DISCORD_EPOCH_MS = 1420070400000      # 2015-01-01 00:00:00 UTC
_INSTAGRAM_EPOCH_MS = 1314220021721    # 2011-08-24 21:07:01.721 UTC

# Pre-Snowflake Twitter (sequential int IDs) cutoff. IDs below this came# before 2010-11-04 and can only say "pre-2010" - not decodable.
_TWITTER_SNOWFLAKE_MIN = 8_000_000_000


@transform(input="Account", produces=())
def date_from_snowflake(node: Node, g: Graph) -> None:
    """Decode the account's numeric ID into a creation timestamp.

    Mutates the node attrs in place: adds `created_at` (ISO 8601) and
    `created_at_source` ("snowflake:twitter" etc.) when successful.
    """
    if node.attrs.get("created_at") and node.attrs.get("created_at_source"):
        return  # already decoded

    site = (node.attrs.get("site") or "").lower()
    raw_id = (
        node.attrs.get("user_id")
        or node.attrs.get("id")
        or node.attrs.get("uid")
    )

    if raw_id is None:
        return
    try:
        uid = int(raw_id)
    except (TypeError, ValueError):
        return
    if uid <= 0:
        return

    iso, source = _decode(site, uid)
    if iso:
        node.attrs["created_at"] = iso
        node.attrs["created_at_source"] = source
        if "snowflake" not in node.sources:
            node.sources.append("snowflake")


def _decode(site: str, uid: int) -> tuple[Optional[str], Optional[str]]:
    # Twitter / X
    if site in ("twitter", "x"):
        if uid < _TWITTER_SNOWFLAKE_MIN:
            return ("pre-2010-11-04", "twitter:legacy-sequential")
        ms = (uid >> 22) + _TWITTER_EPOCH_MS
        return _ms_to_iso(ms), "snowflake:twitter"
    # Discord
    if site == "discord":
        ms = (uid >> 22) + _DISCORD_EPOCH_MS
        return _ms_to_iso(ms), "snowflake:discord"
    # Instagram
    if site == "instagram":
        ms = (uid >> 23) + _INSTAGRAM_EPOCH_MS
        return _ms_to_iso(ms), "snowflake:instagram"
    return None, None


def _ms_to_iso(ms: int) -> Optional[str]:
    try:
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    # Sanity: filter ridiculous dates (before any platform existed, far future).
    if not (datetime(2005, 1, 1, tzinfo=timezone.utc) <= dt <= datetime(2100, 1, 1, tzinfo=timezone.utc)):
        return None
    return dt.isoformat(timespec="seconds")
