"""Unit tests for calling a self-less `impl` function via `Type/name`
path syntax (Emitter._emit_call / _infer_llvm_type / _infer_nyet_type_name).

Regression coverage: an associated/"static" function with no receiver
(e.g. `(fn origin () -> Point ...)` inside `impl Point`, meant to be
called as `(Point/origin)`) had no working call syntax at all.
`_emit_call` only ever dispatched on a Call whose `head` was an
`N.Ident`; a Call whose head is an `N.Path` (how `Type/name` parses)
fell straight through every branch to the final `return None` --
silently producing no call. There's no *other* way to call such a
method (inherent-method dispatch keys off a receiver argument, which a
zero-arg static constructor doesn't have), so this was a complete,
silent dead end -- not a crash, just nothing happening.

Fixing the call itself then surfaced the same "silently defaults to
i32" gap `_infer_llvm_type`/`_infer_nyet_type_name` already had for
other Call shapes (see test_array_struct_element.py): a `Point/origin`
call binding an aggregate return type into a `let` with no explicit
annotation needs its own case in both, or the `let` gets sized as a
scalar and clang rejects the resulting type mismatch.

End-to-end coverage: tests/codegen/static_impl_fn.no.
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


def test_static_impl_call_with_scalar_return():
    ir = _ir_for("""
        (impl i32
          (fn zero () -> i32 0))
        (fn main () -> unit (out (i32/zero)))
    """)
    assert "call i32 @i32__zero()" in ir


def test_static_impl_call_with_args():
    ir = _ir_for("""
        (struct Point x:i32 y:i32)
        (impl Point
          (fn make (x:i32 y:i32) -> Point (Point x:x y:y)))
        (fn main () -> unit
          (do
            (let q (Point/make 5 6))
            (out (. q x))))
    """)
    assert "call ptr @Point__make(i32 5, i32 6)" in ir


def test_static_impl_call_returning_a_struct_gets_correct_slot_type():
    # Before the _infer_llvm_type/_infer_nyet_type_name fix, binding this
    # with no explicit annotation sized the `let` as i32 (the default
    # fallback) instead of ptr, and a subsequent field access on it
    # would also fail since _env_struct_name was never populated.
    ir = _ir_for("""
        (struct Point x:i32 y:i32)
        (impl Point
          (fn origin () -> Point (Point x:0 y:0)))
        (fn main () -> unit
          (do
            (let p (Point/origin))
            (out (. p x))))
    """)
    assert "alloca ptr" in ir
    assert "getelementptr inbounds %Point" in ir


def test_non_static_method_call_is_unaffected():
    # An ordinary instance method (has a receiver, dispatched via
    # inherent-method lookup, no Path involved) must keep working.
    ir = _ir_for("""
        (struct Point x:i32 y:i32)
        (impl Point
          (fn getx (self:&Point) -> i32 (. self x)))
        (fn main () -> unit
          (do
            (let p:&Point (Point x:7 y:8))
            (out (getx p))))
    """)
    assert "call i32 @Point__getx(" in ir
