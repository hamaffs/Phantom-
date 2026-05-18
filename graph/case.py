"""Persistent investigation cases.

A "case" is one accumulating graph on disk. Each scan against a case
merges new nodes/edges into the file. Pattern modeled on watch.py's
snapshot store; format is JSON via graph/io.py.

Layout:
    ~/.local/share/phantom/cases/<slug>.json

XDG-aware: respects XDG_DATA_HOME when set.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from graph.io import graph_from_dict, graph_to_dict
from graph.model import Graph


_FILESAFE = str.maketrans({c: "_" for c in r' /\:?"*<>|'})
_SLUG_OK = re.compile(r"^[A-Za-z0-9._-]+$")


def cases_dir() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return base / "phantom" / "cases"


def case_path(name: str) -> Path:
    slug = name.strip().translate(_FILESAFE) or "case"
    return cases_dir() / f"{slug}.json"


@dataclass
class Case:
    name: str
    created_at: str
    updated_at: str
    targets: list[str] = field(default_factory=list)   # original inputs added so far
    graph: Graph = field(default_factory=Graph)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "targets": list(self.targets),
            "graph": graph_to_dict(self.graph),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Case":
        return cls(
            name=d["name"],
            created_at=d.get("created_at") or _now(),
            updated_at=d.get("updated_at") or _now(),
            targets=list(d.get("targets") or []),
            graph=graph_from_dict(d.get("graph") or {}),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def exists(name: str) -> bool:
    return case_path(name).is_file()


def new(name: str) -> Case:
    """Create an empty case. Errors if one already exists."""
    if not name.strip():
        raise ValueError("case name must be non-empty")
    if exists(name):
        raise FileExistsError(f"case {name!r} already exists at {case_path(name)}")
    p = case_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    now = _now()
    c = Case(name=name, created_at=now, updated_at=now)
    save(c)
    return c


def load(name: str) -> Case:
    p = case_path(name)
    if not p.is_file():
        raise FileNotFoundError(f"case {name!r} not found at {p}")
    return Case.from_dict(json.loads(p.read_text(encoding="utf-8")))


def save(c: Case) -> Path:
    p = case_path(c.name)
    p.parent.mkdir(parents=True, exist_ok=True)
    c.updated_at = _now()
    p.write_text(json.dumps(c.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return p


def remove(name: str) -> None:
    p = case_path(name)
    if p.is_file():
        p.unlink()


def list_cases() -> list[dict]:
    """Return a tiny manifest per case: name, targets, counts, updated_at."""
    out: list[dict] = []
    d = cases_dir()
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            g = graph_from_dict(raw.get("graph") or {})
            out.append({
                "name": raw.get("name", p.stem),
                "targets": raw.get("targets") or [],
                "node_count": len(g),
                "edge_count": g.edge_count,
                "updated_at": raw.get("updated_at"),
                "path": str(p),
            })
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    return out


def merge_into(name: str, target: str, fresh: Graph) -> Case:
    """Merge `fresh` into the case `name`, recording `target` as a member input."""
    c = load(name)
    c.graph.merge(fresh)
    if target and target not in c.targets:
        c.targets.append(target)
    save(c)
    return c
