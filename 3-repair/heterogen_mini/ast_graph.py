"""AST-as-graph export, after Argus's utils/ast_graph.py (Python AST).

Argus's model: stable integer node IDs in pre-order traversal,
nodes {id, type, lineno, col_offset, snippet}, parent->child edges,
lineno -> node ids, and an export that adds the mutation path.
This is the same schema for a pycparser C AST, plus one addition
Argus's graph lacks: the edit walk itself (the list of edit IDs in
application order) is recorded alongside the graph, so the graph
shows *which* edits were applied, not just the tree they acted on.
In the full HeteroGen picture this is where the second edge type
would go: edit-dependence edges (search.py's graph), distinct from
the syntactic parent-child edges here.
"""
from __future__ import annotations

from pycparser import c_ast
from pycparser.c_generator import CGenerator


def _snippet(node, gen: CGenerator) -> str:
    try:
        text = gen.visit(node).replace("\n", " ").strip()
    except Exception:  # noqa: BLE001 - best effort only
        text = type(node).__name__
    return text[:117] + "..." if len(text) > 120 else text


def build_ast_graph(ast) -> dict:
    gen = CGenerator()
    nodes, edges, lineno_to_ids = [], [], {}
    counter = [0]

    def visit(node, parent_id):
        counter[0] += 1
        nid = counter[0]
        line = node.coord.line if getattr(node, "coord", None) else None
        nodes.append({
            "id": nid,
            "type": type(node).__name__,
            "lineno": line,
            "snippet": _snippet(node, gen),
        })
        if isinstance(line, int):
            lineno_to_ids.setdefault(line, []).append(nid)
        if parent_id is not None:
            edges.append({"source": parent_id, "target": nid})
        for _, child in node.children():
            visit(child, nid)

    visit(ast, None)
    return {"nodes": nodes, "edges": edges,
            "lineno_to_node_ids": {str(k): v for k, v in lineno_to_ids.items()}}


def export_graph_json(ast, edit_path: list[str], meta: dict) -> dict:
    """Graph of `ast` plus the edit walk that produced it."""
    graph = build_ast_graph(ast)
    graph["edit_path"] = edit_path
    graph["meta"] = meta
    return graph
