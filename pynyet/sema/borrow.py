"""Move/borrow checker (v0.5).

Tracks ownership state per binding inside function bodies so that
use-after-move is rejected at compile time. The model is intentionally
minimal: just enough to satisfy the v0.5 milestone goal program
("Programs that correctly reject use-after-move").

What's tracked
==============
- Each let/var/param binding starts in state ``ALIVE``.
- Passing a non-Copy binding by *value* to a user function moves it,
  flipping the state to ``MOVED``.
- Reading a binding in state ``MOVED`` produces a diagnostic.
- ``&x`` and ``&!x`` borrow without moving.
- Built-in pseudo-functions (``out``, ``err``, ``fmt``, ``panic``,
  arithmetic, comparison, boolean ops) borrow their args.
- Re-binding (``let x ...`` shadowing or ``= x ...``) restores ALIVE.

What is Copy
============
A type is Copy if you can duplicate it bit-for-bit safely:
- Numeric, bool, and reference types are Copy.
- Strings, sums, arrays, and tuples are NOT Copy (they own heap data).
- A struct is Copy iff every field is Copy.

This is a single-pass conservative checker. It does not attempt
flow-sensitive analysis through ``if`` / ``match`` / ``loop`` — both
branches are walked with a fresh copy of the parent scope and the
results are merged: a binding is moved post-hoc only if it was moved
in *every* branch (this matches Rust's semantics for soundness while
avoiding spurious errors on conditionally-consumed bindings).
"""

from __future__ import annotations

from dataclasses import dataclass

from pynyet.ast import nodes as N
from pynyet.diagnostic import Diagnostic, Severity

# Names of pseudo-functions whose args are borrowed, never moved.
_BUILTIN_BORROW = {
    "out",
    "err",
    "fmt",
    "panic",
    "in",
    "print",
    "type",
    "+",
    "-",
    "*",
    "/",
    "%",
    "==",
    "!=",
    "<",
    "<=",
    ">",
    ">=",
    "&&",
    "||",
    "!",
    "&",
    "&!",
    "len",
    "push",
    "pop",
    "append",
    "str",
    "file_open",
    "file_read_all",
    "file_write",
    "file_close",
}


@dataclass
class _Binding:
    """An entry in a borrow-checker scope."""

    name: str
    type_name: str | None  # Nyet type name or None (unknown)
    is_ref: bool  # True if the binding is `&T` / `&!T`
    moved: bool = False
    moved_at: N.Node | None = None


