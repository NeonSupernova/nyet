"""Unit test for unsigned-typed struct field detection
(Emitter._node_is_unsigned's N.FieldAccess case).

Regression coverage: `_node_is_unsigned` (added for the unsigned-int
signedness fix) originally only recognized a plain `Ident` binding, so
a struct field declared with an unsigned type -- `(struct Counter
val:u32)`, then `(. c val)` -- still fell back to signed behavior
everywhere: printing, comparison, division. Fixed by also consulting
`_struct_field_nyet` (the same per-field Nyet-type-name registry
`_infer_nyet_type_name`'s own FieldAccess case already reads) from
`_node_is_unsigned`.

End-to-end coverage: tests/codegen/unsigned_struct_field.no.
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


def test_unsigned_struct_field_prints_with_percent_u():
    ir = _ir_for("""
        (struct Counter val:u32)
        (fn main () -> unit
          (do
            (let c (Counter val:4000000000))
            (out! (. c val))))
    """)
    assert "%u" in ir


def test_unsigned_struct_field_comparison_uses_unsigned_predicate():
    ir = _ir_for("""
        (struct Counter val:u32)
        (fn main () -> unit
          (do
            (let a (Counter val:4000000000))
            (let b (Counter val:3000000000))
            (out! (> (. a val) (. b val)))))
    """)
    body = _fn_body(ir, "main")
    assert "icmp ugt i32" in body
    assert "icmp sgt" not in body


def test_signed_struct_field_is_unaffected():
    ir = _ir_for("""
        (struct Counter val:i32)
        (fn main () -> unit
          (do
            (let a (Counter val:4))
            (let b (Counter val:3))
            (out! (> (. a val) (. b val)))))
    """)
    body = _fn_body(ir, "main")
    assert "icmp sgt i32" in body
    assert "icmp ugt" not in body
