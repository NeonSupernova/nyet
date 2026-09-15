"""Unit tests for the `len` builtin's string case (Emitter._emit_array_len).

`(len s)` on a `string` unconditionally read the i64 "length" it
assumed sat at offset 0 of the value's heap block -- the layout
Array[T] actually uses, but strings don't have (they're plain
null-terminated C strings, not a length-prefixed block). Confirmed via
direct testing: `(len "Alexandria")` returned 2019912769, not 10.
minibase_core.no's `require_nonempty` macro (`(!= (len s) 0)`) has
been silently broken this whole time as a result. Fixed by routing a
string operand through `strlen` instead, gated on the same
`_is_string_operand` classifier `_emit_cmp` already uses.

End-to-end coverage (literal, plain let, borrowed param, struct field,
and an Array[i32] control unaffected by the fix):
tests/codegen/string_len.no.
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


def test_len_of_plain_string_let_calls_strlen():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let s:string "Alexandria")
            (out (len s))))
    """)
    body = _fn_body(ir, "main")
    assert "call i64 @strlen(" in body
    # The old (wrong) header-read shape must NOT appear for this binding.
    assert "load i64, ptr" not in body


def test_len_of_borrowed_string_param_calls_strlen():
    ir = _ir_for("""
        (fn strlen_ref (s:&string) -> i32 (len s))
        (fn main () -> unit
          (do
            (let s:string "hi")
            (out (strlen_ref &s))))
    """)
    body = _fn_body(ir, "strlen_ref")
    assert "call i64 @strlen(" in body


def test_len_of_array_still_reads_header_not_strlen():
    # Control: the fix must not regress the real Array[T] case.
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let arr:Array[i32] [1 2 3])
            (out (len &arr))))
    """)
    body = _fn_body(ir, "main")
    assert "load i64, ptr" in body
    assert "call i64 @strlen(" not in body
