"""Recursive descent parser for Nyet.

Turns a token stream into an AST. Per PLAN.md §3: S-expressions make the
outer structure trivial — the hard part is dispatching on the head of each
list to the right sub-parser.

Usage:
    from pynyet.parser.parser import parse
    from pynyet.source import SourceFile
    from pynyet.lexer.scanner import lex

    sf = SourceFile("main.no", src)
    tokens = lex(sf)
    program = parse(tokens)   # list[Node]
"""

from __future__ import annotations

from typing import Optional

from pynyet.ast import nodes as N
from pynyet.diagnostic import Diagnostic, NyetError, Severity
from pynyet.lexer.token import Token, TokenKind
from pynyet.source import Span


# Primitive type keyword tokens → their string names.
_PRIM_TYPE_TOKENS: dict[TokenKind, str] = {
    TokenKind.I8: "i8",
    TokenKind.I16: "i16",
    TokenKind.I32: "i32",
    TokenKind.I64: "i64",
    TokenKind.U8: "u8",
    TokenKind.U16: "u16",
    TokenKind.U32: "u32",
    TokenKind.U64: "u64",
    TokenKind.USIZE: "usize",
    TokenKind.F32: "f32",
    TokenKind.F64: "f64",
    TokenKind.BOOL_T: "bool",
    TokenKind.STRING_T: "string",
    TokenKind.UNIT_T: "unit",
}


