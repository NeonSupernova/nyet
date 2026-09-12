"""Unit test for binding a generic function's monomorphized return type
without an annotation (Emitter._infer_nyet_type_name's generic-fn case).

Regression coverage: `_infer_llvm_type` already had a case to infer
type args from a generic call's own arguments and resolve the
substituted return type (mirrors what `_monomorphize_fn` does when it
actually specializes the function), but `_infer_nyet_type_name` had no
matching case -- the plain `name in self._fn_ret_nyet_names` check just
above it only ever holds a *template's* own unsubstituted return type
name (generic-param-shaped, useless for field access), never a specific
instantiation's. `(let p (make_pair 5 "hello")) (. p first)` silently
produced no field load.

End-to-end coverage: tests/codegen/generic_fn_aggregate_return.no.
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


def test_generic_fn_struct_return_bound_without_annotation():
    ir = _ir_for("""
        (struct Pair[A B] first:A second:B)
        (fn make_pair[A B] (a:A b:B) -> Pair[A B] (Pair a b))
        (fn main () -> unit
          (do
            (let p (make_pair 5 "hello"))
            (out (. p first))))
    """)
    # Constructing the 2-field Pair inside `make_pair__i32_string` emits
    # 2 GEPs regardless of this fix; the field *read* on `p` in `main`
    # adds 1 more -- the old bug had 0 for the read, so this would be
    # 2, not 3. (An earlier version of this assertion only checked for
    # the substring's presence, which passed even against the unfixed
    # code for exactly that reason.)
    assert ir.count("getelementptr inbounds %Pair__i32_string") == 3


def test_generic_fn_scalar_return_is_unaffected():
    ir = _ir_for("""
        (fn pick_a[A B] (a:A b:B) -> A a)
        (fn main () -> unit (out (pick_a 5 "hello")))
    """)
    assert "call i32 @pick_a__i32_string(" in ir
