"""Golden test runner for codegen.

For every `<name>.no` under tests/codegen/, produce a canonical codegen
dump (LLVM IR or binary output) and compare it against `<name>.golden`.
Missing `.golden` files are created on the first run; subsequent runs
diff against the stored output.

Usage:
    python3 tests/codegen/run.py              # check all
    python3 tests/codegen/run.py --update     # overwrite golden files
    python3 tests/codegen/run.py name1 name2  # check specific cases
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

# Make the project root importable when run directly.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def dump_codegen_output(path: Path) -> str:
    import contextlib
    import io
    import subprocess
    import tempfile

    from pynyet.diagnostic import NyetError
    from pynyet.lexer.scanner import lex
    from pynyet.parser.parser import parse
    from pynyet.sema.expand import expand_macros
    from pynyet.sema.resolve import resolve_names
    from pynyet.sema.typeck import check_types
    from pynyet.codegen.emit import emit_ir
    from pynyet.source import SourceFile

    build_stderr = io.StringIO()
    with contextlib.redirect_stderr(build_stderr):
        rel_path = path.resolve().relative_to(ROOT)
        sf = SourceFile(str(rel_path), path.read_text())
        try:
            tokens = lex(sf)
            program = parse(tokens)
        except NyetError as e:
            return "".join(d.format() + "\n" for d in e.diagnostics)

        program, expand_errors = expand_macros(program)
        errors = expand_errors + resolve_names(program) + check_types(program)
        for d in errors:
            print(d.format(), file=build_stderr)

        ir_text = emit_ir(program)

    parts: list[str] = []
    if build_stderr.getvalue():
        parts.append("=== build stderr ===")
        parts.append(build_stderr.getvalue().rstrip("\n"))

    with tempfile.TemporaryDirectory() as td:
        ll_path = Path(td) / "out.ll"
        bin_path = Path(td) / "out"
        ll_path.write_text(ir_text)
        runtime_map_c = ROOT / "runtime" / "map.c"
        runtime_async_c = ROOT / "runtime" / "async.c"
        clang = subprocess.run(
            ["clang", "-o", str(bin_path), str(ll_path), str(runtime_map_c), str(runtime_async_c)],
            capture_output=True,
            text=True,
        )
        if clang.returncode != 0:
            parts.append("=== clang error ===")
            parts.append(clang.stderr.rstrip("\n"))
            return "\n".join(parts) + "\n"

        run = subprocess.run([str(bin_path)], capture_output=True, text=True)
        parts.append("=== stdout ===")
        parts.append(run.stdout.rstrip("\n"))
        if run.returncode != 0:
            parts.append(f"=== exit code {run.returncode} ===")

    return "\n".join(parts) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--update", action="store_true", help="overwrite golden files")
    parser.add_argument("cases", nargs="*", help="specific case names (without extension)")
    args = parser.parse_args()

    test_dir = Path(__file__).parent
    sources = sorted(test_dir.glob("*.no"))
    if not sources:
        print("no cases yet")
        return 0
    if args.cases:
        wanted = set(args.cases)
        sources = [s for s in sources if s.stem in wanted]
        if not sources:
            print(f"no matching cases: {args.cases}", file=sys.stderr)
            return 2

    failures = 0
    for src in sources:
        golden = src.with_suffix(".golden")
        actual = dump_codegen_output(src)
        if args.update or not golden.exists():
            golden.write_text(actual)
            print(f"[{'UPDATED' if args.update else 'WROTE '}] {src.name}")
            continue
        expected = golden.read_text()
        if actual == expected:
            print(f"[OK]     {src.name}")
        else:
            failures += 1
            print(f"[FAIL]   {src.name}")
            diff = difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"{golden.name} (expected)",
                tofile=f"{src.name} (actual)",
            )
            sys.stdout.writelines(diff)

    if failures:
        print(f"\n{failures} case(s) failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
