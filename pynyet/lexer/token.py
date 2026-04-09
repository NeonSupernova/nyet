"""Token kinds and Token class for the hand-written Nyet scanner."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Optional

from ..source import Span


class TokenKind(Enum):
    # Structural
    LPAREN = auto()
    RPAREN = auto()
    LBRACKET = auto()
    RBRACKET = auto()
    LBRACE = auto()
    RBRACE = auto()
    HASH_LPAREN = auto()
    COLON = auto()
    COMMA = auto()
    DOT_DOT = auto()
    ARROW = auto()

    # Operators — two-char
    EQ_EQ = auto()
    BANG_EQ = auto()
    LT_EQ = auto()
    GT_EQ = auto()
    LT_LT = auto()
    GT_GT = auto()
    AND_AND = auto()
    OR_OR = auto()
    AMP_BANG = auto()
    PIPE_ARROW = auto()

    # Operators — one-char
    PLUS = auto()
    MINUS = auto()
    STAR = auto()
    SLASH = auto()
    PERCENT = auto()
    EQ = auto()
    LT = auto()
    GT = auto()
    AMP = auto()
    BAR = auto()
    CARET = auto()
    TILDE = auto()
    BANG = auto()
    QUESTION = auto()
    DOT = auto()

    # Literals
    INT_LIT = auto()
    FLOAT_LIT = auto()
    STRING_LIT = auto()
    BOOL_LIT = auto()
    KEYWORD_LIT = auto()

    # Keywords
    LET = auto()
    VAR = auto()
    CONST = auto()
    FN = auto()
    FN_BANG = auto()
    MOVE = auto()
    IF = auto()
    MATCH = auto()
    WHEN = auto()
    DO = auto()
    LOOP = auto()
    BREAK = auto()
    RETURN = auto()
    PASS = auto()
    STRUCT = auto()
    TYPE = auto()
    NEWTYPE = auto()
    ALIAS = auto()
    TRAIT = auto()
    IMPL = auto()
    MACRO = auto()
    QUOTE = auto()
    MODULE = auto()
    USE = auto()
    PUB = auto()
    ASYNC = auto()
    AWAIT = auto()
    SPAWN = auto()
    SELF = auto()
    SELF_TYPE = auto()
    DYN = auto()
    OUT = auto()
    IN = auto()
    ERR = auto()

    # Type keywords
    I8 = auto()
    I16 = auto()
    I32 = auto()
    I64 = auto()
    U8 = auto()
    U16 = auto()
    U32 = auto()
    U64 = auto()
    USIZE = auto()
    F32 = auto()
    F64 = auto()
    BOOL_T = auto()
    STRING_T = auto()
    UNIT_T = auto()

    # Trivia (preserved for autodocs)
    COMMENT_INLINE = auto()
    COMMENT_BLOCK = auto()
    COMMENT_SECTION = auto()
    COMMENT_FILE = auto()

    # Meta
    IDENT = auto()
    UNDERSCORE = auto()
    EOF = auto()


# Reserved words → their token kind. Values are stored in the token's `value`
# field for literal-like keywords (true/false) but most keywords carry no value.
KEYWORDS: dict[str, TokenKind] = {
    "let": TokenKind.LET,
    "var": TokenKind.VAR,
    "const": TokenKind.CONST,
    "fn": TokenKind.FN,
    "fn!": TokenKind.FN_BANG,
    "move": TokenKind.MOVE,
    "if": TokenKind.IF,
    "match": TokenKind.MATCH,
    "when": TokenKind.WHEN,
    "do": TokenKind.DO,
    "loop": TokenKind.LOOP,
    "break": TokenKind.BREAK,
    "return": TokenKind.RETURN,
    "pass": TokenKind.PASS,
    "struct": TokenKind.STRUCT,
    "type": TokenKind.TYPE,
    "newtype": TokenKind.NEWTYPE,
    "alias": TokenKind.ALIAS,
    "trait": TokenKind.TRAIT,
    "impl": TokenKind.IMPL,
    "macro": TokenKind.MACRO,
    "quote": TokenKind.QUOTE,
    "module": TokenKind.MODULE,
    "use": TokenKind.USE,
    "pub": TokenKind.PUB,
    "async": TokenKind.ASYNC,
    "await": TokenKind.AWAIT,
    "spawn": TokenKind.SPAWN,
    "self": TokenKind.SELF,
    "Self": TokenKind.SELF_TYPE,
    "dyn": TokenKind.DYN,
    "out": TokenKind.OUT,
    "in": TokenKind.IN,
    "err": TokenKind.ERR,
    # Primitive type keywords
    "i8": TokenKind.I8,
    "i16": TokenKind.I16,
    "i32": TokenKind.I32,
    "i64": TokenKind.I64,
    "u8": TokenKind.U8,
    "u16": TokenKind.U16,
    "u32": TokenKind.U32,
    "u64": TokenKind.U64,
    "usize": TokenKind.USIZE,
    "f32": TokenKind.F32,
    "f64": TokenKind.F64,
    "bool": TokenKind.BOOL_T,
    "string": TokenKind.STRING_T,
    "unit": TokenKind.UNIT_T,
}


# Boolean literals are tokenized as BOOL_LIT, not as keywords, because they
# carry a value and behave like literals in the grammar.
BOOL_LITERALS: dict[str, bool] = {"true": True, "false": False}


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    span: Span
    value: Any = None
    # For numeric literals: the explicit suffix (e.g. "i64") or None.
    suffix: Optional[str] = None

    @property
    def text(self) -> str:
        return self.span.text()

    def __repr__(self) -> str:
        if self.value is None and self.suffix is None:
            return f"Token({self.kind.name} {self.text!r})"
        if self.suffix is not None:
            return f"Token({self.kind.name} {self.value!r}:{self.suffix})"
        return f"Token({self.kind.name} {self.value!r})"
