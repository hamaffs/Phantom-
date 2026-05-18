"""WHOIS / RDAP transform: Domain → registrant + dates + contact emails.

Uses RDAP (Registration Data Access Protocol) — IANA-standardized JSON
over HTTPS that has replaced raw WHOIS for most TLDs. No `python-whois`
dependency; pure HTTP + JSON.

Lookup flow:
  1. GET https://rdap.org/domain/{etld+1}  → 200 with full record on success.
     `rdap.org` redirects to the correct authoritative server, so one URL
     covers all gTLDs and most ccTLDs.

We only WHOIS domains that came from outbound bio links / verified
socials / GPG keys — domains that just host a platform (twitter.com,
github.com) are skipped because their RDAP records are useless for OSINT.

A Domain node qualifies for WHOIS when:
  - it has at least one `linked` edge whose `role` is NOT "hosted_on", OR
  - it has no edges at all (e.g. emitted by another transform standalone)

When a contact email or registrant name appears in the RDAP entity
records, we emit an Email node and stamp the registrant on the Domain.
"""
from __future__ import annotations

import sys
from typing import Any, Optional

import aiohttp

from graph.model import Graph, Node
from graph.transforms import transform


_USER_AGENT = "Phantom-OSINT"
_TIMEOUT = 15.0
_RDAP = "https://rdap.org/domain/{domain}"

# Well-known platform domains don't bother WHOISing - their records
# are uninteresting and they're hosted by registrars/CDNs anyway.
_SKIP_DOMAINS = {
    "twitter.com", "x.com", "instagram.com", "facebook.com", "fb.com",
    "tiktok.com", "youtube.com", "google.com", "github.com", "gitlab.com",
    "bitbucket.org", "linkedin.com", "medium.com", "reddit.com",
    "pinterest.com", "snapchat.com", "discord.com", "discord.gg",
    "telegram.org", "t.me", "whatsapp.com", "tumblr.com",
    "twitch.tv", "kick.com", "vimeo.com", "soundcloud.com", "spotify.com",
    "bandcamp.com", "lastfm.com", "last.fm", "deezer.com",
    "huggingface.co", "kaggle.com", "stackoverflow.com",
    "wordpress.com", "blogger.com", "blogspot.com",
    "tiktokcdn.com", "fbcdn.net", "twimg.com", "ggpht.com",
    "googleusercontent.com", "cdninstagram.com", "jtvnw.net",
    "pinimg.com", "redditmedia.com", "cdn-thumbnails.huggingface.co",
    "githubusercontent.com", "githubassets.com",
    "pastebin.com", "threads.net", "threads.com",
    "yt3.googleusercontent.com",
}


@transform(input="Domain", produces=("Email",))
async def query_rdap(node: Node, g: Graph) -> None:
    host = (node.attrs.get("host") or "").strip().lower()
    if not host:
        return
    if host in _SKIP_DOMAINS:
        return
    # Strip a leading www.
    if host.startswith("www."):
        host = host[4:]
    if host in _SKIP_DOMAINS:
        return
    if "." not in host:
        return

    # Skip if every edge from this Domain has role="hosted_on" (i.e. it's
    # just a platform host attached automatically by adapt()).
    only_hosting = True
    has_any = False
    for e in g.edges(dst=node.id):
        has_any = True
        if e.attrs.get("role") != "hosted_on":
            only_hosting = False
            break
    if has_any and only_hosting:
        return

    url = _RDAP.format(domain=host)
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
    try:
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/rdap+json"},
        ) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, ValueError) as e:
        print(f"whois: rdap.org failed for {host}: {e}", file=sys.stderr)
        return

    if not isinstance(data, dict):
        return

    # Registration / expiration / last-changed events.
    events = data.get("events") or []
    if isinstance(events, list):
        for ev in events:
            if not isinstance(ev, dict):
                continue
            action = (ev.get("eventAction") or "").lower().replace(" ", "_")
            ts = ev.get("eventDate")
            if action and ts and isinstance(ts, str):
                key = f"whois_{action}"
                node.attrs.setdefault(key, ts)

    # Status flags.
    status = data.get("status") or []
    if isinstance(status, list) and status:
        node.attrs.setdefault("whois_status", status)

    # Entities → registrant name, abuse contact emails, etc.
    entities = data.get("entities") or []
    if isinstance(entities, list):
        for ent in entities:
            _absorb_entity(ent, node, g)


def _absorb_entity(entity: Any, domain_node: Node, g: Graph) -> None:
    if not isinstance(entity, dict):
        return
    roles = entity.get("roles") or []
    role_label = "_".join(r for r in roles if isinstance(r, str)) or "contact"

    # vcardArray = ["vcard", [["version",{},"text","4.0"], ["fn",{},"text","Alice Smith"], ["email",{},"text","abuse@example.com"], ...]]
    vcard = entity.get("vcardArray")
    if not (isinstance(vcard, list) and len(vcard) == 2 and isinstance(vcard[1], list)):
        return
    for field in vcard[1]:
        if not (isinstance(field, list) and len(field) >= 4):
            continue
        key = field[0] if isinstance(field[0], str) else ""
        val = field[3] if len(field) >= 4 else None
        if key == "fn" and isinstance(val, str) and val.strip():
            attr_key = f"whois_{role_label}_name"
            domain_node.attrs.setdefault(attr_key, val.strip())
        elif key == "email" and isinstance(val, str) and "@" in val:
            email = val.strip().lower()
            # Skip obvious "redacted for privacy" placeholders.
            if any(redacted in email for redacted in (
                "redacted", "withheld", "privacy", "abuse@",
                "registry-operator", "domain-contact", "anonymous@",
            )):
                continue
            email_node = g.add_node(
                "Email",
                source="whois",
                address=email,
                via="rdap",
                role=role_label,
            )
            g.add_edge(domain_node.id, email_node.id, "linked", role=f"whois_{role_label}")
        elif key == "tel" and isinstance(val, str) and val.strip():
            attr_key = f"whois_{role_label}_tel"
            domain_node.attrs.setdefault(attr_key, val.strip())
        elif key == "org" and isinstance(val, str) and val.strip():
            attr_key = f"whois_{role_label}_org"
            domain_node.attrs.setdefault(attr_key, val.strip())

    # Some registrars stash a name in publicIds or a `handle` field.
    if "whois_registrant_name" not in domain_node.attrs:
        h = entity.get("handle")
        if isinstance(h, str) and h.strip() and "registrant" in role_label:
            domain_node.attrs["whois_registrant_handle"] = h.strip()
