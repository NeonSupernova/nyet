"""Unit tests for tracking the Nyet type of a struct-valued `if`/`match`/
`do` expression, plus a related gap found while fixing it: field access
chained directly onto a fresh constructor call.

Regression coverage: `_infer_llvm_type` (which sizes an alloca) already
recurses into the tail expression of `if`/`match`/`do` to get the LLVM
type, but `_infer_nyet_type_name` (and the near-duplicate
`_struct_name_of`, used by field access/assignment) had no matching
cases at all -- so `(let p (if cond (make_a) (make_b)))` with no `&T`
annotation, or `(. (do ... (Point x:1 y:2)) x)` chained directly, lost
the struct type and `(. p field)` silently produced no field load.

While fixing this, found `_struct_name_of` was ALSO missing a case for
a direct struct/variant constructor call as a field-access target
(`(. (Point x:1 y:2) x)`, with no `if`/`do` involved at all) --
`_infer_nyet_type_name` already handled that shape via its `name in
self._structs` check, but `_struct_name_of` had never been extended to
match it. Rather than keep both functions in sync shape-by-shape
(the actual root cause of every one of these gaps -- see the other
Nyet-name-tracking bugs fixed elsewhere this session), `_struct_name_of`
is now a thin wrapper delegating to `_infer_nyet_type_name`, so there
is exactly one place left to extend.

End-to-end coverage: tests/codegen/aggregate_control_flow_binding.no.
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


def test_if_expression_struct_result_bound_without_annotation():
    ir = _ir_for("""
        (struct Point x:i32 y:i32)
        (fn main () -> unit
          (do
            (let cond:bool true)
            (let p (if cond (Point x:1 y:2) (Point x:3 y:4)))
            (out! (. p x))))
    """)
    # Constructing a 2-field Point emits 2 GEPs (one per field); both
    # `if` branches each construct one (4 total), plus 1 more for the
    # field *read* on `p` -- the old bug had zero for the read, so
    # this would be 4, not 5.
    assert ir.count("getelementptr inbounds %Point") == 5


def test_do_expression_struct_result_bound_without_annotation():
    ir = _ir_for("""
        (struct Point x:i32 y:i32)
        (fn main () -> unit
          (do
            (let p (do (out! "making\\n") (Point x:5 y:6)))
            (out! (. p x))))
    """)
    # 2 GEPs for construction + 1 for the field read (0 before the fix).
    assert ir.count("getelementptr inbounds %Point") == 3


def test_field_access_chained_directly_onto_a_fresh_constructor():
    # No if/do involved at all -- the narrower gap found while fixing
    # the above: `_struct_name_of` never recognized a direct
    # constructor call as a field-access target.
    ir = _ir_for("""
        (struct Point x:i32 y:i32)
        (fn main () -> unit (out! (. (Point x:9 y:0) x)))
    """)
    assert ir.count("getelementptr inbounds %Point") == 3


def test_field_access_chained_directly_onto_a_do_wrapping_a_constructor():
    ir = _ir_for("""
        (struct Point x:i32 y:i32)
        (fn main () -> unit (out! (. (do (Point x:9 y:0)) x)))
    """)
    assert ir.count("getelementptr inbounds %Point") == 3
