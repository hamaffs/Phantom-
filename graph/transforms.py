"""Transform decorator + registry.

A transform takes one input node and writes new nodes/edges into the
graph. Each is registered with the kind of node it consumes, the kinds
it produces (declarative metadata; runner uses for planning), and an
optional `needs_key` referencing apis.py — if the key isn't configured,
the runner skips the transform silently.

The registry is a process-global list. Re-importing a transform module
is idempotent because the wrapped function is the same object.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, Optional

from graph.model import NodeKind


@dataclass(frozen=True)
class TransformSpec:
    fn: Callable
    input_kind: NodeKind
    produces: tuple[NodeKind, ...]
    needs_key: Optional[str]
    is_async: bool
    name: str


REGISTRY: list[TransformSpec] = []


def transform(
    input: NodeKind,
    produces: tuple[NodeKind, ...] = (),
    needs_key: Optional[str] = None,
):
    """Decorator. Use on a function `f(node, graph) -> None` (or async).

    The function should mutate `graph` directly (add nodes/edges); it
    does not return anything. Idempotency comes from canonical IDs in
    graph/model.py, so re-running a transform on the same node is safe.

    Example:
        @transform(input="email", produces=("Breach",), needs_key="hibp")
        async def hibp(node, g): ...
    """
    def _wrap(fn: Callable) -> Callable:
        spec = TransformSpec(
            fn=fn,
            input_kind=input,
            produces=tuple(produces),
            needs_key=needs_key,
            is_async=asyncio.iscoroutinefunction(fn),
            name=f"{fn.__module__}.{fn.__name__}",
        )
        # Replace any prior registration for the same fn (helps with hot-reload).
        for i, s in enumerate(REGISTRY):
            if s.fn is fn:
                REGISTRY[i] = spec
                return fn
        REGISTRY.append(spec)
        return fn
    return _wrap


def matching(kind: NodeKind) -> list[TransformSpec]:
    return [s for s in REGISTRY if s.input_kind == kind]