class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.pos = 0

    # ---------------------------------------------------------------
    # cursor helpers
    # ---------------------------------------------------------------

    def _eof(self) -> bool:
        return self.pos >= len(self.tokens) or self.tokens[self.pos].kind is TokenKind.EOF

    def _peek(self) -> Token:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return self.tokens[-1]  # EOF sentinel

    def _advance(self) -> Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def _expect(self, kind: TokenKind) -> Token:
        tok = self._peek()
        if tok.kind is not kind:
            raise self._error(f"expected {kind.name}, got {tok.kind.name}", tok.span)
        return self._advance()

    def _at(self, *kinds: TokenKind) -> bool:
        return self._peek().kind in kinds

    def _span_from(self, start: Token) -> Span:
        end = self.tokens[max(0, self.pos - 1)]
        return start.span.merge(end.span)

    def _error(self, msg: str, span: Span) -> NyetError:
        return NyetError(Diagnostic(Severity.ERROR, msg, span))

    # ---------------------------------------------------------------
    # top level
    # ---------------------------------------------------------------

    def parse_program(self) -> list[N.Node]:
        items: list[N.Node] = []
        errors: list[Diagnostic] = []
        while not self._eof():
            try:
                items.append(self.parse_expr())
            except NyetError as e:
                errors.extend(e.diagnostics)
                self._synchronize()
        if errors:
            raise NyetError(errors)
        return items

    def _synchronize(self) -> None:
        """Skip tokens until the start of the next top-level form after an error."""
        depth = 0
        while not self._eof():
            tok = self._peek()
            if tok.kind is TokenKind.LPAREN:
                if depth == 0:
                    return  # positioned at next top-level form
                depth += 1
                self._advance()
            elif tok.kind is TokenKind.RPAREN:
                self._advance()
                depth -= 1
                if depth <= 0:
                    return
            else:
                self._advance()

    # ---------------------------------------------------------------
    # expressions (the core dispatch)
    # ---------------------------------------------------------------

    def parse_expr(self) -> N.Node:
        expr = self._parse_expr_inner()
        # Postfix `...` marks a splice (variadic macro arg). Only meaningful
        # inside macro bodies; surviving Splices error during expansion.
        if self._at(TokenKind.ELLIPSIS):
            ell = self._advance()
            expr = N.Splice(expr.span.merge(ell.span), expr)
        return expr

    def _parse_expr_inner(self) -> N.Node:
        tok = self._peek()

        if tok.kind is TokenKind.LPAREN:
            return self._parse_paren_expr()
        if tok.kind is TokenKind.LBRACKET:
            return self._parse_array_lit()
        if tok.kind is TokenKind.HASH_LPAREN:
            return self._parse_tuple_lit()
        if tok.kind is TokenKind.LBRACE:
            return self._parse_map_lit()
        # Prefix borrow: &expr or &!expr
        if tok.kind is TokenKind.AMP:
            self._advance()
            mutable = False
            if self._at(TokenKind.BANG):
                self._advance()
                mutable = True
            inner = self.parse_expr()
            op = N.Ident(tok.span, "&!" if mutable else "&")
            return N.Call(self._span_from(tok), op, [inner])
        if tok.kind is TokenKind.AMP_BANG:
            self._advance()
            inner = self.parse_expr()
            op = N.Ident(tok.span, "&!")
            return N.Call(self._span_from(tok), op, [inner])
        return self._parse_atom()

    def _parse_atom(self) -> N.Expr:
        tok = self._peek()

        if tok.kind is TokenKind.INT_LIT:
            self._advance()
            return N.IntLit(tok.span, tok.value)
        if tok.kind is TokenKind.FLOAT_LIT:
            self._advance()
            return N.FloatLit(tok.span, tok.value)
        if tok.kind is TokenKind.STRING_LIT:
            self._advance()
            return N.StringLit(tok.span, tok.value)
        if tok.kind is TokenKind.BOOL_LIT:
            self._advance()
            return N.BoolLit(tok.span, tok.value)
        if tok.kind is TokenKind.KEYWORD_LIT:
            self._advance()
            return N.KeywordLit(tok.span, tok.value)
        if tok.kind is TokenKind.UNDERSCORE:
            self._advance()
            return N.Ident(tok.span, "_")
        if tok.kind is TokenKind.IDENT:
            self._advance()
            name = tok.value
            if "/" in name:
                return N.Path(tok.span, name.split("/"))
            return N.Ident(tok.span, name)
        if tok.kind is TokenKind.SELF:
            self._advance()
            return N.Ident(tok.span, "self")
        if tok.kind is TokenKind.SELF_TYPE:
            self._advance()
            return N.Ident(tok.span, "Self")
        # Type keyword used as value (rare but legal)
        if tok.kind in _PRIM_TYPE_TOKENS:
            self._advance()
            return N.Ident(tok.span, _PRIM_TYPE_TOKENS[tok.kind])
        # Bare `...` as a placeholder/TODO body (e.g. `(fn foo () ...)`).
        # A postfix `...` after another expression is handled in `parse_expr`
        # as a Splice — bare standalone form means "to be filled in".
        if tok.kind is TokenKind.ELLIPSIS:
            self._advance()
            return N.Pass(tok.span)
        # Spread operator ..ident
        if tok.kind is TokenKind.DOT_DOT:
            self._advance()
            return N.Ident(tok.span, "..")
        # Bare operators as function values: (fold + 0 arr)
        if tok.kind in _OPERATOR_TOKENS:
            self._advance()
            return N.Ident(tok.span, _OPERATOR_TOKENS[tok.kind])
        # IO keywords as identifiers (channel names)
        if tok.kind in (TokenKind.OUT, TokenKind.IN, TokenKind.ERR):
            self._advance()
            return N.Ident(tok.span, tok.kind.name.lower())
        # Bare keywords as expressions
        if tok.kind is TokenKind.PASS:
            self._advance()
            return N.Pass(tok.span)
        if tok.kind is TokenKind.BREAK:
            self._advance()
            return N.Break(tok.span)
        if tok.kind is TokenKind.RETURN:
            self._advance()
            return N.Return(tok.span)

        raise self._error(f"unexpected token {tok.kind.name}", tok.span)

    # ---------------------------------------------------------------
    # parenthesized expressions — the big dispatch
    # ---------------------------------------------------------------

    def _parse_paren_expr(self) -> N.Node:
        lparen = self._expect(TokenKind.LPAREN)
        head = self._peek()

        # Empty parens → unit literal
        if head.kind is TokenKind.RPAREN:
            self._advance()
            return N.UnitLit(self._span_from(lparen))

        # Dispatch on special form keywords
        handler = _SPECIAL_FORMS.get(head.kind)
        if handler is not None:
            return handler(self, lparen)

        # Otherwise: function call — (head arg1 arg2 ...)
        return self._parse_call(lparen)

    def _parse_call(self, lparen: Token) -> N.Call:
        head = self.parse_expr()
        args: list[N.Expr] = []
        while not self._at(TokenKind.RPAREN):
            # Keyword argument: name:value (e.g. x:0.0 in struct constructors)
            if (self._at(TokenKind.IDENT)
                    and self.pos + 1 < len(self.tokens)
                    and self.tokens[self.pos + 1].kind is TokenKind.COLON):
                name_tok = self._advance()  # consume ident
                self._advance()  # consume colon
                value = self.parse_expr()
                args.append(N.KeywordArg(
                    name_tok.span.merge(value.span), name_tok.value, value))
            else:
                args.append(self.parse_expr())
        self._expect(TokenKind.RPAREN)
        return N.Call(self._span_from(lparen), head, args)

    # ---------------------------------------------------------------
    # special forms
    # ---------------------------------------------------------------

    def _parse_let(self, lparen: Token) -> N.LetDecl:
        self._advance()  # consume 'let'
        return self._parse_binding(lparen, mutable=False)

    def _parse_var(self, lparen: Token) -> N.LetDecl:
        self._advance()  # consume 'var'
        return self._parse_binding(lparen, mutable=True)

    def _parse_binding(self, lparen: Token, mutable: bool) -> N.Node:
        # Destructuring: (let #(a b) expr)
        if self._at(TokenKind.HASH_LPAREN):
            _pat = self.parse_expr()
            value = self.parse_expr()
            self._expect(TokenKind.RPAREN)
            return N.LetDecl(self._span_from(lparen), "_destructure", None, value, mutable)

        name, ty = self._parse_name_maybe_type()
        value = self.parse_expr()
        # Optional body (let-in pattern): (let name value body...)
        if not self._at(TokenKind.RPAREN):
            body_exprs: list[N.Expr] = []
            while not self._at(TokenKind.RPAREN):
                body_exprs.append(self.parse_expr())
            let_decl = N.LetDecl(self._span_from(lparen), name, ty, value, mutable)
            self._expect(TokenKind.RPAREN)
            return N.Do(self._span_from(lparen), [let_decl] + body_exprs)
        self._expect(TokenKind.RPAREN)
        return N.LetDecl(self._span_from(lparen), name, ty, value, mutable)

    def _parse_const(self, lparen: Token) -> N.ConstDecl:
        self._advance()  # consume 'const'
        name, ty = self._parse_name_maybe_type()
        value = self.parse_expr()
        self._expect(TokenKind.RPAREN)
        return N.ConstDecl(self._span_from(lparen), name, ty, value)

    def _parse_fn(self, lparen: Token) -> N.Node:
        self._advance()  # consume 'fn' or 'fn!'
        # Named fn if next token is IDENT or operator (for operator overloading)
        if self._at(TokenKind.IDENT):
            return self._parse_fn_decl(lparen)
        if self._peek().kind in _OPERATOR_TOKENS:
            return self._parse_fn_decl_op(lparen)
        # () as function name: call operator (e.g. in Index trait)
        if (self._at(TokenKind.LPAREN)
                and self.pos + 1 < len(self.tokens)
                and self.tokens[self.pos + 1].kind is TokenKind.RPAREN
                and self.pos + 2 < len(self.tokens)
                and self.tokens[self.pos + 2].kind is TokenKind.LPAREN):
            self._advance()  # consume (
            self._advance()  # consume )
            return self._parse_fn_decl_named(lparen, "()")
        return self._parse_fn_expr(lparen)

    def _parse_fn_decl(self, lparen: Token) -> N.FnDecl:
        name = self._expect(TokenKind.IDENT).value
        generics = self._parse_optional_generics()
        params = self._parse_param_list()
        ret = self._parse_optional_return_type()
        body = self._parse_implicit_body(lparen)
        self._expect(TokenKind.RPAREN)
        return N.FnDecl(self._span_from(lparen), name, params, ret, body, generics)

    def _parse_fn_decl_named(self, lparen: Token, name: str) -> N.FnDecl:
        """Parse the rest of a named fn decl where the name is already known."""
        generics = self._parse_optional_generics()
        params = self._parse_param_list()
        ret = self._parse_optional_return_type()
        body = self._parse_implicit_body(lparen)
        self._expect(TokenKind.RPAREN)
        return N.FnDecl(self._span_from(lparen), name, params, ret, body, generics)

    def _parse_fn_decl_op(self, lparen: Token) -> N.FnDecl:
        """Parse a named fn where the name is an operator (e.g. fn + ...)."""
        op_tok = self._advance()
        name = _OPERATOR_TOKENS[op_tok.kind]
        generics = self._parse_optional_generics()
        params = self._parse_param_list()
        ret = self._parse_optional_return_type()
        body = self._parse_implicit_body(lparen)
        self._expect(TokenKind.RPAREN)
        return N.FnDecl(self._span_from(lparen), name, params, ret, body, generics)

    def _parse_implicit_body(self, lparen: Token) -> N.Expr | None:
        """Parse zero or more body expressions. Multiple → implicit do block."""
        if self._at(TokenKind.RPAREN):
            return None
        exprs: list[N.Expr] = []
        while not self._at(TokenKind.RPAREN):
            exprs.append(self.parse_expr())
        if len(exprs) == 1:
            return exprs[0]
        return N.Do(self._span_from(lparen), exprs)

    def _parse_fn_expr(self, lparen: Token) -> N.FnExpr:
        params = self._parse_param_list()
        ret = self._parse_optional_return_type()
        body = self._parse_implicit_body(lparen)
        self._expect(TokenKind.RPAREN)
        return N.FnExpr(self._span_from(lparen), params, ret, body)

    def _parse_move(self, lparen: Token) -> N.FnExpr:
        self._advance()  # consume 'move'
        self._expect(TokenKind.FN)
        params = self._parse_param_list()
        ret = self._parse_optional_return_type()
        body = self._parse_implicit_body(lparen)
        self._expect(TokenKind.RPAREN)
        return N.FnExpr(self._span_from(lparen), params, ret, body)

    def _parse_if(self, lparen: Token) -> N.If:
        self._advance()  # consume 'if'
        cond = self.parse_expr()
        then_branch = self.parse_expr()
        else_branch = None
        if not self._at(TokenKind.RPAREN):
            else_branch = self.parse_expr()
        self._expect(TokenKind.RPAREN)
        return N.If(self._span_from(lparen), cond, then_branch, else_branch)

    def _parse_match(self, lparen: Token) -> N.Match:
        self._advance()  # consume 'match'
        scrutinee = self.parse_expr()
        arms: list[N.MatchArm] = []
        while not self._at(TokenKind.RPAREN):
            arms.append(self._parse_match_arm())
        self._expect(TokenKind.RPAREN)
        return N.Match(self._span_from(lparen), scrutinee, arms)

    def _parse_match_arm(self) -> N.MatchArm:
        arm_start = self._expect(TokenKind.LPAREN)
        pattern = self._parse_pattern()
        if self._at(TokenKind.WHEN):
            self._advance()
            guard = self.parse_expr()
            body = self.parse_expr()
            self._expect(TokenKind.RPAREN)
            guarded = N.GuardedPat(pattern.span.merge(guard.span), pattern, guard)
            return N.MatchArm(self._span_from(arm_start), guarded, body)
        body = self.parse_expr()
        self._expect(TokenKind.RPAREN)
        return N.MatchArm(self._span_from(arm_start), pattern, body)

    def _parse_pattern(self) -> N.Pattern:
        tok = self._peek()
        if tok.kind is TokenKind.UNDERSCORE:
            self._advance()
            return N.WildPat(tok.span)
        if tok.kind is TokenKind.INT_LIT:
            self._advance()
            return N.LitPat(tok.span, N.IntLit(tok.span, tok.value))
        if tok.kind is TokenKind.FLOAT_LIT:
            self._advance()
            return N.LitPat(tok.span, N.FloatLit(tok.span, tok.value))
        if tok.kind is TokenKind.STRING_LIT:
            self._advance()
            return N.LitPat(tok.span, N.StringLit(tok.span, tok.value))
        if tok.kind is TokenKind.BOOL_LIT:
            self._advance()
            return N.LitPat(tok.span, N.BoolLit(tok.span, tok.value))
        if tok.kind is TokenKind.HASH_LPAREN:
            return self._parse_tuple_pat()
        if tok.kind is TokenKind.LPAREN:
            return self._parse_variant_pat()
        if tok.kind is TokenKind.IDENT:
            self._advance()
            name = tok.value
            if name[0].isupper():
                return N.VariantPat(tok.span, name, [])
            return N.VarPat(tok.span, name)
        raise self._error(f"unexpected token in pattern: {tok.kind.name}", tok.span)

    def _parse_variant_pat(self) -> N.Pattern:
        lp = self._expect(TokenKind.LPAREN)
        tok = self._peek()
        if tok.kind is TokenKind.IDENT and tok.value and tok.value[0].isupper():
            self._advance()
            name = tok.value
            args: list[N.Pattern] = []
            while not self._at(TokenKind.RPAREN) and not self._at(TokenKind.WHEN):
                args.append(self._parse_pattern())
            if self._at(TokenKind.RPAREN):
                self._expect(TokenKind.RPAREN)
            return N.VariantPat(self._span_from(lp), name, args)
        inner = self._parse_pattern()
        if self._at(TokenKind.RPAREN):
            self._expect(TokenKind.RPAREN)
        return inner

    def _parse_tuple_pat(self) -> N.TuplePat:
        start = self._expect(TokenKind.HASH_LPAREN)
        elements: list[N.Pattern] = []
        while not self._at(TokenKind.RPAREN):
            elements.append(self._parse_pattern())
        self._expect(TokenKind.RPAREN)
        return N.TuplePat(self._span_from(start), elements)

    def _parse_do(self, lparen: Token) -> N.Do:
        self._advance()
        exprs: list[N.Expr] = []
        while not self._at(TokenKind.RPAREN):
            exprs.append(self.parse_expr())
        self._expect(TokenKind.RPAREN)
        return N.Do(self._span_from(lparen), exprs)

    def _parse_loop(self, lparen: Token) -> N.Loop:
        self._advance()
        body = self._parse_implicit_body(lparen)
        self._expect(TokenKind.RPAREN)
        return N.Loop(self._span_from(lparen), body)

    def _parse_break(self, lparen: Token) -> N.Break:
        self._advance()
        value = None if self._at(TokenKind.RPAREN) else self.parse_expr()
        self._expect(TokenKind.RPAREN)
        return N.Break(self._span_from(lparen), value)

    def _parse_return(self, lparen: Token) -> N.Return:
        self._advance()
        value = None if self._at(TokenKind.RPAREN) else self.parse_expr()
        self._expect(TokenKind.RPAREN)
        return N.Return(self._span_from(lparen), value)

    def _parse_pass(self, lparen: Token) -> N.Pass:
        self._advance()
        self._expect(TokenKind.RPAREN)
        return N.Pass(self._span_from(lparen))

    def _parse_assign(self, lparen: Token) -> N.Assign:
        self._advance()
        target = self.parse_expr()
        value = self.parse_expr()
        self._expect(TokenKind.RPAREN)
        return N.Assign(self._span_from(lparen), target, value)

    def _parse_try(self, lparen: Token) -> N.Try:
        self._advance()
        value = self.parse_expr()
        self._expect(TokenKind.RPAREN)
        return N.Try(self._span_from(lparen), value)

    def _parse_await(self, lparen: Token) -> N.Await:
        self._advance()
        value = self.parse_expr()
        self._expect(TokenKind.RPAREN)
        return N.Await(self._span_from(lparen), value)

    def _parse_spawn(self, lparen: Token) -> N.Spawn:
        self._advance()
        value = self.parse_expr()
        self._expect(TokenKind.RPAREN)
        return N.Spawn(self._span_from(lparen), value)

    def _parse_quote(self, lparen: Token) -> N.Quote:
        self._advance()
        value = self.parse_expr()
        self._expect(TokenKind.RPAREN)
        return N.Quote(self._span_from(lparen), value)

    # ---------------------------------------------------------------
    # declarations: struct, type, newtype, alias, trait, impl, macro
    # ---------------------------------------------------------------

    def _parse_struct(self, lparen: Token) -> N.StructDecl:
        self._advance()
        name = self._expect(TokenKind.IDENT).value
        generics = self._parse_optional_generics()
        fields: list[N.Param] = []
        while not self._at(TokenKind.RPAREN):
            fields.append(self._parse_struct_field())
        self._expect(TokenKind.RPAREN)
        return N.StructDecl(self._span_from(lparen), name, fields, generics)

    def _parse_struct_field(self) -> N.Param:
        tok = self._peek()
        # (pub name:Type) form
        if tok.kind is TokenKind.LPAREN:
            save = self.pos
            lp = self._advance()
            if self._at(TokenKind.PUB):
                self._advance()
                name, ty = self._parse_name_maybe_type()
                self._expect(TokenKind.RPAREN)
                return N.Param(self._span_from(lp), name, ty)
            self.pos = save
        name, ty = self._parse_name_maybe_type()
        return N.Param(tok.span, name, ty)

    def _parse_type_decl(self, lparen: Token) -> N.TypeDecl:
        self._advance()
        name = self._expect(TokenKind.IDENT).value
        generics = self._parse_optional_generics()
        variants: list[tuple[str, list[N.TypeNode]]] = []
        while not self._at(TokenKind.RPAREN):
            variants.append(self._parse_variant_decl())
        self._expect(TokenKind.RPAREN)
        return N.TypeDecl(self._span_from(lparen), name, variants, generics)

    def _parse_variant_decl(self) -> tuple[str, list[N.TypeNode]]:
        self._expect(TokenKind.LPAREN)
        name = self._expect(TokenKind.IDENT).value
        fields: list[N.TypeNode] = []
        while not self._at(TokenKind.RPAREN):
            # Variant fields: name:Type or bare Type
            if (self._at(TokenKind.IDENT)
                    and self.pos + 1 < len(self.tokens)
                    and self.tokens[self.pos + 1].kind is TokenKind.COLON):
                self._advance()  # skip field name
                self._advance()  # skip colon
            fields.append(self._parse_type())
        self._expect(TokenKind.RPAREN)
        return (name, fields)

    def _parse_newtype(self, lparen: Token) -> N.NewtypeDecl:
        self._advance()
        name = self._expect(TokenKind.IDENT).value
        generics = self._parse_optional_generics()
        inner = self._parse_type()
        self._expect(TokenKind.RPAREN)
        return N.NewtypeDecl(self._span_from(lparen), name, inner, generics)

    def _parse_alias(self, lparen: Token) -> N.AliasDecl:
        self._advance()
        name = self._expect(TokenKind.IDENT).value
        generics = self._parse_optional_generics()
        target = self._parse_type()
        self._expect(TokenKind.RPAREN)
        return N.AliasDecl(self._span_from(lparen), name, target, generics)

    def _parse_trait(self, lparen: Token) -> N.TraitDecl:
        self._advance()
        name = self._expect(TokenKind.IDENT).value
        generics = self._parse_optional_generics()
        # Optional supertrait: `Trait: Super`
        if self._at(TokenKind.COLON):
            self._advance()
            _super = self._parse_type()  # consume, store later
        items: list[N.Decl] = []
        while not self._at(TokenKind.RPAREN):
            items.append(self.parse_expr())
        self._expect(TokenKind.RPAREN)
        return N.TraitDecl(self._span_from(lparen), name, items, generics)

    def _parse_impl(self, lparen: Token) -> N.ImplDecl:
        self._advance()
        first = self._parse_type()
        trait_type = None
        target_type = first
        # If next is IDENT or prim-type, first was the trait name.
        if self._at(TokenKind.IDENT, *_PRIM_TYPE_TOKENS.keys()):
            trait_type = first
            target_type = self._parse_type()
        items: list[N.Decl] = []
        while not self._at(TokenKind.RPAREN):
            items.append(self.parse_expr())
        self._expect(TokenKind.RPAREN)
        return N.ImplDecl(self._span_from(lparen), target_type, trait_type, items)

    def _parse_macro(self, lparen: Token) -> N.MacroDecl:
        self._advance()
        name = self._expect(TokenKind.IDENT).value
        params = self._parse_param_list()
        body = self.parse_expr()
        self._expect(TokenKind.RPAREN)
        return N.MacroDecl(self._span_from(lparen), name, params, body)

    def _parse_module(self, lparen: Token) -> N.ModuleDecl:
        self._advance()
        name = self._expect(TokenKind.IDENT).value
        self._expect(TokenKind.RPAREN)
        return N.ModuleDecl(self._span_from(lparen), name, [])

    def _parse_use(self, lparen: Token) -> N.UseDecl:
        self._advance()
        path_tok = self._expect(TokenKind.IDENT)
        path = path_tok.value.split("/")
        alias = None
        # Selective import: (use std/collections (Array Map Set))
        if self._at(TokenKind.LPAREN):
            self._advance()
            selected: list[str] = []
            while not self._at(TokenKind.RPAREN):
                selected.append(self._expect(TokenKind.IDENT).value)
            self._expect(TokenKind.RPAREN)
            alias = ",".join(selected)
        self._expect(TokenKind.RPAREN)
        return N.UseDecl(self._span_from(lparen), path, alias)

    def _parse_pub(self, lparen: Token) -> N.Node:
        self._advance()  # consume 'pub'
        head = self._peek()
        # pub modifies a declaration keyword: dispatch directly
        handler = _SPECIAL_FORMS.get(head.kind)
        if handler is not None and handler is not Parser._parse_pub:
            return handler(self, lparen)
        # Fallback: pub wraps an expression
        inner = self.parse_expr()
        self._expect(TokenKind.RPAREN)
        return inner

    def _parse_async(self, lparen: Token) -> N.FnDecl:
        self._advance()
        self._expect(TokenKind.FN)
        name = self._expect(TokenKind.IDENT).value
        generics = self._parse_optional_generics()
        params = self._parse_param_list()
        ret = self._parse_optional_return_type()
        body = self._parse_implicit_body(lparen)
        self._expect(TokenKind.RPAREN)
        return N.FnDecl(self._span_from(lparen), name, params, ret, body, generics)

    # ---------------------------------------------------------------
    # compound literals
    # ---------------------------------------------------------------

    def _parse_array_lit(self) -> N.ArrayLit:
        start = self._expect(TokenKind.LBRACKET)
        elements: list[N.Expr] = []
        while not self._at(TokenKind.RBRACKET):
            elements.append(self.parse_expr())
        self._expect(TokenKind.RBRACKET)
        return N.ArrayLit(self._span_from(start), elements)

    def _parse_tuple_lit(self) -> N.TupleLit:
        start = self._expect(TokenKind.HASH_LPAREN)
        elements: list[N.Expr] = []
        while not self._at(TokenKind.RPAREN):
            elements.append(self.parse_expr())
        self._expect(TokenKind.RPAREN)
        return N.TupleLit(self._span_from(start), elements)

    def _parse_map_lit(self) -> N.MapLit:
        start = self._expect(TokenKind.LBRACE)
        entries: list[tuple[N.Expr, N.Expr]] = []
        while not self._at(TokenKind.RBRACE):
            key = self.parse_expr()
            value = self.parse_expr()
            entries.append((key, value))
        self._expect(TokenKind.RBRACE)
        return N.MapLit(self._span_from(start), entries)

    # ---------------------------------------------------------------
    # type expressions
    # ---------------------------------------------------------------

    def _parse_type(self) -> N.TypeNode:
        tok = self._peek()

        if tok.kind is TokenKind.AMP:
            self._advance()
            mutable = False
            if self._at(TokenKind.BANG):
                self._advance()
                mutable = True
            inner = self._parse_type()
            return N.RefType(self._span_from(tok), inner, mutable)

        if tok.kind is TokenKind.AMP_BANG:
            self._advance()
            inner = self._parse_type()
            return N.RefType(self._span_from(tok), inner, True)

        if tok.kind is TokenKind.HASH_LPAREN:
            self._advance()
            elements: list[N.TypeNode] = []
            while not self._at(TokenKind.RPAREN):
                elements.append(self._parse_type())
            self._expect(TokenKind.RPAREN)
            return N.TupleType(self._span_from(tok), elements)

        if tok.kind is TokenKind.LPAREN:
            self._advance()
            # () in type position is the unit type
            if self._at(TokenKind.RPAREN):
                self._advance()
                return N.UnitType(self._span_from(tok))
            if self._at(TokenKind.FN):
                self._advance()
                params: list[N.TypeNode] = []
                while not self._at(TokenKind.ARROW, TokenKind.RPAREN):
                    params.append(self._parse_type())
                ret = None
                if self._at(TokenKind.ARROW):
                    self._advance()
                    ret = self._parse_type()
                self._expect(TokenKind.RPAREN)
                return N.FnType(self._span_from(tok), params, ret)
            inner = self._parse_type()
            self._expect(TokenKind.RPAREN)
            return inner

        if tok.kind is TokenKind.DYN:
            self._advance()
            trait = self._parse_type()
            return N.DynType(self._span_from(tok), trait)

        if tok.kind is TokenKind.SELF_TYPE:
            self._advance()
            return N.SelfType(tok.span)

        if tok.kind is TokenKind.UNIT_T:
            self._advance()
            return N.UnitType(tok.span)

        if tok.kind in _PRIM_TYPE_TOKENS:
            self._advance()
            return N.PrimType(tok.span, _PRIM_TYPE_TOKENS[tok.kind])

        if tok.kind is TokenKind.IDENT:
            self._advance()
            base = N.NamedType(tok.span, tok.value)
            if self._at(TokenKind.LBRACKET):
                self._advance()
                args: list[N.TypeNode] = []
                while not self._at(TokenKind.RBRACKET):
                    args.append(self._parse_type())
                self._expect(TokenKind.RBRACKET)
                return N.GenericType(self._span_from(tok), base, args)
            return base

        if tok.kind is TokenKind.LBRACKET:
            self._advance()
            inner = self._parse_type()
            self._expect(TokenKind.RBRACKET)
            return N.GenericType(
                self._span_from(tok), N.NamedType(tok.span, "Array"), [inner]
            )

        raise self._error(f"unexpected token in type: {tok.kind.name}", tok.span)

    # ---------------------------------------------------------------
    # helpers: params, generics, name:type
    # ---------------------------------------------------------------

    def _parse_name_maybe_type(self) -> tuple[str, Optional[N.TypeNode]]:
        tok = self._peek()
        if tok.kind is TokenKind.IDENT:
            name = self._advance().value
        elif tok.kind is TokenKind.SELF:
            name = self._advance().text
        else:
            raise self._error(f"expected identifier, got {tok.kind.name}", tok.span)
        ty = None
        if self._at(TokenKind.COLON):
            self._advance()
            ty = self._parse_type()
        return name, ty

    def _parse_param_list(self) -> list[N.Param]:
        self._expect(TokenKind.LPAREN)
        params: list[N.Param] = []
        while not self._at(TokenKind.RPAREN):
            # Bare `...` at end of param list — accept and stop.
            if self._at(TokenKind.ELLIPSIS):
                self._advance()
                break
            start = self._peek()
            name, ty = self._parse_name_maybe_type()
            variadic = False
            # Variadic marker after a name: `name ...` (must come last).
            if self._at(TokenKind.ELLIPSIS):
                self._advance()
                variadic = True
            params.append(N.Param(self._span_from(start), name, ty, variadic=variadic))
            if variadic:
                break
        self._expect(TokenKind.RPAREN)
        return params

    def _parse_optional_generics(self) -> list[N.GenericParam]:
        if not self._at(TokenKind.LBRACKET):
            return []
        self._advance()
        params: list[N.GenericParam] = []
        while not self._at(TokenKind.RBRACKET):
            start = self._peek()
            name = self._expect(TokenKind.IDENT).value
            bounds: list[N.TypeNode] = []
            if self._at(TokenKind.COLON):
                self._advance()
                while self._at(TokenKind.IDENT) and not self._is_next_generic_param():
                    bounds.append(self._parse_type())
            params.append(N.GenericParam(self._span_from(start), name, bounds))
        self._expect(TokenKind.RBRACKET)
        return params

    def _is_next_generic_param(self) -> bool:
        if self.pos + 1 >= len(self.tokens):
            return False
        nxt = self.tokens[self.pos + 1]
        return nxt.kind in (TokenKind.COLON, TokenKind.RBRACKET)

    def _parse_optional_return_type(self) -> Optional[N.TypeNode]:
        if self._at(TokenKind.ARROW):
            self._advance()
            return self._parse_type()
        return None

    # ---------------------------------------------------------------
    # field access: (. obj field1 field2 ...)
    # ---------------------------------------------------------------

    def _parse_dot(self, lparen: Token) -> N.FieldAccess:
        self._advance()
        target = self.parse_expr()
        fields: list[str] = []
        while not self._at(TokenKind.RPAREN):
            fields.append(self._expect(TokenKind.IDENT).value)
        self._expect(TokenKind.RPAREN)
        result = target
        for f in fields:
            result = N.FieldAccess(self._span_from(lparen), result, f)
        return result

    def _parse_pipe(self, lparen: Token) -> N.Call:
        """(|> val f g h) desugars to (h (g (f val)))"""
        self._advance()
        val = self.parse_expr()
        stages: list[N.Expr] = []
        while not self._at(TokenKind.RPAREN):
            stages.append(self.parse_expr())
        self._expect(TokenKind.RPAREN)
        result = val
        for stage in stages:
            result = N.Call(self._span_from(lparen), stage, [result])
        return result

    # out/in/err — consume the keyword, then parse args like a regular call
    def _parse_builtin_call(self, lparen: Token) -> N.Call:
        kw = self._advance()  # consume out/in/err
        head = N.Ident(kw.span, kw.kind.name.lower())
        args: list[N.Expr] = []
        while not self._at(TokenKind.RPAREN):
            # Keyword argument: name:value
            if (self._at(TokenKind.IDENT)
                    and self.pos + 1 < len(self.tokens)
                    and self.tokens[self.pos + 1].kind is TokenKind.COLON):
                name_tok = self._advance()
                self._advance()  # colon
                value = self.parse_expr()
                args.append(N.KeywordArg(
                    name_tok.span.merge(value.span), name_tok.value, value))
            else:
                args.append(self.parse_expr())
        self._expect(TokenKind.RPAREN)
        return N.Call(self._span_from(lparen), head, args)


