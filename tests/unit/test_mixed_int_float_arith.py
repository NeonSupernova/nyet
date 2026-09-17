"""Unit tests for int/float promotion in mixed arithmetic, comparisons,
and casts (Emitter._emit_arith / _emit_cmp / _emit_cast).

Regression coverage: the int-side promotion in `_emit_arith`'s and
`_emit_cmp`'s float branches hardcoded `sitofp i32 <value> to <fty>`
regardless of the integer operand's REAL LLVM width -- any non-i32
integer (i64, i8, i16, or an i32-width-but-differently-named u32) mixed
with a float in `+`/`-`/`*`/`/`/`%` or a comparison produced an
ill-typed `sitofp i32 %tN to double` where `%tN` actually held an i64 (or
other width) value, which clang's LLVM verifier rejects outright
('%tN' defined with type 'iN' but expected 'i32').

Separately (found alongside, same investigation): every int<->float
conversion -- this same implicit promotion, plus the explicit `(as ...)`
cast in `_emit_cast` -- always used the signed `sitofp`/`fptosi`
instructions, never `uitofp`/`fptoui`, so converting an unsigned value
outside i32's signed range (e.g. a `u32` >= 2^31) the other way gave a
sign-flipped or saturated result instead of the true magnitude.

End-to-end coverage: tests/codegen/mixed_int_float_arith.no.
"""

from pynyet.codegen.emit import Emitter
from pynyet.lexer.scanner import lex
from pynyet.parser.parser import parse
from pynyet.sema.expand import expand_macros
from pynyet.sema.resolve import resolve_names
from pynyet.sema.typeck import check_types
from pynyet.source import SourceFile


def _ir_for(src: str) -> str:
    sf = SourceFile("t.no", src)
    program = parse(lex(sf))
    program, _ = expand_macros(program)
    resolve_names(program)
    check_types(program)
    return Emitter().emit(program)


def _fn_body(ir: str, fn_name: str) -> str:
    for prefix in (f"define void @{fn_name}(", f"define i32 @{fn_name}("):
        if prefix in ir:
            start = ir.index(prefix)
            end = ir.index("\n}", start)
            return ir[start:end]
    raise AssertionError(f"no definition found for @{fn_name} in IR")


def test_i64_plus_float_promotes_at_i64_not_hardcoded_i32():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let x:i64 5)
            (out! (+ x 3.5))))
    """)
    body = _fn_body(ir, "main")
    assert "sitofp i64" in body
    assert "sitofp i32" not in body


def test_i64_compared_to_float_promotes_at_i64_not_hardcoded_i32():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let x:i64 5)
            (out! (> x 3.5))))
    """)
    body = _fn_body(ir, "main")
    assert "sitofp i64" in body
    assert "sitofp i32" not in body


def test_unsigned_int_to_float_uses_uitofp():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let a:u32 4000000000)
            (out! (as a f64))))
    """)
    body = _fn_body(ir, "main")
    assert "uitofp i32" in body
    assert "sitofp" not in body


def test_signed_int_to_float_still_uses_sitofp():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let a:i32 5)
            (out! (as a f64))))
    """)
    body = _fn_body(ir, "main")
    assert "sitofp i32" in body
    assert "uitofp" not in body


def test_float_to_unsigned_int_cast_uses_fptoui():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let g:f64 4000000000.0)
            (let b:u32 (as g u32))
            (out! b)))
    """)
    body = _fn_body(ir, "main")
    assert "fptoui double" in body
    assert "fptosi" not in body


def test_float_to_signed_int_cast_still_uses_fptosi():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let g:f64 3.99)
            (let b:i32 (as g i32))
            (out! b)))
    """)
    body = _fn_body(ir, "main")
    assert "fptosi double" in body
    assert "fptoui" not in body


def test_unsigned_int_promoted_to_float_in_arith_uses_uitofp():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let a:u32 4000000000)
            (out! (+ a 1.5))))
    """)
    body = _fn_body(ir, "main")
    assert "uitofp i32" in body
