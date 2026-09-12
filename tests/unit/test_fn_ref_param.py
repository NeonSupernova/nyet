"""Unit test for a by-reference function-typed parameter.

Regression coverage: the same bug just fixed for by-reference tuple
params (see test_tuple_of_structs.py), for function-typed params
instead. `f:&(fn i32 -> i32)` wasn't recognized as callable at all --
the param-type check was a bare `isinstance(p.type, N.FnType)` with no
`&`/`&!` unwrapping, unlike Array[T]/struct/sum/dyn-Trait params, which
all unwrap `RefType` internally. `(f x)` inside the function body was
parsed as a call to an undefined function `f`.

End-to-end coverage: tests/codegen/fn_ref_param.no.
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


def test_by_reference_fn_param_is_called_indirectly_not_as_undefined_fn():
    ir = _ir_for("""
        (fn apply (f:&(fn i32 -> i32) x:i32) -> i32 (f x))
        (fn double (n:i32) -> i32 (* n 2))
        (fn main () -> unit (out (apply double 5)))
    """)
    apply_fn = ir[ir.index("define i32 @apply") : ir.index("\n}\n", ir.index("define i32 @apply"))]
    assert "@f(" not in apply_fn
    # An indirect call through the loaded fn-pointer slot.
    assert "call i32 %" in apply_fn


def test_plain_value_fn_param_is_unaffected():
    ir = _ir_for("""
        (fn apply (f:(fn i32 -> i32) x:i32) -> i32 (f x))
        (fn double (n:i32) -> i32 (* n 2))
        (fn main () -> unit (out (apply double 5)))
    """)
    apply_fn = ir[ir.index("define i32 @apply") : ir.index("\n}\n", ir.index("define i32 @apply"))]
    assert "@f(" not in apply_fn
