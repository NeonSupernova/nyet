"""Unit tests for `Map[K V]` value-type tracking (Emitter._env_map_val_nyet
/ _map_val_nyet_name), plus a deeper bug found while testing it: Map
params weren't registered as maps at all.

Two bugs here:

1. The exact same bug already fixed for `Array[T]` (see
   test_array_struct_element.py), but for maps. Reading a struct value
   out of a `Map[K V]` into a `let` with no explicit `&T` annotation
   (`(let a (m key))`) lost track of which struct type the binding
   held -- only the *LLVM* value type ("ptr") was ever recorded
   (`_env_map_val_ty`), nothing recorded the *Nyet* name, so
   `_infer_nyet_type_name` had no case for "a Call indexing a known map
   binding" and `_emit_let` took the scalar-`ptr` path instead of the
   aggregate path -- so `a` was never registered in `_env_struct_name`,
   and `(. a field)` on it silently produced no field load at all.
2. Discovered while writing a test for #1 with the map behind a
   function param rather than a local `let`: `Map[K V]` params had NO
   registration case at all in `_emit_fn`'s param-handling loop (unlike
   Array[T]/struct/tuple/dyn-Trait params, which each get their own
   case) -- they fell into the generic scalar-`ptr` fallback, so `(m
   key)` inside the function body wasn't recognized as a map lookup at
   all and parsed as an ordinary call to an undefined function
   literally named `m` (an undefined-symbol link error at best).

End-to-end coverage: tests/codegen/map_struct_value.no.
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


def test_struct_read_from_map_lookup_gets_a_field_load():
    ir = _ir_for("""
        (struct Point x:i32 y:i32)
        (fn main () -> unit
          (do
            (var m:Map[string Point] {"origin" (Point x:0 y:0)})
            (let a (m "origin"))
            (out (. a x))))
    """)
    assert "getelementptr inbounds %Point" in ir
    assert "load i32," in ir


def test_map_param_is_registered_as_a_map_at_all():
    # A deeper bug found while testing the one above: `Map[K V]`
    # function params had NO registration case at all in `_emit_fn`'s
    # param-handling loop (unlike Array[T]/struct/tuple/dyn-Trait
    # params, which each have their own case) -- they fell into the
    # generic scalar-`ptr` fallback, so `(m key)` inside the function
    # body wasn't recognized as a map lookup at all. It parsed as an
    # ordinary call to an undefined function literally named `m`
    # (`call i32 @m(...)`, an undefined-symbol link error), not a
    # `nyet_map_get` call.
    ir = _ir_for("""
        (fn first (m:&Map[string i32]) -> i32 (m "k"))
        (fn main () -> unit
          (do
            (var m:Map[string i32] {"k" 9})
            (out (first &m))))
    """)
    first_fn = ir[ir.index("define i32 @first") : ir.index("\n}\n", ir.index("define i32 @first"))]
    assert "call i64 @nyet_map_get(" in first_fn
    assert "@m(" not in first_fn


def test_map_val_type_annotation_also_tracks_nyet_name():
    # The explicit-`Map[string Point]`-annotation path, as opposed to
    # inferring from a literal's first entry -- covers a map PARAM
    # specifically, since that's a distinct code path from a `let`.
    ir = _ir_for("""
        (struct Point x:i32 y:i32)
        (fn first (m:&Map[string Point]) -> i32
          (do
            (let a (m "k"))
            (. a x)))
        (fn main () -> unit
          (do
            (var m:Map[string Point] {"k" (Point x:9 y:9)})
            (out (first &m))))
    """)
    first_fn = ir[ir.index("define i32 @first") : ir.index("\n}\n", ir.index("define i32 @first"))]
    assert "call i64 @nyet_map_get(" in first_fn
    assert "getelementptr inbounds %Point" in first_fn


def test_non_struct_map_value_is_unaffected():
    # Plain scalar-valued maps must keep working exactly as before.
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (var m:Map[string i32] {"a" 1})
            (let a (m "a"))
            (out a)))
    """)
    assert "getelementptr inbounds %" not in ir