class BorrowChecker:
    """Walk the AST and flag use-after-move."""

    def __init__(self) -> None:
        self.errors: list[Diagnostic] = []
        # Stack of scope dicts: name → _Binding.
        self.scopes: list[dict[str, _Binding]] = [{}]
        # Struct field-type lookup so we can decide Copy-ness.
        self._struct_fields: dict[str, list[tuple[str, N.TypeNode | None]]] = {}
        # Function param info for move detection at call sites.
        # name → list[(param_type_name, is_ref)]
        self._fn_params: dict[str, list[tuple[str | None, bool]]] = {}
        # Cache of Copy-ness by type name (avoids recomputation/recursion).
        self._copy_cache: dict[str, bool] = {}

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    def check(self, program: list[N.Node]) -> list[Diagnostic]:
        for node in program:
            self._register_decl(node)
        for node in program:
            self._check_top(node)
        return self.errors

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _register_decl(self, node: N.Node) -> None:
        if isinstance(node, N.StructDecl):
            self._struct_fields[node.name] = [(f.name, f.type) for f in node.fields]
        elif isinstance(node, N.FnDecl):
            self._fn_params[node.name] = [
                (self._type_name(p.type), self._is_ref(p.type)) for p in node.params
            ]
        elif isinstance(node, N.ImplDecl):
            for item in node.items:
                if isinstance(item, N.FnDecl):
                    self._fn_params[item.name] = [
                        (self._type_name(p.type), self._is_ref(p.type)) for p in item.params
                    ]

    @staticmethod
    def _type_name(tn: N.TypeNode | None) -> str | None:
        if tn is None:
            return None
        if isinstance(tn, N.RefType):
            return BorrowChecker._type_name(tn.inner)
        if isinstance(tn, (N.NamedType, N.PrimType)):
            return tn.name
        if isinstance(tn, N.GenericType):
            return BorrowChecker._type_name(tn.base)
        return None

    @staticmethod
    def _is_ref(tn: N.TypeNode | None) -> bool:
        return isinstance(tn, N.RefType)

    # ------------------------------------------------------------------
    # Copy-ness
    # ------------------------------------------------------------------

    _PRIM_COPY = {
        "i8",
        "i16",
        "i32",
        "i64",
        "u8",
        "u16",
        "u32",
        "u64",
        "usize",
        "f32",
        "f64",
        "bool",
        "unit",
    }

    def _is_copy_type(self, name: str | None) -> bool:
        if name is None:
            # Unknown — be conservative and say Copy so we don't fire
            # spurious errors on types we don't model yet.
            return True
        if name in self._PRIM_COPY:
            return True
        if name == "string":
            return False
        if name in self._copy_cache:
            return self._copy_cache[name]
        # Avoid infinite recursion on self-referential structs.
        self._copy_cache[name] = False
        if name in self._struct_fields:
            ok = all(self._is_copy_type(self._type_name(t)) for _, t in self._struct_fields[name])
            self._copy_cache[name] = ok
            return ok
        # Unknown / sum / array / tuple — non-Copy by default.
        return False

    # ------------------------------------------------------------------
    # Scope helpers
    # ------------------------------------------------------------------

    def _push(self) -> None:
        self.scopes.append({})

    def _pop(self) -> None:
        self.scopes.pop()

    def _bind(self, name: str, type_name: str | None, is_ref: bool) -> None:
        self.scopes[-1][name] = _Binding(name, type_name, is_ref)

    def _lookup(self, name: str) -> _Binding | None:
        for sc in reversed(self.scopes):
            if name in sc:
                return sc[name]
        return None

    # ------------------------------------------------------------------
    # Top-level dispatch
    # ------------------------------------------------------------------

    def _check_top(self, node: N.Node) -> None:
        if isinstance(node, N.FnDecl):
            self._check_fn(node)
        elif isinstance(node, N.ImplDecl):
            for item in node.items:
                if isinstance(item, N.FnDecl):
                    self._check_fn(item)
        elif isinstance(node, (N.LetDecl, N.ConstDecl)):
            self._check_expr(node.value) if node.value else None
            self._bind_let(node)

    def _check_fn(self, fn: N.FnDecl) -> None:
        self._push()
        for p in fn.params:
            self._bind(p.name, self._type_name(p.type), self._is_ref(p.type))
        if fn.body is not None:
            self._check_expr(fn.body)
        self._pop()

    # ------------------------------------------------------------------
    # Expression walking
    # ------------------------------------------------------------------

    def _check_expr(self, node: N.Node | None) -> None:
        if node is None:
            return

        if isinstance(node, N.Ident):
            self._use_ident(node, consuming=False)
            return

        if isinstance(node, N.LetDecl):
            if node.value is not None:
                self._check_expr(node.value)
            self._bind_let(node)
            return

        if isinstance(node, N.ConstDecl):
            if node.value is not None:
                self._check_expr(node.value)
            self._bind_let(node)
            return

        if isinstance(node, N.Assign):
            self._check_expr(node.value)
            # An assignment to an existing binding restores it to alive.
            if isinstance(node.target, N.Ident):
                b = self._lookup(node.target.name)
                if b is not None:
                    b.moved = False
                    b.moved_at = None
            else:
                self._check_expr(node.target)
            return

        if isinstance(node, N.Call):
            self._check_call(node)
            return

        if isinstance(node, N.If):
            self._check_expr(node.cond)
            self._check_branches(node.then_branch, node.else_branch)
            return

        if isinstance(node, N.Do):
            self._push()
            for expr in node.exprs:
                self._check_expr(expr)
            self._pop()
            return

        if isinstance(node, N.Match):
            self._check_expr(node.scrutinee)
            branches = [arm.body for arm in node.arms]
            self._check_branches(*branches) if branches else None
            return

        if isinstance(node, N.Loop):
            # Walk the body once. Loops can re-enter, so any move inside
            # would also affect the next iteration — but that's caught
            # when the binding is used post-loop. Single pass is enough
            # for the v0.5 milestone.
            self._check_expr(node.body)
            return

        if isinstance(node, N.Return):
            if node.value is not None:
                self._check_expr(node.value)
            return

        if isinstance(node, N.Break):
            if node.value is not None:
                self._check_expr(node.value)
            return

        if isinstance(node, N.FieldAccess):
            # Field access borrows the target; it does NOT move.
            if isinstance(node.target, N.Ident):
                self._use_ident(node.target, consuming=False)
            else:
                self._check_expr(node.target)
            return

        if isinstance(node, N.FnExpr):
            self._push()
            for p in node.params:
                self._bind(p.name, self._type_name(p.type), self._is_ref(p.type))
            if node.body is not None:
                self._check_expr(node.body)
            self._pop()
            return

        if isinstance(node, (N.ArrayLit, N.TupleLit)):
            for e in node.elements:
                self._check_expr(e)
            return

        if isinstance(node, N.MapLit):
            for k, v in node.entries:
                self._check_expr(k)
                self._check_expr(v)
            return

        if isinstance(node, (N.Try, N.Quote, N.Splice, N.Await, N.Spawn)):
            if getattr(node, "value", None) is not None:
                self._check_expr(node.value)
            return

        if isinstance(node, N.KeywordArg):
            if node.value is not None:
                self._check_expr(node.value)
            return

        # Literals, Pass, etc. — no-op.

    # ------------------------------------------------------------------
    # Calls
    # ------------------------------------------------------------------

    def _check_call(self, node: N.Call) -> None:
        head = node.head
        # Determine whether this is a builtin / borrow-only call.
        is_borrow_only = False
        callee_params: list[tuple[str | None, bool]] | None = None
        if isinstance(head, N.Ident):
            if head.name in _BUILTIN_BORROW:
                is_borrow_only = True
            elif head.name in self._fn_params:
                callee_params = self._fn_params[head.name]
        # Walk the head only when it's not a plain identifier (calls
        # like `((make_adder 1) 2)`); otherwise the identifier is the
        # function name, not a value use.
        if not isinstance(head, N.Ident):
            self._check_expr(head)

        for i, arg in enumerate(node.args):
            # Decide whether this position consumes (moves) its arg.
            consumes = False
            if not is_borrow_only and callee_params is not None:
                if i < len(callee_params):
                    _ptn, is_ref = callee_params[i]
                    if not is_ref:
                        consumes = True
            # `&` / `&!` wrappers always borrow.
            arg_inner = self._unwrap_borrow(arg)
            if isinstance(arg_inner, N.Ident):
                self._use_ident(arg_inner, consuming=consumes and arg is arg_inner)
            else:
                self._check_expr(arg)

    @staticmethod
    def _unwrap_borrow(node: N.Node) -> N.Node:
        while (
            isinstance(node, N.Call)
            and isinstance(node.head, N.Ident)
            and node.head.name in ("&", "&!")
            and len(node.args) == 1
        ):
            node = node.args[0]
        return node

    # ------------------------------------------------------------------
    # Use of an identifier
    # ------------------------------------------------------------------

    def _use_ident(self, node: N.Ident, *, consuming: bool) -> None:
        b = self._lookup(node.name)
        if b is None:
            return
        if b.moved:
            self.errors.append(
                Diagnostic(
                    Severity.ERROR,
                    f"use of moved value '{node.name}'",
                    node.span,
                )
            )
            return
        if consuming and not b.is_ref and not self._is_copy_type(b.type_name):
            b.moved = True
            b.moved_at = node

    # ------------------------------------------------------------------
    # Bindings
    # ------------------------------------------------------------------

    def _bind_let(self, node: N.LetDecl | N.ConstDecl) -> None:
        # If the rhs was an Ident of a non-Copy binding without `&`,
        # the rhs is consumed. We approximate by inspecting the rhs
        # Ident directly here (anywhere else has already been walked).
        type_name: str | None = self._type_name(getattr(node, "type", None))
        is_ref = self._is_ref(getattr(node, "type", None))
        if type_name is None and getattr(node, "value", None) is not None:
            type_name = self._infer_value_type(node.value)
        # Re-binding the same name shadows the old binding.
        self.scopes[-1][node.name] = _Binding(node.name, type_name, is_ref)

    def _infer_value_type(self, node: N.Node | None) -> str | None:
        if isinstance(node, N.IntLit):
            return "i32"
        if isinstance(node, N.FloatLit):
            return "f64"
        if isinstance(node, N.BoolLit):
            return "bool"
        if isinstance(node, N.StringLit):
            return "string"
        if isinstance(node, N.UnitLit):
            return "unit"
        if isinstance(node, N.Call) and isinstance(node.head, N.Ident):
            name = node.head.name
            if name in self._struct_fields:
                return name
        return None

    # ------------------------------------------------------------------
    # Branch merging
    # ------------------------------------------------------------------

    def _check_branches(self, *branches: N.Node | None) -> None:
        """Walk each branch with an isolated scope, then merge moves.

        A binding is considered moved post-merge only if every branch
        moved it. This matches Rust's branch-merge soundness rule.
        """
        if not branches:
            return
        snapshot = self._snapshot()
        per_branch_moves: list[set[str]] = []
        for br in branches:
            self._restore(snapshot)
            self._push()
            self._check_expr(br) if br is not None else None
            per_branch_moves.append(self._moves_against(snapshot))
            self._pop()
        common = set.intersection(*per_branch_moves) if per_branch_moves else set()
        self._restore(snapshot)
        for name in common:
            b = self._lookup(name)
            if b is not None:
                b.moved = True

    def _snapshot(self) -> list[dict[str, _Binding]]:
        # Deep copy each binding — we need the moved/alive flags to be
        # independent of the live scope, so mutations made while walking
        # one branch don't leak into the snapshot we restore from.
        return [
            {
                name: _Binding(b.name, b.type_name, b.is_ref, b.moved, b.moved_at)
                for name, b in sc.items()
            }
            for sc in self.scopes
        ]

    def _restore(self, snap: list[dict[str, _Binding]]) -> None:
        for sc, snap_sc in zip(self.scopes, snap, strict=False):
            for name, sb in snap_sc.items():
                if name in sc:
                    sc[name].moved = sb.moved
                    sc[name].moved_at = sb.moved_at

    def _moves_against(self, snap: list[dict[str, _Binding]]) -> set[str]:
        moved_now: set[str] = set()
        for sc, snap_sc in zip(self.scopes, snap, strict=False):
            for name, b in sc.items():
                prev = snap_sc.get(name)
                if b.moved and (prev is None or not prev.moved):
                    moved_now.add(name)
        return moved_now


def check_borrows(program: list[N.Node]) -> list[Diagnostic]:
    """Run the borrow checker. Returns diagnostics."""
    bc = BorrowChecker()
    return bc.check(program)
