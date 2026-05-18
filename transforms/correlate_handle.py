"""Handle-based correlation: same Username on different platforms.

Weaker signal than photo-hash matching — many people share a common
handle without being the same person. So this transform adds
`same_as` edges with **low** confidence (0.3) between Accounts sharing
a Username node. Identity promotion in correlate_photo.py only fires on
high-confidence evidence, so these soft edges accumulate as hints
without false-merging strangers.

This is a bulk operation, called from the boundary after `adapt()`.
"""
from __future__ import annotations

from graph.model import Graph


def correlate_handles(g: Graph) -> int:
    """Add Account↔Account same_as edges for accounts sharing a Username.

    Returns the count of newly added edges.
    """
    added = 0
    for username in g.nodes("Username"):
        # Find every Account linked to this Username (role="username").
        accounts: list[str] = []
        for e in g.edges(kind="linked", dst=username.id):
            if e.attrs.get("role") == "username" and g.get(e.src) and g.get(e.src).kind == "Account":  # type: ignore[union-attr]
                accounts.append(e.src)
        if len(accounts) < 2:
            continue
        # Pairwise same_as.
        for i, a in enumerate(accounts):
            for b in accounts[i + 1:]:
                # Don't downgrade if a stronger edge already exists.
                existing = next(
                    (ee for ee in g.edges(kind="same_as", src=a, dst=b)),
                    None,
                ) or next(
                    (ee for ee in g.edges(kind="same_as", src=b, dst=a)),
                    None,
                )
                if existing and existing.attrs.get("confidence", 0) >= 0.3:
                    continue
                key_before = (a, b, "same_as") in g._edges  # type: ignore[attr-defined]
                g.add_edge(a, b, "same_as", via="shared_handle", confidence=0.3)
                if not key_before:
                    added += 1
    return added
