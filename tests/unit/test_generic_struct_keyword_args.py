"""Unit tests for generic type-arg inference from a keyword-style
struct constructor call (Emitter._reorder_ctor_args).

Regression coverage: `_infer_type_args_for_struct` used to filter
keyword args out of the constructor call entirely before unifying
against the struct's declared field types (which are matched
positionally). A generic struct constructed with ALL keyword args
(no positional args left at all -- `(Pair first:a second:b)`, the
primary documented struct-construction style per main.no's own
examples) had nothing left to unify its type params against and could
never be monomorphized. The constructor call then fell through every
dispatch case in `_emit_call` and was misparsed as an ordinary
function call, whose still-keyword-shaped args hit `_emit_expr`'s
catch-all `NotImplementedError` for `KeywordArg`.

End-to-end coverage: tests/codegen/generic_struct_keyword_args.no.
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


def test_generic_struct_with_all_keyword_args_monomorphizes():
    ir = _ir_for("""
        (struct Pair[A B] first:A second:B)
        (fn make_pair[A B] (a:A b:B) -> Pair[A B] (Pair first:a second:b))
        (fn main () -> unit
          (do
            (let p (make_pair 5 "hello"))
            (out (. p first))))
    """)
    assert "%Pair__i32_string = type { i32, ptr }" in ir


def test_generic_struct_with_mixed_positional_and_keyword_args():
    # Fields declared (first, second); call supplies `second` by
    # keyword but `first` positionally, out of field order -- exercises
    # _reorder_ctor_args actually reordering, not just detecting an
    # all-or-nothing keyword/positional split.
    ir = _ir_for("""
        (struct Pair[A B] first:A second:B)
        (fn make_pair[A B] (a:A b:B) -> Pair[A B] (Pair second:b a))
        (fn main () -> unit
          (do
            (let p (make_pair 5 "hello"))
            (out (. p first))))
    """)
    assert "%Pair__i32_string = type { i32, ptr }" in ir


def test_non_generic_struct_keyword_construction_is_unaffected():
    ir = _ir_for("""
        (struct Point x:i32 y:i32)
        (fn main () -> unit (out (. (Point x:1 y:2) x)))
    """)
    assert "%Point = type { i32, i32 }" in ir
