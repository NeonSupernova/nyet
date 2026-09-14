"""Macro expansion pass.

Walks the AST and replaces calls to user-defined macros with their
expansion. Substitution is positional: each parameter name is bound to
the corresponding argument expression, and every ``Ident`` in the macro
body whose name matches a parameter is replaced with a deep copy of
that argument.

Supported beyond the v0.7 core (per PLAN.md §5a / v0.8 milestone):

- **Variadic params.** A trailing ``name ...`` parameter captures all
  remaining arguments as a list. Inside the body, ``name ...`` (a
  postfix-ellipsis splice) flattens that list into the surrounding
  position — typically a call's argument list or a ``do`` block.
- **Quote.** ``(quote expr)`` inside a body renders ``expr`` (after
  substitution) as a Nyet source string via the pretty-printer, yielding
  a ``StringLit``. Useful for ``log/debug``-style macros that capture
  the original surface form of a value.
- **Hygiene.** ``let``/``var`` bindings introduced inside a macro body
  are renamed to fresh suffixed names before parameter substitution so
  they cannot capture (or be captured by) names from the call site —
  even when the call site supplies a variable with the same name as a
  body local. Each expansion site gets its own fresh names, so
  recursive macros work correctly.

Still missing:

- **Nested macro definitions.** Macros must appear at the top level.

Macros are erased from the output program after expansion. Diagnostics
(arity mismatches, redefinitions, expansion-depth overflow, splice
misuse) are collected and returned alongside the expanded program;
expansion does its best to continue past errors so later passes still
receive a usable AST.

A small prelude of built-in macros (currently just ``assert``) is
pre-registered before user macros, and can be shadowed by a user
``macro assert`` declaration without producing a redefinition error.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass

from pynyet.ast import nodes as N
from pynyet.ast.pretty import pretty
from pynyet.diagnostic import Diagnostic, Severity

MAX_MACRO_DEPTH = 64


# ----------------------------------------------------------------------
# prelude
# ----------------------------------------------------------------------
#
# Built-in macros lazily parsed from source text on first use. Storing
# the source (instead of a hand-built AST) keeps the prelude in the
# language's own surface syntax and avoids tracking AST shapes by hand.

PRELUDE_SOURCE = """\
;; Standard prelude — built-in macros loaded before every program.

;; (assert cond) — abort with "assertion failed" if cond is false.
(macro assert (cond)
  (if (! cond) (panic "assertion failed") pass))

;; (do_all forms ...) — evaluate each form in sequence.
;; Useful for macros that need to expand to multiple statements.
(macro do_all (forms ...)
  (do forms ...))
