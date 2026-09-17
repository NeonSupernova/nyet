"""Unit tests for indexing an `Array[T]`-typed struct field directly,
without first binding it to a local (Emitter._emit_array_index_on_field
/ _emit_array_assign_on_field).

Regression coverage: `_emit_call`'s array-indexing dispatch, and
`_emit_assign`'s indexed-assignment dispatch, only ever recognized a
plain `Ident` head bound in `_env_array_elem` -- a `FieldAccess` head
(`((. v data) i)`, reading or `(= ((. v data) i) x)`, writing) fell
through every case and silently evaluated to `None` / no-op'd. This
blocks the single most natural growable-Vector-style pattern: a struct
wrapping a `data:Array[T]` field whose own methods need to index
`data` directly (as opposed to first copying it out to a local, which
works today but isn't how anyone would naturally write it).

End-to-end coverage: tests/codegen/array_field_index.no.
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


def test_reading_array_struct_field_directly_emits_a_load():
    ir = _ir_for("""
        (struct Box data:Array[i32])
        (fn main () -> unit
          (do
            (let b (Box data:(array_new 3)))
            (out ((. b data) 0))))
    """)
    body = _fn_body(ir, "main")
    assert "getelementptr i32, ptr" in body
    assert body.count("load i32,") >= 1


def test_writing_array_struct_field_directly_emits_a_store():
    ir = _ir_for("""
        (struct Box data:Array[i32])
        (fn main () -> unit
          (do
            (var b (Box data:(array_new 3)))
            (= ((. b data) 0) 42)))
    """)
    body = _fn_body(ir, "main")
    assert "store i32 42, ptr" in body


def test_array_struct_field_index_via_reference_param():
    # The realistic shape: a `&!Vector`-style method indexing its own
    # `data` field directly, not via a plain local struct binding.
    ir = _ir_for("""
        (struct Vector data:Array[i32])
        (fn get (v:&Vector i:i32) -> i32 ((. v data) i))
        (fn main () -> unit
          (do
            (let v (Vector data:(array_new 3)))
            (out (get &v 0))))
    """)
    body = _fn_body(ir, "get")
    assert "getelementptr i32, ptr" in body
    assert "ret i32 %" in body
