"""Typed graph data model for Phantom V2.

A `Graph` holds typed `Node`s connected by typed `Edge`s. Nodes have
canonical IDs so the same Email / Photo / Account appearing in two
different scans collapses to one node when merged.

Node kinds and edge kinds are open-coded strings (Literal types) rather
than enums so a new transform module can introduce a new kind without
touching this file.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Literal, Optional
from urllib.parse import urlsplit, urlunsplit


NodeKind = Literal[
    "Identity", "Account", "Username", "Email", "Phone",
    "Photo", "Domain", "Url", "Bio", "Location",
    "Breach", "Wallet",
]

EdgeKind = Literal[
    "owns", "has_photo", "has_bio", "has_email", "located",
    "linked", "same_as", "appeared_in", "derived_from",
    "registered_at",
]


# ---------------------------------------------------------------------------# Canonical IDs - same input always produces same ID, so dedup is free.# ---------------------------------------------------------------------------
_WS_RE = re.compile(r"\s+")
_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")


def _norm_url(url: str) -> str:
    """Canonicalise a URL: lower scheme/host, strip fragment, trim trailing /."""
    try:
        s = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    scheme = (s.scheme or "https").lower()
    netloc = s.netloc.lower()
    path = s.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, s.query, ""))


def _etld_plus_one(host: str) -> str:
    """Cheap eTLD+1 — last two labels. Wrong for .co.uk etc., good enough for v1."""
    host = host.strip().lower().lstrip("www.")
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    return ".".join(parts[-2:])


def canonical_id(kind: NodeKind, **attrs: Any) -> str:
    """Build the dedup ID for a node from its kind + attributes.

    Identity is the only kind whose ID is randomly assigned (one per
    cluster); every other kind hashes to a deterministic ID so re-scans
    don't duplicate nodes.
    """
    if kind == "Identity":
        return f"Identity:{attrs.get('uid') or uuid.uuid4().hex}"
    if kind == "Account":
        site = (attrs.get("site") or "").lower()
        handle = (attrs.get("handle") or "").lower()
        return f"Account:{site}:{handle}"
    if kind == "Username":
        return f"Username:{(attrs.get('handle') or '').lower()}"
    if kind == "Email":
        return f"Email:{(attrs.get('address') or '').strip().lower()}"
    if kind == "Phone":
        return f"Phone:{re.sub(r'[^0-9+]', '', attrs.get('number') or '')}"
    if kind == "Photo":
        # Prefer phash if known - same photo across CDNs collapses to one node.
        ph = attrs.get("phash")
        if ph:
            return f"Photo:{ph}"
        url = attrs.get("url") or ""
        h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        return f"Photo:url:{h}"
    if kind == "Url":
        return f"Url:{_norm_url(attrs.get('url') or '')}"
    if kind == "Domain":
        return f"Domain:{_etld_plus_one(attrs.get('host') or '')}"
    if kind == "Bio":
        text = (attrs.get("text") or "").strip()
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
        return f"Bio:{h}"
    if kind == "Location":
        return f"Location:{_WS_RE.sub(' ', (attrs.get('label') or '').strip().lower())}"
    if kind == "Breach":
        return f"Breach:{(attrs.get('name') or '').strip().lower()}"
    if kind == "Wallet":
        chain = (attrs.get("chain") or "").lower()
        addr = (attrs.get("address") or "").lower()
        return f"Wallet:{chain}:{addr}"
    # Fallback: random - caller should have provided enough to dedup.
    return f"{kind}:{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------# Node / Edge dataclasses# ---------------------------------------------------------------------------
@dataclass
class Node:
    id: str
    kind: NodeKind
    attrs: dict[str, Any] = field(default_factory=dict)
    # Provenance: list of transform names that contributed to this node.
    sources: list[str] = field(default_factory=list)

    def merge(self, other: "Node") -> None:
        """Fold another node's attrs into this one. Lists union, scalars favour non-empty."""
        if other.id != self.id:
            raise ValueError(f"cannot merge nodes with different IDs: {self.id} vs {other.id}")
        for k, v in other.attrs.items():
            if v in (None, "", [], {}):
                continue
            existing = self.attrs.get(k)
            if existing in (None, "", [], {}):
                self.attrs[k] = v
            elif isinstance(existing, list) and isinstance(v, list):
                for item in v:
                    if item not in existing:
                        existing.append(item)
            elif isinstance(existing, dict) and isinstance(v, dict):
                for kk, vv in v.items():
                    existing.setdefault(kk, vv)
            # else: keep existing (first writer wins for scalars)
        for s in other.sources:
            if s not in self.sources:
                self.sources.append(s)


