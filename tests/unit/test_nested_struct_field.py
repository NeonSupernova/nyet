"""Unit tests for tracking the Nyet type of a struct field that itself
holds a struct/sum-type value (Emitter._struct_field_nyet).

Regression coverage: the same class of bug fixed elsewhere in this
session for Array[T]/Map[K V]/tuple-returning-fn bindings, one more
level down -- a struct field. `(let i (. o inner))` (no `&T`
annotation) and `(. (. o inner) val)` (chained directly, no binding at
all) both lost track of which struct type the nested field held.
`_structs[struct_name]` only ever recorded each field's *LLVM* type
("ptr" for any struct/sum/array/tuple/Map field alike); nothing
recorded the *Nyet* name, so `_struct_name_of`/`_infer_nyet_type_name`
had no case for a `FieldAccess` node at all, and `(. i val)` on the
nested binding silently produced no field load.

End-to-end coverage: tests/codegen/nested_struct_field.no.
"""

from pynyet.codegen.emit import Emitter
from pynyet.lexer.scanner import lex
from pynyet.parser.parser import parse
from pynyet.sema.expand import expand_macros
from pynyet.sema.resolve import resolve_names
from pynyet.sema.typeck import check_types
from pynyet.source import SourceFile


def _emitter_for(src: str) -> Emitter:
    sf = SourceFile("t.no", src)
    program = parse(lex(sf))
    program, _ = expand_macros(program)
    resolve_names(program)
    check_types(program)
    e = Emitter()
    e.emit(program)
    return e


def _ir_for(src: str) -> str:
    sf = SourceFile("t.no", src)
    program = parse(lex(sf))
    program, _ = expand_macros(program)
    resolve_names(program)
    check_types(program)
    return Emitter().emit(program)


def test_struct_field_nyet_registry_tracks_named_field_types():
    e = _emitter_for("""
        (struct Inner val:i32)
        (struct Outer inner:Inner name:string count:i32)
        (fn main () -> unit pass)
    """)
    assert e._struct_field_nyet["Outer"]["inner"] == "Inner"
    assert e._struct_field_nyet["Outer"]["name"] == "string"
    assert e._struct_field_nyet["Outer"]["count"] == "i32"


def test_nested_struct_field_bound_without_annotation_gets_a_field_load():
    # Constructing `Inner` itself always emits one `getelementptr
    # inbounds %Inner` (storing its `val` field) regardless of this fix
    # -- an earlier version of this assertion only checked for that
    # substring's presence and passed even against the unfixed code.
    # The real signal is a SECOND one, for the field *read* on `i`.
    ir = _ir_for("""
        (struct Inner val:i32)
        (struct Outer inner:Inner)
        (fn main () -> unit
          (do
            (let o (Outer inner:(Inner val:42)))
            (let i (. o inner))
            (out! (. i val))))
    """)
    assert ir.count("getelementptr inbounds %Inner") == 2
    assert "load i32, ptr" in ir


def test_nested_struct_field_chained_directly_gets_a_field_load():
    ir = _ir_for("""
        (struct Inner val:i32)
        (struct Outer inner:Inner)
        (fn main () -> unit
          (do
            (let o (Outer inner:(Inner val:42)))
            (out! (. (. o inner) val))))
    """)
    assert ir.count("getelementptr inbounds %Inner") == 2
    assert "load i32, ptr" in ir
