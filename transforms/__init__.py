"""Phantom transforms — auto-loads every sibling module so each
`@transform(...)` decorator registers at import time.

Boundary helpers (not @transforms):
    adapt(results)        — scan results → Graph
    correlate_photos(g)   — bulk photo-hash correlation + identity promotion
    correlate_handles(g)  — bulk shared-handle same_as edges

Per-node transforms (registered via @transform):
    hibp.query_hibp       — Email → Breach (needs HIBP API key)
"""
from __future__ import annotations

import importlib
import pkgutil

# Auto-import every sibling module so its @transform decorators fire.
for _info in pkgutil.iter_modules(__path__):
    if _info.name.startswith("_"):
        continue
    importlib.import_module(f"{__name__}.{_info.name}")

# Public bulk helpers - direct re-exports for cli.py.
from transforms.correlate_handle import correlate_handles
from transforms.correlate_photo import correlate_photos
from transforms.from_scan import adapt

__all__ = ["adapt", "correlate_handles", "correlate_photos"]
