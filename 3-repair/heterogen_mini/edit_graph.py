"""Dependence graph over grounded (parameterized) edits.

HeteroGen's key search idea: fix patterns are not applied in a random
order -- they have *dependences*. One edit can enable another (its
precondition only holds after the first), or conflict with it. The
graph below is the simplified reproduction of that idea for the allocation
task; the search loop (search.py) walks it in topological order and
rolls back any step that breaks compilation or behaviour.

Nodes are Edit objects; an edge A -> B means "A must be applied before B".

allocation plan (MAX_N = 8, matrices are 8x8 = 64 floats):

  remove_free(A) ──> array_static(A)
  remove_free(B) ──> array_static(B)
  remove_free(C) ──> array_static(C)
  pointer(matmul, A) ─┐
  pointer(matmul, B) ─┼─> index_static(matmul, n -> MAX_N)
  pointer(matmul, C) ─┘         │
  add_define(MAX_N, 8) ────────────────────────┘
                                              │
                                              v
                                   insert(PIPELINE, matmul)

Why these edges:
- The malloc/free pair must be ordered free-first: deleting
  malloc(A) while free(A) still stands makes the intermediate program
  free a stack array and crash at runtime, so the per-step validator
  rejects it. Deleting free(A) first only leaks (in the intermediate
  state) and keeps every prefix executable -- the graph must order
  edits so that *every prefix* is runnable, not just the final state.
  (The first draft of this graph had the edge backwards; the validator
  caught it -- see README.)
- index_static replaces `n` by MAX_N, so MAX_N must exist
  (add_define) and the kernel's parameters must already be fixed-size
  arrays -- otherwise the body would index a raw pointer with a
  constant bound while the signature still promises a runtime size.
- The PIPELINE pragma only makes sense once loop bounds are constant.
"""
from __future__ import annotations

from collections import defaultdict

from .edits import Edit


class EditGraph:
    def __init__(self):
        self.nodes: dict[str, Edit] = {}
        self._order: list[str] = []
        self.edges: set[tuple[str, str]] = set()  # (before, after)

    def add(self, edit: Edit) -> Edit:
        self.nodes[edit.id] = edit
        self._order.append(edit.id)
        return edit

    def depends_on(self, after_id: str, before_id: str):
        """`after` may only run once `before` has been applied."""
        if after_id not in self.nodes or before_id not in self.nodes:
            raise KeyError(f"unknown edit in edge {before_id} -> {after_id}")
        self.edges.add((before_id, after_id))

    def add_group(self, name: str, ids: list[str]):
        """Mark edits as mutually exclusive alternatives: at most one
        edit in the group is applied (paper Fig. 7: two repair paths
        through the DAG)."""
        for nid in ids:
            if nid not in self.nodes:
                raise KeyError(f"unknown edit in group {name}: {nid}")
            self.nodes[nid].group = name

    def prerequisites(self, edit_id: str) -> list[str]:
        return [b for (b, a) in sorted(self.edges) if a == edit_id]

    def topo_order(self) -> list[Edit]:
        indeg = {nid: 0 for nid in self.nodes}
        succ: dict[str, list[str]] = defaultdict(list)
        for b, a in self.edges:
            succ[b].append(a)
            indeg[a] += 1
        ready = [nid for nid in self._order if indeg[nid] == 0]
        out: list[str] = []
        while ready:
            nid = ready.pop(0)
            out.append(nid)
            for nxt in sorted(succ[nid]):
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    ready.append(nxt)
        if len(out) != len(self.nodes):
            raise ValueError("dependence graph has a cycle")
        return [self.nodes[nid] for nid in out]

    def to_dot(self) -> str:
        lines = ["digraph edits {"]
        for nid in self._order:
            e = self.nodes[nid]
            lines.append(f'  "{nid}" [label="{nid}\\n{e.kind}"];')
        for b, a in sorted(self.edges):
            lines.append(f'  "{b}" -> "{a}";')
        lines.append("}")
        return "\n".join(lines)


def build_allocation_plan(max_n: int = 8) -> EditGraph:
    """Grounded edit graph for tasks/allocation.c, parameterized by MAX_N."""
    max_elems = max_n * max_n
    g = EditGraph()
    g.add(Edit("E1", "add_define", {"name": "MAX_N", "value": str(max_n)},
               f"introduce compile-time constant MAX_N={max_n}"))
    for i, var in enumerate("ABC", start=2):
        g.add(Edit(f"E{i}", "array_static",
                   {"var": var, "max_elems": max_elems},
                   f"heap allocation of {var} -> static array [{max_elems}]"))
    for j, var in enumerate("ABC", start=5):
        g.add(Edit(f"E{j}", "remove_free", {"var": var},
                   f"delete free({var})"))
        # Free-removal precedes malloc-removal: every graph prefix must
        # stay executable (see module docstring).
        g.depends_on(f"E{j - 3}", f"E{j}")
    for k, var in enumerate("ABC", start=8):
        g.add(Edit(f"E{k}", "pointer",
                   {"func": "matmul", "param": var, "max_elems": max_elems},
                   f"matmul param {var}: float* -> float[{max_elems}]"))
    g.add(Edit("E11", "index_static",
               {"func": "matmul", "bound_var": "n", "const_name": "MAX_N"},
               "matmul loop bounds and indexing: n -> MAX_N"))
    g.add(Edit("E12", "insert",
               {"func": "matmul", "pragma": "#pragma HLS PIPELINE"},
               "annotate matmul's outer loop for HLS pipelining"))
    # MAX_N must exist, and params must be arrays, before fixing bounds.
    g.depends_on("E11", "E1")
    for k in ("E8", "E9", "E10"):
        g.depends_on("E11", k)
    # Pipelining only makes sense once bounds are constant.
    g.depends_on("E12", "E11")
    return g


