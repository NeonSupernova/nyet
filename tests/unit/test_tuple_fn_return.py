"""Unit tests for tracking a function's tuple return type
(Emitter._fn_ret_tuple_types).

Regression coverage: the same class of bug already fixed for
Array[T]/Map[K V] elsewhere in this session, but for tuples returned
from a function. `(let p (make_pair))` with no explicit `#(T1 T2)`
annotation lost track of which synthesized tuple struct type the
binding held. Tuples are structurally typed (an anonymous struct name
keyed by element LLVM types, not a user-given name like a struct/sum
type), so `_fn_ret_nyet_names` -- which only ever holds a Nyet type
NAME -- couldn't represent one; `_emit_let` never registered `p` in
`_env_tuple_types`, so `(p 0)` was parsed as an ordinary call to an
undefined function literally named `p` instead of a tuple index.

End-to-end coverage: tests/codegen/tuple_fn_return.no.
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


def test_tuple_returning_fn_bound_without_annotation_gets_indexed_correctly():
    ir = _ir_for("""
        (fn make_pair () -> #(i32 string) #(42 "hi"))
        (fn main () -> unit
          (do
            (let p (make_pair))
            (out (p 0))))
    """)
    assert "call ptr @make_pair()" in ir
    # A real tuple-field GEP+load, not a bogus `call i32 @p(...)`.
    assert "@p(" not in ir
    assert "getelementptr inbounds %tuple" in ir


def test_tuple_returning_fn_call_used_inline_is_unaffected():
    # The already-supported shapes (explicit annotation, or a call used
    # directly as an argument rather than bound to a `let`) must keep
    # working.
    ir = _ir_for("""
        (fn make_pair () -> #(i32 string) #(42 "hi"))
        (fn main () -> unit
          (do
            (let p:#(i32 string) (make_pair))
            (out (p 0))))
    """)
    assert "getelementptr inbounds %tuple" in ir
    assert "@p(" not in ir
