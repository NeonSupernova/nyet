"""Unit tests for top-level `const` inlining (Emitter._register_const).

Regression coverage for the const-silently-no-ops bug: a reference to a
top-level `(const NAME:type val)` from inside a function body used to
emit no code at all -- not an error, just a dropped operand -- because
`_emit_ident` only ever checked `_env` (local let/var/param bindings)
and `_fn_sigs` (top-level functions), never anything for module-level
consts. Concretely this turned `(if (== i MAX) (break) pass)` inside a
`loop` into a guard that silently vanished, making the loop infinite.

main.no documents consts as pure compile-time values ("inlined at every
use site -- no runtime allocation"), so the fix precomputes each
literal's LLVM immediate once and _emit_ident/_infer_llvm_type return it
directly instead of trying to `load` a nonexistent address.
"""

import re
import struct as _struct

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


def test_int_const_defaults_to_i32():
    e = _emitter_for("(const MAX:i32 5) (fn main () -> unit unit)")
    assert e._const_values["MAX"] == ("5", "i32")


def test_const_type_annotation_overrides_default_width():
    e = _emitter_for("(const BIG:i64 100) (fn main () -> unit unit)")
    assert e._const_values["BIG"] == ("100", "i64")


def test_string_const_registers_a_string_global():
    e = _emitter_for('(const GREETING:string "hi") (fn main () -> unit unit)')
    val, ty = e._const_values["GREETING"]
    assert ty == "ptr"
    assert val.startswith("@.str.")


def test_float_const_is_the_ieee754_double_bit_pattern():
    e = _emitter_for("(const PI:f64 3.5) (fn main () -> unit unit)")
    val, ty = e._const_values["PI"]
    assert ty == "double"
    expected = _struct.unpack("Q", _struct.pack("d", 3.5))[0]
    assert val == f"0x{expected:016X}"


def test_bool_const():
    e = _emitter_for("(const ENABLED:bool true) (fn main () -> unit unit)")
    assert e._const_values["ENABLED"] == ("1", "i1")


def test_const_reference_inside_loop_guard_is_not_dropped():
    ir = _ir_for("""
        (const MAX:i32 5)
        (fn main () -> unit
          (do
            (var i:i32 0)
            (loop
              (if (== i MAX) (break) pass)
              (= i (+ i 1)))
            (out i)))
    """)
    # Before the fix this comparison's second operand simply vanished
    # (the guard was deleted entirely), making the loop infinite.
    assert re.search(r"icmp eq i32 %\w+, 5\b", ir)


def test_const_reference_in_arithmetic_is_not_dropped():
    ir = _ir_for("""
        (const STEP:i32 3)
        (fn main () -> unit (out (+ 10 STEP)))
    """)
    assert re.search(r"add i32 10, 3\b", ir)


def test_const_usable_before_its_declaration_in_source_order():
    # Resolution already allows forward references; codegen must too --
    # registration happens in a pass before any function body is emitted,
    # regardless of where the const appears in the file.
    ir = _ir_for("""
        (fn get_limit () -> i32 LIMIT)
        (const LIMIT:i32 42)
        (fn main () -> unit (out (get_limit)))
    """)
    assert re.search(r"ret i32 42\b", ir)
