"""Command-line driver for the Nyet compiler (v0.1)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from pynyet.diagnostic import NyetError
from pynyet.lexer.scanner import lex
from pynyet.source import SourceFile


def _dump_tokens(sf: SourceFile, keep_trivia: bool) -> str:
    lines: list[str] = []
    for tok in lex(sf, keep_trivia=keep_trivia):
        line, col = tok.span.start_line_col()
        loc = f"{line}:{col}"
        parts = [f"{tok.kind.name:<16} {loc:<8}"]
        if tok.value is not None:
            parts.append(f"value={tok.value!r}")
        if tok.suffix is not None:
            parts.append(f"suffix={tok.suffix}")
        lines.append(" ".join(parts))
    return "\n".join(lines) + "\n"


def _load_source(path_str: str) -> SourceFile:
    path = Path(path_str)
    if not path.is_file():
        print(f"error: file not found: {path_str}", file=sys.stderr)
        sys.exit(1)
    return SourceFile(str(path), path.read_text())


def _cmd_lex(args: argparse.Namespace) -> int:
    sf = _load_source(args.file)
    try:
        sys.stdout.write(_dump_tokens(sf, keep_trivia=args.keep_trivia))
    except NyetError as e:
        print(e.diagnostic.format(), file=sys.stderr)
        return 1
    return 0


def _stub(name: str) -> int:
    print(f"{name}: not implemented in v0.1", file=sys.stderr)
    return 2


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pynyet", description="Nyet compiler driver")
    sub = parser.add_subparsers(dest="command", required=True)

    p_lex = sub.add_parser("lex", help="tokenize a source file")
    p_lex.add_argument("file")
    p_lex.add_argument("--keep-trivia", action="store_true", help="include comment tokens")
    p_lex.set_defaults(func=_cmd_lex)

    for name in ("parse", "check", "build", "run"):
        sp = sub.add_parser(name, help=f"{name} a source file (stub)")
        sp.add_argument("file")
        sp.set_defaults(func=lambda _a, _n=name: _stub(_n))

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
