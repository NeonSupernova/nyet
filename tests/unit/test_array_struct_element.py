"""Unit tests for tracking the Nyet element type of `Array[T]` bindings
(Emitter._env_array_elem_nyet / _array_elem_nyet_name / _infer_nyet_type_name).

Regression coverage: reading a struct element out of an `Array[T]` into
a `let` with no explicit `&T` annotation (`(let a (pts 0))`, as opposed
to the documented `(let a:&Point (pts 0))` workaround) used to lose
track of which struct type the binding held. Only the *LLVM* element
type ("ptr") was ever recorded (`_env_array_elem`); nothing recorded
the *Nyet* name ("Point"), so `_infer_nyet_type_name` had no case for
"a Call indexing a known array binding" and `_emit_let` took the
scalar-`ptr` path instead of the aggregate path -- so `a` was never
registered in `_env_struct_name`, and `(. a field)` on it silently
produced no field load at all (not an error, just wrong output).

End-to-end coverage: tests/codegen/array_struct_field.no.
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


def test_struct_read_from_array_index_gets_a_field_load():
    ir = _ir_for("""
        (struct Point x:i32 y:i32)
        (fn main () -> unit
          (do
            (let pts:Array[Point] [(Point x:1 y:2)])
            (let a (pts 0))
            (out (. a x))))
    """)
    # A real field load through the struct's own GEP index, not a
    # dropped/no-op reference.
    assert "getelementptr inbounds %Point" in ir
    assert "load i32," in ir


def test_array_elem_type_annotation_also_tracks_nyet_name():
    # The explicit-`Array[Point]`-annotation path (as opposed to
    # inferring the element type from an inline ArrayLit) must track
    # the Nyet name too -- covers function params and lets whose RHS
    # isn't a literal (e.g. a HOF call).
    ir = _ir_for("""
        (struct Point x:i32 y:i32)
        (fn first (pts:&Array[Point]) -> i32
          (do
            (let a (pts 0))
            (. a x)))
        (fn main () -> unit
          (do
            (let pts:Array[Point] [(Point x:7 y:8)])
            (out (first &pts))))
    """)
    assert "getelementptr inbounds %Point" in ir


def test_non_struct_array_element_is_unaffected():
    # Plain scalar arrays must keep working exactly as before -- this
    # fix only adds tracking for named (struct/sum) element types.
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let xs:Array[i32] [1 2 3])
            (let a (xs 0))
            (out a)))
    """)
    assert "getelementptr inbounds %" not in ir
