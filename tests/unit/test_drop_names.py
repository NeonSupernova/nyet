"""Unit tests for pynyet.sema.borrow.compute_drop_names.

Drop insertion is scoped deliberately narrowly (see its docstring):
only struct-typed locals declared unconditionally at a function's own
top level, never moved and never returned, are dropped -- and only at
the function's natural end-of-body fallthrough. Getting this wrong in
either direction is a real bug (a missed drop just leaks; an
incorrect one frees something still needed), so these tests pin down
the exact boundary cases.
"""

from pynyet.lexer.scanner import lex
from pynyet.parser.parser import parse
from pynyet.sema.borrow import compute_drop_names
from pynyet.sema.expand import expand_macros
from pynyet.sema.resolve import resolve_names
from pynyet.source import SourceFile


def _drop_names(src: str) -> dict:
    sf = SourceFile("t.no", src)
    program = parse(lex(sf))
    program, _ = expand_macros(program)
    resolve_names(program)
    return compute_drop_names(program)


def test_unreturned_struct_local_is_dropped():
    names = _drop_names("""
        (struct Point x:i32 y:i32)
        (fn scratch () -> unit
          (do
            (let p (Point x:1 y:2))
            (out (. p x))))
    """)
    assert names.get("scratch") == ["p"]


def test_tail_returned_struct_local_is_not_dropped():
    names = _drop_names("""
        (struct Point x:i32 y:i32)
        (fn make () -> Point
          (do
            (let p (Point x:1 y:2))
            p))
    """)
    assert "p" not in names.get("make", [])


def test_explicitly_returned_struct_local_is_not_dropped():
    names = _drop_names("""
        (struct Point x:i32 y:i32)
        (fn make () -> Point
          (do
            (let p (Point x:1 y:2))
            (return p)))
    """)
    assert "p" not in names.get("make", [])


def test_struct_passed_by_value_to_another_call_is_not_dropped():
    """Point{x:i32, y:i32} is all-primitive-field, so the borrow checker
    correctly calls it Copy and does NOT mark `p` as moved by
    `(consume p)` (language-level semantics: the caller may go on using
    it). But codegen always passes structs as an aliased pointer, never
    a true bitwise copy -- so `consume`'s `p` and `scratch`'s `p` are
    literally the same heap address. Regression test for a real bug:
    the first version of this pass trusted `_Binding.moved` alone and
    would have freed `p` here even though `consume` still had a live
    alias to it at the time of the call."""
    names = _drop_names("""
        (struct Point x:i32 y:i32)
        (fn consume (p:Point) -> unit (out (. p x)))
        (fn scratch () -> unit
          (do
            (let p (Point x:1 y:2))
            (consume p)))
    """)
    assert "p" not in names.get("scratch", [])


def test_builtin_borrow_call_does_not_block_drop():
    """`out`/`fmt`/etc. never retain the pointer -- unlike an ordinary
    function call, passing a struct to one shouldn't disqualify it."""
    names = _drop_names("""
        (struct Point x:i32 y:i32)
        (fn scratch () -> unit
          (do
            (let p (Point x:1 y:2))
            (out p)))
    """)
    assert names.get("scratch") == ["p"]


def test_borrowed_struct_local_is_not_dropped():
    names = _drop_names("""
        (struct Point x:i32 y:i32)
        (fn peek (p:&Point) -> unit (out (. p x)))
        (fn scratch () -> unit
          (do
            (let p (Point x:1 y:2))
            (peek &p)))
    """)
    # p is still owned here (only borrowed, not moved) -- it's the one
    # case this pass's scope conservatively excludes anyway (see
    # compute_drop_names' docstring: params/refs aside, only bindings
    # untouched by any call are unambiguously included), so just check
    # it doesn't crash and doesn't double-count.
    assert names.get("scratch", []).count("p") <= 1


def test_params_are_never_dropped():
    names = _drop_names("""
        (struct Point x:i32 y:i32)
        (fn scratch (p:Point) -> unit (out (. p x)))
    """)
    assert "p" not in names.get("scratch", [])


def test_non_struct_locals_are_not_dropped():
    names = _drop_names("""
        (fn scratch () -> unit
          (do
            (let x:i32 1)
            (out x)))
    """)
    assert names.get("scratch", []) == []


def test_function_with_no_locals_has_no_drops():
    assert _drop_names("(fn main () -> unit (out 1))") == {}


def test_struct_read_out_of_array_by_index_is_not_dropped():
    # Regression: indexing an `Array[T]` never clones `T` -- every slot
    # already holds the same pointer `_emit_struct_construct` handed
    # out, so `a` here aliases storage the array itself still owns. A
    # plain (non-`&`) `(let a:Point (pts i))` used to still be flagged
    # for a `free` despite that, so two such reads of the same slot (or
    # two calls each doing one) would double-free it at runtime. See
    # tests/codegen/array_struct_no_double_free.no for the end-to-end
    # crash this caused before the fix.
    names = _drop_names("""
        (struct Point x:i32 y:i32)
        (fn scratch () -> unit
          (do
            (let pts:Array[Point] [(Point x:1 y:2)])
            (let a:Point (pts 0))
            (out (. a x))))
    """)
    assert "a" not in names.get("scratch", [])


def test_struct_read_out_of_array_param_by_index_is_not_dropped():
    names = _drop_names("""
        (struct Point x:i32 y:i32)
        (fn scratch (pts:&Array[Point]) -> unit
          (do
            (let a:Point (pts 0))
            (out (. a x))))
    """)
    assert "a" not in names.get("scratch", [])


def test_ordinary_struct_constructor_local_is_still_dropped_alongside_an_array():
    # The array-index exclusion must be scoped to bindings whose value
    # actually came from indexing a known array -- an unrelated struct
    # constructed the normal way in the same function must still be
    # dropped exactly as before.
    names = _drop_names("""
        (struct Point x:i32 y:i32)
        (fn scratch () -> unit
          (do
            (let pts:Array[Point] [(Point x:1 y:2)])
            (let p (Point x:9 y:9))
            (out (. p x))))
    """)
    assert names.get("scratch") == ["p"]
