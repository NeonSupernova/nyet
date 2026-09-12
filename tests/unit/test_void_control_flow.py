"""Unit tests for two `-> unit` control-flow codegen crashes.

Regression coverage for two bugs found in the same "macro shared
between -> bool and -> unit call sites" area of minibase/main.no:

1. `(return ())` inside a `-> unit` function emitted the literal text
   "ret i32 None" -- Python's `None` interpolated straight into the IR
   -- instead of "ret void". A UnitLit `node.value` is not Python
   `None`, but `_emit_expr` legitimately yields no SSA value for it
   (unit has no runtime representation), and the codegen for `return`
   didn't distinguish "no value at all" from "a value that happens to
   produce None". End-to-end: tests/codegen/return_unit.no.
2. An `if` whose branch is a bare call to a `-> unit` function (not
   wrapped in a `do` alongside other statements) crashed codegen
   entirely: `_emit_if` sizes its result slot from
   `_infer_llvm_type(then_branch)`, which for a direct call to a void
   function returns the callee's real LLVM return type, "void" --
   illegal as an `alloca`/local-variable type (only ever legal as a
   function's own return type). End-to-end: tests/codegen/if_void_branch.no.
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


def test_return_unit_literal_emits_ret_void():
    ir = _ir_for("(fn f () -> unit (return ()))")
    assert "ret void" in ir
    assert "None" not in ir


def test_bare_return_still_emits_ret_void():
    ir = _ir_for("(fn f () -> unit (return))")
    assert "ret void" in ir


def test_return_of_a_real_value_is_unaffected():
    ir = _ir_for("(fn f () -> i32 (return 5))")
    assert "ret i32 5" in ir


def test_if_with_bare_unit_call_branches_does_not_alloca_void():
    ir = _ir_for("""
        (fn say () -> unit (out "hi\\n"))
        (fn main () -> unit (if true (say) (say)))
    """)
    assert "alloca void" not in ir
    assert "alloca ptr" in ir


def test_if_with_a_real_valued_branch_is_unaffected():
    ir = _ir_for("""
        (fn main () -> unit
          (let x:i32 (if true 1 2))
          (out x))
    """)
    assert "alloca i32" in ir
    assert "alloca void" not in ir
