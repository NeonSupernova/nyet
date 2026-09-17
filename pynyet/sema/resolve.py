"""Name resolution pass.

Walks the AST, builds scopes, and resolves every Ident/Path to its
declaration. Reports undefined names. Populates the ``resolved_def_id``
annotation slot on Ident and Path nodes.

For v0.1 we support:
- Top-level fn declarations
- let/var/const bindings (within fn bodies)
- Built-in names: out, in, err (IO builtins)
- Primitive type names resolved during type checking
"""

from __future__ import annotations

from dataclasses import dataclass

from pynyet.ast import nodes as N
from pynyet.diagnostic import Diagnostic, Severity


@dataclass
class Symbol:
    """An entry in a scope — a resolved name."""

    name: str
    node: N.Node
    def_id: int


class Scope:
    """A lexical scope mapping names to Symbols."""

    def __init__(self, parent: Scope | None = None) -> None:
        self.parent = parent
        self.symbols: dict[str, Symbol] = {}

    def define(self, name: str, node: N.Node, def_id: int) -> Symbol:
        sym = Symbol(name, node, def_id)
        self.symbols[name] = sym
        return sym

    def lookup(self, name: str) -> Symbol | None:
        if name in self.symbols:
            return self.symbols[name]
        if self.parent is not None:
            return self.parent.lookup(name)
        return None


# Built-in names that don't require declaration
BUILTINS = {
    "out",
    "in",
    "err",
    "fmt",
    "str",
    "len",
    "push",
    "pop",
    "append",
    "type",
    "print",
    "gensym",
    "parse",
    "panic",
    "http/get",
    "io/on",
    "io",
    "FileIO",
    "write",
    "read",
    "close",
    "file_open",
    "file_read_all",
    "file_write",
    "file_close",
    "map",
    "filter",
    "fold",
    "any",
    "all",
    "zip",
    "array_new",
    "now",
}

# Primitive type names valid in expression position (e.g. `(in i32)`)
PRIM_TYPE_NAMES = {
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
    "char",
    "string",
    "unit",
}


