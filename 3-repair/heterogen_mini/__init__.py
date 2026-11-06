"""Simplified HeteroGen: pattern-oriented AST edits over a dependence graph."""
from .ast_engine import CProgram
from .edit_graph import (
    EditGraph, build_allocation_plan, build_recursion_plan, build_pointer_plan,
)
from .edits import Edit
from .hls_check import check_hls_violations
from .search import Searcher
from .testgen import (
    default_recursion_test_args, default_allocation_test_args,
    default_pointer_test_args,
)

__all__ = [
    "CProgram", "Edit", "EditGraph", "build_allocation_plan",
    "build_recursion_plan", "build_pointer_plan",
    "check_hls_violations", "Searcher",
    "default_allocation_test_args", "default_recursion_test_args",
    "default_pointer_test_args",
]
