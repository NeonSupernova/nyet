"""Golden test runner for the lexer.

For every `<name>.no` under tests/lexer/, produce a canonical token dump
and compare it against `<name>.tokens`. Missing `.tokens` files are
created on the first run; subsequent runs diff against the stored output.

Usage:
    python3 tests/lexer/run.py              # check all
    python3 tests/lexer/run.py --update     # overwrite golden files
    python3 tests/lexer/run.py name1 name2  # check specific cases
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


def dump_tokens(path: Path) -> str:
    text = path.read_text()
    sf = SourceFile(str(path), text)
    lines: list[str] = []
    for tok in lex(sf):
        line, col = tok.span.start_line_col()
        loc = f"{line}:{col}"
        parts = [f"{tok.kind.name:<16} {loc:<8}"]
        if tok.value is not None:
            parts.append(f"value={tok.value!r}")
        if tok.suffix is not None:
            parts.append(f"suffix={tok.suffix}")
        lines.append(" ".join(parts))
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--update", action="store_true", help="overwrite golden files")
    parser.add_argument("cases", nargs="*", help="specific case names (without extension)")
    args = parser.parse_args()

    test_dir = Path(__file__).parent
    sources = sorted(test_dir.glob("*.no"))
    if args.cases:
        wanted = set(args.cases)
        sources = [s for s in sources if s.stem in wanted]
        if not sources:
            print(f"no matching cases: {args.cases}", file=sys.stderr)
            return 2

    failures = 0
    for src in sources:
        golden = src.with_suffix(".tokens")
        actual = dump_tokens(src)
        if args.update or not golden.exists():
            golden.write_text(actual)
            status = "WROTE" if not golden.exists() else "UPDATED"
            # golden.exists() check was before write — re-check for clarity:
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
