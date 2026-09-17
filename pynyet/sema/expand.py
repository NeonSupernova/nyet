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

The same prelude source also carries non-macro top-level declarations
(the ``IOChannel`` trait and its supporting ``Result``/``IOError``/
``IOMode``/``FileMode`` types) that are prepended to every program's
declaration list, ahead of the user's own -- so a user file that
declares its own same-named type (several examples already define
their own ``Result``) processes second and simply overwrites the
prelude symbol (`resolve.py`'s `Scope.define` has no duplicate-name
guard), the same "last one wins" shadowing the macro prelude already
relies on for ``assert``.
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
;; Standard prelude — built-in macros and declarations loaded before
;; every program.

;; (assert! cond) — abort with "assertion failed" if cond is false.
(macro assert (cond)
  (if (! cond) (panic "assertion failed") pass))

;; (do_all! forms ...) — evaluate each form in sequence.
;; Useful for macros that need to expand to multiple statements.
(macro do_all (forms ...)
  (do forms ...))

;; IOChannel — see main.no's "IO Channels" section. `out!`/`err!` are
;; thin macro sugar over the `io` builtin for the common (implicit
;; stdout/stderr) case; `io` itself, `in`, `FileIO`, and the builtin
;; `out`/`in`/`err` channel-value constants are compiler builtins
;; registered directly in resolve.py/typeck.py/emit.py, not expressed
;; here in Nyet source.

(macro out (msg) (io out msg))
(macro err (msg) (io err msg))

(type IOError (Other string))

;; write/read/close each get their own concrete Result-shaped type
;; rather than sharing one generic `Result[T E]` instantiated three
;; ways. When this was written, generic variant construction resolved
;; `(Variant val)` calls by variant name only (`_variant_ctors`,
;; pynyet/codegen/emit.py), so simultaneous instantiations of the same
;; generic sum type collided and silently miscompiled. Commit 3cb712f
;; since added a fallback that recovers the type args from the
;; enclosing fn's return type or a `let` annotation; the concrete
;; types were kept as-is rather than reworked onto a generic Result,
;; and still sidestep the problem entirely.

(type WriteResult (WriteOk usize) (WriteErr IOError))
(type ReadResult  (ReadOk string) (ReadErr IOError))
(type CloseResult (CloseOk) (CloseErr IOError))

(type IOMode (In) (Out) (Err))
(type FileMode (Read) (Write) (Append) (ReadWrite))

(trait IOChannel
  (fn write (self:&!Self data:string) -> WriteResult)
  (fn read  (self:&!Self)             -> ReadResult)
  (fn close (self:&!Self)             -> CloseResult))
"""

_prelude_program_cache: list[N.Node] | None = None
_prelude_macros_cache: dict[str, N.MacroDecl] | None = None


def _load_prelude_program() -> list[N.Node]:
    """Parse the prelude source once and return its top-level nodes."""
    global _prelude_program_cache
    if _prelude_program_cache is not None:
        return _prelude_program_cache
    # Local imports avoid a hard dependency at module import time and
    # sidestep the cycle pynyet.sema.expand → parser → ast nodes.
    from pynyet.lexer.scanner import lex
    from pynyet.parser.parser import parse
    from pynyet.source import SourceFile

    sf = SourceFile("<prelude>", PRELUDE_SOURCE)
    _prelude_program_cache = parse(lex(sf))
    return _prelude_program_cache


def _load_prelude_macros() -> dict[str, N.MacroDecl]:
    """Return the prelude's macro decls, keyed by their bare (un-banged) name."""
    global _prelude_macros_cache
    if _prelude_macros_cache is not None:
        return _prelude_macros_cache
    macros: dict[str, N.MacroDecl] = {}
    for node in _load_prelude_program():
        if isinstance(node, N.MacroDecl):
            macros[node.name] = node
    _prelude_macros_cache = macros
    return macros


def _load_prelude_decls() -> list[N.Node]:
    """Non-macro top-level prelude declarations (trait/type), prepended
    to every program ahead of the user's own -- see module docstring."""
    return [node for node in _load_prelude_program() if not isinstance(node, N.MacroDecl)]


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

        # Prepend the prelude's non-macro declarations (IOChannel and
        # friends) ahead of the user's own so a same-named user
        # declaration processed afterwards shadows it (see module
        # docstring). Deep-copied since the cached prelude AST is reused
        # across every compilation in this process and later passes
        # annotate nodes in place (e.g. `resolved_def_id`).
        prelude_decls = [copy.deepcopy(n) for n in _load_prelude_decls()]
        return prelude_decls + expanded

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

        # Macro calls require a trailing `!` at the call site (e.g.
        # `(assert! cond)`, not `(assert cond)`) so a macro invocation is
        # never visually confused with an ordinary call. Macros are always
        # *declared* under their bare name -- the bang belongs to the call,
        # not the definition -- so we strip it before the registry lookup.
        # A bang-suffixed call whose stripped name isn't a registered macro
        # (e.g. a real function literally named `insert!`) falls through
        # unchanged and resolves normally against that function; a bare
        # call to a macro's plain name (no bang) is no longer expanded at
        # all, which surfaces downstream as resolve.py's ordinary
        # "undefined name" error if the bang is forgotten.
        if (
            isinstance(node, N.Call)
            and isinstance(node.head, N.Ident)
            and node.head.name.endswith("!")
            and node.head.name[:-1] in self.macros
        ):
            return self._expand_macro_call(node, depth)

        self._walk_children(node, depth)
        return node

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

        assert isinstance(call.head, N.Ident)
        name = call.head.name
        macro_name = name[:-1] if name.endswith("!") else name
        macro = self.macros[macro_name]

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
    return expanded, expander.errors
