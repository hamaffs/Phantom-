"""Transform runner — Phase 3: recursive run-until-quiescent.

Two entry points:

- `run_transforms(g)` — one pass over current nodes (used by tests and as
  a primitive). Records every `(transform_name, node_id)` it dispatches
  into an internal invocation log so repeated calls are idempotent.

- `run_until_quiescent(g, max_depth=N, max_wall_seconds=T)` — the real
  Phase 3 entry. Calls `run_transforms` until either no new nodes
  appear in a round, depth is exhausted, or wall-clock budget is hit.
  Between rounds it re-runs the bulk correlation helpers (handles +
  photos) so new Accounts/Photos produced mid-recursion can form fresh
  Identity clusters that the next round's transforms can act on.

Transform idempotency: every transform we ship uses canonical IDs that
collapse on `Graph.add_node`, so even without the invocation log a
transform running twice on the same node is safe. The log is a pure
optimization — it prevents redundant network requests when a node was
already processed in an earlier round.
"""
from __future__ import annotations

import asyncio
import sys
import time
from typing import Optional

import apis
from graph.model import Graph, Node
from graph.transforms import REGISTRY, TransformSpec, matching


# ---------------------------------------------------------------------------# Invocation log helpers - attached lazily to the Graph instance.# ---------------------------------------------------------------------------
def _log(g: Graph) -> set[tuple[str, str]]:
    """Return the per-graph set of (transform_name, node_id) pairs that have
    already been dispatched. Lazily created on first access."""
    log = getattr(g, "_invocation_log", None)
    if log is None:
        log = set()
        # Store on the instance so .merge() / serializers can choose to
        # preserve it. For now, runtime-only (not persisted across runs).
        g._invocation_log = log  # type: ignore[attr-defined]
    return log


async def _invoke(spec: TransformSpec, node: Node, g: Graph) -> None:
    try:
        if spec.is_async:
            await spec.fn(node, g)
        else:
            spec.fn(node, g)
    except Exception as e:
        print(
            f"transform {spec.name} failed on {node.id}: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------# Single-pass runner# ---------------------------------------------------------------------------
async def run_transforms(
    g: Graph,
    *,
    max_concurrency: int = 10,
    only: Optional[list[str]] = None,
    skip: Optional[list[str]] = None,
    only_nodes: Optional[set[str]] = None,
) -> int:
    """Apply every applicable transform to every existing node, once.

    `only_nodes`, when provided, restricts dispatch to those node IDs —
    used by the recursive runner to focus a round on the frontier.

    Returns the count of transform invocations that actually fired
    (after filtering and skipping already-logged pairs).
    """
    if not REGISTRY:
        return 0

    sem = asyncio.Semaphore(max_concurrency)
    skipped_missing_key: set[str] = set()
    log = _log(g)

    async def _bound(spec: TransformSpec, node: Node) -> None:
        async with sem:
            await _invoke(spec, node, g)

    snapshot = list(g.nodes())
    tasks: list[asyncio.Task] = []

    for node in snapshot:
        if only_nodes is not None and node.id not in only_nodes:
            continue
        for spec in matching(node.kind):
            if only and spec.name not in only:
                continue
            if skip and spec.name in skip:
                continue
            if spec.needs_key and not apis.get(spec.needs_key):
                skipped_missing_key.add(spec.name)
                continue
            key = (spec.name, node.id)
            if key in log:
                continue
            log.add(key)
            tasks.append(asyncio.create_task(_bound(spec, node)))

    if not tasks:
        return 0

    await asyncio.gather(*tasks, return_exceptions=True)

    if skipped_missing_key:
        for name in sorted(skipped_missing_key):
            print(
                f"transform {name} skipped: missing API key "
                f"(configure via `phantom --api add ...`)",
                file=sys.stderr,
            )

    return len(tasks)


# ---------------------------------------------------------------------------# Recursive runner - Phase 3# ---------------------------------------------------------------------------
async def run_until_quiescent(
    g: Graph,
    *,
    max_depth: int = 4,
    max_wall_seconds: float = 90.0,
    max_concurrency: int = 10,
    re_correlate: bool = True,
    verbose: bool = True,
) -> dict:
    """Keep firing transforms until the graph stops growing.

    Each round:
      1. Snapshot the current node IDs as the "before" set.
      2. Run every applicable transform on every node (skipping pairs
         already in the invocation log).
      3. If new nodes appeared, optionally re-run correlate_handles and
         correlate_photos so freshly-discovered Accounts/Photos can
         contribute to Identity clusters.
      4. If nothing new appeared, return.

    Budgets:
      - `max_depth` caps the number of rounds (default 4).
      - `max_wall_seconds` caps total wall time spent inside this call.
      - The invocation log dedupes work, so a hung transform can't be
        re-invoked on the same node across rounds.

    Returns a summary dict with stats per round.
    """
    started = time.monotonic()
    summary: dict = {"rounds": []}

    for depth in range(1, max_depth + 1):
        # Frontier = nodes that didn't exist before THIS round started.
        # On round 1, the entire graph is the frontier.
        before_ids = set(n.id for n in g.nodes())
        before_count = len(before_ids)
        before_edges = g.edge_count

        elapsed = time.monotonic() - started
        if elapsed >= max_wall_seconds:
            if verbose:
                print(
                    f"graph: round {depth} skipped — wall-clock budget "
                    f"({max_wall_seconds:.0f}s) exhausted",
                    file=sys.stderr,
                )
            break

        round_started = time.monotonic()
        invocations = await run_transforms(g, max_concurrency=max_concurrency)
        round_elapsed = time.monotonic() - round_started

        after_count = len(g)
        after_edges = g.edge_count
        new_nodes = after_count - before_count
        new_edges = after_edges - before_edges

        # Inter-round re-correlation when fresh accounts/photos appeared.
        if re_correlate and new_nodes > 0:
            new_accounts = any(
                n.kind == "Account" and n.id not in before_ids for n in g.nodes()
            )
            new_photos = any(
                n.kind == "Photo" and n.id not in before_ids for n in g.nodes()
            )
            if new_accounts or new_photos:
                # Lazy imports - runner shouldn't depend on the
                # transforms package at module-load time (avoids cycles).
                from transforms.correlate_handle import correlate_handles
                from transforms.correlate_photo import correlate_photos
                if new_accounts:
                    correlate_handles(g)
                if new_photos:
                    await correlate_photos(g)

        round_stat = {
            "depth": depth,
            "invocations": invocations,
            "new_nodes": new_nodes,
            "new_edges": new_edges,
            "elapsed_seconds": round(round_elapsed, 2),
            "total_nodes": after_count,
            "total_edges": after_edges,
        }
        summary["rounds"].append(round_stat)

        if verbose:
            print(
                f"graph: round {depth} — {invocations} transform calls, "
                f"+{new_nodes} nodes, +{new_edges} edges "
                f"({round_elapsed:.1f}s)",
                file=sys.stderr,
            )

        if new_nodes == 0:
            if verbose:
                print(
                    f"graph: quiescent at depth {depth} "
                    f"(total {after_count} nodes, {after_edges} edges)",
                    file=sys.stderr,
                )
            break
    else:
        if verbose:
            print(
                f"graph: depth budget {max_depth} reached "
                f"(total {len(g)} nodes, {g.edge_count} edges)",
                file=sys.stderr,
            )

    summary["total_seconds"] = round(time.monotonic() - started, 2)
    summary["final_nodes"] = len(g)
    summary["final_edges"] = g.edge_count
    return summary
