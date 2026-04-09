"""Golden test runner for the parser.

For every `<name>.no` under tests/parser/, produce a canonical AST dump
and compare it against `<name>.golden`. Missing `.golden` files are
created on the first run; subsequent runs diff against the stored output.

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


def dump_parser_output(path: Path) -> str:
    raise NotImplementedError(
        "parser harness awaits v0.2 — no parser implementation yet"
    )


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
        actual = dump_parser_output(src)
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
