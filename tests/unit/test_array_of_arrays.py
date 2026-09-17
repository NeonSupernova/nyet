"""Unit tests for `Array[Array[T]]` (2D grid) bindings and indexing
(Emitter._env_array_elem_of_array / _array_elem_ty_of_expr /
_emit_array_index_nested / _emit_array_assign_nested).

Regression coverage: two independent gaps.

1. `(let row (grid i))` with no annotation registered `row` as an
   opaque scalar `ptr` binding instead of an array one -- `_emit_let`'s
   array-binding detection only ever inferred an element type from an
   explicit `Array[T]` annotation or an `ArrayLit` RHS, neither of
   which matches "RHS is a Call indexing another array-of-arrays
   binding." `(row j)` was then parsed as a call to an undefined
   function literally named `row`. Fixed with a new
   `_env_array_elem_of_array` registry (name -> inner element LLVM
   type), populated from an explicit `Array[Array[U]]` annotation and
   consulted when binding an unannotated `let`/`var` from an indexing
   Call into such a binding.

2. Indexing a further level with NO intermediate `let` at all
   (`((grid i) j)`, read or write) silently produced nothing --
   `_emit_call`/`_emit_assign`'s array-indexing dispatch only ever
   recognized an `Ident` head (or, after a prior fix, a `FieldAccess`
   head for struct fields); a `Call` head fell through every case.
   Fixed by generalizing the struct-field fix's `_array_field_elem_ty`/
   `_emit_array_index_on_field` into `_array_elem_ty_of_expr`/
   `_emit_array_index_nested`, which also recognizes a Call indexing an
   `_env_array_elem_of_array` binding.

End-to-end coverage: tests/codegen/array_of_arrays.no.
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


def test_unannotated_let_from_array_of_arrays_index_is_indexable():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let grid:Array[Array[i32]] [[1 2] [3 4]])
            (let row (grid 0))
            (out (row 1))))
    """)
    body = _fn_body(ir, "main")
    assert "@row" not in ir
    # A real element load, not a bogus call to an undefined `row`.
    assert "getelementptr i32, ptr" in body


def test_chained_index_read_with_no_intermediate_let():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let grid:Array[Array[i32]] [[1 2] [3 4]])
            (out ((grid 1) 0))))
    """)
    body = _fn_body(ir, "main")
    assert body.count("getelementptr i32, ptr") >= 1
    assert body.count("load i32,") >= 1


def test_chained_index_write_with_no_intermediate_let():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let grid:Array[Array[i32]] [[1 2] [3 4]])
            (= ((grid 1) 0) 99)))
    """)
    body = _fn_body(ir, "main")
    assert "store i32 99, ptr" in body


def test_plain_array_binding_is_unaffected():
    # A one-level Array[T] (not Array[Array[T]]) must not be mistaken
    # for an array-of-arrays -- `(x i)` on its scalar elements must
    # still work exactly as before.
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let a:Array[i32] [1 2 3])
            (out (a 0))))
    """)
    body = _fn_body(ir, "main")
    assert "getelementptr i32, ptr" in body
