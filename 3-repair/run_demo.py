#!/usr/bin/env python3
"""Demo: simplified HeteroGen.

Usage:
    .venv/bin/python run_demo.py [--task allocation|recursion|pointer]
                                 [--branch stack|iterative] [--max-n N]
                                 [--out DIR]

Walks the parameterized edit graph in topological order, applies one
AST edit at a time, validates after each step (gcc + differential
tests + HLS-violation count), rolls back failures, tries alternative
parameterizations, and takes at most one path through each
alternative-group. Writes:
  <out>/<task>_hls.c    final HLS-compatible program
  <out>/trace.json      per-step trace (accepted/rejected, violations)
  <out>/steps/*.c       snapshot after each accepted step
  <out>/edits.dot       dependence graph (Graphviz)
  <out>/ast_graph.json  final AST as a graph (Argus-style) + edit path
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from heterogen_mini import (  # noqa: E402
    CProgram, build_allocation_plan, build_recursion_plan,
    build_pointer_plan, check_hls_violations, Searcher,
    default_recursion_test_args, default_allocation_test_args,
    default_pointer_test_args,
)
from heterogen_mini.ast_graph import export_graph_json  # noqa: E402

TASKS = {
    "allocation": {
        "file": "allocation.c",
        "plan": build_allocation_plan,
        "test_args": default_allocation_test_args,
        "default_max_n": 8,
        "default_out": ROOT / "output-allocation",
        "label": "allocation",
    },
    "recursion": {
        "file": "recursion.c",
        "plan": build_recursion_plan,
        "test_args": default_recursion_test_args,
        "default_max_n": 64,
        "default_out": None,  # output-recursion-<branch>
        "label": "recursion",
    },
    "pointer": {
        "file": "pointer.c",
        "plan": build_pointer_plan,
        "test_args": default_pointer_test_args,
        "default_max_n": 64,
        "default_out": ROOT / "output-pointer",
        "label": "pointer",
    },
}


def main() -> int:
    ap = argparse.ArgumentParser(description="Simplified HeteroGen demo")
    ap.add_argument("--task", choices=list(TASKS), default="allocation")
    ap.add_argument("--branch", choices=["stack", "iterative"], default="stack",
                    help="recursion only: which recursion-removal path to take")
    ap.add_argument("--max-n", type=int, default=None)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    cfg = TASKS[args.task]
    max_n = args.max_n if args.max_n is not None else cfg["default_max_n"]
    out = args.out
    if out is None:
        out = cfg["default_out"] or (ROOT / f"output-{args.task}-{args.branch}")

    task = ROOT / "tasks" / cfg["file"]
    src = task.read_text()
    graph = cfg["plan"](max_n=max_n)
    skip_ids = {"M10"} if (args.task == "recursion" and args.branch == "iterative") else set()
    searcher = Searcher(src, graph, cfg["test_args"](max_n), skip_ids=skip_ids)

    orig_violations = check_hls_violations(CProgram.parse(src).ast)
    branch_note = f", branch={args.branch}" if args.task == "recursion" else ""
    print(f"task: {args.task} (max_n={max_n}{branch_note})")
    print(f"original: {len(orig_violations)} HLS violations")
    for v in orig_violations:
        print(f"  - [{v['kind']}] {v['func']}: {v['detail']}")

    result = searcher.run()

    print("\nedit walk (topological order):")
    for s in result.steps:
        mark = "ACCEPT" if s.accepted else "reject/no-op"
        after = "" if s.violations_after is None else f" -> {s.violations_after}"
        print(f"  {s.edit_id} {s.kind:<18} {mark:<13} "
              f"violations {s.violations_before}{after}  {s.description}"
              + (f"  ({s.reason})" if s.reason and not s.accepted else ""))

    print(f"\nfinal: {len(result.final_violations)} HLS violations remaining")
    for v in result.final_violations:
        print(f"  - [{v['kind']}] {v['func']}: {v['detail']}")

    (out / "steps").mkdir(parents=True, exist_ok=True)
    (out / f"{args.task}_hls.c").write_text(result.final_code)
    (out / "trace.json").write_text(result.to_json())
    (out / "edits.dot").write_text(graph.to_dot())
    graph_json = export_graph_json(
        searcher.program.ast,
        [s.edit_id for s in result.accepted],
        {"task": cfg["label"], "max_n": max_n, "branch": args.branch,
         "final_violations": len(result.final_violations)})
    (out / "ast_graph.json").write_text(json.dumps(graph_json, indent=2))
    for edit_id, code in result.snapshots.items():
        (out / "steps" / f"{edit_id}.c").write_text(code)
    print(f"\nwrote {out / (args.task + '_hls.c')}")
    print(f"wrote {out / 'trace.json'} and {out / 'edits.dot'}")
    return 0 if not result.final_violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
