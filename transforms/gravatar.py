"""Gravatar transform: Email → Photo + display_name + verified accounts.

Uses Gravatar's v3 Profiles API (`api.gravatar.com/v3/profiles/{sha256}`)
which replaced the legacy `gravatar.com/<md5>.json` endpoint in 2024.
The v3 API uses **SHA-256** of the lowercase email, not MD5 (MD5 still
works for the legacy `/avatar/{md5}` image route as a back-compat
shim, but the rich profile data is SHA-256-only).

Yield:
  - Avatar image URL → Photo node + `linked` edge
  - Verified social accounts (Twitter, GitHub, etc.) → Url nodes
  - display_name / location / description → stamped onto the Email node
"""
from __future__ import annotations

import hashlib
import sys
from typing import Any

import aiohttp

from graph.model import Graph, Node
from graph.transforms import transform


_PROFILE_URL = "https://api.gravatar.com/v3/profiles/{sha}"
_AVATAR_URL = "https://gravatar.com/avatar/{sha}?d=404"
_USER_AGENT = "Phantom-OSINT"
_TIMEOUT = 10.0


@transform(input="Email", produces=("Photo", "Url"))
async def query_gravatar(node: Node, g: Graph) -> None:
    address = (node.attrs.get("address") or "").strip().lower()
    if not address or "@" not in address:
        return

    sha = hashlib.sha256(address.encode("utf-8")).hexdigest()
    md5 = hashlib.md5(address.encode("utf-8")).hexdigest()
    profile_url = _PROFILE_URL.format(sha=sha)

    timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        try:
            async with session.get(profile_url) as resp:
                if resp.status == 404:
                    return
                if resp.status != 200:
                    return
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, OSError, ValueError) as e:
            print(f"gravatar: profile fetch failed for {address}: {e}", file=sys.stderr)
            return

    if not isinstance(data, dict) or data.get("error"):
        return

    display_name = data.get("display_name")
    location = data.get("location")
    description = data.get("description")
    job_title = data.get("job_title")
    company = data.get("company")
    pronouns = data.get("pronouns")
    profile_html = data.get("profile_url")
    avatar_url = data.get("avatar_url") or f"https://gravatar.com/avatar/{md5}"

    # Annotate the Email node directly with Gravatar's claim.
    if display_name:
        node.attrs.setdefault("gravatar_display_name", display_name)
    if location:
        node.attrs.setdefault("gravatar_location", location)
    if description:
        node.attrs.setdefault("gravatar_description", description[:280])
    if job_title:
        node.attrs.setdefault("gravatar_job_title", job_title)
    if company:
        node.attrs.setdefault("gravatar_company", company)
    if pronouns:
        node.attrs.setdefault("gravatar_pronouns", pronouns)
    if "gravatar" not in node.sources:
        node.sources.append("gravatar")

    # Photo node - uses the SHA-256-based URL (canonical, v3-style).
    photo = g.add_node(
        "Photo",
        source="gravatar",
        url=avatar_url,
        via="gravatar",
        gravatar_sha256=sha,
        gravatar_md5=md5,
    )
    g.add_edge(node.id, photo.id, "linked", role="gravatar_avatar")

    # Profile page URL.
    if profile_html:
        profile_node = g.add_node("Url", source="gravatar", url=profile_html)
        g.add_edge(node.id, profile_node.id, "linked", role="gravatar_profile")

    # Location → Location node.
    if isinstance(location, str) and location.strip():
        loc_node = g.add_node("Location", source="gravatar", label=location.strip())
        g.add_edge(node.id, loc_node.id, "located")

    # Verified accounts - v3 returns these with {service_label, service_icon, url, ...}.
    verified = data.get("verified_accounts") or []
    if isinstance(verified, list):
        for v in verified:
            if not isinstance(v, dict):
                continue
            url = v.get("url")
            if not isinstance(url, str) or not url.strip():
                continue
            u_node = g.add_node(
                "Url",
                source="gravatar",
                url=url.strip(),
                via="gravatar_verified",
                service=v.get("service_label") or v.get("service_type"),
            )
            g.add_edge(node.id, u_node.id, "linked", role="gravatar_verified")

    # Links - free-form site list.
    free_links = data.get("links") or []
    if isinstance(free_links, list):
        for item in free_links:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if isinstance(url, str) and url.strip():
                u_node = g.add_node(
                    "Url",
                    source="gravatar",
                    url=url.strip(),
                    label=item.get("label"),
                )
                g.add_edge(node.id, u_node.id, "linked", role="gravatar_link")
