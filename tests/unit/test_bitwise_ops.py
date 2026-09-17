"""Unit tests for bitwise operator codegen (Emitter._emit_bitwise /
_emit_shift / _emit_bitnot) and the parser's `&`-as-call-head fix
(Parser._parse_call_head).

Regression coverage: main.no documents `&`/`|`/`^`/`~`/`<<`/`>>` as
ordinary S-expression operators (`(& a b)`, `(~ a)`, etc.), and the
lexer/parser already tokenize them via `_OPERATOR_TOKENS` -- but before
this fix nothing in codegen handled any of them:
  - `(& a b)` (2-arg bitwise AND) fell through every case in
    `_emit_call` and silently evaluated to `None` -- dropped output,
    no error, no crash.
  - `|`/`^`/`~`/`<<`/`>>` aren't recognized as borrow syntax at all, so
    they fell all the way to `_emit_user_call`, which emitted an
    outright illegal LLVM identifier (`call i32 @|(...)`) that clang's
    parser rejects.

Separately (found while fixing the first): even after adding codegen
for all six operators, `(& a b)` specifically still produced nothing,
because of an independent PARSER bug -- `&` is also the prefix-borrow
token for a bare sub-expression (`&x`), and `_parse_call`'s generic
`head = self.parse_expr()` let that prefix rule fire even when `&` was
the explicit head of a parenthesized call, greedily parsing the next
argument as the borrow's own operand and returning a `Call` (not an
`Ident`) as the head -- so `(& a b)` parsed as
`Call(head=Call(Ident("&"), [a]), args=[b])`, a shape no downstream
dispatch (which only ever checks for an `Ident`/`Path` head) recognizes.
Fixed with `_parse_call_head`, which treats a leading `&`/`&!` as the
plain operator identifier when it's the head of a call.

End-to-end coverage: tests/codegen/bitwise_ops.no.
"""

from pynyet.codegen.emit import Emitter
from pynyet.lexer.scanner import lex
from pynyet.parser.parser import parse
from pynyet.sema.expand import expand_macros
from pynyet.sema.resolve import resolve_names
from pynyet.sema.typeck import check_types
from pynyet.source import SourceFile

import pynyet.ast.nodes as N


def _program(src: str) -> list[N.Node]:
    sf = SourceFile("t.no", src)
    program = parse(lex(sf))
    program, _ = expand_macros(program)
    resolve_names(program)
    check_types(program)
    return program


def _ir_for(src: str) -> str:
    return Emitter().emit(_program(src))


def _fn_body(ir: str, fn_name: str) -> str:
    for prefix in (f"define void @{fn_name}(", f"define i32 @{fn_name}("):
        if prefix in ir:
            start = ir.index(prefix)
            end = ir.index("\n}", start)
            return ir[start:end]
    raise AssertionError(f"no definition found for @{fn_name} in IR")


def test_ampersand_as_call_head_parses_as_flat_ident_call():
    program = _program("""
        (fn main () -> unit
          (do
            (let a:i32 12)
            (let b:i32 10)
            (let r (& a b))))
    """)
    main_fn = next(d for d in program if isinstance(d, N.FnDecl) and d.name == "main")
    let_r = main_fn.body.exprs[-1]
    and_call = let_r.value
    assert isinstance(and_call, N.Call)
    assert isinstance(and_call.head, N.Ident)
    assert and_call.head.name == "&"
    assert len(and_call.args) == 2


def test_bitwise_and_emits_and_instruction():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let a:i32 12)
            (let b:i32 10)
            (out! (& a b))))
    """)
    body = _fn_body(ir, "main")
    assert "and i32" in body


def test_bitwise_or_and_xor_emit_or_xor_instructions():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let a:i32 12)
            (let b:i32 10)
            (out! (| a b))
            (out! (^ a b))))
    """)
    body = _fn_body(ir, "main")
    assert "or i32" in body
    assert "xor i32" in body


def test_bitwise_not_emits_xor_with_negative_one():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let a:i32 5)
            (out! (~ a))))
    """)
    body = _fn_body(ir, "main")
    assert "xor i32 %t2, -1" in body


def test_left_shift_emits_shl():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let a:i32 1)
            (out! (<< a 4))))
    """)
    body = _fn_body(ir, "main")
    assert "shl i32" in body


def test_signed_right_shift_emits_ashr():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let a:i32 8)
            (out! (>> a 1))))
    """)
    body = _fn_body(ir, "main")
    assert "ashr i32" in body
    assert "lshr" not in body


def test_unsigned_right_shift_emits_lshr():
    ir = _ir_for("""
        (fn main () -> unit
          (do
            (let a:u32 8)
            (out! (>> a 1))))
    """)
    body = _fn_body(ir, "main")
    assert "lshr i32" in body
    assert "ashr" not in body


def test_borrow_of_ident_used_as_argument_is_unaffected():
    # The common, pre-existing usage: bare `&x` as a function ARGUMENT
    # (not the head of its own call) must still work exactly as before.
    ir = _ir_for("""
        (struct Point x:i32 y:i32)
        (fn show (p:&Point) -> unit (out! (. p x)))
        (fn main () -> unit
          (do
            (let pt (Point x:1 y:2))
            (show &pt)))
    """)
    assert "call void @show(ptr" in ir or "call i32 @show(ptr" in ir
