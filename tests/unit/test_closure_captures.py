"""Unit tests for closure captures (Emitter._lift_closures,
_emit_closure_value, _bind_closure_captures).

A lambda that refers to a local of an enclosing function captures it.
`(fn ...)` and `(fn! ...)` capture the enclosing binding by reference, so
the closure sees -- and a `fn!` closure makes -- later changes to it;
`(move fn ...)` copies the value into the closure record when the closure
is created. Every function value (a lambda, or a named function used as a
value) is a pointer to a closure record whose first field is the code
pointer, and calls pass the record as a hidden first argument.

Before captures were implemented, referencing an outer variable raised
NotImplementedError at codegen time (and before that, silently read
garbage). End-to-end coverage: tests/codegen/closure_captures.no.
"""

from pynyet.codegen.emit import Emitter
from pynyet.lexer.scanner import lex
from pynyet.parser.parser import parse
from pynyet.sema.expand import expand_macros
from pynyet.sema.resolve import resolve_names
from pynyet.sema.typeck import check_types
from pynyet.source import SourceFile


def _emit(src: str) -> str:
    sf = SourceFile("t.no", src)
    program = parse(lex(sf))
    program, _ = expand_macros(program)
    resolve_names(program)
    check_types(program)
    return Emitter().emit(program)


def test_borrowing_closure_captures_the_binding_by_address():
    ir = _emit("""
        (fn main () -> unit
          (do
            (var counter:i32 5)
            (let read_it (fn () -> i32 counter))
            (out (read_it))))
    """)
    assert "define i32 @__closure_0(ptr %__env)" in ir
    # The capture field is a pointer to `counter`'s slot, not a copy.
    assert "getelementptr inbounds { ptr, ptr }" in ir


def test_move_closure_copies_the_captured_value():
    ir = _emit("""
        (fn main () -> unit
          (do
            (var counter:i32 5)
            (let take (move fn () -> i32 counter))
            (out (take))))
    """)
    assert "getelementptr inbounds { ptr, i32 }" in ir


def test_non_capturing_closure_uses_a_static_record():
    ir = _emit("""
        (fn main () -> unit
          (let triple (fn (n:i32) -> i32 (* n 3)))
          (out (triple 4)))
    """)
    assert "define i32 @__closure_0(ptr %__env, i32 %n)" in ir
    assert "@__closure_0.closure = internal constant { ptr } { ptr @__closure_0 }" in ir


def test_named_function_used_as_a_value_goes_through_a_thunk():
    ir = _emit("""
        (fn inc (x:i32) -> i32 (+ x 1))
        (fn apply (f:(fn i32 -> i32) x:i32) -> i32 (f x))
        (fn main () -> unit (out (apply inc 1)))
    """)
    assert "define internal i32 @inc.thunk(ptr %__env, i32 %a0)" in ir
    assert "@inc.thunk.closure" in ir


def test_outer_lambda_captures_what_a_nested_lambda_needs():
    ir = _emit("""
        (fn main () -> unit
          (do
            (let base 1000)
            (let outer (fn (x:i32) -> i32
              (do
                (let inner (fn (y:i32) -> i32 (+ y base)))
                (inner x))))
            (out (outer 7))))
    """)
    # Both lambdas take a record that carries `base`.
    assert ir.count("getelementptr inbounds { ptr, ptr }") >= 2
