"""Macro expansion pass.

Walks the AST and replaces calls to user-defined macros with their
expansion. Substitution is positional: each parameter name is bound to
the corresponding argument expression, and every ``Ident`` in the macro
body whose name matches a parameter is replaced with a deep copy of
that argument.

This is the v0.7 minimal expander. Per PLAN.md §5a it intentionally
omits:

- **Hygiene.** A binding introduced inside the macro body shares the
  call site's namespace, so user names can capture macro-introduced
  ones (and vice versa). True hygiene is slated for v0.8.
- **Variadic parameters** (``...``).
- **Nested macro definitions.** Macros must appear at the top level.

Macros are erased from the output program after expansion. Diagnostics
(arity mismatches, redefinitions, expansion-depth overflow) are
collected and returned alongside the expanded program; expansion does
its best to continue past errors so later passes still receive a
usable AST.

A small prelude of built-in macros (currently just ``assert``) is
pre-registered before user macros, and can be shadowed by a user
``macro assert`` declaration without producing a redefinition error.
"""

from __future__ import annotations

import copy
from dataclasses import fields, is_dataclass
from typing import Callable, Optional

from pynyet.ast import nodes as N
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
"""

_prelude_macros_cache: Optional[dict[str, N.MacroDecl]] = None


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


class MacroExpander:
    """Collect macro definitions and expand calls to them."""

    def __init__(self) -> None:
        self.errors: list[Diagnostic] = []
        # Prelude macros come pre-registered. Tracking them separately
        # lets a user-defined macro shadow a prelude entry without
        # producing a redefinition error.
        self.macros: dict[str, N.MacroDecl] = dict(_load_prelude_macros())
        self._prelude_names: set[str] = set(self.macros.keys())

    def expand(self, program: list[N.Node]) -> list[N.Node]:
        for node in program:
            if isinstance(node, N.MacroDecl):
                if (node.name in self.macros
                        and node.name not in self._prelude_names):
                    self.errors.append(Diagnostic(
                        Severity.ERROR,
                        f"macro '{node.name}' is already defined",
                        node.span,
                    ))
                else:
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

    # ------------------------------------------------------------------
    # core walk
    # ------------------------------------------------------------------

    def _expand_node(self, node: Optional[N.Node], depth: int) -> Optional[N.Node]:
        if node is None:
            return None

        if (isinstance(node, N.Call)
                and isinstance(node.head, N.Ident)
                and node.head.name in self.macros):
            return self._expand_macro_call(node, depth)

        self._walk_children(node, depth)
        return node

    def _expand_macro_call(self, call: N.Call, depth: int) -> N.Node:
        if depth >= MAX_MACRO_DEPTH:
            self.errors.append(Diagnostic(
                Severity.ERROR,
                f"macro expansion exceeded depth {MAX_MACRO_DEPTH} "
                "(possible infinite recursion)",
                call.span,
            ))
            return call

        assert isinstance(call.head, N.Ident)
        name = call.head.name
        macro = self.macros[name]

        if len(call.args) != len(macro.params):
            self.errors.append(Diagnostic(
                Severity.ERROR,
                f"macro '{name}' expects {len(macro.params)} argument(s), "
                f"got {len(call.args)}",
                call.span,
            ))
            return call

        # Expand any macros that appear inside the arguments first.
        expanded_args: list[N.Node] = []
        for arg in call.args:
            new_arg = self._expand_node(arg, depth + 1)
            expanded_args.append(new_arg if new_arg is not None else arg)

        if macro.body is None:
            return N.UnitLit(call.span)

        bindings: dict[str, N.Node] = {
            p.name: a for p, a in zip(macro.params, expanded_args)
        }
        cloned_body = copy.deepcopy(macro.body)
        substituted = _substitute(cloned_body, bindings)
        # The substituted result may itself contain macro calls.
        result = self._expand_node(substituted, depth + 1)
        return result if result is not None else call

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


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _walk_value(value, fn: Callable[[N.Node], Optional[N.Node]]):
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


def _substitute(
    node: Optional[N.Node],
    bindings: dict[str, N.Node],
) -> Optional[N.Node]:
    """Replace Idents whose name is bound, deep-copying each substitution."""
    if node is None:
        return None
    if isinstance(node, N.Ident) and node.name in bindings:
        return copy.deepcopy(bindings[node.name])
    if not is_dataclass(node):
        return node
    for f in fields(node):
        if f.name == "span":
            continue
        value = getattr(node, f.name)
        new = _walk_value(value, lambda n: _substitute(n, bindings))
        if new is not value:
            setattr(node, f.name, new)
    return node


def expand_macros(program: list[N.Node]) -> tuple[list[N.Node], list[Diagnostic]]:
    """Run macro expansion on a parsed program.

    Returns the expanded program (with ``MacroDecl`` nodes removed) and
    a list of diagnostics. Errors do not abort expansion: the offending
    call is left in place and other expansions proceed.
    """
    expander = MacroExpander()
    expanded = expander.expand(program)
    return expanded, expander.errors