def build_recursion_plan(max_n: int = 64) -> EditGraph:
    """Grounded edit graph for tasks/recursion.c.

    The graph has a genuine fork -- two alternative repair paths for
    the recursion, in the style of the paper's Fig. 7 struct example
    (constructor+static-stream vs. flatten+update-instances):

      Path A (stack_trans): recursion -> explicit stack, then resize
      Path B (rewrite_iterative): recursion -> bottom-up loop, no stack

    Only one path is taken per run; the search tries A first (its first
    parameterization, stack_size=6, is deliberately too small for the
    n=64 tests and is rejected -- paper §6.2: generated tests force
    the stack size up -- then the 32 alternative is accepted).
    """
    g = EditGraph()
    g.add(Edit("M1", "add_define", {"name": "MAX_N", "value": str(max_n)},
               f"introduce compile-time constant MAX_N={max_n}"))
    for i, var in (("M2", "a"), ("M3", "L"), ("M4", "R")):
        g.add(Edit(i, "remove_free", {"var": var}, f"delete free({var})"))
    g.add(Edit("M5", "array_static", {"var": "a", "max_elems": max_n},
               f"main array a: malloc -> static [{max_n}]"))
    g.add(Edit("M6", "array_static", {"var": "L", "max_elems": max_n},
               f"merge temp L: malloc -> static [{max_n}]"))
    g.add(Edit("M7", "array_static", {"var": "R", "max_elems": max_n},
               f"merge temp R: malloc -> static [{max_n}]"))
    g.add(Edit("M8", "pointer", {"func": "merge", "param": "a",
                                 "max_elems": max_n},
               f"merge param a: int* -> int[{max_n}]"))
    g.add(Edit("M10", "stack_trans",
               {"func": "merge_sort", "stack_size": 6},
               "recursion -> explicit stack (size 6 first try)",
               alternatives=[{"func": "merge_sort", "stack_size": 32}],
               ))
    g.add(Edit("M11", "rewrite_iterative", {"func": "merge_sort"},
               "recursion -> bottom-up iterative merge sort"))
    g.add(Edit("M9", "pointer", {"func": "merge_sort", "param": "a",
                                 "max_elems": max_n},
               f"merge_sort param a: int* -> int[{max_n}]"))
    g.add(Edit("M12", "resize", {"var": "stack", "new_size": 64},
               "explicit stack 32 -> 64 (size exploration)"))
    g.add_group("recursion", ["M10", "M11"])
    # Free-removal precedes malloc-removal (every prefix executable).
    g.depends_on("M5", "M2")
    g.depends_on("M6", "M3")
    g.depends_on("M7", "M4")
    # The pointer rewrite of merge_sort must come after whichever
    # recursion removal replaced its definition (the splice restores
    # the original `int *a` signature).
    g.depends_on("M9", "M10")
    g.depends_on("M9", "M11")
    # Resize only makes sense on the explicit-stack path.
    g.depends_on("M12", "M10")
    return g


def build_pointer_plan(max_n: int = 64) -> EditGraph:
    """Grounded edit graph for tasks/pointer.c (binary-tree traversal,
    the paper's worked example: malloc'd nodes + pointers + recursion).

    The kernel transformation is pool_trans (the paper's Fig. 2
    malloc -> static pool + pointer -> index change, bundled as one
    template-guided edit), which leaves build/traverse recursive over
    indices; stack_trans and rewrite_iterative then remove the two
    remaining recursions as separate edits.
    """
    g = EditGraph()
    g.add(Edit("T1", "add_define", {"name": "MAX_N", "value": str(max_n)},
               f"introduce compile-time constant MAX_N={max_n}"))
    g.add(Edit("T2", "remove_free", {"var": "vals"}, "delete free(vals)"))
    g.add(Edit("T3", "remove_free", {"var": "out"}, "delete free(out)"))
    g.add(Edit("T4", "array_static", {"var": "vals", "max_elems": max_n},
               f"main array vals: malloc -> static [{max_n}]"))
    g.add(Edit("T5", "array_static", {"var": "out", "max_elems": max_n},
               f"main array out: malloc -> static [{max_n}]"))
    g.add(Edit("T6", "pool_trans", {},
               "nodes: malloc -> static pool, Node* -> index (-1 = null)"))
    g.add(Edit("T7", "stack_trans",
               {"func": "traverse", "template": "traverse"},
               "traverse recursion -> explicit index stack"))
    g.add(Edit("T8", "rewrite_iterative",
               {"func": "build", "template": "build"},
               "build recursion -> iterative pool linking"))
    g.add(Edit("T9", "pointer", {"func": "traverse", "param": "out",
                                 "max_elems": max_n},
               f"traverse param out: int* -> int[{max_n}]"))
    g.add(Edit("T10", "pointer", {"func": "build", "param": "vals",
                                  "max_elems": max_n},
               f"build param vals: int* -> int[{max_n}]"))
    # Free-removal precedes malloc-removal (every prefix executable).
    g.depends_on("T4", "T2")
    g.depends_on("T5", "T3")
    # The pool needs MAX_N; the recursion removals need the pool-based
    # definitions (their splices restore the pool-based signatures).
    g.depends_on("T6", "T1")
    g.depends_on("T7", "T6")
    g.depends_on("T8", "T6")
    # Pointer rewrites must follow the splice that defines the function.
    g.depends_on("T9", "T7")
    g.depends_on("T10", "T8")
    return g