# -------------------------------------------------------------------
# Operator token → identifier name mapping
# -------------------------------------------------------------------

_OPERATOR_TOKENS: dict[TokenKind, str] = {
    TokenKind.PLUS: "+",
    TokenKind.MINUS: "-",
    TokenKind.STAR: "*",
    TokenKind.SLASH: "/",
    TokenKind.PERCENT: "%",
    TokenKind.EQ_EQ: "==",
    TokenKind.BANG_EQ: "!=",
    TokenKind.LT: "<",
    TokenKind.LT_EQ: "<=",
    TokenKind.GT: ">",
    TokenKind.GT_EQ: ">=",
    TokenKind.AND_AND: "&&",
    TokenKind.OR_OR: "||",
    TokenKind.BANG: "!",
    TokenKind.AMP: "&",
    TokenKind.BAR: "|",
    TokenKind.CARET: "^",
    TokenKind.TILDE: "~",
    TokenKind.LT_LT: "<<",
    TokenKind.GT_GT: ">>",
    TokenKind.EQ: "=",
    TokenKind.QUESTION: "?",
    TokenKind.DOT: ".",
    TokenKind.PIPE_ARROW: "|>",
}

# -------------------------------------------------------------------
# Special form dispatch table
# -------------------------------------------------------------------

_SPECIAL_FORMS: dict[TokenKind, any] = {
    TokenKind.LET: Parser._parse_let,
    TokenKind.VAR: Parser._parse_var,
    TokenKind.CONST: Parser._parse_const,
    TokenKind.FN: Parser._parse_fn,
    TokenKind.FN_BANG: Parser._parse_fn,
    TokenKind.MOVE: Parser._parse_move,
    TokenKind.IF: Parser._parse_if,
    TokenKind.MATCH: Parser._parse_match,
    TokenKind.DO: Parser._parse_do,
    TokenKind.LOOP: Parser._parse_loop,
    TokenKind.BREAK: Parser._parse_break,
    TokenKind.RETURN: Parser._parse_return,
    TokenKind.PASS: Parser._parse_pass,
    TokenKind.EQ: Parser._parse_assign,
    TokenKind.QUESTION: Parser._parse_try,
    TokenKind.AWAIT: Parser._parse_await,
    TokenKind.SPAWN: Parser._parse_spawn,
    TokenKind.QUOTE: Parser._parse_quote,
    TokenKind.STRUCT: Parser._parse_struct,
    TokenKind.TYPE: Parser._parse_type_decl,
    TokenKind.NEWTYPE: Parser._parse_newtype,
    TokenKind.ALIAS: Parser._parse_alias,
    TokenKind.TRAIT: Parser._parse_trait,
    TokenKind.IMPL: Parser._parse_impl,
    TokenKind.MACRO: Parser._parse_macro,
    TokenKind.MODULE: Parser._parse_module,
    TokenKind.USE: Parser._parse_use,
    TokenKind.PUB: Parser._parse_pub,
    TokenKind.ASYNC: Parser._parse_async,
    TokenKind.DOT: Parser._parse_dot,
    TokenKind.PIPE_ARROW: Parser._parse_pipe,
    TokenKind.OUT: Parser._parse_builtin_call,
    TokenKind.IN: Parser._parse_builtin_call,
    TokenKind.ERR: Parser._parse_builtin_call,
}


# -------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------


def parse(tokens: list[Token]) -> list[N.Node]:
    """Parse a token stream into a list of top-level AST nodes."""
    return Parser(tokens).parse_program()
