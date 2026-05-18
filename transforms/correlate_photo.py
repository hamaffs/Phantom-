"""Photo-hash correlation: same-photo across platforms → same person.

This is a **bulk** operation on the graph, not a per-node `@transform`.
The reason: phash comparison is inherently pairwise, and fetching N
photos is fastest when batched through one aiohttp session. Phase 3's
recursion can call this between rounds.

Pipeline:
  1. Collect Photo nodes that don't already have a phash on them.
  2. Fetch + hash all of them via identity.fetch_photo_data_multi (which
     already handles CDN referers, default-avatar nulling, and budgets).
  3. Store phash/dhash/whash as hex strings on the Photo node attrs.
  4. Pairwise compare hashes — if ANY of the three is below its
     threshold, add a `same_as` edge between the two Photo nodes.
  5. Promote connected components of `same_as` Photo nodes into Identity
     nodes with `owns` edges to the owning Accounts. (A component
     covering only one Account is not promoted — no cross-platform
     evidence yet.)

Thresholds mirror the values tuned in identity.py.
"""
from __future__ import annotations

import sys
from typing import Any, Iterable, Optional

from graph.model import Graph, Node


# Mirrored from identity.py - keep in sync.
_PHASH_MATCH_DISTANCE = 16
_DHASH_MATCH_DISTANCE = 16
_WHASH_MATCH_DISTANCE = 12


async def correlate_photos(g: Graph, *, source: str = "correlate_photo") -> int:
    """Hash unseen Photo nodes, add same_as edges, promote Identities.

    Returns the number of `same_as` edges added (new only).
    """
    try:
        from identity import fetch_photo_data_multi
    except ImportError as e:
        print(f"correlate_photos: identity module unavailable: {e}", file=sys.stderr)
        return 0

    # Collect work - photos that have a url but no phash yet.
    todo: list[Node] = []
    for n in g.nodes("Photo"):
        if not n.attrs.get("url"):
            continue
        if n.attrs.get("phash"):
            continue
        todo.append(n)

    if todo:
        urls = [n.attrs["url"] for n in todo]
        _, phashes, dhashes, whashes = await fetch_photo_data_multi(urls)
        for node, ph, dh, wh in zip(todo, phashes, dhashes, whashes):
            if ph is not None:
                node.attrs["phash"] = str(ph)
            if dh is not None:
                node.attrs["dhash"] = str(dh)
            if wh is not None:
                node.attrs["whash"] = str(wh)
            if source not in node.sources:
                node.sources.append(source)

    edges_added = _add_same_as_edges(g)
    _promote_identities(g)
    return edges_added


def _add_same_as_edges(g: Graph) -> int:
    """Pairwise compare every Photo with a hash; add same_as below threshold."""
    photos: list[Node] = [
        n for n in g.nodes("Photo")
        if n.attrs.get("phash") or n.attrs.get("dhash") or n.attrs.get("whash")
    ]
    added = 0
    for i, a in enumerate(photos):
        for b in photos[i + 1:]:
            dist = _best_distance(a, b)
            if dist is None:
                continue
            ph_d, dh_d, wh_d = dist
            below = []
            if ph_d is not None and ph_d <= _PHASH_MATCH_DISTANCE:
                below.append(("phash", ph_d))
            if dh_d is not None and dh_d <= _DHASH_MATCH_DISTANCE:
                below.append(("dhash", dh_d))
            if wh_d is not None and wh_d <= _WHASH_MATCH_DISTANCE:
                below.append(("whash", wh_d))
            if not below:
                continue
            # Confidence: 1.0 when all three agree on a near-zero distance;
            # ~0.5 when only one of the three trips its threshold.
            confidence = round(min(1.0, 0.4 + 0.2 * len(below)), 3)
            existed_before = (a.id, b.id, "same_as") in g._edges  # type: ignore[attr-defined]
            g.add_edge(
                a.id, b.id, "same_as",
                via="photo_hash",
                hashes=[k for k, _ in below],
                distance={k: v for k, v in below},
                confidence=confidence,
            )
            if not existed_before:
                added += 1
    return added


def _best_distance(a: Node, b: Node) -> Optional[tuple[Optional[int], Optional[int], Optional[int]]]:
    """Return (phash, dhash, whash) hamming distances, None per missing hash."""
    try:
        import imagehash  # type: ignore
    except ImportError:
        return None

    def parse(hex_or_str: Optional[str]):
        if not hex_or_str:
            return None
        try:
            return imagehash.hex_to_hash(hex_or_str)
        except Exception:
            return None

    pa, pb = parse(a.attrs.get("phash")), parse(b.attrs.get("phash"))
    da, db = parse(a.attrs.get("dhash")), parse(b.attrs.get("dhash"))
    wa, wb = parse(a.attrs.get("whash")), parse(b.attrs.get("whash"))

    ph_d = (pa - pb) if (pa is not None and pb is not None) else None
    dh_d = (da - db) if (da is not None and db is not None) else None
    wh_d = (wa - wb) if (wa is not None and wb is not None) else None
    if ph_d is None and dh_d is None and wh_d is None:
        return None
    return ph_d, dh_d, wh_d


def _promote_identities(g: Graph) -> None:
    """Connected components of Photo `same_as` edges → one Identity per multi-Account component."""
    # Find which Account owns each Photo (via incoming has_photo edges).
    photo_to_accounts: dict[str, list[str]] = {}
    for e in g.edges(kind="has_photo"):
        photo_to_accounts.setdefault(e.dst, []).append(e.src)

    # Connected components over photo same_as edges.
    parent: dict[str, str] = {}
    for n in g.nodes("Photo"):
        parent[n.id] = n.id

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for e in g.edges(kind="same_as"):
        if e.src in parent and e.dst in parent:
            union(e.src, e.dst)

    components: dict[str, set[str]] = {}
    for pid in parent:
        components.setdefault(find(pid), set()).add(pid)

    for photos in components.values():
        if len(photos) < 2:
            continue
        owning_accounts: set[str] = set()
        for pid in photos:
            for aid in photo_to_accounts.get(pid, []):
                owning_accounts.add(aid)
        if len(owning_accounts) < 2:
            # Same photo on a single account (e.g. wayback dup) - no cross
            # platform evidence; skip Identity promotion.
            continue
        # Deterministic Identity ID based on the sorted set of owned
        # accounts - re-running this transform won't create duplicates.
        import hashlib
        signature = hashlib.sha1(
            "|".join(sorted(owning_accounts)).encode("utf-8"),
        ).hexdigest()[:16]
        identity_id = f"Identity:photo:{signature}"
        display_name = _pick_display_name(g, owning_accounts)
        identity = g.add_node(
            "Identity",
            node_id=identity_id,
            source="correlate_photo",
            display_name=display_name,
            account_count=len(owning_accounts),
        )
        for aid in owning_accounts:
            g.add_edge(identity.id, aid, "owns", via="photo_cluster")


def _pick_display_name(g: Graph, account_ids: Iterable[str]) -> Optional[str]:
    """Pick the most popular non-empty display_name across the cluster's accounts."""
    counts: dict[str, int] = {}
    for aid in account_ids:
        node = g.get(aid)
        if not node:
            continue
        name = node.attrs.get("display_name")
        if isinstance(name, str) and name.strip():
            counts[name.strip()] = counts.get(name.strip(), 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: (kv[1], len(kv[0])))[0]
