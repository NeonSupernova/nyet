"""Unit tests for closures used as Map values (a dispatch-table
pattern: `{"go" (fn () -> unit ...)}`).

Two bugs here:

1. `_lift_closures`'s `_lift_in` walk is an explicit node-type
   whitelist, not a generic recursive walk over every dataclass field
   -- `N.MapLit` was simply missing from it, so a closure literal used
   as a map *value* never got hoisted to a top-level function at all.
   The raw `FnExpr` reached `_emit_expr`, which has no case for it (an
   `FnExpr` is only ever valid pre-lifting), and hit the catch-all
   `NotImplementedError`.
2. Even after lifting, binding a map-lookup result with no annotation
   (`(let h (handlers "go"))`) didn't recognize `h` as callable --
   `_fn_sig_of_value` (which `_emit_let` uses to populate
   `_env_fn_sig` for indirect calls) only ever checked a plain `Ident`
   value, never a Call indexing a map whose values are themselves
   closures. `(h)` was parsed as a call to an undefined function `h`
   instead of an indirect call through the loaded fn pointer.

End-to-end coverage: tests/codegen/closure_in_map.no.
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


def test_closure_literal_in_map_value_is_lifted_not_left_inline():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (var handlers {"go" (fn () -> unit (out "hi\\n"))})
            pass))
    """)
    assert "define void @__closure_0()" in ir


def test_closure_retrieved_from_map_is_called_indirectly():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (var handlers {"go" (fn () -> unit (out "hi\\n"))})
            (let h (handlers "go"))
            (h)))
    """)
    assert "@h(" not in ir
    assert "call void %t" in ir


def test_map_of_scalars_is_unaffected():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (var m {"a" 1})
            (let x (m "a"))
            (out x)))
    """)
    assert "define void @__closure_0" not in ir
