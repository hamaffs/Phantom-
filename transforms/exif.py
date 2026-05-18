"""EXIF transform: Photo → GPS / camera / software / datetime.

Most major platforms (Twitter, Instagram, Facebook, GitHub avatars, …)
strip EXIF on upload. But the long tail — personal sites, Tumblr, niche
forums, Wayback snapshots of old uploads — often doesn't. When EXIF is
preserved, you typically get **at least** the camera make/model and
software; sometimes datetime; rarely-but-jackpot, GPS coordinates.

This transform downloads each Photo node's bytes (if not already cached
on the node), runs Pillow's `_getexif()`, and writes whatever it finds
into `attrs.exif`. When GPS is present, we also emit a Location node
with the rounded coordinates as the label.
"""
from __future__ import annotations

import asyncio
import sys
from io import BytesIO
from typing import Any, Optional

import aiohttp

from graph.model import Graph, Node
from graph.transforms import transform


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_TIMEOUT = 12.0
_MAX_BYTES = 4 * 1024 * 1024  # EXIF lives in the first few KB; 4MB is plenty


@transform(input="Photo", produces=("Location",))
async def extract_exif(node: Node, g: Graph) -> None:
    # Skip when EXIF has already been extracted on a prior pass.
    if "exif" in node.attrs:
        return
    url = node.attrs.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return

    # Identify if already have bytes elsewhere - Phase 1's
    # correlate_photo doesn't store bytes on the node, so fetch.
    data = await _fetch(url)
    if not data:
        return

    exif = _read_exif(data)
    if not exif:
        # Stamp an empty marker so a Phase 3 re-run doesn't re-fetch.
        node.attrs["exif"] = {}
        return

    node.attrs["exif"] = exif
    if "exif" not in node.sources:
        node.sources.append("exif")

    # GPS → Location node.    lat, lon = exif.get("gps_latitude"), exif.get("gps_longitude")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        label = f"{lat:.4f},{lon:.4f}"
        loc = g.add_node(
            "Location", source="exif",
            label=label, latitude=lat, longitude=lon,
            via="photo_exif", source_url=url,
        )
        g.add_edge(node.id, loc.id, "located", via="exif")


async def _fetch(url: str) -> Optional[bytes]:
    headers = {"User-Agent": _USER_AGENT}
    # Re-use the platform-specific Referer table from identity.py.
    try:
        from identity import _referer_for
        ref = _referer_for(url)
        if ref:
            headers["Referer"] = ref
    except Exception:
        pass

    timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return None
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = await resp.content.readany()
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= _MAX_BYTES:
                        break
                if total < 200:
                    return None
                return b"".join(chunks)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return None


def _read_exif(data: bytes) -> dict[str, Any]:
    try:
        from PIL import Image, ExifTags  # type: ignore
    except ImportError:
        return {}
    try:
        img = Image.open(BytesIO(data))
        raw = getattr(img, "_getexif", lambda: None)()
    except Exception:
        return {}
    if not raw:
        return {}

    tag_map = {v: k for k, v in ExifTags.TAGS.items()}
    gps_tag_id = tag_map.get("GPSInfo")

    out: dict[str, Any] = {}
    for tag_id, value in raw.items():
        name = ExifTags.TAGS.get(tag_id, str(tag_id))
        if name == "GPSInfo" and isinstance(value, dict):
            gps = _gps_to_decimal(value)
            if gps:
                out["gps_latitude"], out["gps_longitude"] = gps
            continue
        if not isinstance(value, (str, int, float, bytes)):
            continue
        if isinstance(value, bytes):
            try:
                value = value.decode("ascii", errors="replace").strip("\x00").strip()
            except Exception:
                continue
        if isinstance(value, str):
            value = value.strip().strip("\x00").strip()
            if not value:
                continue
        # Whitelist the fields actually want - random EXIF tags are noise.
        if name in (
            "Make", "Model", "Software", "DateTime", "DateTimeOriginal",
            "DateTimeDigitized", "Artist", "Copyright", "ImageDescription",
            "LensModel", "LensMake", "BodySerialNumber", "CameraOwnerName",
            "HostComputer", "ExifVersion", "Orientation",
            "ApertureValue", "FocalLength", "ISOSpeedRatings",
        ):
            out[_snake(name)] = value
    return out


def _gps_to_decimal(gps: dict) -> Optional[tuple[float, float]]:
    """Convert the EXIF GPS rationals → decimal degrees, signed."""
    try:
        from PIL.ExifTags import GPSTAGS  # type: ignore
    except ImportError:
        return None
    named = {GPSTAGS.get(k, k): v for k, v in gps.items()}
    lat = named.get("GPSLatitude")
    lat_ref = named.get("GPSLatitudeRef")
    lon = named.get("GPSLongitude")
    lon_ref = named.get("GPSLongitudeRef")
    if not (lat and lon and lat_ref and lon_ref):
        return None
    try:
        def _to_deg(v):
            d, m, s = v
            return float(d) + float(m) / 60.0 + float(s) / 3600.0
        lat_d = _to_deg(lat)
        lon_d = _to_deg(lon)
    except Exception:
        return None
    if isinstance(lat_ref, bytes):
        lat_ref = lat_ref.decode("ascii", errors="replace")
    if isinstance(lon_ref, bytes):
        lon_ref = lon_ref.decode("ascii", errors="replace")
    if str(lat_ref).strip().upper() == "S":
        lat_d = -lat_d
    if str(lon_ref).strip().upper() == "W":
        lon_d = -lon_d
    return (round(lat_d, 6), round(lon_d, 6))


def _snake(name: str) -> str:
    return "".join("_" + c.lower() if c.isupper() else c for c in name).lstrip("_")
