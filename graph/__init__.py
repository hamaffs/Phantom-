"""Phantom graph data model + transform engine.

Public API:
    Graph, Node, Edge — typed data model (graph.model)
    transform()        — decorator for registering transforms (graph.transforms)
    run_transforms()   — async one-pass runner (graph.runner)
    write_graph()      — emit JSON/GEXF/HTML by file extension (graph.io)
    case               — persistent investigation cases (graph.case)
"""
from graph.case import (
    Case,
    case_path,
    cases_dir,
    exists as case_exists,
    list_cases,
    load as load_case,
    merge_into as merge_into_case,
    new as new_case,
    remove as remove_case,
    save as save_case,
)
from graph.io import (
    from_json,
    graph_from_dict,
    graph_to_dict,
    to_gexf,
    to_html,
    to_json,
    write_graph,
)
from graph.model import Edge, EdgeKind, Graph, Node, NodeKind, canonical_id
from graph.runner import run_transforms, run_until_quiescent
from graph.transforms import REGISTRY, TransformSpec, matching, transform

__all__ = [
    "Case", "Edge", "EdgeKind", "Graph", "Node", "NodeKind",
    "REGISTRY", "TransformSpec",
    "canonical_id", "case_exists", "case_path", "cases_dir",
    "from_json", "graph_from_dict", "graph_to_dict",
    "list_cases", "load_case", "matching", "merge_into_case",
    "new_case", "remove_case", "run_transforms", "run_until_quiescent", "save_case",
    "to_gexf", "to_html", "to_json", "transform", "write_graph",
]