"""

_prelude_macros_cache: dict[str, N.MacroDecl] | None = None


def _load_prelude_macros() -> dict[str, N.MacroDecl]:
    """Parse the prelude source once and return its macro decls."""
    global _prelude_macros_cache
    if _prelude_macros_cache is not None:
        return _prelude_macros_cache
    # Local imports avoid a hard dependency at module import time and
    # sidestep the cycle pynyet.sema.expand → parser → ast nodes.
    from pynyet.lexer.scanner import lex
    from pynyet.parser.parser import parse
    from pynyet.source import SourceFile

    sf = SourceFile("<prelude>", PRELUDE_SOURCE)
    program = parse(lex(sf))
    macros: dict[str, N.MacroDecl] = {}
    for node in program:
        if isinstance(node, N.MacroDecl):
            macros[node.name] = node
    _prelude_macros_cache = macros
    return macros


# Sentinel used inside substitution to flatten a splice into the
# enclosing list-or-tuple field. Never escapes _substitute.
@dataclass
class _SpliceList:
    items: list


# Substitution binding values: a single node for regular params, or a
# list of nodes for variadic params.
_BindingValue = N.Node | list


class MacroExpander:
    """Collect macro definitions and expand calls to them."""

    def __init__(self) -> None:
        self.errors: list[Diagnostic] = []
        # Prelude macros come pre-registered. Tracking them separately
        # lets a user-defined macro shadow a prelude entry without
        # producing a redefinition error.
        self.macros: dict[str, N.MacroDecl] = dict(_load_prelude_macros())
        self._prelude_names: set[str] = set(self.macros.keys())
        # Counter for hygienic renaming. Increments per macro expansion
        # site so each invocation gets fresh local names.
        self._hyg_counter: int = 0

    def expand(self, program: list[N.Node]) -> list[N.Node]:
        for node in program:
            if isinstance(node, N.MacroDecl):
                if node.name in self.macros and node.name not in self._prelude_names:
                    self.errors.append(
                        Diagnostic(
                            Severity.ERROR,
                            f"macro '{node.name}' is already defined",
                            node.span,
                        )
                    )
                else:
                    self._validate_params(node)
                    self.macros[node.name] = node
                    self._prelude_names.discard(node.name)

        expanded: list[N.Node] = []
        for node in program:
            if isinstance(node, N.MacroDecl):
                continue
            new_node = self._expand_node(node, depth=0)
            if new_node is not None:
                expanded.append(new_node)
        return expanded

    def _validate_params(self, macro: N.MacroDecl) -> None:
        """A variadic param must be the last one in the list."""
        for i, p in enumerate(macro.params):
            if p.variadic and i != len(macro.params) - 1:
                self.errors.append(
                    Diagnostic(
                        Severity.ERROR,
                        f"variadic parameter '{p.name}' must be the last "
                        f"parameter of macro '{macro.name}'",
                        p.span,
                    )
                )

    # ------------------------------------------------------------------
    # core walk
    # ------------------------------------------------------------------

    def _expand_node(self, node: N.Node | None, depth: int) -> N.Node | None:
        if node is None:
            return None

        # `(quote expr)` outside a macro body: render to a source string.
        # Inside a macro body, _substitute handles it before we get here.
        if isinstance(node, N.Quote):
            inner = self._expand_node(node.value, depth)
            return N.StringLit(node.span, pretty(inner) if inner is not None else "()")

        # A leftover Splice means `...` was used outside a macro body or
        # against a non-variadic name. Preserve the inner expr so later
        # passes still see something sensible.
        if isinstance(node, N.Splice):
            self.errors.append(
                Diagnostic(
                    Severity.ERROR,
                    "`...` splice can only appear inside a macro body "
                    "referencing a variadic parameter",
                    node.span,
                )
            )
            return self._expand_node(node.value, depth)

        if isinstance(node, N.Call) and self._macro_name(node.head) in self.macros:
            return self._expand_macro_call(node, depth)

        self._walk_children(node, depth)
        return node

    @staticmethod
    def _macro_name(head: N.Node | None) -> str | None:
        """The macro name a call head refers to: a plain name like `unless`, or
        a slash name like `log/debug`, which the parser produces as a Path (so
        slash-named macros used to never expand)."""
        if isinstance(head, N.Ident):
            return head.name
        if isinstance(head, N.Path):
            return "/".join(head.segments)
        return None

    def _expand_macro_call(self, call: N.Call, depth: int) -> N.Node:
        if depth >= MAX_MACRO_DEPTH:
            self.errors.append(
                Diagnostic(
                    Severity.ERROR,
                    f"macro expansion exceeded depth {MAX_MACRO_DEPTH} "
                    "(possible infinite recursion)",
                    call.span,
                )
            )
            return call

        name = self._macro_name(call.head)
        assert name is not None
        macro = self.macros[name]

        params = macro.params
        variadic = params[-1] if params and params[-1].variadic else None
        fixed = params[:-1] if variadic is not None else params

        if variadic is not None:
            if len(call.args) < len(fixed):
                self.errors.append(
                    Diagnostic(
                        Severity.ERROR,
                        f"macro '{name}' expects at least {len(fixed)} "
                        f"argument(s), got {len(call.args)}",
                        call.span,
                    )
                )
                return call
        else:
            if len(call.args) != len(params):
                self.errors.append(
                    Diagnostic(
                        Severity.ERROR,
                        f"macro '{name}' expects {len(params)} argument(s), got {len(call.args)}",
                        call.span,
                    )
                )
                return call

        # Expand any macros that appear inside the arguments first so the
        # body sees fully-expanded forms.
        expanded_args: list[N.Node] = []
        for arg in call.args:
            new_arg = self._expand_node(arg, depth + 1)
            expanded_args.append(new_arg if new_arg is not None else arg)

        if macro.body is None:
            return N.UnitLit(call.span)

        bindings: dict[str, _BindingValue] = {}
        for p, a in zip(fixed, expanded_args[: len(fixed)], strict=False):
            bindings[p.name] = a
        if variadic is not None:
            bindings[variadic.name] = list(expanded_args[len(fixed) :])
        variadic_names = {variadic.name} if variadic is not None else set()

        cloned_body = copy.deepcopy(macro.body)

        # Hygiene: rename any `let`/`var` bindings introduced inside the
        # body to fresh names so they cannot collide with user names at
        # the call site (or with names appearing inside arguments).
        param_names = {p.name for p in macro.params}
        locals_ = _collect_local_bindings(cloned_body, param_names)
        if locals_:
            rename_map = {n: self._fresh_local(n) for n in locals_}
            _apply_rename(cloned_body, rename_map)

        substituted = self._substitute(cloned_body, bindings, variadic_names)
        if isinstance(substituted, _SpliceList):
            # A bare splice as the body produces a list of forms — wrap
            # them in a `do` so the result remains a single expression.
            substituted = N.Do(call.span, list(substituted.items))

        # The substituted result may itself contain macro calls.
        result = self._expand_node(substituted, depth + 1)
        return result if result is not None else call

    def _fresh_local(self, base: str) -> str:
        self._hyg_counter += 1
        return f"{base}__hyg{self._hyg_counter}"

    def _walk_children(self, node: N.Node, depth: int) -> None:
        if not is_dataclass(node):
            return
        # Macro bodies are templates; do not pre-expand them.
        if isinstance(node, N.MacroDecl):
            return
        for f in fields(node):
            if f.name == "span":
                continue
            value = getattr(node, f.name)
            new = _walk_value(value, lambda n: self._expand_node(n, depth))
            if new is not value:
                setattr(node, f.name, new)

    # ------------------------------------------------------------------
    # substitution
    # ------------------------------------------------------------------

    def _substitute(
        self,
        node: N.Node | None,
        bindings: dict[str, _BindingValue],
        variadic_names: set[str],
    ) -> N.Node | _SpliceList | None:
        if node is None:
            return None

        # `(quote expr)`: substitute inside expr, then render to string.
        if isinstance(node, N.Quote):
            inner = self._substitute(node.value, bindings, variadic_names)
            if isinstance(inner, _SpliceList):
                # Quote of a splice doesn't really make sense; fall back
                # to rendering the items joined by spaces.
                text = " ".join(pretty(it) for it in inner.items)
            else:
                text = pretty(inner) if inner is not None else "()"
            return N.StringLit(node.span, text)

        # `expr ...` splice. The only well-formed shape is `name ...`
        # where `name` is a variadic parameter.
        if isinstance(node, N.Splice):
            inner = node.value
            if (
                isinstance(inner, N.Ident)
                and inner.name in variadic_names
                and inner.name in bindings
            ):
                items = bindings[inner.name]
                assert isinstance(items, list)
                return _SpliceList([copy.deepcopy(it) for it in items])
            self.errors.append(
                Diagnostic(
                    Severity.ERROR,
                    "`...` splice must reference a variadic macro parameter",
                    node.span,
                )
            )
            return self._substitute(inner, bindings, variadic_names)

        # Reference to a macro parameter.
        if isinstance(node, N.Ident) and node.name in bindings:
            if node.name in variadic_names:
                self.errors.append(
                    Diagnostic(
                        Severity.ERROR,
                        f"variadic parameter '{node.name}' must be spliced with `...`",
                        node.span,
                    )
                )
                return node
            value = bindings[node.name]
            assert isinstance(value, N.Node)
            return copy.deepcopy(value)

        if not is_dataclass(node):
            return node

        for f in fields(node):
            if f.name == "span":
                continue
            value = getattr(node, f.name)
            new = self._walk_subst(value, bindings, variadic_names)
            if new is not value:
                setattr(node, f.name, new)
        return node

    def _walk_subst(
        self,
        value,
        bindings: dict[str, _BindingValue],
        variadic_names: set[str],
    ):
        if isinstance(value, list):
            new_list: list = []
            changed = False
            for item in value:
                new_item = self._walk_subst(item, bindings, variadic_names)
                if isinstance(new_item, _SpliceList):
                    new_list.extend(new_item.items)
                    changed = True
                else:
                    new_list.append(new_item)
                    if new_item is not item:
                        changed = True
            return new_list if changed else value
        if isinstance(value, tuple):
            new_items: list = []
            changed = False
            for item in value:
                new_item = self._walk_subst(item, bindings, variadic_names)
                if isinstance(new_item, _SpliceList):
                    new_items.extend(new_item.items)
                    changed = True
                else:
                    new_items.append(new_item)
                    if new_item is not item:
                        changed = True
            return tuple(new_items) if changed else value
        if isinstance(value, N.Node):
            return self._substitute(value, bindings, variadic_names)
        return value


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _walk_value(value, fn: Callable[[N.Node], N.Node | None]):
    """Recurse into containers. Apply ``fn`` to every Node encountered."""
    if isinstance(value, list):
        new_list: list = []
        changed = False
        for item in value:
            new_item = _walk_value(item, fn)
            new_list.append(new_item)
            if new_item is not item:
                changed = True
        return new_list if changed else value
    if isinstance(value, tuple):
        new_tuple = tuple(_walk_value(item, fn) for item in value)
        return new_tuple
    if isinstance(value, N.Node):
        return fn(value)
    return value


def _collect_local_bindings(node: N.Node | None, exclude: set[str]) -> set[str]:
    """Collect names of `let`/`var` bindings inside ``node`` whose names
    are not in ``exclude`` (typically the macro's own parameter names).

    Used by hygiene to find macro-introduced locals that need fresh names.
    """
    found: set[str] = set()

    def walk(n: N.Node | None) -> None:
        if n is None:
            return
        if isinstance(n, N.LetDecl) and n.name not in exclude:
            found.add(n.name)
        if not is_dataclass(n):
            return
        for f in fields(n):
            if f.name == "span":
                continue
            value = getattr(n, f.name)
            _walk_collect(value, walk)

    walk(node)
    return found


def _walk_collect(value, walk_fn) -> None:
    if isinstance(value, list):
        for item in value:
            _walk_collect(item, walk_fn)
    elif isinstance(value, tuple):
        for item in value:
            _walk_collect(item, walk_fn)
    elif isinstance(value, N.Node):
        walk_fn(value)


def _apply_rename(node: N.Node | None, rename_map: dict[str, str]) -> None:
    """Rename Ident references and LetDecl declarations in-place.

    Both the binding site (LetDecl.name) and every Ident referencing the
    same name receive the fresh name. Lexical scoping inside the body is
    preserved because the same name maps to the same fresh name
    everywhere — nested shadowing collapses to a single fresh name, but
    the resolver still picks the closest enclosing binding.
    """
    if node is None:
        return
    if isinstance(node, N.Ident) and node.name in rename_map:
        node.name = rename_map[node.name]
        return
    if isinstance(node, N.LetDecl) and node.name in rename_map:
        node.name = rename_map[node.name]
    if not is_dataclass(node):
        return
    for f in fields(node):
        if f.name == "span":
            continue
        value = getattr(node, f.name)
        _walk_rename(value, rename_map)


def _walk_rename(value, rename_map: dict[str, str]) -> None:
    if isinstance(value, list):
        for item in value:
            _walk_rename(item, rename_map)
    elif isinstance(value, tuple):
        for item in value:
            _walk_rename(item, rename_map)
    elif isinstance(value, N.Node):
        _apply_rename(value, rename_map)


def expand_macros(program: list[N.Node]) -> tuple[list[N.Node], list[Diagnostic]]:
    """Run macro expansion on a parsed program.

    Returns the expanded program (with ``MacroDecl`` nodes removed) and
    a list of diagnostics. Errors do not abort expansion: the offending
    call is left in place and other expansions proceed.
    """
    expander = MacroExpander()
    expanded = expander.expand(program)
    expanded = _desugar_aliases_and_newtypes(expanded)
    return _apply_trait_default_methods(expanded), expander.errors


def _desugar_aliases_and_newtypes(program: list[N.Node]) -> list[N.Node]:
    """Lower `alias` and `newtype` declarations to forms every later pass
    already understands.

    - `(alias Duration f64)`: every `Duration` type is replaced by `f64`, so
      the alias and its target are the same type everywhere.
    - `(newtype Meters f64)`: becomes `(struct Meters value:f64)` plus
      `(impl Index Meters (fn () (self:&Meters idx:i32) -> f64 (. self value)))`,
      so `(Meters 3.0)` wraps and `(m 0)` unwraps -- main.no documents
      newtypes as implementing `Index[i32 InnerType]`.

    Previously both parsed but no pass handled them: an alias fell back to
    codegen's default `i32`, and a newtype had no constructor. Generic
    aliases/newtypes are left alone.
    """
    aliases = {
        n.name: n.target
        for n in program
        if isinstance(n, N.AliasDecl) and not n.generics and n.target is not None
    }
    result: list[N.Node] = []
    for node in program:
        if isinstance(node, N.AliasDecl) and node.name in aliases:
            continue
        if isinstance(node, N.NewtypeDecl) and not node.generics and node.inner is not None:
            span = node.span
            struct = N.StructDecl(
                span, name=node.name, fields=[N.Param(span, "value", copy.deepcopy(node.inner))]
            )
            unwrap = N.FnDecl(
                span,
                name="()",
                params=[
                    N.Param(span, "self", N.RefType(span, N.NamedType(span, node.name))),
                    N.Param(span, "idx", N.PrimType(span, "i32")),
                ],
                return_type=copy.deepcopy(node.inner),
                body=N.FieldAccess(span, N.Ident(span, "self"), "value"),
            )
            impl = N.ImplDecl(
                span,
                target=N.NamedType(span, node.name),
                trait=N.NamedType(span, "Index"),
                items=[unwrap],
            )
            for decl in (struct, impl):
                decl.is_public = node.is_public
                decl.module = node.module
            result.extend([struct, impl])
            continue
        result.append(node)
    # Resolve alias-to-alias chains (bounded, in case of a cycle).
    for _ in range(8):
        changed = False
        for name, target in list(aliases.items()):
            if isinstance(target, N.NamedType) and target.name in aliases:
                aliases[name] = aliases[target.name]
                changed = True
        if not changed:
            break
    for node in result:
        _replace_named_types(node, aliases)
    return result


def _replace_named_types(node: object, replacements: dict[str, N.TypeNode]) -> None:
    """Replace every `NamedType` whose name is in `replacements` under `node`
    with a copy of its replacement, in place."""
    if not isinstance(node, N.Node):
        return
    for f in fields(node):
        value = getattr(node, f.name)
        if isinstance(value, N.NamedType) and value.name in replacements:
            setattr(node, f.name, copy.deepcopy(replacements[value.name]))
        elif isinstance(value, list):
            for i, elem in enumerate(value):
                if isinstance(elem, N.NamedType) and elem.name in replacements:
                    value[i] = copy.deepcopy(replacements[elem.name])
                elif isinstance(elem, tuple):
                    for sub in elem:
                        _replace_named_types(sub, replacements)
                else:
                    _replace_named_types(elem, replacements)
        else:
            _replace_named_types(value, replacements)


def _apply_trait_default_methods(program: list[N.Node]) -> list[N.Node]:
    """Copy each trait method's default body into every impl that doesn't
    define that method itself.

    `(trait Greeter (fn greeting (self:&Self) -> string) (fn introduce
    (self:&Self) -> string (+ (greeting self) "!")))` gives `introduce` a
    default body; `(impl Greeter Person (fn greeting ...))` then gets its
    own copy of `introduce` with `Self` replaced by `Person`, so every
    later pass (resolve, typeck, borrow, codegen) just sees an ordinary
    impl method. Runs after macro expansion so macro-generated traits and
    impls are covered too.
    """
    traits = {n.name: n for n in program if isinstance(n, N.TraitDecl)}
    for node in program:
        if not isinstance(node, N.ImplDecl) or not isinstance(node.trait, N.NamedType):
            continue
        trait = traits.get(node.trait.name)
        if trait is None or node.target is None:
            continue
        defined = {item.name for item in node.items if isinstance(item, N.FnDecl)}
        for item in trait.items:
            if isinstance(item, N.FnDecl) and item.body is not None and item.name not in defined:
                method = copy.deepcopy(item)
                _replace_self_type(method, node.target)
                node.items.append(method)
    return program


def _is_self_type(value: object) -> bool:
    return isinstance(value, N.SelfType) or (
        isinstance(value, N.NamedType) and value.name == "Self"
    )


def _replace_self_type(node: object, target: N.TypeNode) -> None:
    """Replace every `Self` type under `node` with a copy of `target`, in place."""
    if not isinstance(node, N.Node):
        return
    for f in fields(node):
        value = getattr(node, f.name)
        if _is_self_type(value):
            setattr(node, f.name, copy.deepcopy(target))
        elif isinstance(value, list):
            for i, elem in enumerate(value):
                if _is_self_type(elem):
                    value[i] = copy.deepcopy(target)
                else:
                    _replace_self_type(elem, target)
        else:
            _replace_self_type(value, target)