class NameResolver:
    """Walk the AST and resolve names to declarations."""

    def __init__(self) -> None:
        self._next_id = 0
        self.errors: list[Diagnostic] = []
        self.symbols: dict[int, Symbol] = {}  # def_id → Symbol

    def resolve(self, program: list[N.Node]) -> list[Diagnostic]:
        scope = Scope()
        # First pass: register all top-level declarations
        for node in program:
            self._register_top_level(node, scope)
        # Second pass: resolve bodies
        for node in program:
            self._resolve_node(node, scope)
        return self.errors

    def _fresh_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _register_top_level(self, node: N.Node, scope: Scope) -> None:
        if isinstance(node, N.FnDecl):
            did = self._fresh_id()
            sym = scope.define(node.name, node, did)
            self.symbols[did] = sym
        elif isinstance(node, N.StructDecl):
            did = self._fresh_id()
            sym = scope.define(node.name, node, did)
            self.symbols[did] = sym
        elif isinstance(node, N.TypeDecl):
            did = self._fresh_id()
            sym = scope.define(node.name, node, did)
            self.symbols[did] = sym
        elif isinstance(node, N.TraitDecl):
            did = self._fresh_id()
            sym = scope.define(node.name, node, did)
            self.symbols[did] = sym
        elif isinstance(node, N.ConstDecl):
            did = self._fresh_id()
            sym = scope.define(node.name, node, did)
            self.symbols[did] = sym
        elif isinstance(node, N.LetDecl):
            did = self._fresh_id()
            sym = scope.define(node.name, node, did)
            self.symbols[did] = sym
        elif isinstance(node, N.ImplDecl):
            # Hoist named (non-operator) methods to the top-level scope so
            # `(method receiver ...)` calls resolve. Operator methods stay
            # internal — the codegen built-in dispatch handles those names.
            for item in node.items:
                if isinstance(item, N.FnDecl) and item.name.isidentifier():
                    did = self._fresh_id()
                    sym = scope.define(item.name, item, did)
                    self.symbols[did] = sym

    def _resolve_node(self, node: N.Node | None, scope: Scope) -> None:
        if node is None:
            return

        if isinstance(node, N.FnDecl):
            inner = Scope(scope)
            for p in node.params:
                did = self._fresh_id()
                sym = inner.define(p.name, p, did)
                self.symbols[did] = sym
            if node.body is not None:
                self._resolve_node(node.body, inner)

        elif isinstance(node, N.LetDecl):
            if node.value is not None:
                self._resolve_node(node.value, scope)
            # Register in current scope (already done for top-level)
            if scope.lookup(node.name) is None:
                did = self._fresh_id()
                sym = scope.define(node.name, node, did)
                self.symbols[did] = sym

        elif isinstance(node, N.ConstDecl):
            if node.value is not None:
                self._resolve_node(node.value, scope)

        elif isinstance(node, N.Ident):
            name = node.name
            found_sym = scope.lookup(name)
            if found_sym is not None:
                node.resolved_def_id = found_sym.def_id
            elif name not in BUILTINS and name not in PRIM_TYPE_NAMES and not name[0:1].isupper():
                # Upper-case names might be type constructors (Ok, Some, etc.)
                # Operators (+, -, etc.) are also fine
                if name.isidentifier() and name not in {"self", "Self", "_"}:
                    self.errors.append(
                        Diagnostic(
                            Severity.ERROR,
                            f"undefined name '{name}'",
                            node.span,
                        )
                    )

        elif isinstance(node, N.Path):
            # Path like std/io — resolve first segment
            full = "/".join(node.segments)
            found_sym = scope.lookup(full)
            if found_sym is not None:
                node.resolved_def_id = found_sym.def_id

        elif isinstance(node, N.Call):
            self._resolve_node(node.head, scope)
            for arg in node.args:
                self._resolve_node(arg, scope)

        elif isinstance(node, N.KeywordArg):
            if node.value is not None:
                self._resolve_node(node.value, scope)

        elif isinstance(node, N.If):
            self._resolve_node(node.cond, scope)
            self._resolve_node(node.then_branch, scope)
            if node.else_branch is not None:
                self._resolve_node(node.else_branch, scope)

        elif isinstance(node, N.Do):
            inner = Scope(scope)
            for expr in node.exprs:
                self._resolve_node(expr, inner)
                # let/var in do block register in inner scope
                if isinstance(expr, N.LetDecl) and inner.lookup(expr.name) is None:
                    did = self._fresh_id()
                    sym = inner.define(expr.name, expr, did)
                    self.symbols[did] = sym

        elif isinstance(node, N.Match):
            self._resolve_node(node.scrutinee, scope)
            for arm in node.arms:
                arm_scope = Scope(scope)
                self._resolve_pattern(arm.pattern, arm_scope)
                self._resolve_node(arm.body, arm_scope)

        elif isinstance(node, N.Loop):
            self._resolve_node(node.body, scope)

        elif isinstance(node, N.Return):
            if node.value is not None:
                self._resolve_node(node.value, scope)

        elif isinstance(node, N.Break):
            if node.value is not None:
                self._resolve_node(node.value, scope)

        elif isinstance(node, N.Assign):
            self._resolve_node(node.target, scope)
            self._resolve_node(node.value, scope)

        elif isinstance(node, N.FieldAccess):
            self._resolve_node(node.target, scope)

        elif isinstance(node, N.FnExpr):
            inner = Scope(scope)
            for p in node.params:
                did = self._fresh_id()
                sym = inner.define(p.name, p, did)
                self.symbols[did] = sym
            if node.body is not None:
                self._resolve_node(node.body, inner)

        elif isinstance(node, N.ArrayLit):
            for elem in node.elements:
                self._resolve_node(elem, scope)

        elif isinstance(node, N.TupleLit):
            for elem in node.elements:
                self._resolve_node(elem, scope)

        elif isinstance(node, N.MapLit):
            for k, v in node.entries:
                self._resolve_node(k, scope)
                self._resolve_node(v, scope)

        elif isinstance(node, N.Try):
            self._resolve_node(node.value, scope)

        elif isinstance(node, N.Cast):
            # Resolve the expression being cast; target_type is a TypeNode,
            # not a name reference, so it doesn't need scope resolution.
            if node.value is not None:
                self._resolve_node(node.value, scope)

        elif isinstance(node, N.Splice):
            self._resolve_node(node.value, scope)

        elif isinstance(node, N.Quote):
            self._resolve_node(node.value, scope)

        elif isinstance(node, N.Await):
            self._resolve_node(node.value, scope)

        elif isinstance(node, N.Spawn):
            self._resolve_node(node.value, scope)

        elif isinstance(node, N.StructDecl):
            pass  # already registered

        elif isinstance(node, N.TypeDecl):
            pass

        elif isinstance(node, N.TraitDecl):
            for item in node.items:
                self._resolve_node(item, scope)

        elif isinstance(node, N.ImplDecl):
            for item in node.items:
                self._resolve_node(item, scope)

        elif isinstance(node, N.MacroDecl):
            inner = Scope(scope)
            for p in node.params:
                did = self._fresh_id()
                inner.define(p.name, p, did)
            if node.body is not None:
                self._resolve_node(node.body, inner)

    def _resolve_pattern(self, pat: N.Pattern, scope: Scope) -> None:
        if isinstance(pat, N.VarPat):
            did = self._fresh_id()
            sym = scope.define(pat.name, pat, did)
            self.symbols[did] = sym
        elif isinstance(pat, N.VariantPat):
            for arg in pat.args:
                self._resolve_pattern(arg, scope)
        elif isinstance(pat, N.TuplePat):
            for elem in pat.elements:
                self._resolve_pattern(elem, scope)
        elif isinstance(pat, N.GuardedPat):
            self._resolve_pattern(pat.inner, scope)
            self._resolve_node(pat.guard, scope)


def resolve_names(program: list[N.Node]) -> list[Diagnostic]:
    """Run name resolution on a parsed program. Returns diagnostics."""
    resolver = NameResolver()
    return resolver.resolve(program)
