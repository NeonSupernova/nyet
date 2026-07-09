"""Type inference and checking pass.

For v0.1 scope: primitives, let/var, arithmetic, if, do, fn, out.
No generics, no ownership, all types Copy.

Walks the AST after name resolution and:
- Resolves type annotations to NyetType
- Infers types for expressions
- Checks that operations are type-correct
- Populates inferred_type annotation slots
"""

from __future__ import annotations

from pynyet.ast import nodes as N
from pynyet.diagnostic import Diagnostic, Severity
from pynyet.sema.types import (
    BOOL,
    CHAR,
    ERROR,
    F64,
    I32,
    KEYWORD,
    PRIM_TYPES,
    STRING,
    UNIT,
    ArrayType,
    CharType,
    FloatType,
    FnSig,
    IntType,
    NyetType,
    StringType,
    StructType,
    SumType,
    TupleType,
)


class TypeChecker:
    """Walk the AST and infer/check types."""

    def __init__(self) -> None:
        self.errors: list[Diagnostic] = []
        # name → NyetType for functions and bindings
        self.env: dict[str, NyetType] = {}
        # name → whether the binding was declared `var` (mutable). Only
        # `let`/`var` locals are tracked; names absent here (fn params,
        # etc.) are treated as unconstrained, not immutable.
        self.mutable: dict[str, bool] = {}
        # Struct/type declarations
        self.type_decls: dict[str, NyetType] = {}

    def check(self, program: list[N.Node]) -> list[Diagnostic]:
        # First pass: register all top-level declarations
        for node in program:
            self._register_decl(node)
        # Second pass: check bodies
        for node in program:
            self._check_node(node)
        return self.errors

    def _resolve_type_node(self, tn: N.TypeNode | None) -> NyetType:
        """Convert an AST TypeNode to a semantic NyetType."""
        if tn is None:
            return ERROR
        if isinstance(tn, N.PrimType):
            return PRIM_TYPES.get(tn.name, ERROR)
        if isinstance(tn, N.UnitType):
            return UNIT
        if isinstance(tn, N.NamedType):
            if tn.name in PRIM_TYPES:
                return PRIM_TYPES[tn.name]
            if tn.name in self.type_decls:
                return self.type_decls[tn.name]
            # Unknown type — might be a generic param or forward ref
            return ERROR
        if isinstance(tn, N.RefType):
            inner = self._resolve_type_node(tn.inner)
            from pynyet.sema.types import RefType

            return RefType(inner, tn.mutable)
        if isinstance(tn, N.FnType):
            params = tuple(self._resolve_type_node(p) for p in tn.params)
            ret = self._resolve_type_node(tn.ret) if tn.ret else UNIT
            return FnSig(params, ret)
        if isinstance(tn, N.TupleType):
            elems = tuple(self._resolve_type_node(e) for e in tn.elements)
            return TupleType(elems)
        if isinstance(tn, N.SelfType):
            return ERROR  # resolved during trait checking
        if isinstance(tn, N.GenericType):
            base = tn.base
            base_name = base.name if isinstance(base, (N.NamedType, N.PrimType)) else None
            if base_name == "Array" and tn.args:
                return ArrayType(self._resolve_type_node(tn.args[0]))
            # For now, just return the base type name
            return self._resolve_type_node(base)
        if isinstance(tn, N.DynType):
            return ERROR  # deferred
        return ERROR

    def _register_decl(self, node: N.Node) -> None:
        if isinstance(node, N.FnDecl):
            param_types = tuple(self._resolve_type_node(p.type) for p in node.params)
            ret = self._resolve_type_node(node.return_type) if node.return_type else UNIT
            self.env[node.name] = FnSig(param_types, ret)

        elif isinstance(node, N.StructDecl):
            fields = tuple((f.name, self._resolve_type_node(f.type)) for f in node.fields)
            st = StructType(node.name, fields)
            self.type_decls[node.name] = st
            # Also register as a callable (constructor)
            param_types = tuple(t for _, t in fields)
            self.env[node.name] = FnSig(param_types, st)

        elif isinstance(node, N.TypeDecl):
            variants = tuple(
                (name, tuple(self._resolve_type_node(t) for t in types))
                for name, types in node.variants
            )
            sum_ty = SumType(node.name, variants)
            self.type_decls[node.name] = sum_ty
            # Register variant constructors
            for vname, vtypes in variants:
                self.env[vname] = FnSig(vtypes, sum_ty)

        elif isinstance(node, N.ConstDecl):
            ty = self._resolve_type_node(node.type) if node.type else ERROR
            if ty is ERROR and node.value is not None:
                ty = self._infer(node.value)
            self.env[node.name] = ty

        elif isinstance(node, N.LetDecl):
            ty = self._resolve_type_node(node.type) if node.type else ERROR
            if ty is ERROR and node.value is not None:
                ty = self._infer(node.value)
            self.env[node.name] = ty

        elif isinstance(node, N.ImplDecl):
            # Register named methods so their signatures are visible at
            # call sites. Operator methods are skipped — the built-in
            # codegen dispatch handles those.
            for item in node.items:
                if isinstance(item, N.FnDecl) and item.name.isidentifier():
                    self._register_decl(item)

    def _check_node(self, node: N.Node) -> None:
        if node is None:
            return
        if isinstance(node, N.FnDecl):
            # Save outer env, create inner for params
            saved = dict(self.env)
            saved_mutable = dict(self.mutable)
            for p in node.params:
                self.env[p.name] = self._resolve_type_node(p.type) if p.type else ERROR
                # Params shadow any outer let/var of the same name -- don't
                # let a stale immutability entry leak in from another scope.
                self.mutable.pop(p.name, None)
            if node.body is not None:
                self._infer(node.body)
            self.env = saved
            self.mutable = saved_mutable

        elif isinstance(node, N.LetDecl):
            if node.value is not None:
                val_ty = self._infer(node.value)
                declared = self._resolve_type_node(node.type) if node.type else None
                if declared and declared is not ERROR:
                    is_bare_int_lit = isinstance(node.value, N.IntLit)
                    if not self._compatible(declared, val_ty, is_bare_int_lit):
                        self.errors.append(
                            Diagnostic(
                                Severity.ERROR,
                                f"type mismatch: expected {declared}, got {val_ty}",
                                node.span,
                            )
                        )
                    self.env[node.name] = declared
                else:
                    self.env[node.name] = val_ty
                self.mutable[node.name] = node.mutable

        elif isinstance(node, N.ImplDecl):
            for item in node.items:
                self._check_node(item)
        elif isinstance(node, N.TraitDecl):
            for item in node.items:
                self._check_node(item)

    def _infer(self, node: N.Node | None) -> NyetType:
        """Infer the type of an expression node."""
        if node is None:
            return UNIT

        if isinstance(node, N.IntLit):
            return I32
        if isinstance(node, N.FloatLit):
            return F64
        if isinstance(node, N.StringLit):
            return STRING
        if isinstance(node, N.BoolLit):
            return BOOL
        if isinstance(node, N.UnitLit):
            return UNIT
        if isinstance(node, N.KeywordLit):
            return KEYWORD
        if isinstance(node, N.Pass):
            return UNIT

        if isinstance(node, N.Ident):
            ty = self.env.get(node.name)
            if ty is not None:
                return ty
            # Builtins
            if node.name == "out":
                return FnSig((), UNIT)
            if node.name == "in":
                return FnSig((), STRING)
            if node.name == "err":
                return FnSig((), UNIT)
            if node.name == "panic":
                return FnSig((), UNIT)
            # v1.0 file IO builtins. STRING return for handles is approximate
            # — handles are opaque pointers, but STRING shares the LLVM `ptr`
            # shape and isn't Copy, which keeps the borrow checker honest if
            # someone tries to alias them.
            if node.name == "file_open":
                return FnSig((STRING, STRING), STRING)
            if node.name == "file_read_all":
                return FnSig((STRING,), STRING)
            if node.name == "file_write":
                return FnSig((STRING, STRING), UNIT)
            if node.name == "file_close":
                return FnSig((STRING,), UNIT)
            return ERROR

        if isinstance(node, N.Call):
            head_ty = self._infer(node.head)
            # Infer arg types (for side effects and registration)
            for arg in node.args:
                self._infer(arg)
            # `(in type)` — return the specified type
            if (
                isinstance(node.head, N.Ident)
                and node.head.name == "in"
                and node.args
                and isinstance(node.args[0], N.Ident)
                and node.args[0].name in PRIM_TYPES
            ):
                return PRIM_TYPES[node.args[0].name]
            # `(arr i)` — array indexing yields the element type.
            if isinstance(head_ty, ArrayType) and len(node.args) == 1:
                return head_ty.element
            # `(t i)` — tuple indexing with a literal int yields that
            # position's type. Heterogeneous, so the index must be known
            # at compile time (unlike Array[T] indexing).
            if (
                isinstance(head_ty, TupleType)
                and len(node.args) == 1
                and isinstance(node.args[0], N.IntLit)
                and 0 <= node.args[0].value < len(head_ty.elements)
            ):
                return head_ty.elements[node.args[0].value]
            # `(str i)` — indexing a string by byte position yields a char
            # (the UTF-8 byte at that offset). The index must be an integer.
            if (
                isinstance(head_ty, StringType)
                and len(node.args) == 1
                and isinstance(self._infer(node.args[0]), IntType)
            ):
                return CHAR
            if isinstance(head_ty, FnSig):
                return head_ty.ret
            # Operator calls — infer from first operand
            if isinstance(node.head, N.Ident):
                op = node.head.name
                if op in ("+", "-", "*", "/", "%"):
                    if node.args:
                        return self._infer(node.args[0])
                    return I32
                if op in ("==", "!=", "<", "<=", ">", ">=", "&&", "||", "!"):
                    return BOOL
                if op in ("&", "&!"):
                    if node.args:
                        return self._infer(node.args[0])
                    return ERROR
            return ERROR

        if isinstance(node, N.If):
            self._infer(node.cond)
            then_ty = self._infer(node.then_branch)
            if node.else_branch is not None:
                self._infer(node.else_branch)
            return then_ty

        if isinstance(node, N.Do):
            ty = UNIT
            for expr in node.exprs:
                if isinstance(expr, N.LetDecl):
                    self._check_node(expr)
                    ty = UNIT
                else:
                    ty = self._infer(expr)
            return ty

        if isinstance(node, N.Match):
            scrut_ty = self._infer(node.scrutinee)
            ty = UNIT
            for arm in node.arms:
                ty = self._infer(arm.body)
            if isinstance(scrut_ty, SumType):
                self._check_match_exhaustive(node, scrut_ty)
            return ty

        if isinstance(node, N.Loop):
            self._infer(node.body)
            return UNIT  # loop returns via break

        if isinstance(node, N.Return):
            if node.value is not None:
                return self._infer(node.value)
            return UNIT

        if isinstance(node, N.Break):
            if node.value is not None:
                return self._infer(node.value)
            return UNIT

        if isinstance(node, N.Assign):
            self._infer(node.target)
            if isinstance(node.target, N.Ident) and self.mutable.get(node.target.name) is False:
                self.errors.append(
                    Diagnostic(
                        Severity.ERROR,
                        f"cannot assign to '{node.target.name}': declared with `let`, not `var`",
                        node.span,
                    )
                )
            return self._infer(node.value)

        if isinstance(node, N.FieldAccess):
            self._infer(node.target)
            return ERROR  # field types resolved with struct info

        if isinstance(node, N.FnExpr):
            return FnSig(
                tuple(self._resolve_type_node(p.type) for p in node.params),
                self._resolve_type_node(node.return_type) if node.return_type else UNIT,
            )

        if isinstance(node, N.ArrayLit):
            if node.elements:
                elem_ty = self._infer(node.elements[0])
                for e in node.elements[1:]:
                    self._infer(e)
                return ArrayType(elem_ty)
            return ArrayType(I32)

        if isinstance(node, N.TupleLit):
            elem_tys = tuple(self._infer(e) for e in node.elements)
            return TupleType(elem_tys)

        if isinstance(node, N.KeywordArg):
            if node.value is not None:
                return self._infer(node.value)
            return ERROR

        if isinstance(node, N.Try):
            inner = self._infer(node.value)
            # For Option[T] / Result[T, E]: (? expr) yields the success variant's payload
            if isinstance(inner, SumType) and inner.variants:
                first_variant = inner.variants[0]
                payload_types = first_variant[1]
                if payload_types:
                    return payload_types[0]
            return inner

        if isinstance(node, N.Splice):
            # A surviving Splice is a macro misuse; the expander will have
            # reported it. Just descend so we don't lose downstream errors.
            self._infer(node.value)
            return ERROR

        if isinstance(node, N.Cast):
            src_ty = self._infer(node.value)
            dst_ty = self._resolve_type_node(node.target_type)
            _castable = (IntType, FloatType, CharType)
            if dst_ty is not ERROR and src_ty is not ERROR:
                if not isinstance(src_ty, _castable):
                    self.errors.append(
                        Diagnostic(
                            Severity.ERROR,
                            f"cannot cast from non-primitive type {src_ty}",
                            node.span,
                        )
                    )
                elif not isinstance(dst_ty, _castable):
                    self.errors.append(
                        Diagnostic(
                            Severity.ERROR,
                            f"cannot cast to non-primitive type {dst_ty}",
                            node.span,
                        )
                    )
            return dst_ty

        if isinstance(node, N.Quote):
            # Quote is consumed by the macro expander; if one survives here,
            # it had no enclosing macro — fall back to its inner expression.
            return self._infer(node.value)

        if isinstance(node, N.Await):
            return self._infer(node.value)

        if isinstance(node, N.LetDecl):
            self._check_node(node)
            return UNIT

        if isinstance(node, N.Path):
            return ERROR  # paths need module resolution

        return ERROR

    def _check_match_exhaustive(self, node: N.Match, sum_ty: SumType) -> None:
        """Warn (not error — this is advisory, matching Rust's own
        `#[warn(non_exhaustive)]` treatment) when a match on a sum type
        covers neither every variant nor has a catch-all arm.

        Previously computed ad hoc inside codegen's match lowering
        (pynyet/codegen/emit.py's now-removed _exhaustiveness_warn) as a
        bare stderr print, invisible to `driver check` and to any
        program whose match target never reached codegen. Same
        variant-coverage logic, now a real Diagnostic that flows through
        the normal pipeline.
        """
        all_names = {v[0] for v in sum_ty.variants}
        covered: set[str] = set()
        has_catchall = False
        for arm in node.arms:
            pat = arm.pattern
            if isinstance(pat, N.GuardedPat):
                continue  # conditional — doesn't guarantee coverage
            if isinstance(pat, (N.WildPat, N.VarPat)):
                has_catchall = True
                break
            if isinstance(pat, N.VariantPat) and pat.name in all_names:
                covered.add(pat.name)
        if has_catchall:
            return
        missing = sorted(all_names - covered)
        if missing:
            self.errors.append(
                Diagnostic(
                    Severity.WARNING,
                    f"non-exhaustive match on {sum_ty.name}; "
                    f"missing variants: {', '.join(missing)}",
                    node.span,
                )
            )

    def _compatible(
        self, expected: NyetType, actual: NyetType, is_bare_int_lit: bool = False
    ) -> bool:
        """Check if actual is compatible with expected.

        `is_bare_int_lit` is set when `actual` came directly from an
        unsuffixed integer literal (which always infers as I32 — see
        `_infer`). Such literals have no fixed width/signedness of their
        own, so they may satisfy any integer or char annotation, e.g.
        `(let d:u64 100000)` or `(let ch:char 65)`.
        """
        if expected is ERROR or actual is ERROR:
            return True  # don't cascade errors
        if isinstance(expected, FloatType) and isinstance(actual, IntType):
            return True  # integer literals can be assigned to float bindings
        if is_bare_int_lit and isinstance(expected, (IntType, CharType)):
            return True
        return expected == actual


def check_types(program: list[N.Node]) -> list[Diagnostic]:
    """Run type checking on a parsed program. Returns diagnostics."""
    checker = TypeChecker()
    return checker.check(program)
