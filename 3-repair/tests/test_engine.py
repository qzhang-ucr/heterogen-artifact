"""Tests for the simplified HeteroGen prototype (run: .venv/bin/python -m pytest tests/ -q,
or .venv/bin/python tests/test_engine.py)."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from heterogen_mini import (  # noqa: E402
    CProgram, build_allocation_plan, build_recursion_plan,
    build_pointer_plan, check_hls_violations, Searcher,
    default_recursion_test_args, default_allocation_test_args,
    default_pointer_test_args,
)

ROOT = Path(__file__).resolve().parent.parent
ALLOC_TASK = (ROOT / "tasks" / "allocation.c").read_text()
REC_TASK = (ROOT / "tasks" / "recursion.c").read_text()
PTR_TASK = (ROOT / "tasks" / "pointer.c").read_text()


def test_allocation_checker_finds_violations():
    vs = check_hls_violations(CProgram.parse(ALLOC_TASK).ast)
    kinds = {v["kind"] for v in vs}
    assert {"dynamic_alloc", "free", "pointer_param", "variable_loop_bound"} <= kinds
    assert len(vs) >= 10  # 3 malloc + 3 free + 3 ptr params + 3 loop bounds


def test_allocation_graph_is_acyclic_and_covers_all_edits():
    g = build_allocation_plan(8)
    order = g.topo_order()
    assert len(order) == 12
    pos = {e.id: i for i, e in enumerate(order)}
    for b, a in g.edges:
        assert pos[b] < pos[a], f"edge {b}->{a} violated in topo order"
    # remove_free before array_static; pragma (insert) last
    assert pos["E12"] == len(order) - 1


def test_allocation_full_search_reaches_zero_violations_and_preserves_behaviour():
    result = Searcher(ALLOC_TASK, build_allocation_plan(8), default_allocation_test_args(8)).run()
    assert result.final_violations == [], result.final_violations
    assert len(result.accepted) == 12, [s.edit_id for s in result.steps]
    assert "#define MAX_N 8" in result.final_code
    assert "float A[64]" in result.final_code
    assert "#pragma HLS PIPELINE" in result.final_code
    assert "malloc" not in result.final_code and "free(" not in result.final_code
    # Final program compiles with gcc and reproduces golden outputs.
    for args in default_allocation_test_args(8):
        ok, out, err = _compile_and_run(result.final_code, args)
        assert ok, err
        assert out.strip() == result.golden[" ".join(args)]


def test_recursion_checker_finds_recursion():
    vs = check_hls_violations(CProgram.parse(REC_TASK).ast)
    kinds = [v["kind"] for v in vs]
    assert kinds.count("recursion") == 2
    assert kinds.count("dynamic_alloc") == 3
    assert len(vs) == 10


def test_recursion_stack_branch():
    result = Searcher(REC_TASK, build_recursion_plan(64),
                      default_recursion_test_args(64)).run()
    assert result.final_violations == [], result.final_violations
    by_id = {s.edit_id: s for s in result.steps}
    # stack_size=6 was tried and rejected by the n=64 tests, 32 accepted.
    assert any("stack_size': 6" in s.reason and not s.accepted
               for s in result.steps), [s.reason for s in result.steps]
    assert by_id["M10"].accepted
    assert "stack_size': 32" in by_id["M10"].reason
    # The alternative path was skipped, resize applied on the stack.
    assert not by_id["M11"].accepted and "already accepted" in by_id["M11"].reason
    assert by_id["M12"].accepted
    assert "Frame stack[64]" in result.final_code
    assert "merge_sort(int a[64]" in result.final_code
    for args in default_recursion_test_args(64):
        ok, out, err = _compile_and_run(result.final_code, args)
        assert ok, err
        assert out.strip() == result.golden[" ".join(args)]


def test_recursion_iterative_branch():
    result = Searcher(REC_TASK, build_recursion_plan(64),
                      default_recursion_test_args(64),
                      skip_ids={"M10"}).run()
    assert result.final_violations == [], result.final_violations
    by_id = {s.edit_id: s for s in result.steps}
    assert by_id["M11"].accepted
    assert "Frame stack" not in result.final_code
    assert "width" in result.final_code  # bottom-up loop survived
    assert not by_id["M12"].accepted  # no stack array to resize
    for args in default_recursion_test_args(64):
        ok, out, err = _compile_and_run(result.final_code, args)
        assert ok, err
        assert out.strip() == result.golden[" ".join(args)]


def test_pointer_full_search():
    vs = check_hls_violations(CProgram.parse(PTR_TASK).ast)
    assert len(vs) == 16
    result = Searcher(PTR_TASK, build_pointer_plan(64),
                      default_pointer_test_args(64)).run()
    assert result.final_violations == [], result.final_violations
    by_id = {s.edit_id: s for s in result.steps}
    assert by_id["T6"].accepted  # pool_trans
    assert by_id["T7"].accepted  # stack_trans traverse
    assert by_id["T8"].accepted  # rewrite_iterative build
    assert len(result.accepted) == 10
    assert "Node pool[MAX_N]" in result.final_code
    assert "malloc" not in result.final_code
    assert "free(" not in result.final_code
    assert "traverse(pool[root].left" not in result.final_code  # no recursion left
    for args in default_pointer_test_args(64):
        ok, out, err = _compile_and_run(result.final_code, args)
        assert ok, err
        assert out.strip() == result.golden[" ".join(args)]


def _compile_and_run(src, args):
    import tempfile
    from heterogen_mini.ast_engine import compile_and_run
    return compile_and_run(src, args)


if __name__ == "__main__":
    test_allocation_checker_finds_violations()
    test_allocation_graph_is_acyclic_and_covers_all_edits()
    test_allocation_full_search_reaches_zero_violations_and_preserves_behaviour()
    test_recursion_checker_finds_recursion()
    test_recursion_stack_branch()
    test_recursion_iterative_branch()
    test_pointer_full_search()
    print("all tests passed")
