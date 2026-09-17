"""Unit tests for resolving a generic sum type variant's remaining
type parameters from the enclosing return-type/let-annotation context
when its own field types can't fully determine them
(Emitter._resolve_generic_variant / _sum_type_args_from_type_node).

Regression coverage: `_infer_type_args_for_variant` unifies a variant
constructor call's arguments against ONLY that variant's own field
types -- for `(type Result[T E] (Ok T) (Err E))`, calling `(Ok v)`
only ever mentions `T`, never `E`, so `E` can never be resolved from
the call's own arguments no matter what. `_resolve_generic_variant`
used to just give up in that case (returning `None`), which made
`_emit_call`'s dispatch fall through to the bare-name
`_variant_ctors[vname]` lookup -- and since `_register_sum_type`
registers each variant under its plain name UNCONDITIONALLY on every
monomorphization, whichever concrete instantiation of the SAME generic
sum type got registered LAST always won for every other instantiation
in the same program. Confirmed: `(fn a () -> Result[i32 string] (Ok
5))` and `(fn b () -> Result[string string] (Ok data))` in the same
program corrupted one or the other -- an ill-typed `store ptr 5, ptr
...` that clang's LLVM verifier rejects outright.

Fixed by falling back to the enclosing function's declared return type
(`_current_fn_return_type_node`) or a `let`'s own annotation when
per-argument unification can't resolve every generic parameter --
covers the two places a generic variant construction's "missing"
type args are actually knowable from context.

End-to-end coverage: tests/codegen/generic_variant_disambiguation.no.
"""

from pynyet.codegen.emit import Emitter
from pynyet.lexer.scanner import lex
from pynyet.parser.parser import parse
from pynyet.sema.expand import expand_macros
from pynyet.sema.resolve import resolve_names
from pynyet.sema.typeck import check_types
from pynyet.source import SourceFile

SRC_PREFIX = "(type MyResult[T E] (Ok2 T) (Err2 E))\n"


def _ir_for(src: str) -> str:
    sf = SourceFile("t.no", SRC_PREFIX + src)
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


def test_two_instantiations_via_function_return_type_dont_collide():
    ir = _ir_for("""
        (fn make_i32 () -> MyResult[i32 string] (Ok2 5))
        (fn make_str (s:string) -> MyResult[string string] (Ok2 s))
        (fn main () -> unit
          (do
            (out (match (make_i32) ((Ok2 v) v) ((Err2 e) 0)))
            (out (match (make_str "hi") ((Ok2 v) v) ((Err2 e) "x")))))
    """)
    body_i32 = _fn_body(ir, "make_i32")
    body_str = _fn_body(ir, "make_str")
    # The i32 instantiation stores an i32 payload; the string one
    # stores a ptr payload -- if the bare-name collision bug were still
    # present, one of these would store the WRONG type into a `ptr`
    # slot (a store type mismatch), not just have the wrong value.
    assert "store i32 5" in body_i32
    assert "store ptr %" in body_str


def test_two_instantiations_via_let_annotation_dont_collide():
    # A plain `let a:...(Ok2 1)` immediately followed by `let
    # b:...(Ok2 "two")` happens to self-correct even pre-fix: a `let`'s
    # own OWN annotation monomorphizes (and re-registers the bare-name
    # variant ctor for) its OWN instantiation immediately before its
    # value is emitted. The genuine ordering conflict needs something
    # ELSE to monomorphize a DIFFERENT instantiation of the same sum
    # type in between -- here, `a`'s own value expression is a `do`
    # block that constructs `b` (a different instantiation) first, so
    # by the time `(Ok2 1)` itself is emitted, the bare-name ctor entry
    # has already been overwritten to point to `b`'s (wrong) shape.
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let a:MyResult[i32 string]
              (do
                (let b:MyResult[string bool] (Ok2 "two"))
                (Ok2 1)))
            (out (match a ((Ok2 v) v) ((Err2 e) 0)))))
    """)
    body = _fn_body(ir, "main")
    assert "store i32 1" in body


def test_err_variant_resolves_via_return_type_too():
    # Needs a genuinely COMPETING instantiation, or there's nothing for
    # the bare-name lookup to collide with even pre-fix. Both
    # instantiations' `Err2` payload happens to be `string` (`ptr`) in
    # this repro, so a loose "did it store a ptr" assertion passes
    # either way -- the actual bug is that `make_err`'s malloc'd struct
    # TYPE NAME itself was wrong (`%MyResult__string_string` instead of
    # `%MyResult__i32_string`), so assert on that directly.
    ir = _ir_for("""
        (fn make_err () -> MyResult[i32 string] (Err2 "bad"))
        (fn make_ok_str (s:string) -> MyResult[string string] (Ok2 s))
        (fn main () -> unit
          (do
            (out (match (make_err) ((Ok2 v) 0) ((Err2 e) e)))
            (out (match (make_ok_str "hi") ((Ok2 v) v) ((Err2 e) "x")))))
    """)
    body = _fn_body(ir, "make_err")
    assert "%MyResult__i32_string" in body
    assert "%MyResult__string_string" not in body


def test_single_type_param_variant_still_infers_from_args_alone():
    # The common case (every generic param appears in the variant's own
    # field types) must keep working via plain per-argument inference,
    # with no return-type context needed at all.
    ir = _ir_for("""
        (type Box[T] (Wrap T))
        (fn main () -> unit
          (do
            (let b (Wrap 7))
            (out (match b ((Wrap v) v)))))
    """)
    assert "call ptr @malloc" in ir
