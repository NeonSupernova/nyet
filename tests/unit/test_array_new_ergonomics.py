"""Unit tests for `(array_new n)` working in every position that
already knows its expected `Array[T]` element type
(Emitter._emit_expr_as_array and its call sites), and for the
double-`ret` bug `_body_definitely_returns` fixes.

Regression coverage: `(array_new n)` only ever worked as the literal,
direct RHS of a `let`/`var` with an explicit `Array[T]` annotation --
`_emit_let` special-cased exactly that one AST shape. Everywhere else
`array_new` was written (a function's implicit tail-return value, an
explicit `(return (array_new n))`, a `do` block's tail position,
reassigning an existing array `var`, a struct field initializer, a
bare function argument), it fell through to the ordinary `_emit_call`
dispatch and `_emit_user_call`, which has no idea `array_new` means
anything special -- it compiled a call to an undefined external
function returning the wrong type entirely (`call i32 @array_new(...)`
where a `ptr` was expected), a hard clang build failure.

Found alongside: a function whose ENTIRE body is a single top-level
`(return expr)` emitted a spurious SECOND, invalid trailing `ret` right
after the body's own one -- tolerated by clang for scalar return types
(`ret i32 0` parses fine as dead code) but a hard build failure for a
pointer return type (`ret ptr 0`).

End-to-end coverage: tests/codegen/array_new_ergonomics.no,
tests/codegen/bare_return_body.no.
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


def _fn_body(ir: str, fn_name: str) -> str:
    for prefix in (f"define void @{fn_name}(", f"define i32 @{fn_name}(", f"define ptr @{fn_name}("):
        if prefix in ir:
            start = ir.index(prefix)
            end = ir.index("\n}", start)
            return ir[start:end]
    raise AssertionError(f"no definition found for @{fn_name} in IR")


def test_array_new_as_function_tail_return():
    ir = _ir_for("""
        (fn make (n:i32) -> Array[i32] (array_new n))
        (fn main () -> unit (out (len (make 3))))
    """)
    body = _fn_body(ir, "make")
    assert "define ptr @make(" in ir
    assert "call ptr @malloc" in body
    assert "@array_new" not in ir


def test_array_new_as_explicit_return():
    ir = _ir_for("""
        (fn make (n:i32) -> Array[i32] (return (array_new n)))
        (fn main () -> unit (out (len (make 3))))
    """)
    body = _fn_body(ir, "make")
    assert "call ptr @malloc" in body
    assert body.count("ret ptr") == 1


def test_array_new_as_do_tail():
    ir = _ir_for("""
        (fn make (n:i32) -> Array[i32]
          (do (let cap:i32 (* n 2)) (array_new cap)))
        (fn main () -> unit (out (len (make 3))))
    """)
    body = _fn_body(ir, "make")
    assert "call ptr @malloc" in body


def test_array_new_as_var_reassignment():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (var v:Array[i32] (array_new 2))
            (= v (array_new 5))
            (out (len v))))
    """)
    body = _fn_body(ir, "main")
    assert body.count("call ptr @malloc") == 2


def test_array_new_as_struct_field_initializer():
    ir = _ir_for("""
        (struct Vector data:Array[i32])
        (fn main () -> unit
          (do
            (let v (Vector data:(array_new 5)))
            (out (len (. v data)))))
    """)
    assert "call ptr @malloc" in ir
    assert "@array_new" not in ir


def test_array_new_as_bare_function_argument():
    ir = _ir_for("""
        (fn use_arr (a:Array[i32]) -> i32 (len a))
        (fn main () -> unit (out (use_arr (array_new 5))))
    """)
    body = _fn_body(ir, "main")
    assert "call ptr @malloc" in body
    assert "@array_new" not in ir


def test_bare_return_body_emits_single_terminator():
    ir = _ir_for("""
        (fn f (n:i32) -> i32 (return (* n 2)))
        (fn main () -> unit (out (f 5)))
    """)
    body = _fn_body(ir, "f")
    assert body.count("ret i32") == 1


def test_bare_return_of_array_emits_single_ptr_terminator():
    ir = _ir_for("""
        (fn make (n:i32) -> Array[i32] (return (array_new n)))
        (fn main () -> unit (out (len (make 3))))
    """)
    body = _fn_body(ir, "make")
    assert body.count("ret ptr") == 1
    assert "ret ptr 0" not in body


def test_do_tail_return_emits_single_terminator():
    ir = _ir_for("""
        (fn f (n:i32) -> i32 (do (let x:i32 (+ n 1)) (return x)))
        (fn main () -> unit (out (f 5)))
    """)
    body = _fn_body(ir, "f")
    assert body.count("ret i32") == 1


def test_normal_fallthrough_body_is_unaffected():
    # The common case (no explicit `return` at all) must still emit its
    # one synthetic `ret` exactly as before.
    ir = _ir_for("""
        (fn f (n:i32) -> i32 (* n 2))
        (fn main () -> unit (out (f 5)))
    """)
    body = _fn_body(ir, "f")
    assert body.count("ret i32") == 1
    assert "ret i32 %" in body
