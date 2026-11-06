"""Iterative search: walk the edit graph, apply one AST edit at a time,
validate after every step, roll back on failure.

This mirrors HeteroGen's search-based repair loop in miniature, and
Argus's validation pipeline (syntax -> semantics -> execution):
  1. re-parse + regenerate the program after the edit;
  2. gcc compile (syntax/semantics gate);
  3. differential execution vs. the original program on generated
     test inputs (behaviour-preservation gate);
  4. HLS-violation count must not increase (progress gate).
A step that fails any gate is rolled back and recorded as rejected;
the search continues with the next edit in topological order instead
of aborting -- a greedy walk with per-step backtracking, which is all
this simplified version needs (no beam, no HLS-in-the-loop).
"""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .ast_engine import CProgram, compile_and_run
from .edit_graph import EditGraph
from .edits import Edit, apply_ast_edit
from .hls_check import check_hls_violations


@dataclass
class Step:
    edit_id: str
    kind: str
    description: str
    accepted: bool
    violations_before: int
    violations_after: int | None
    reason: str = ""
    changed: bool = False


@dataclass
class SearchResult:
    final_code: str
    steps: list[Step] = field(default_factory=list)
    golden: dict = field(default_factory=dict)
    final_violations: list[dict] = field(default_factory=list)
    snapshots: dict[str, str] = field(default_factory=dict)  # edit_id -> code after step

    @property
    def accepted(self):
        return [s for s in self.steps if s.accepted]

    def to_json(self) -> str:
        return json.dumps(
            {"steps": [asdict(s) for s in self.steps],
             "golden": self.golden,
             "final_violations": self.final_violations},
            indent=2, ensure_ascii=False)


class Searcher:
    def __init__(self, original_src: str, graph: EditGraph,
                 test_args: list[tuple[str, ...]], skip_ids=None):
        self.original_src = original_src
        self.graph = graph
        self.test_args = test_args
        self.skip_ids: set[str] = set(skip_ids or [])
        self.program = CProgram.parse(original_src)
        self.golden: dict[str, str] = {}

    def _compute_golden(self):
        """Run the ORIGINAL program on generated inputs; its outputs are
        the behaviour oracle for every later step."""
        for args in self.test_args:
            ok, out, err = compile_and_run(self.original_src, args)
            if not ok:
                raise RuntimeError(f"original program failed on {args}: {err}")
            self.golden[" ".join(args)] = out.strip()

    def _validate(self, code: str):
        """(compiles, outputs_match, violations, error)."""
        violations = None
        try:
            prog = CProgram.parse(code)
            violations = check_hls_violations(prog.ast)
        except Exception as exc:  # noqa: BLE001 - re-parse failure = reject
            return False, False, None, f"re-parse failed: {exc}"
        for args in self.test_args:
            ok, out, err = compile_and_run(code, args)
            if not ok:
                return False, False, violations, f"compile/run failed: {err.strip()[:300]}"
            if out.strip() != self.golden[" ".join(args)]:
                return True, False, violations, (
                    f"output changed on args {args}: got {out.strip()!r}, "
                    f"expected {self.golden[' '.join(args)]!r}")
        return True, True, violations, ""

    def run(self) -> SearchResult:
        self._compute_golden()
        result = SearchResult(final_code="", golden=self.golden)
        cur_violations = check_hls_violations(self.program.ast)
        accepted_groups: set[str] = set()
        for edit in self.graph.topo_order():
            before_n = len(cur_violations)
            if edit.id in self.skip_ids:
                result.steps.append(Step(edit.id, edit.kind, edit.description,
                                         False, before_n, before_n,
                                         reason="skipped: excluded by caller",
                                         changed=False))
                continue
            if edit.group is not None and edit.group in accepted_groups:
                result.steps.append(Step(
                    edit.id, edit.kind, edit.description,
                    False, before_n, before_n,
                    reason=f"skipped: another edit in group "
                           f"'{edit.group}' was already accepted",
                    changed=False))
                continue
            variants = [edit.params] + list(edit.alternatives)
            for vi, params in enumerate(variants):
                snapshot = copy.deepcopy(self.program)
                changed, reason = self._try_edit(edit, params)
                if not changed:
                    result.steps.append(Step(
                        edit.id, edit.kind, edit.description,
                        False, before_n, before_n,
                        reason=reason or "no-op", changed=False))
                    break
                code = self.program.generate()
                compiles, matches, violations, err = self._validate(code)
                if not compiles or not matches or len(violations) > before_n:
                    self.program = snapshot
                    why = err or "violations increased"
                    result.steps.append(Step(
                        edit.id, edit.kind, edit.description,
                        False, before_n, None,
                        reason=f"rejected: params {params} -- {why}",
                        changed=True))
                    continue  # try the next parameterization
                cur_violations = violations
                result.snapshots[edit.id] = code
                result.steps.append(Step(
                    edit.id, edit.kind, edit.description,
                    True, before_n, len(violations),
                    reason=(f"accepted with alternative params {params}"
                            if vi else reason),
                    changed=True))
                if edit.group is not None:
                    accepted_groups.add(edit.group)
                break
        result.final_code = self.program.generate()
        result.final_violations = check_hls_violations(self.program.ast)
        return result

    def _try_edit(self, edit: Edit, params: dict) -> tuple[bool, str]:
        if edit.kind == "add_define":
            ok = self.program.add_define(edit.params["name"], edit.params["value"])
            return ok, "" if ok else "define already present"
        if edit.kind == "insert":
            ok = self.program.add_pragma(edit.params["func"],
                                         edit.params["pragma"])
            return ok, ("" if ok else "pragma already present")
        return apply_ast_edit(self.program.ast, edit, params), ""
