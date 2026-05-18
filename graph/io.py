"""Graph serialization: JSON, GEXF (Gephi), HTML (cytoscape.js).

JSON is the case-on-disk format. GEXF is for opening in Gephi. The HTML
exporter ships a single self-contained page that loads cytoscape.js
from a CDN and inlines the graph data — handy for quick visual review
without spinning up a server.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from graph.model import Edge, Graph, Node


SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------# JSON# ---------------------------------------------------------------------------
def graph_to_dict(g: Graph) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "nodes": [
            {
                "id": n.id,
                "kind": n.kind,
                "attrs": _sanitize_for_json(n.attrs),
                "sources": list(n.sources),
            }
            for n in g.nodes()
        ],
        "edges": [
            {
                "src": e.src,
                "dst": e.dst,
                "kind": e.kind,
                "attrs": _sanitize_for_json(e.attrs),
            }
            for e in g.edges()
        ],
    }


def graph_from_dict(d: dict) -> Graph:
    g = Graph()
    for n in d.get("nodes", []):
        node = g.add_node(n["kind"], node_id=n["id"], **(n.get("attrs") or {}))
        for s in n.get("sources") or []:
            if s not in node.sources:
                node.sources.append(s)
    for e in d.get("edges", []):
        if g.get(e["src"]) and g.get(e["dst"]):
            g.add_edge(e["src"], e["dst"], e["kind"], **(e.get("attrs") or {}))
    return g


def to_json(g: Graph, path: Path) -> None:
    path.write_text(json.dumps(graph_to_dict(g), indent=2, ensure_ascii=False), encoding="utf-8")


def from_json(path: Path) -> Graph:
    return graph_from_dict(json.loads(path.read_text(encoding="utf-8")))


def _sanitize_for_json(d: dict) -> dict:
    """Drop bytes / non-serializable values so json.dump never raises."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, bytes):
            continue
        try:
            json.dumps(v)
        except (TypeError, ValueError):
            continue
        out[k] = v
    return out


