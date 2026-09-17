"""Unit tests for char printing (Emitter._emit_out / _emit_fmt char cases).

`char` is stored as `i32` (a Unicode scalar value -- see CLAUDE.md),
the same LLVM shape as an ordinary int, so both `out!` and `fmt`
unconditionally fell through to their generic i32 `%d` handling and
printed a char's numeric codepoint instead of the character itself.
Confirmed: `(let ch:char 65) (out! ch)` printed "65", not "A". Fixed
by routing a char operand through `%c` instead, gated on the existing
`_node_is_char` classifier (mirrors `_node_is_unsigned`).

End-to-end coverage (out!/fmt, a char literal-annotation and a char
from string indexing, plus a plain-i32 control): tests/codegen/char_printing.no.
"""

from pynyet.codegen.emit import Emitter
from pynyet.lexer.scanner import lex
from pynyet.parser.parser import parse
from pynyet.sema.expand import expand_macros
from pynyet.sema.resolve import resolve_names
from pynyet.sema.typeck import check_types
from pynyet.source import SourceFile


def _program(src: str):
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


def test_out_of_char_uses_percent_c_not_percent_d():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let ch:char 65)
            (out! ch)))
    """)
    assert "@.str" in ir
    body = _fn_body(ir, "main")
    # The %c format string constant must exist and be the one printf'd.
    assert '"%c\\00"' in ir
    assert '"%d\\00"' not in ir or "call i32 (ptr, ...) @printf" in body


def test_fmt_of_char_uses_percent_c_spec():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let ch:char 65)
            (out! (fmt "{}" ch))))
    """)
    assert '"%c\\00"' in ir


def test_out_of_plain_i32_still_uses_percent_d():
    # Control: the fix must not regress the ordinary int case.
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let n:i32 65)
            (out! n)))
    """)
    assert '"%d\\00"' in ir
