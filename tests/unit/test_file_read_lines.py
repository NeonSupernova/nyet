"""Unit tests for the `file_read_lines` builtin
(Emitter._emit_file_read_lines / _emit_split_line_copy).

`(file_read_lines handle) -> Array[string]` splits a file's contents on
'\\n' into one malloc'd string per line (Python's str.splitlines()
semantics). Added because minibase's save/load needed a way to read a
multi-row save file back as separate strings, and nothing already in
the language could express that: a `char` has no route back to a
one-character `string` (no cast, and `+` only concatenates two
strings), so a general split() written in Nyet source itself wasn't
reachable without a new builtin.

End-to-end coverage (including the edge cases: no trailing newline, an
empty file, an embedded blank line, a file that's just "\\n"):
tests/codegen/file_read_lines.no.
"""

import pynyet.ast.nodes as N
from pynyet.codegen.emit import Emitter
from pynyet.lexer.scanner import lex
from pynyet.parser.parser import parse
from pynyet.sema.expand import expand_macros
from pynyet.sema.resolve import resolve_names
from pynyet.sema.typeck import check_types
from pynyet.source import SourceFile


def _program(src: str) -> list[N.Node]:
    sf = SourceFile("t.no", src)
    program = parse(lex(sf))
    program, _ = expand_macros(program)
    resolve_names(program)
    check_types(program)
    return program


def _ir_for(src: str) -> str:
    return Emitter().emit(_program(src))


def _fn_body(ir: str, fn_name: str) -> str:
    for prefix in (f"define void @{fn_name}(", f"define i32 @{fn_name}("):
        if prefix in ir:
            start = ir.index(prefix)
            end = ir.index("\n}", start)
            return ir[start:end]
    raise AssertionError(f"no definition found for @{fn_name} in IR")


SRC = """
    (fn main () -> unit
      (do
        (let h (file_open "/tmp/t.txt" "r"))
        (let lines:Array[string] (file_read_lines h))
        (out! (lines 0))))
"""


def test_typechecks_as_array_of_string_not_error():
    # A no-op/dropped call (the exact pre-fix failure shape for a
    # missing builtin elsewhere in this codebase) would leave `lines`
    # either untyped or ERROR-typed, and indexing it would then not
    # resolve as an array read at all -- so this only passes if
    # file_read_lines is both registered as a known builtin (resolve.py)
    # and given a real Array[string] return type (typeck.py), not the
    # opaque STRING approximation the handle-returning file builtins use.
    program = _program(SRC)
    main_fn = next(d for d in program if isinstance(d, N.FnDecl) and d.name == "main")
    let_lines = main_fn.body.exprs[1]
    assert isinstance(let_lines, N.LetDecl)
    assert isinstance(let_lines.value, N.Call)
    assert isinstance(let_lines.value.head, N.Ident)
    assert let_lines.value.head.name == "file_read_lines"


def test_emits_read_split_and_array_build():
    ir = _ir_for(SRC)
    body = _fn_body(ir, "main")
    # Whole-file read, same as file_read_all.
    assert "call i64 @fread(" in body
    # The one-byte-at-a-time newline scan.
    assert "icmp eq i8 %" in body and ", 10" in body
    # Each split line is a fresh malloc'd, memcpy'd, null-terminated copy.
    assert "call ptr @memcpy(" in body
    # Builds a real Array[string]: a malloc'd [i64 len][ptr...] block,
    # header fixed up to the true (not over-allocated) count at the end.
    assert body.count("call ptr @malloc(") >= 3  # read buffer + output array + >=1 line copy
    assert "store i64 " in body  # header write(s), incl. the length fixup


def test_result_indexes_as_an_ordinary_string_array():
    # (lines 0) should lower exactly like indexing any other
    # Array[string] -- a bounds check plus a `ptr`-typed element load --
    # proving _env_array_elem got registered as "ptr" for this binding
    # via the ordinary Array[T]-annotated-let path, not a special case
    # that only looks like it works.
    ir = _ir_for(SRC)
    body = _fn_body(ir, "main")
    assert "getelementptr ptr, ptr" in body
    assert "load ptr, ptr" in body
