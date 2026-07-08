"""S-expression pretty-printer for the v0.1 AST.

Emits Nyet source that round-trips through the lexer. Handlers are kept
deliberately minimal: enough structure to make output recognizable Nyet
and for debugging the parser.
"""

from __future__ import annotations

import json
from typing import Any


def pretty(node: Any) -> str:
    if node is None:
        return "()"
    handler = _HANDLERS.get(type(node).__name__)
    if handler is not None:
        return handler(node)
    return f"(?{type(node).__name__})"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _escape_string(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _list(items: list) -> str:
    return " ".join(pretty(i) for i in items)


def _type(t) -> str:
    if t is None:
        return ""
    return pretty_type(t)


def pretty_type(t) -> str:
    if t is None:
        return "()"
    cls = type(t).__name__
    if cls == "PrimType":
        return t.name
    if cls == "NamedType":
        return t.name
    if cls == "GenericType":
        base = pretty_type(t.base)
        args = " ".join(pretty_type(a) for a in t.args)
        return f"{base}[{args}]"
    if cls == "RefType":
        return ("&!" if t.mutable else "&") + pretty_type(t.inner)
    if cls == "FnType":
        ps = " ".join(pretty_type(p) for p in t.params)
        ret = f" -> {pretty_type(t.ret)}" if t.ret else ""
        return f"(fn {ps}{ret})"
    if cls == "TupleType":
        return f"#({' '.join(pretty_type(e) for e in t.elements)})"
    if cls == "DynType":
        return f"(dyn {pretty_type(t.trait)})"
    if cls == "SelfType":
        return "Self"
    if cls == "UnitType":
        return "unit"
    return f"(?{cls})"


def _param(p) -> str:
    ty = _type(getattr(p, "type", None))
    return f"{p.name}:{ty}" if ty else p.name


def _pat(p) -> str:
    if p is None:
        return "_"
    cls = type(p).__name__
    if cls == "WildPat":
        return "_"
    if cls == "VarPat":
        return p.name
    if cls == "LitPat":
        return pretty(p.value)
    if cls == "VariantPat":
        if p.args:
            return f"({p.name} {' '.join(_pat(a) for a in p.args)})"
        return f"({p.name})"
    if cls == "StructPat":
        body = " ".join(f"{n}:{_pat(v)}" for n, v in p.fields)
        return f"({p.name} {body})"
    if cls == "TuplePat":
        return f"#({' '.join(_pat(e) for e in p.elements)})"
    if cls == "GuardedPat":
        return f"({_pat(p.inner)} when {pretty(p.guard)})"
    return f"(?{cls})"


# ---------------------------------------------------------------------------
# Expression handlers
# ---------------------------------------------------------------------------


def _h_int(n):
    return str(n.value)


def _h_float(n):
    return repr(n.value)


def _h_string(n):
    return _escape_string(n.value)


def _h_bool(n):
    return "true" if n.value else "false"


def _h_unit(_n):
    return "()"


def _h_keyword(n):
    return f":{n.name}"


def _h_array(n):
    return f"[{_list(n.elements)}]"


def _h_tuple(n):
    return f"#({_list(n.elements)})"


def _h_map(n):
    body = " ".join(f"{pretty(k)} {pretty(v)}" for k, v in n.entries)
    return "{" + body + "}"


def _h_ident(n):
    return n.name


def _h_path(n):
    return "/".join(n.segments)


def _h_call(n):
    head = pretty(n.head)
    args = _list(n.args)
    return f"({head}{' ' + args if args else ''})"


def _h_field(n):
    return f"(. {pretty(n.target)} {n.field_name})"


def _h_if(n):
    parts = [pretty(n.cond), pretty(n.then_branch)]
    if n.else_branch is not None:
        parts.append(pretty(n.else_branch))
    return f"(if {' '.join(parts)})"


def _h_match(n):
    arms = " ".join(f"({_pat(a.pattern)} {pretty(a.body)})" for a in n.arms)
    return f"(match {pretty(n.scrutinee)} {arms})"


def _h_do(n):
    return f"(do {_list(n.exprs)})"


def _h_loop(n):
    return f"(loop {pretty(n.body)})"


def _h_break(n):
    return "(break)" if n.value is None else f"(break {pretty(n.value)})"


def _h_return(n):
    return "(return)" if n.value is None else f"(return {pretty(n.value)})"


def _h_pass(_n):
    return "(pass)"


def _h_assign(n):
    return f"(= {pretty(n.target)} {pretty(n.value)})"


def _h_fn_expr(n):
    params = " ".join(_param(p) for p in n.params)
    rt = _type(n.return_type)
    ret_str = f" -> {rt}" if rt else ""
    return f"(fn ({params}){ret_str} {pretty(n.body)})"


def _h_await(n):
    return f"(await {pretty(n.value)})"


def _h_spawn(n):
    return f"(spawn {pretty(n.value)})"


def _h_quote(n):
    return f"(quote {pretty(n.value)})"


def _h_splice(n):
    return f"{pretty(n.value)} ..."


def _h_try(n):
    return f"(try {pretty(n.value)})"


def _h_cast(n):
    return f"(as {pretty(n.value)} {pretty_type(n.target_type)})"


def _h_keyword_arg(n):
    return f"{n.name}:{pretty(n.value)}"


# ---------------------------------------------------------------------------
# Declaration handlers
# ---------------------------------------------------------------------------


def _h_let(n):
    ty = _type(n.type)
    name = f"{n.name}:{ty}" if ty else n.name
    kw = "var" if n.mutable else "let"
    return f"({kw} {name} {pretty(n.value)})"


def _h_const(n):
    ty = _type(n.type)
    name = f"{n.name}:{ty}" if ty else n.name
    return f"(const {name} {pretty(n.value)})"


def _h_fn_decl(n):
    generics = ""
    if n.generics:
        gp = " ".join(g.name for g in n.generics)
        generics = f" [{gp}]"
    params = " ".join(_param(p) for p in n.params)
    rt = _type(n.return_type)
    ret_str = f" -> {rt}" if rt else ""
    if n.body is None:
        return f"(fn {n.name}{generics} ({params}){ret_str})"
    return f"(fn {n.name}{generics} ({params}){ret_str} {pretty(n.body)})"


def _h_struct(n):
    generics = ""
    if n.generics:
        gp = " ".join(g.name for g in n.generics)
        generics = f"[{gp}]"
    fields_str = " ".join(_param(p) for p in n.fields)
    return f"(struct {n.name}{generics} {fields_str})"


def _h_type_decl(n):
    generics = ""
    if n.generics:
        gp = " ".join(g.name for g in n.generics)
        generics = f"[{gp}]"
    def _fmt_variant(name, types):
        if types:
            return f"({name} {' '.join(pretty_type(t) for t in types)})"
        return f"({name})"
    variants = " ".join(_fmt_variant(name, types) for name, types in n.variants)
    return f"(type {n.name}{generics} {variants})"


def _h_newtype(n):
    return f"(newtype {n.name} {pretty_type(n.inner)})"


def _h_alias(n):
    return f"(alias {n.name} {pretty_type(n.target)})"


def _h_trait(n):
    items = " ".join(pretty(i) for i in n.items)
    return f"(trait {n.name} {items})"


def _h_impl(n):
    items = " ".join(pretty(i) for i in n.items)
    head = pretty_type(n.target)
    if n.trait is not None:
        head = f"{pretty_type(n.trait)} {head}"
    return f"(impl {head} {items})"


def _h_macro(n):
    params = " ".join(_param(p) for p in n.params)
    return f"(macro {n.name} ({params}) {pretty(n.body)})"


def _h_module(n):
    items = " ".join(pretty(i) for i in n.items)
    return f"(module {n.name} {items})"


def _h_use(n):
    path = "/".join(n.path)
    if n.alias:
        # Selective imports stored as comma-joined names
        names = n.alias.split(",")
        return f"(use {path} ({' '.join(names)}))"
    return f"(use {path})"


_HANDLERS: dict[str, Any] = {
    # Expressions
    "IntLit": _h_int,
    "FloatLit": _h_float,
    "StringLit": _h_string,
    "BoolLit": _h_bool,
    "UnitLit": _h_unit,
    "KeywordLit": _h_keyword,
    "ArrayLit": _h_array,
    "TupleLit": _h_tuple,
    "MapLit": _h_map,
    "Ident": _h_ident,
    "Path": _h_path,
    "Call": _h_call,
    "FieldAccess": _h_field,
    "If": _h_if,
    "Match": _h_match,
    "Do": _h_do,
    "Loop": _h_loop,
    "Break": _h_break,
    "Return": _h_return,
    "Pass": _h_pass,
    "Assign": _h_assign,
    "FnExpr": _h_fn_expr,
    "Await": _h_await,
    "Spawn": _h_spawn,
    "Quote": _h_quote,
    "Splice": _h_splice,
    "Try": _h_try,
    "Cast": _h_cast,
    "KeywordArg": _h_keyword_arg,
    # Declarations
    "LetDecl": _h_let,
    "ConstDecl": _h_const,
    "FnDecl": _h_fn_decl,
    "StructDecl": _h_struct,
    "TypeDecl": _h_type_decl,
    "NewtypeDecl": _h_newtype,
    "AliasDecl": _h_alias,
    "TraitDecl": _h_trait,
    "ImplDecl": _h_impl,
    "MacroDecl": _h_macro,
    "ModuleDecl": _h_module,
    "UseDecl": _h_use,
}
