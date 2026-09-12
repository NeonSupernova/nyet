"""Unit tests for tracking the Nyet type of tuple elements
(Emitter._get_or_register_tuple_type's elem_nyet parameter).

Regression coverage: a tuple containing struct elements, indexed and
then field-accessed with no intermediate annotation (`(let p (t 0))
(. p x)`), lost the struct type. `_get_or_register_tuple_type` only
ever took a bare `list[str]` of LLVM types -- no Nyet names threaded
through at all -- so `_infer_nyet_type_name` had no way to recover
which struct type a given tuple slot held, unlike every other
aggregate-tracking registry fixed elsewhere this session
(`_env_array_elem_nyet`, `_env_map_val_nyet`, `_struct_field_nyet`).

Also covers a distinct bug found while testing the fix with the tuple
behind a function param: a *by-reference* tuple param (`t:&#(T1 T2)`)
wasn't recognized as a tuple at all, regardless of element types --
the param-type check never unwrapped `&`/`&!` the way every other
aggregate param shape does.

End-to-end coverage: tests/codegen/tuple_of_structs.no,
tuple_ref_param.no.
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


def test_tuple_literal_of_structs_indexed_without_annotation():
    ir = _ir_for("""
        (struct Point x:i32 y:i32)
        (fn main () -> unit
          (do
            (let t #((Point x:1 y:2) (Point x:3 y:4)))
            (let p (t 0))
            (out (. p x))))
    """)
    # Constructing the two 2-field Points emits 2 GEPs each (4 total);
    # the field *read* on `p` adds 1 more -- the old bug had 0 for the
    # read, so this would be 4, not 5.
    assert ir.count("getelementptr inbounds %Point") == 5


def test_tuple_param_of_structs_indexed_without_annotation():
    ir = _ir_for("""
        (struct Point x:i32 y:i32)
        (fn first (t:&#(Point Point)) -> i32
          (do
            (let p (t 0))
            (. p x)))
        (fn main () -> unit
          (do
            (let t #((Point x:7 y:8) (Point x:1 y:2)))
            (out (first &t))))
    """)
    first_fn = ir[ir.index("define i32 @first") : ir.index("\n}\n", ir.index("define i32 @first"))]
    # Only the field read happens inside `first` (the tuple itself is
    # constructed in `main`), so exactly 1 GEP into %Point there.
    assert first_fn.count("getelementptr inbounds %Point") == 1


def test_tuple_param_by_reference_is_recognized_at_all():
    # A distinct bug found while writing the test above: the param-type
    # check for tuples was a bare `isinstance(p.type, N.TupleType)`
    # with no `&`/`&!` unwrapping -- unlike Array[T]/struct/sum/dyn
    # Trait params, which all unwrap `RefType` internally. A
    # by-reference tuple param (the natural way to avoid copying one
    # into a function) fell into the generic scalar-`ptr` fallback, so
    # `(t i)` was parsed as a call to an undefined function `t`.
    ir = _ir_for("""
        (fn first (t:&#(i32 i32)) -> i32 (t 0))
        (fn main () -> unit
          (do
            (let t #(7 8))
            (out (first &t))))
    """)
    first_fn = ir[ir.index("define i32 @first") : ir.index("\n}\n", ir.index("define i32 @first"))]
    assert "@t(" not in first_fn
    assert "getelementptr inbounds %tuple" in first_fn


def test_tuple_of_scalars_is_unaffected():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let t #(1 2))
            (out (t 0))))
    """)
    assert "getelementptr inbounds %tuple" in ir
    assert "%Point" not in ir
