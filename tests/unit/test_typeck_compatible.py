"""Unit tests for TypeChecker._compatible().

Regression coverage for the bare-integer-literal widening fix: an
unsuffixed integer literal (which always infers as I32 -- see
TypeChecker._infer) should satisfy any integer or char annotation, not
just I32 or float, since the literal itself carries no fixed
width/signedness. This also happens to be the fix for the pre-existing
`(let s:usize 0)` / `(let d:u64 100000)` typecheck failure -- see
CONTINUATION_PLAN.md Phase 1.5.
"""

from pynyet.sema.typeck import TypeChecker
from pynyet.sema.types import BOOL, CHAR, F64, I32, STRING, U8, U64, USIZE


def test_i32_literal_satisfies_i32():
    tc = TypeChecker()
    assert tc._compatible(I32, I32, is_bare_int_lit=True)


def test_bare_int_literal_satisfies_wider_unsigned_type():
    # (let d:u64 100000)
    tc = TypeChecker()
    assert tc._compatible(U64, I32, is_bare_int_lit=True)


def test_bare_int_literal_satisfies_usize():
    # (let s:usize 0)
    tc = TypeChecker()
    assert tc._compatible(USIZE, I32, is_bare_int_lit=True)


def test_bare_int_literal_satisfies_narrower_unsigned_type():
    tc = TypeChecker()
    assert tc._compatible(U8, I32, is_bare_int_lit=True)


def test_bare_int_literal_satisfies_char():
    # (let ch:char 65)
    tc = TypeChecker()
    assert tc._compatible(CHAR, I32, is_bare_int_lit=True)


def test_non_literal_int_does_not_satisfy_char():
    # (let x:i32 42) (let ch:char x) -- x is a variable, not a literal;
    # widening must not apply.
    tc = TypeChecker()
    assert not tc._compatible(CHAR, I32, is_bare_int_lit=False)


def test_non_literal_int_does_not_satisfy_a_different_int_type():
    tc = TypeChecker()
    assert not tc._compatible(U64, I32, is_bare_int_lit=False)


def test_bare_int_literal_does_not_satisfy_string():
    tc = TypeChecker()
    assert not tc._compatible(STRING, I32, is_bare_int_lit=True)


def test_bare_int_literal_does_not_satisfy_bool():
    tc = TypeChecker()
    assert not tc._compatible(BOOL, I32, is_bare_int_lit=True)


def test_int_literal_still_satisfies_float_regardless_of_literalness():
    tc = TypeChecker()
    assert tc._compatible(F64, I32, is_bare_int_lit=False)