# ---------------------------------------------------------------------------# GEXF (Gephi)# ---------------------------------------------------------------------------
def to_gexf(g: Graph, path: Path) -> None:
    """Minimal GEXF 1.3 with node `kind` as an attribute. Suitable for Gephi."""
    lines: list[str] = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append(
        '<gexf xmlns="http://gexf.net/1.3" version="1.3" '
        'xmlns:viz="http://gexf.net/1.3/viz">'
    )
    lines.append('  <graph mode="static" defaultedgetype="directed">')
    lines.append('    <attributes class="node">')
    lines.append('      <attribute id="0" title="kind" type="string"/>')
    lines.append('      <attribute id="1" title="label" type="string"/>')
    lines.append('    </attributes>')
    lines.append('    <attributes class="edge">')
    lines.append('      <attribute id="0" title="kind" type="string"/>')
    lines.append('    </attributes>')
    lines.append('    <nodes>')
    for n in g.nodes():
        label = _node_label(n)
        lines.append(
            f'      <node id="{html.escape(n.id)}" label="{html.escape(label)}">'
        )
        lines.append('        <attvalues>')
        lines.append(f'          <attvalue for="0" value="{html.escape(n.kind)}"/>')
        lines.append(f'          <attvalue for="1" value="{html.escape(label)}"/>')
        lines.append('        </attvalues>')
        r, gc, b = _kind_rgb(n.kind)
        lines.append(f'        <viz:color r="{r}" g="{gc}" b="{b}"/>')
        lines.append('      </node>')
    lines.append('    </nodes>')
    lines.append('    <edges>')
    for i, e in enumerate(g.edges()):
        lines.append(
            f'      <edge id="{i}" source="{html.escape(e.src)}" '
            f'target="{html.escape(e.dst)}">'
        )
        lines.append('        <attvalues>')
        lines.append(f'          <attvalue for="0" value="{html.escape(e.kind)}"/>')
        lines.append('        </attvalues>')
        lines.append('      </edge>')
    lines.append('    </edges>')
    lines.append('  </graph>')
    lines.append('</gexf>')
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------# HTML (cytoscape.js, self-contained)# ---------------------------------------------------------------------------
_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Phantom graph</title>
<script src="https://unpkg.com/cytoscape@3.28.1/dist/cytoscape.min.js"></script>
<style>
  html, body { margin: 0; height: 100%; background: #0f1115; color: #e8eaed; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  #toolbar { padding: 8px 12px; border-bottom: 1px solid #2a2f37; display: flex; gap: 16px; align-items: center; font-size: 13px; }  #cy { position: absolute; top: 41px; left: 0; right: 0; bottom: 0; }  .legend-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 4px; vertical-align: middle; }
  .stat { color: #9aa0a6; }
  #detail { position: absolute; right: 12px; top: 53px; width: 320px; max-height: 70%; overflow: auto; background: #161a21; border: 1px solid #2a2f37; border-radius: 6px; padding: 10px; font-size: 12px; display: none; }  #detail .k { color: #9aa0a6; }  #detail pre { white-space: pre-wrap; word-break: break-word; margin: 4px 0; }</style>
</head>
<body>
<div id="toolbar">
  <strong>Phantom graph</strong>
  <span class="stat">__STATS__</span>
  <span style="margin-left:auto">__LEGEND__</span>
</div>
<div id="cy"></div>
<div id="detail"></div>
<script>
const data = __DATA__;
const KIND_COLORS = __COLORS__;
const elements = [];
for (const n of data.nodes) {
  elements.push({ data: { id: n.id, label: n.label, kind: n.kind, attrs: n.attrs, sources: n.sources } });
}
for (const e of data.edges) {
  elements.push({ data: { source: e.src, target: e.dst, kind: e.kind, attrs: e.attrs } });
}
const cy = cytoscape({
  container: document.getElementById('cy'),
  elements,
  layout: { name: 'cose', animate: false, idealEdgeLength: 90, nodeOverlap: 8, padding: 30 },
  style: [
    { selector: 'node', style: {
      'background-color': (ele) => KIND_COLORS[ele.data('kind')] || '#888',
      'label': 'data(label)', 'color': '#e8eaed', 'font-size': 10,
      'text-valign': 'bottom', 'text-margin-y': 4, 'width': 20, 'height': 20,
      'text-outline-color': '#0f1115', 'text-outline-width': 2,
    }},
    { selector: 'node:selected', style: { 'border-width': 3, 'border-color': '#fff' } },
    { selector: 'edge', style: {
      'width': 1.4, 'line-color': '#3a414c', 'target-arrow-color': '#3a414c',
      'target-arrow-shape': 'triangle', 'curve-style': 'bezier', 'opacity': 0.7,
    }},
    { selector: 'edge[kind = "same_as"]', style: { 'line-color': '#e8a23a', 'target-arrow-color': '#e8a23a', 'line-style': 'dashed' }},
    { selector: 'edge[kind = "owns"]', style: { 'line-color': '#5fb95f', 'target-arrow-color': '#5fb95f' }},
    { selector: 'edge[kind = "appeared_in"]', style: { 'line-color': '#e85f5f', 'target-arrow-color': '#e85f5f' }},
  ],
});
const detail = document.getElementById('detail');
cy.on('tap', 'node', (evt) => {
  const n = evt.target.data();
  let html = `<div><span class="k">id</span> ${n.id}</div>`;
  html += `<div><span class="k">kind</span> ${n.kind}</div>`;
  if (n.sources && n.sources.length) html += `<div><span class="k">sources</span> ${n.sources.join(', ')}</div>`;
  html += `<pre>${JSON.stringify(n.attrs, null, 2)}</pre>`;
  detail.innerHTML = html;
  detail.style.display = 'block';
});
cy.on('tap', (evt) => { if (evt.target === cy) detail.style.display = 'none'; });
</script>
</body>
</html>
"""


_KIND_COLORS = {
    "Identity": "#e8a23a",   # amber — the cluster
    "Account": "#5b8def",    # blue
    "Username": "#a1b5d0",
    "Email": "#9b6dff",      # purple
    "Phone": "#bd6dff",
    "Photo": "#5fb95f",      # green
    "Domain": "#d59d4a",
    "Url": "#7e8a99",
    "Bio": "#c3cba6",
    "Location": "#e8d24a",   # yellow
    "Breach": "#e85f5f",     # red
    "Wallet": "#5fdcdc",
}


def _kind_rgb(kind: str) -> tuple[int, int, int]:
    hexc = _KIND_COLORS.get(kind, "#888888").lstrip("#")
    return int(hexc[0:2], 16), int(hexc[2:4], 16), int(hexc[4:6], 16)


def _node_label(n: Node) -> str:
    """A short human label for a node — never the full canonical ID."""
    a = n.attrs
    if n.kind == "Account":
        return f"{a.get('site', '?')}/{a.get('handle', '?')}"
    if n.kind == "Identity":
        return a.get("display_name") or "Identity"
    if n.kind == "Email":
        return a.get("address") or "email"
    if n.kind == "Photo":
        return f"photo {a.get('phash', '')[:8]}" if a.get("phash") else "photo"
    if n.kind == "Url":
        u = a.get("url") or ""
        return u[:40] + ("…" if len(u) > 40 else "")
    if n.kind == "Domain":
        return a.get("host") or "domain"
    if n.kind == "Location":
        return a.get("label") or "location"
    if n.kind == "Breach":
        return a.get("name") or "breach"
    if n.kind == "Bio":
        t = a.get("text") or ""
        return (t[:32] + "…") if len(t) > 32 else (t or "bio")
    return n.kind


def to_html(g: Graph, path: Path) -> None:
    payload = {
        "nodes": [
            {
                "id": n.id,
                "kind": n.kind,
                "label": _node_label(n),
                "attrs": _sanitize_for_json(n.attrs),
                "sources": list(n.sources),
            }
            for n in g.nodes()
        ],
        "edges": [
            {"src": e.src, "dst": e.dst, "kind": e.kind, "attrs": _sanitize_for_json(e.attrs)}
            for e in g.edges()
        ],
    }
    legend = " ".join(
        f'<span><span class="legend-dot" style="background:{c}"></span>{k}</span>'
        for k, c in _KIND_COLORS.items()
    )
    stats = (
        f'{len(payload["nodes"])} nodes &middot; {len(payload["edges"])} edges &middot; '
        + " &middot; ".join(f"{k} {v}" for k, v in sorted(g.counts_by_kind().items()))
    )
    out = _HTML_TEMPLATE
    out = out.replace("__DATA__", json.dumps(payload, ensure_ascii=False))
    out = out.replace("__COLORS__", json.dumps(_KIND_COLORS))
    out = out.replace("__LEGEND__", legend)
    out = out.replace("__STATS__", stats)
    path.write_text(out, encoding="utf-8")


# ---------------------------------------------------------------------------# Dispatch by extension - used by cli.py# ---------------------------------------------------------------------------
def write_graph(g: Graph, path: Path) -> None:
    suffix = path.suffix.lower()
    if suffix == ".gexf":
        to_gexf(g, path)
    elif suffix in (".html", ".htm"):
        to_html(g, path)
    else:
        to_json(g, path)
