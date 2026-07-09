"""v0.1 AST node definitions.

Per PLAN.md section 4. Every node carries a ``Span`` (first field). Nodes are
defined with ``@dataclass`` (NOT frozen) so that semantic passes can populate
the annotation slots (``inferred_type``, ``resolved_def_id``, ``trait_impl``,
``monomorphized_name``, ``borrow_info``) in place. The design principle of
"immutable after construction" from PLAN.md is honored by convention: only the
annotation slots should ever be assigned to after the node is built; the
structural fields are treated as read-only.

Annotation slots use ``field(default=None, init=False, compare=False,
repr=False)`` so that they stay out of the constructor signature (callers
only pass the structural fields), don't participate in equality/hashing,
and don't clutter ``repr()`` or golden comparisons.

This module deliberately does not import from ``pynyet.ast.ast`` (the legacy
tree-walk interpreter). Consumers should import directly from
``pynyet.ast.nodes``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from pynyet.source import Span

# ---------------------------------------------------------------------------
# Base classes
# ---------------------------------------------------------------------------


@dataclass
class Node:
    """Root of every AST node. Carries a source span."""

    span: Span


@dataclass
class TypeNode(Node):
    """Base class for type-level AST nodes."""


@dataclass
class Pattern(Node):
    """Base class for pattern AST nodes."""


@dataclass
class Expr(Node):
    """Base class for expression AST nodes.

    Carries the ``inferred_type`` annotation slot populated by the type
    checker.
    """

    inferred_type: TypeNode | None = field(default=None, init=False, compare=False, repr=False)


@dataclass
class Decl(Node):
    """Base class for top-level / item-level declarations.

    v0.9 annotation slots:
      - ``is_public``: set to True when the decl was wrapped with ``pub``.
      - ``module``: name of the source module the decl was loaded from
        (set by the multi-file loader; ``None`` for the entry program).
    """

    is_public: bool = field(default=False, init=False, compare=False, repr=False)
    module: str | None = field(default=None, init=False, compare=False, repr=False)


# ---------------------------------------------------------------------------
# Support
# ---------------------------------------------------------------------------


class CaptureMode(Enum):
    BORROW = "borrow"
    BORROW_MUT = "borrow_mut"
    MOVE = "move"


@dataclass
class Param(Node):
    name: str
    type: TypeNode | None = None
    default: Expr | None = None
    variadic: bool = False


@dataclass
class GenericParam(Node):
    name: str
    bounds: list[TypeNode] = field(default_factory=list)
    default: TypeNode | None = None


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class PrimType(TypeNode):
    name: str  # e.g. "i32", "f64", "bool", "string"


@dataclass
class NamedType(TypeNode):
    name: str


@dataclass
class GenericType(TypeNode):
    base: TypeNode
    args: list[TypeNode] = field(default_factory=list)


@dataclass
class RefType(TypeNode):
    inner: TypeNode
    mutable: bool = False


@dataclass
class FnType(TypeNode):
    params: list[TypeNode] = field(default_factory=list)
    ret: TypeNode | None = None


@dataclass
class TupleType(TypeNode):
    elements: list[TypeNode] = field(default_factory=list)


@dataclass
class DynType(TypeNode):
    trait: TypeNode


@dataclass
class SelfType(TypeNode):
    pass


@dataclass
class UnitType(TypeNode):
    pass


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------


@dataclass
class WildPat(Pattern):
    pass


@dataclass
class VarPat(Pattern):
    name: str
    borrow_info: str | None = field(default=None, init=False, compare=False, repr=False)


@dataclass
class LitPat(Pattern):
    value: Expr


@dataclass
class VariantPat(Pattern):
    name: str
    args: list[Pattern] = field(default_factory=list)


@dataclass
class StructPat(Pattern):
    name: str
    fields: list[tuple[str, Pattern]] = field(default_factory=list)


@dataclass
class TuplePat(Pattern):
    elements: list[Pattern] = field(default_factory=list)


@dataclass
class GuardedPat(Pattern):
    inner: Pattern
    guard: Expr


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------


@dataclass
class IntLit(Expr):
    value: int = 0


@dataclass
class FloatLit(Expr):
    value: float = 0.0


@dataclass
class StringLit(Expr):
    value: str = ""


@dataclass
class BoolLit(Expr):
    value: bool = False


@dataclass
class UnitLit(Expr):
    pass


@dataclass
class KeywordLit(Expr):
    name: str = ""


@dataclass
class ArrayLit(Expr):
    elements: list[Expr] = field(default_factory=list)


@dataclass
class TupleLit(Expr):
    elements: list[Expr] = field(default_factory=list)


@dataclass
class MapLit(Expr):
    entries: list[tuple[Expr, Expr]] = field(default_factory=list)


@dataclass
class Ident(Expr):
    name: str = ""
    resolved_def_id: int | None = field(default=None, init=False, compare=False, repr=False)
    borrow_info: str | None = field(default=None, init=False, compare=False, repr=False)


@dataclass
class Path(Expr):
    segments: list[str] = field(default_factory=list)
    resolved_def_id: int | None = field(default=None, init=False, compare=False, repr=False)


@dataclass
class Call(Expr):
    head: Expr | None = None
    args: list[Expr] = field(default_factory=list)
    trait_impl: str | None = field(default=None, init=False, compare=False, repr=False)
    monomorphized_name: str | None = field(default=None, init=False, compare=False, repr=False)


@dataclass
class FieldAccess(Expr):
    target: Expr | None = None
    field_name: str = ""


@dataclass
class If(Expr):
    cond: Expr | None = None
    then_branch: Expr | None = None
    else_branch: Expr | None = None


@dataclass
class MatchArm(Node):
    pattern: Pattern
    body: Expr


@dataclass
class Match(Expr):
    scrutinee: Expr | None = None
    arms: list[MatchArm] = field(default_factory=list)


@dataclass
class Do(Expr):
    exprs: list[Expr] = field(default_factory=list)


@dataclass
class Loop(Expr):
    body: Expr | None = None


@dataclass
class Break(Expr):
    value: Expr | None = None


@dataclass
class Return(Expr):
    value: Expr | None = None


@dataclass
class Pass(Expr):
    pass


@dataclass
class Assign(Expr):
    target: Expr | None = None
    value: Expr | None = None
    borrow_info: str | None = field(default=None, init=False, compare=False, repr=False)


@dataclass
class FnExpr(Expr):
    params: list[Param] = field(default_factory=list)
    return_type: TypeNode | None = None
    body: Expr | None = None
    captures: list[tuple[str, CaptureMode]] = field(default_factory=list)


@dataclass
class Await(Expr):
    value: Expr | None = None


@dataclass
class Spawn(Expr):
    value: Expr | None = None


@dataclass
class Quote(Expr):
    value: Expr | None = None


@dataclass
class Splice(Expr):
    """``expr ...`` — splice a variadic macro argument into the surrounding list.

    Only meaningful inside a macro body. The expander replaces it with the
    bound list of argument nodes; if it survives expansion (e.g. used outside
    a macro, or referencing a non-variadic name), an error is reported.
    """

    value: Expr | None = None


@dataclass
class Try(Expr):
    value: Expr | None = None


@dataclass
class Cast(Expr):
    """Type cast: (as expr type).

    Explicit, primitive-only coercion — numeric widening/narrowing and
    char ↔ int conversions. Does not trigger implicit coercions.
    """

    value: Expr | None = None
    target_type: TypeNode | None = None


@dataclass
class KeywordArg(Expr):
    """Keyword argument in a call: ``name:value``."""

    name: str = ""
    value: Expr | None = None


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


@dataclass
class LetDecl(Decl):
    name: str = ""
    type: TypeNode | None = None
    value: Expr | None = None
    mutable: bool = False
    borrow_info: str | None = field(default=None, init=False, compare=False, repr=False)


@dataclass
class ConstDecl(Decl):
    name: str = ""
    type: TypeNode | None = None
    value: Expr | None = None


@dataclass
class FnDecl(Decl):
    name: str = ""
    params: list[Param] = field(default_factory=list)
    return_type: TypeNode | None = None
    body: Expr | None = None
    generics: list[GenericParam] = field(default_factory=list)


@dataclass
class StructDecl(Decl):
    name: str = ""
    fields: list[Param] = field(default_factory=list)
    generics: list[GenericParam] = field(default_factory=list)


@dataclass
class TypeDecl(Decl):
    name: str = ""
    variants: list[tuple[str, list[TypeNode]]] = field(default_factory=list)
    generics: list[GenericParam] = field(default_factory=list)


@dataclass
class NewtypeDecl(Decl):
    name: str = ""
    inner: TypeNode | None = None
    generics: list[GenericParam] = field(default_factory=list)


@dataclass
class AliasDecl(Decl):
    name: str = ""
    target: TypeNode | None = None
    generics: list[GenericParam] = field(default_factory=list)


@dataclass
class TraitDecl(Decl):
    name: str = ""
    items: list[Decl] = field(default_factory=list)
    generics: list[GenericParam] = field(default_factory=list)


@dataclass
class ImplDecl(Decl):
    target: TypeNode | None = None
    trait: TypeNode | None = None
    items: list[Decl] = field(default_factory=list)
    generics: list[GenericParam] = field(default_factory=list)


@dataclass
class MacroDecl(Decl):
    name: str = ""
    params: list[Param] = field(default_factory=list)
    body: Expr | None = None


@dataclass
class ModuleDecl(Decl):
    name: str = ""
    items: list[Decl] = field(default_factory=list)


@dataclass
class UseDecl(Decl):
    path: list[str] = field(default_factory=list)
    alias: str | None = None
