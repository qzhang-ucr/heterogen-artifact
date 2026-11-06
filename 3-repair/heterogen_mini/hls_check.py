"""Static HLS-incompatibility checker.

This is the simplified stand-in for HeteroGen's real oracle (HLS
compilation + simulation, which needs Vivado HLS and is far too slow to
run per candidate). It detects, on the AST, the classic C constructs
that HLS tools reject -- the same classes HeteroGen's fix patterns
target -- and counts remaining violations. The search loop minimises
this count while preserving behaviour (differential test outputs).
"""
from __future__ import annotations

from pycparser import c_ast


class Violation(dict):
    pass


class _Checker(c_ast.NodeVisitor):
    def __init__(self):
        self.violations: list[dict] = []
        self._func: str | None = None
        self._params: set[str] = set()
        self._pointers: set[str] = set()

    def _at(self, node, kind, detail):
        line = node.coord.line if getattr(node, "coord", None) else None
        self.violations.append(
            {"kind": kind, "func": self._func, "line": line, "detail": detail}
        )

    def visit_FuncDef(self, node):
        prev_func, prev_params, prev_pointers = self._func, self._params, self._pointers
        self._func = node.decl.name
        self._params, self._pointers = set(), set()
        args = node.decl.type.args
        if args:
            for p in args.params or []:
                if isinstance(p, c_ast.Decl):
                    self._params.add(p.name)
                    # `main` is the host test harness, not the synthesized
                    # kernel: its `char **argv` is not an HLS violation.
                    if (isinstance(p.type, c_ast.PtrDecl)
                            and node.decl.name != "main"):
                        self._pointers.add(p.name)
                        self._at(p, "pointer_param",
                                 f"parameter '{p.name}' is a raw pointer")
        self.generic_visit(node)
        self._func, self._params, self._pointers = prev_func, prev_params, prev_pointers

    def visit_Decl(self, node):
        if isinstance(node.type, c_ast.PtrDecl):
            self._pointers.add(node.name)
        if isinstance(node.type, c_ast.ArrayDecl):
            dim = node.type.dim
            if isinstance(dim, c_ast.ID) and dim.name in self._params:
                self._at(node, "variable_length_array",
                         f"array '{node.name}' sized by runtime parameter '{dim.name}'")
        self.generic_visit(node)

    def visit_FuncCall(self, node):
        name = node.name.name if isinstance(node.name, c_ast.ID) else None
        if name in ("malloc", "calloc", "realloc"):
            self._at(node, "dynamic_alloc", f"call to {name}()")
        elif name == "free":
            self._at(node, "free", "call to free()")
        elif name is not None and name == self._func:
            self._at(node, "recursion", f"recursive call to {name}()")
        self.generic_visit(node)

    def visit_For(self, node):
        cond = node.cond
        if isinstance(cond, c_ast.BinaryOp) and cond.op in ("<", "<="):
            rhs = cond.right
            if isinstance(rhs, c_ast.ID) and rhs.name in self._params:
                self._at(node, "variable_loop_bound",
                         f"loop bound is runtime parameter '{rhs.name}'")
        self.generic_visit(node)

    def visit_BinaryOp(self, node):
        if node.op in ("+", "-"):
            for side in (node.left, node.right):
                if isinstance(side, c_ast.ID) and side.name in self._pointers:
                    self._at(node, "pointer_arith",
                             f"pointer arithmetic on '{side.name}'")
        self.generic_visit(node)


def check_hls_violations(ast) -> list[dict]:
    """Return the list of HLS incompatibilities found in the AST."""
    checker = _Checker()
    checker.visit(ast)
    return checker.violations
