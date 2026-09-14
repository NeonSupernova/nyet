"""Unit tests for Emitter._emit_ident's undefined-identifier error.

Regression coverage: `_lift_closures` hoists a `(fn ...)`/`(fn! ...)`
literal to a top-level function, and its own docstring already said
"any free variable in the lambda body will fail later as an undefined
identifier" -- but it didn't actually fail. `_emit_ident`'s fallback
for a name found in none of `_env`/`_const_values`/`_fn_sigs` was to
silently return `None` (the same silent-no-op pattern behind several
other bugs fixed in this session), so a closure referencing an outer
variable -- captures aren't implemented at all, see
`_lift_closures`'s docstring in emit.py -- compiled and ran with no
error, silently reading/writing nothing and producing 0/garbage
instead of the outer value.

Concretely, before this fix:
    (var counter:i32 5)
    (let read_it (fn () -> i32 counter))
    (out (read_it))
compiled and printed "0", not 5 (and not an error either).

Fixed by making `_emit_ident` raise `NotImplementedError` instead of
returning `None` for a name that resolves fine at the sema level (where
lexical scoping still sees the enclosing function) but not at codegen
time (after lifting, in a function whose own `_env` never had it) --
matching the existing convention elsewhere in this file (e.g.
`_register_const` for a non-literal initializer). Verified this is
safe against every other existing passage through `_emit_ident`: the
full golden + unit suite is unaffected by the change (an `N.Ident` node
has no legitimate reason to denote "no value" the way a `Pass`/`UnitLit`
does, so there was no other case relying on the old silent fallback).
"""

import pytest

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


def test_closure_reading_an_outer_variable_raises_instead_of_reading_garbage():
    with pytest.raises(NotImplementedError, match="counter"):
        _emit("""
            (fn main () -> unit
              (do
                (var counter:i32 5)
                (let read_it (fn () -> i32 counter))
                (out (read_it))))
        """)


def test_mutable_closure_writing_an_outer_variable_also_raises():
    with pytest.raises(NotImplementedError, match="counter"):
        _emit("""
            (fn main () -> unit
              (do
                (var counter:i32 0)
                (let inc (fn! () -> unit (= counter (+ counter 1))))
                (inc)))
        """)


def test_non_capturing_closure_is_unaffected():
    # A lambda that only references its own params (no free variables)
    # must keep working -- this is the whole v0.6 milestone's supported
    # shape (see _lift_closures' docstring), unaffected by this fix.
    ir = _emit("""
        (fn main () -> unit
          (let triple (fn (n:i32) -> i32 (* n 3)))
          (out (triple 4)))
    """)
    assert "define i32 @__closure_0(i32 %n)" in ir
    assert "mul i32 %t2, 3" in ir
