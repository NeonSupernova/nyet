"""Command-line driver for the Nyet compiler (v0.3)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pynyet.ast.nodes as N
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


def _print_errors(e: NyetError) -> None:
    for d in e.diagnostics:
        print(d.format(), file=sys.stderr)


def _load_program_with_deps(entry_path: str) -> list[N.Node]:
    """Load `entry_path` and recursively any `(use ...)` deps it imports.

    Returns a single combined program with imported modules' decls listed
    before the entry's, and `ModuleDecl` / `UseDecl` nodes stripped before
    returning so downstream passes don't need to know about them. Each
    surviving `Decl` has its `module` annotation set.

    Resolution: `(use foo/bar)` in a file at `<dir>/<file>.no` looks for
    `<dir>/foo/bar.no`. Missing targets are silently ignored — that's how
    references to not-yet-implemented stdlib modules (`std/io`, etc.) stay
    benign while we ship the multi-file driver.
    """
    from pynyet.parser.parser import parse

    entry = Path(entry_path).resolve()
    seen: set[Path] = set()
    order: list[Path] = []
    loaded: dict[Path, list[N.Node]] = {}

    def load(path: Path) -> None:
        path = path.resolve()
        if path in seen:
            return
        seen.add(path)
        if not path.is_file():
            return
        sf = SourceFile(str(path), path.read_text())
        tokens = lex(sf)
        program = parse(tokens)
        mod_name = path.stem
        for n in program:
            if isinstance(n, N.ModuleDecl):
                mod_name = n.name
                break
        for n in program:
            if isinstance(n, N.Decl):
                n.module = mod_name
        loaded[path] = program
        for n in program:
            if isinstance(n, N.UseDecl):
                target = path.parent / ("/".join(n.path) + ".no")
                load(target)
        order.append(path)

    load(entry)

    combined: list[N.Node] = []
    for p in order:
        for n in loaded[p]:
            if isinstance(n, (N.ModuleDecl, N.UseDecl)):
                continue
            combined.append(n)
    return combined


def _cmd_lex(args: argparse.Namespace) -> int:
    sf = _load_source(args.file)
    try:
        sys.stdout.write(_dump_tokens(sf, keep_trivia=args.keep_trivia))
    except NyetError as e:
        _print_errors(e)
        return 1
    return 0


def _cmd_parse(args: argparse.Namespace) -> int:
    from pynyet.ast.pretty import pretty
    from pynyet.parser.parser import parse

    sf = _load_source(args.file)
    try:
        tokens = lex(sf)
        program = parse(tokens)
        for node in program:
            print(pretty(node))
    except NyetError as e:
        _print_errors(e)
        return 1
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    from pynyet.sema.borrow import check_borrows
    from pynyet.sema.expand import expand_macros
    from pynyet.sema.resolve import resolve_names
    from pynyet.sema.typeck import check_types

    try:
        program = _load_program_with_deps(args.file)
    except NyetError as e:
        _print_errors(e)
        return 1

    program, expand_errors = expand_macros(program)
    errors = expand_errors + resolve_names(program) + check_types(program) + check_borrows(program)
    if errors:
        for d in errors:
            print(d.format(), file=sys.stderr)
        return 1
    print("check: ok")
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    import subprocess

    from pynyet.codegen.emit import emit_ir
    from pynyet.sema.borrow import check_borrows
    from pynyet.sema.expand import expand_macros
    from pynyet.sema.resolve import resolve_names
    from pynyet.sema.typeck import check_types

    try:
        program = _load_program_with_deps(args.file)
    except NyetError as e:
        _print_errors(e)
        return 1

    program, expand_errors = expand_macros(program)
    errors = expand_errors + resolve_names(program) + check_types(program) + check_borrows(program)
    if errors:
        for d in errors:
            print(d.format(), file=sys.stderr)
        return 1

    # Emit LLVM IR
    ir_text = emit_ir(program)
    ll_path = Path(args.output + ".ll") if args.output else Path("output.ll")
    ll_path.write_text(ir_text)

    if args.emit_llvm:
        print(ir_text)
        return 0

    # Compile with clang
    out_path = Path(args.output) if args.output else Path("output")
    try:
        result = subprocess.run(
            ["clang", "-o", str(out_path), str(ll_path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"clang error:\n{result.stderr}", file=sys.stderr)
            return 1
    except FileNotFoundError:
        print("error: clang not found — install LLVM/clang to compile", file=sys.stderr)
        return 1

    print(f"built: {out_path}", file=sys.stderr)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    import subprocess

    # Build first
    args.emit_llvm = False
    if not hasattr(args, "output") or args.output is None:
        args.output = None
    rc = _cmd_build(args)
    if rc != 0:
        return rc

    out_path = Path(args.output) if args.output else Path("output")
    result = subprocess.run([str(out_path.resolve())], capture_output=False)
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pynyet", description="Nyet compiler driver")
    sub = parser.add_subparsers(dest="command", required=True)

    p_lex = sub.add_parser("lex", help="tokenize a source file")
    p_lex.add_argument("file")
    p_lex.add_argument("--keep-trivia", action="store_true", help="include comment tokens")
    p_lex.set_defaults(func=_cmd_lex)

    p_parse = sub.add_parser("parse", help="parse a source file and print the AST")
    p_parse.add_argument("file")
    p_parse.set_defaults(func=_cmd_parse)

    p_check = sub.add_parser("check", help="type-check a source file")
    p_check.add_argument("file")
    p_check.set_defaults(func=_cmd_check)

    p_build = sub.add_parser("build", help="compile a source file to a native binary")
    p_build.add_argument("file")
    p_build.add_argument("-o", "--output", default=None, help="output file name")
    p_build.add_argument("--emit-llvm", action="store_true", help="print LLVM IR and stop")
    p_build.set_defaults(func=_cmd_build)

    p_run = sub.add_parser("run", help="compile and run a source file")
    p_run.add_argument("file")
    p_run.add_argument("-o", "--output", default=None, help="output file name")
    p_run.set_defaults(func=_cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
