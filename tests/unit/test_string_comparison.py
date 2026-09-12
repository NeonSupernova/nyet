"""Unit tests for string equality/ordering codegen (Emitter._is_string_operand
/ _emit_cmp / _emit_string_cmp).

Regression coverage: `string` lowers to `ptr` the same as struct/array/
tuple/Map values, but unlike those it's a primitive value type with no
`impl Eq` to dispatch `==`/`!=`/`<`/`>` through -- so every string
comparison used to fall into `_emit_cmp`'s generic pointer branch,
comparing *addresses* instead of contents. This was masked in every
prior example: two identical string *literals* get interned to the
same global constant by `_get_string`, so they were accidentally
pointer-equal. A runtime-built string (from `(in)`, `fmt`, or string
concatenation) compared against anything else -- an equal-content
literal, another runtime string -- could never compare equal no matter
how identical the contents, which breaks the single most common thing
a REPL does (`(== cmd "quit")` against a line just read from stdin).

End-to-end behavior (including via a struct's `string` field, and `<`/
`>` ordering) is covered by tests/codegen/string_eq.no; these tests pin
the underlying classification and lowering directly.

Also covers the same bug's `match` counterpart: `_emit_match_simple`
unconditionally compiled every `LitPat` arm as `icmp eq i32` regardless
of the scrutinee's real type (its own docstring said "integers, etc.");
a `string` scrutinee is a straight type mismatch against `ptr` and
failed to build. `_emit_pattern_test_value`'s `LitPat` case (used for
literal patterns nested inside sum-type payloads) had the identical
bug. End-to-end: tests/codegen/match_string.no.
"""

import re

from pynyet.codegen.emit import Emitter
from pynyet.lexer.scanner import lex
from pynyet.parser.parser import parse
from pynyet.sema.expand import expand_macros
from pynyet.sema.resolve import resolve_names
from pynyet.sema.typeck import check_types
from pynyet.source import SourceFile


def _emitter_for(src: str) -> Emitter:
    sf = SourceFile("t.no", src)
    program = parse(lex(sf))
    program, _ = expand_macros(program)
    resolve_names(program)
    check_types(program)
    e = Emitter()
    e.emit(program)
    return e


def _ir_for(src: str) -> str:
    sf = SourceFile("t.no", src)
    program = parse(lex(sf))
    program, _ = expand_macros(program)
    resolve_names(program)
    check_types(program)
    return Emitter().emit(program)


def test_two_runtime_strings_compare_via_strcmp_not_pointer_identity():
    ir = _ir_for("""
        (fn main () -> unit
          (let a:string (fmt "{}" 1))
          (let b:string (fmt "{}" 1))
          (out (== a b)))
    """)
    assert "call i32 @strcmp(" in ir
    # The old pointer-identity path used a bare `icmp eq ptr` between
    # the two loaded string pointers with no strcmp at all.
    assert "call i32 @strcmp(ptr" in ir


def test_struct_string_field_registry_tracks_string_typed_fields():
    e = _emitter_for("""
        (struct Row name:string id:i32)
        (fn main () -> unit unit)
    """)
    assert e._struct_string_fields["Row"] == {"name"}


def test_string_field_access_compares_via_strcmp():
    ir = _ir_for("""
        (struct Row name:string)
        (fn main () -> unit
          (let r:&Row (Row name:"Bob"))
          (out (== (. r name) "Bob")))
    """)
    assert "call i32 @strcmp(" in ir


def test_non_string_pointer_comparison_is_unaffected():
    # A struct-typed comparison (no `impl Eq`, same "==" builtin dispatch
    # path) must still fall through to plain pointer-identity -- the fix
    # is scoped to strings specifically, not every `ptr`-shaped value.
    ir = _ir_for("""
        (struct Point x:i32)
        (fn main () -> unit
          (let a:&Point (Point x:1))
          (let b:&Point (Point x:1))
          (out (== a b)))
    """)
    assert "call i32 @strcmp(" not in ir
    assert "icmp eq ptr" in ir


def test_match_on_string_scrutinee_compares_via_strcmp_not_directly():
    ir = _ir_for("""
        (fn classify (s:string) -> string
          (match s
            ("a" "first")
            (other other)))
        (fn main () -> unit (out (classify "a")))
    """)
    assert "call i32 @strcmp(" in ir
    # The old bug compared the raw `ptr` scrutinee against the pattern's
    # string constant directly via `icmp eq i32` -- a type mismatch that
    # failed to build. `icmp eq i32` legitimately still appears, but
    # only downstream of strcmp, checking *its* i32 result against 0
    # (see `_emit_string_cmp`) -- never applied straight to a `ptr`.
    assert "icmp eq i32 %s" not in ir
    assert re.search(r"icmp eq i32 %\w+, 0\b", ir)


def test_match_string_default_arm_binds_at_ptr_not_i32():
    # The `(pat)`-bound default arm used to always allocate its binding
    # as `i32`, regardless of the scrutinee's real type.
    ir = _ir_for("""
        (fn classify (s:string) -> string
          (match s
            ("a" "first")
            (other other)))
        (fn main () -> unit (out (classify "z")))
    """)
    assert "alloca ptr" in ir
