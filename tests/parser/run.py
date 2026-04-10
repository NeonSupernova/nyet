"""Golden test runner for the parser.

For every `<name>.no` under tests/parser/, produce a canonical AST dump
(pretty-printed S-expression) and compare it against `<name>.ast`.
Missing `.ast` files are created on the first run; subsequent runs diff
against the stored output.

Also smoke-tests main.no (parse-only, no golden compare).

Usage:
    python3 tests/parser/run.py              # check all
    python3 tests/parser/run.py --update     # overwrite golden files
    python3 tests/parser/run.py name1 name2  # check specific cases
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

# Make the project root importable when run directly.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pynyet.source import SourceFile  # noqa: E402
from pynyet.lexer.scanner import lex  # noqa: E402
from pynyet.parser.parser import parse  # noqa: E402
from pynyet.ast.pretty import pretty  # noqa: E402


def dump_parser_output(path: Path) -> str:
    text = path.read_text()
    sf = SourceFile(str(path), text)
    tokens = lex(sf)
    program = parse(tokens)
    lines: list[str] = []
    for node in program:
        lines.append(pretty(node))
    return "\n".join(lines) + "\n"


def smoke_test_main(root: Path) -> int:
    """Try to parse main.no — report success/failure, no golden comparison."""
    main_no = root / "main.no"
    if not main_no.exists():
        return 0
    try:
        text = main_no.read_text()
        sf = SourceFile(str(main_no), text)
        tokens = lex(sf)
        program = parse(tokens)
        print(f"[SMOKE] main.no: {len(program)} top-level nodes parsed")
        return 0
    except Exception as e:
        print(f"[SMOKE] main.no: FAILED — {e}")
        return 1


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
        golden = src.with_suffix(".ast")
        try:
            actual = dump_parser_output(src)
        except Exception as e:
            failures += 1
            print(f"[ERROR]  {src.name}: {e}")
            import traceback
            traceback.print_exc()
            continue
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

    # Smoke-test main.no (no golden compare)
    failures += smoke_test_main(ROOT)

    if failures:
        print(f"\n{failures} case(s) failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
