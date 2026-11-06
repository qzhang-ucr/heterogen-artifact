"""AST engine for C: parse (pycparser), regenerate, compile and run.

Design notes (simplified HeteroGen, see README):
- pycparser 3.x cannot parse comments or real system headers, so the
  engine strips ``#``-lines into a preamble (restored verbatim on output)
  and strips comments before parsing. The body that pycparser sees is
  plain C with no includes -- malloc/printf/etc. are just identifiers,
  which is all the AST edits need. Final code is validated by real gcc.
- ``#pragma`` lines are likewise not representable in pycparser's AST;
  they are applied as a text splice on generated code (see search.py).
  This is a documented limitation, not a silent loss.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pycparser import CParser
from pycparser.c_generator import CGenerator

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")


def split_preamble(src: str) -> tuple[list[str], str]:
    """Return (preamble_lines, body). Preamble = lines starting with '#'."""
    preamble, body = [], []
    for line in src.splitlines():
        (preamble if line.strip().startswith("#") else body).append(line)
    return preamble, "\n".join(body)


def strip_comments(body: str) -> str:
    # No comment-like text inside string literals in our task files;
    # documented scope limitation of this simplified engine.
    body = _COMMENT_RE.sub("", body)
    return _LINE_COMMENT_RE.sub("", body)


@dataclass
class CProgram:
    """A parsed C file: AST + preamble lines + extra #defines added by edits."""

    ast: object
    preamble: list[str] = field(default_factory=list)
    extra_preamble: list[str] = field(default_factory=list)
    # Text splices pycparser cannot represent (e.g. #pragma lines):
    # list of (func, pragma), re-applied idempotently on every generate().
    splices: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def parse(cls, src: str) -> "CProgram":
        preamble, body = split_preamble(src)
        ast = CParser().parse(strip_comments(body))
        return cls(ast=ast, preamble=preamble)

    def generate(self) -> str:
        import re as _re

        head = "\n".join(self.preamble + self.extra_preamble)
        code = head + "\n\n" + CGenerator().visit(self.ast) + "\n"
        for func, pragma in self.splices:
            if pragma in code:
                continue
            lines = code.splitlines()
            in_func, depth = False, 0
            for i, line in enumerate(lines):
                if not in_func:
                    if _re.match(rf"\s*(void|int|float|double)\s+{_re.escape(func)}\s*\(", line):
                        in_func, depth = True, line.count("{") - line.count("}")
                    continue
                depth += line.count("{") - line.count("}")
                if _re.match(r"\s*for\s*\(", line):
                    indent = line[: len(line) - len(line.lstrip())]
                    lines.insert(i, f"{indent}{pragma}")
                    break
            code = "\n".join(lines) + "\n"
        return code

    def add_define(self, name: str, value: str) -> bool:
        line = f"#define {name} {value}"
        if line in self.extra_preamble:
            return False
        self.extra_preamble.append(line)
        return True

    def add_pragma(self, func: str, pragma: str) -> bool:
        if (func, pragma) in self.splices:
            return False
        self.splices.append((func, pragma))
        return True


def compile_c(src_path: Path, out_path: Path, extra_flags: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gcc", "-O2", "-Wall", "-o", str(out_path), str(src_path), *extra_flags],
        capture_output=True, text=True, timeout=60,
    )


def run_binary(binary: Path, args: tuple[str, ...]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(binary), *args], capture_output=True, text=True, timeout=30
    )


def compile_and_run(src_text: str, args: tuple[str, ...]) -> tuple[bool, str, str]:
    """Compile src_text with gcc and run it; return (ok, stdout, stderr)."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src_path, binary = td / "prog.c", td / "prog"
        src_path.write_text(src_text)
        cc = compile_c(src_path, binary)
        if cc.returncode != 0:
            return False, "", cc.stderr
        run = run_binary(binary, args)
        if run.returncode != 0:
            return False, run.stdout, run.stderr
        return True, run.stdout, ""
