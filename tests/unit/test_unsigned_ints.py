"""Unit tests for unsigned-integer (u8/u16/u32/u64/usize) signedness
tracking in codegen (Emitter._env_unsigned_names / _node_is_unsigned).

Regression coverage: LLVM's plain iN integer types carry no signedness
of their own -- i32 is used for both `i32` and `u32` alike. Before this
fix, nothing in emit.py ever consulted the original Nyet type name to
tell the two apart, so every operation that behaves differently for
unsigned values picked the signed behavior unconditionally:
  - widening casts always `sext` (should `zext` a `u8`/`u16`/etc source)
  - comparisons always used signed `icmp` predicates (`slt`/`sgt`/...)
  - division/remainder always used `sdiv`/`srem`
  - `out`/`fmt` always formatted with `%d`/`%lld`

Separately, `_llvm_type` had no case at all for `u64`/`usize` (only
`u8`/`u16` were mapped): they fell through to the function's generic
"i32" default, silently storing a 64-bit-wide Nyet type in a 32-bit
LLVM int. `u32` happened to be unaffected since 32 bits is already
i32's width.

End-to-end coverage: tests/codegen/unsigned_ints.no.
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


def test_u64_and_usize_are_stored_as_i64_not_i32():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let a:u64 10000000000000000000)
            (let b:usize 5)
            (out! (as a i32))
            (out! (as b i32))))
    """)
    body = _fn_body(ir, "main")
    assert "alloca i64" in body
    assert body.count("alloca i32") == 0


def test_widening_cast_from_unsigned_source_zero_extends():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let a:u8 255)
            (out! (as a i32))))
    """)
    body = _fn_body(ir, "main")
    assert "zext i8" in body
    assert "sext i8" not in body


def test_widening_cast_from_signed_source_still_sign_extends():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let a:i8 100)
            (out! (as a i32))))
    """)
    body = _fn_body(ir, "main")
    assert "sext i8" in body
    assert "zext i8" not in body


def test_unsigned_comparison_uses_unsigned_predicate():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let a:u32 4000000000)
            (let b:u32 3000000000)
            (out! (> a b))))
    """)
    body = _fn_body(ir, "main")
    assert "icmp ugt i32" in body
    assert "icmp sgt" not in body


def test_signed_comparison_still_uses_signed_predicate():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let a:i32 4)
            (let b:i32 3)
            (out! (> a b))))
    """)
    body = _fn_body(ir, "main")
    assert "icmp sgt i32" in body
    assert "icmp ugt" not in body


def test_unsigned_division_and_remainder_use_unsigned_instructions():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let x:u32 10)
            (let y:u32 3)
            (out! (/ x y))
            (out! (% x y))))
    """)
    body = _fn_body(ir, "main")
    assert "udiv i32" in body
    assert "urem i32" in body
    assert "sdiv" not in body
    assert "srem" not in body


def test_signed_division_and_remainder_still_use_signed_instructions():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let x:i32 10)
            (let y:i32 3)
            (out! (/ x y))
            (out! (% x y))))
    """)
    body = _fn_body(ir, "main")
    assert "sdiv i32" in body
    assert "srem i32" in body
    assert "udiv" not in body
    assert "urem" not in body


def test_out_prints_unsigned_value_with_percent_u():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let a:u32 4000000000)
            (out! a)))
    """)
    assert '@.str.0 = ' in ir
    assert "%u" in ir


def test_out_prints_unsigned_64_bit_value_with_percent_llu():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let a:u64 10000000000000000000)
            (out! a)))
    """)
    assert "%llu" in ir


def test_fmt_template_uses_percent_u_for_unsigned_placeholder():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let a:u32 4000000000)
            (out! (fmt "{}" a))))
    """)
    assert "%u" in ir
    assert "%d\\00" not in ir


def test_unsigned_param_is_tracked_too():
    ir = _ir_for("""
        (fn show (n:u16) -> unit
          (out! (as n i32)))
        (fn main () -> unit
          (show 40000))
    """)
    body = _fn_body(ir, "show")
    assert "zext i16" in body