@dataclass
class Edge:
    src: str
    dst: str
    kind: EdgeKind
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.src, self.dst, self.kind)


# ---------------------------------------------------------------------------# Graph# ---------------------------------------------------------------------------
class Graph:
    """In-memory graph. Adds are idempotent via canonical IDs."""

    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        self._edges: dict[tuple[str, str, str], Edge] = {}

    # --- add / merge ------------------------------------------------------
    def add_node(
        self,
        kind: NodeKind,
        *,
        source: Optional[str] = None,
        node_id: Optional[str] = None,
        **attrs: Any,
    ) -> Node:
        """Add or merge a node by canonical ID. Returns the stored Node."""
        nid = node_id or canonical_id(kind, **attrs)
        existing = self._nodes.get(nid)
        if existing is None:
            n = Node(id=nid, kind=kind, attrs={k: v for k, v in attrs.items() if v not in (None, "")})
            if source:
                n.sources.append(source)
            self._nodes[nid] = n
            return n
        incoming = Node(id=nid, kind=kind, attrs=dict(attrs), sources=[source] if source else [])
        existing.merge(incoming)
        return existing

    def add_edge(
        self,
        src: str,
        dst: str,
        kind: EdgeKind,
        **attrs: Any,
    ) -> Edge:
        """Add or update an edge. (src, dst, kind) is the dedup key."""
        if src not in self._nodes or dst not in self._nodes:
            raise KeyError(f"edge endpoints must exist: {src} -> {dst}")
        key = (src, dst, kind)
        existing = self._edges.get(key)
        if existing is None:
            e = Edge(src=src, dst=dst, kind=kind, attrs=dict(attrs))
            self._edges[key] = e
            return e
        # Merge attrs: keep highest confidence if both have it.
        for k, v in attrs.items():
            if k == "confidence" and isinstance(v, (int, float)):
                existing.attrs[k] = max(existing.attrs.get(k, 0.0), v)
            else:
                existing.attrs.setdefault(k, v)
        return existing

    def merge(self, other: "Graph") -> None:
        """Fold another graph in. Used by the case CLI."""
        for n in other._nodes.values():
            self.add_node(n.kind, node_id=n.id, **n.attrs)
            stored = self._nodes[n.id]
            for s in n.sources:
                if s not in stored.sources:
                    stored.sources.append(s)
        for e in other._edges.values():
            if e.src in self._nodes and e.dst in self._nodes:
                self.add_edge(e.src, e.dst, e.kind, **e.attrs)

    # --- query ------------------------------------------------------------
    def get(self, node_id: str) -> Optional[Node]:
        return self._nodes.get(node_id)

    def nodes(self, kind: Optional[NodeKind] = None) -> Iterator[Node]:
        if kind is None:
            yield from self._nodes.values()
        else:
            for n in self._nodes.values():
                if n.kind == kind:
                    yield n

    def edges(
        self,
        *,
        kind: Optional[EdgeKind] = None,
        src: Optional[str] = None,
        dst: Optional[str] = None,
    ) -> Iterator[Edge]:
        for e in self._edges.values():
            if kind is not None and e.kind != kind:
                continue
            if src is not None and e.src != src:
                continue
            if dst is not None and e.dst != dst:
                continue
            yield e

    def neighbors(self, node_id: str, *, kind: Optional[EdgeKind] = None) -> list[Node]:
        out: list[Node] = []
        for e in self._edges.values():
            if kind is not None and e.kind != kind:
                continue
            if e.src == node_id and e.dst in self._nodes:
                out.append(self._nodes[e.dst])
            elif e.dst == node_id and e.src in self._nodes:
                out.append(self._nodes[e.src])
        return out

    def connected_components(self, *, edge_kind: EdgeKind = "same_as") -> list[set[str]]:
        """Union-find over edges of `edge_kind`. Returns sets of node IDs."""
        parent: dict[str, str] = {nid: nid for nid in self._nodes}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for e in self._edges.values():
            if e.kind == edge_kind and e.src in parent and e.dst in parent:
                union(e.src, e.dst)

        groups: dict[str, set[str]] = {}
        for nid in self._nodes:
            r = find(nid)
            groups.setdefault(r, set()).add(nid)
        # Only return components with more than one node OR singletons that
        # might still matter to callers - strip purely uninteresting singletons.
        return list(groups.values())

    # --- stats ------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._nodes)

    @property
    def edge_count(self) -> int:
        return len(self._edges)

    def counts_by_kind(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for n in self._nodes.values():
            out[n.kind] = out.get(n.kind, 0) + 1
        return out

    def edge_counts_by_kind(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self._edges.values():
            out[e.kind] = out.get(e.kind, 0) + 1
        return out
