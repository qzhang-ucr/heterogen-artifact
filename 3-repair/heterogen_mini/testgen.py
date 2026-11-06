"""Test-input generation (simplified HeteroGen §test generation).

HeteroGen auto-generates test inputs for checking C -> HLS-C conversion
errors with high branch coverage. Here the "inputs" are (n, rng-seed)
pairs fed to the program's argv: the program fills its matrices from a
deterministic LCG seeded by argv, so each pair is a distinct test case
with a golden output produced by the ORIGINAL program. The search loop
requires every accepted edit to reproduce the golden output on all of
them (differential testing). All cases use n == MAX_N, because the
grounded edits specialize the kernel to that size -- see README.
"""
from __future__ import annotations


def default_allocation_test_args(max_n: int = 8, seeds=(1, 2, 3, 42, 123)) -> list[tuple[str, ...]]:
    args = [(str(max_n), str(s)) for s in seeds]
    args.append((str(max_n),))  # default-seed run, no seed arg
    return args


def default_recursion_test_args(max_n: int = 64) -> list[tuple[str, ...]]:
    """(n, seed) sweep for merge sort. Two sizes on purpose: the small
    n=16 cases pass even with an undersized explicit stack, while the
    n=64 cases need stack depth 7 -- so a stack_size=6 candidate is
    accepted by the small tests and rejected by the large ones
    (paper §6.2: generated tests force the stack size up)."""
    args = [("16", str(s)) for s in (1, 2)]
    args += [(str(max_n), str(s)) for s in (1, 42)]
    args.append((str(max_n),))  # default-seed run, no seed arg
    return args


def default_pointer_test_args(max_n: int = 64) -> list[tuple[str, ...]]:
    """(n, seed) sweep for binary-tree traversal, including the n=1
    edge case (a single-node tree)."""
    args = [("1", "1")]
    args += [("16", str(s)) for s in (1, 2)]
    args += [(str(max_n), str(s)) for s in (1, 42)]
    args.append((str(max_n),))  # default-seed run, no seed arg
    return args
