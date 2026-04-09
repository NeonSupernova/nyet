"""Hand-written scanner for Nyet source.

Turns a SourceFile into a list of Tokens. No dependencies on rply.
Implements v0.1 of the lexer plan from PLAN.md §2: enough of the token
set to lex the full main.no spec, with trivia preserved for autodocs.
"""

from __future__ import annotations

from typing import Optional

from ..diagnostic import Diagnostic, NyetError, Severity
from ..source import SourceFile, Span
from .token import BOOL_LITERALS, KEYWORDS, Token, TokenKind


# Characters that can appear inside an identifier body (after the leading char).
_IDENT_BODY = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_IDENT_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_DIGIT = set("0123456789")
_HEX_DIGIT = set("0123456789abcdefABCDEF")
_BIN_DIGIT = set("01")
_OCT_DIGIT = set("01234567")

# Valid numeric suffixes.
_NUMERIC_SUFFIXES = {
    "i8", "i16", "i32", "i64",
    "u8", "u16", "u32", "u64",
    "usize",
    "f32", "f64",
}


class Lexer:
    def __init__(self, file: SourceFile, *, keep_trivia: bool = False) -> None:
        self.file = file
        self.src = file.text
        self.pos = 0
        self.tokens: list[Token] = []
        self.keep_trivia = keep_trivia

    # ---------- cursor helpers ----------

    def _eof(self) -> bool:
        return self.pos >= len(self.src)

    def _peek(self, offset: int = 0) -> str:
        idx = self.pos + offset
        if idx >= len(self.src):
            return ""
        return self.src[idx]

    def _advance(self) -> str:
        ch = self.src[self.pos]
        self.pos += 1
        return ch

    def _match(self, expected: str) -> bool:
        if self._peek() == expected:
            self.pos += 1
            return True
        return False

    def _span(self, start: int) -> Span:
        return Span(self.file, start, self.pos)

    def _emit(self, kind: TokenKind, start: int, value=None, suffix: Optional[str] = None) -> None:
        self.tokens.append(Token(kind, self._span(start), value, suffix))

    def _error(self, message: str, start: int, hint: Optional[str] = None) -> NyetError:
        span = self._span(max(start, start + 1) if start == self.pos else start)
        return NyetError(Diagnostic(Severity.ERROR, message, span, hint))

    # ---------- main loop ----------

    def lex(self) -> list[Token]:
        while not self._eof():
            ch = self._peek()
            if ch in " \t\r\n":
                self.pos += 1
                continue
            if ch == ";":
                self._lex_comment()
                continue
            start = self.pos
            if ch == "(":
                self._advance(); self._emit(TokenKind.LPAREN, start); continue
            if ch == ")":
                self._advance(); self._emit(TokenKind.RPAREN, start); continue
            if ch == "[":
                self._advance(); self._emit(TokenKind.LBRACKET, start); continue
            if ch == "]":
                self._advance(); self._emit(TokenKind.RBRACKET, start); continue
            if ch == "{":
                self._advance(); self._emit(TokenKind.LBRACE, start); continue
            if ch == "}":
                self._advance(); self._emit(TokenKind.RBRACE, start); continue
            if ch == ",":
                self._advance(); self._emit(TokenKind.COMMA, start); continue
            if ch == "#":
                if self._peek(1) == "(":
                    self.pos += 2
                    self._emit(TokenKind.HASH_LPAREN, start)
                    continue
                raise self._error("unexpected '#' — expected '#(' for tuple", start)
            if ch == '"':
                self._lex_string(start); continue
            if ch == ":":
                # Disambiguation:
                #   `x:i32`  — type annotation: ':' is adjacent to the preceding ident/digit.
                #   `:read`  — keyword literal: ':' is NOT adjacent to an ident/digit on the left.
                # The rule: keyword literal only when the char immediately before ':'
                # is whitespace, paren/bracket/brace, or the start of input.
                prev_ch = self.src[start - 1] if start > 0 else ""
                attached_left = prev_ch in _IDENT_BODY  # ident body covers letters, digits, underscore
                next_ch = self._peek(1)
                if next_ch in _IDENT_START and not attached_left:
                    self._lex_keyword_literal(start)
                else:
                    self._advance()
                    self._emit(TokenKind.COLON, start)
                continue
            if ch == ".":
                if self._peek(1) == ".":
                    self.pos += 2
                    self._emit(TokenKind.DOT_DOT, start)
                else:
                    self._advance()
                    self._emit(TokenKind.DOT, start)
                continue
            if ch == "-":
                if self._peek(1) == ">":
                    self.pos += 2
                    self._emit(TokenKind.ARROW, start)
                    continue
                self._advance()
                self._emit(TokenKind.MINUS, start)
                continue
            if ch == "+":
                self._advance(); self._emit(TokenKind.PLUS, start); continue
            if ch == "*":
                self._advance(); self._emit(TokenKind.STAR, start); continue
            if ch == "%":
                self._advance(); self._emit(TokenKind.PERCENT, start); continue
            if ch == "^":
                self._advance(); self._emit(TokenKind.CARET, start); continue
            if ch == "~":
                self._advance(); self._emit(TokenKind.TILDE, start); continue
            if ch == "?":
                self._advance(); self._emit(TokenKind.QUESTION, start); continue
            if ch == "=":
                if self._peek(1) == "=":
                    self.pos += 2
                    self._emit(TokenKind.EQ_EQ, start)
                else:
                    self._advance()
                    self._emit(TokenKind.EQ, start)
                continue
            if ch == "!":
                if self._peek(1) == "=":
                    self.pos += 2
                    self._emit(TokenKind.BANG_EQ, start)
                else:
                    self._advance()
                    self._emit(TokenKind.BANG, start)
                continue
            if ch == "<":
                nxt = self._peek(1)
                if nxt == "=":
                    self.pos += 2; self._emit(TokenKind.LT_EQ, start)
                elif nxt == "<":
                    self.pos += 2; self._emit(TokenKind.LT_LT, start)
                else:
                    self._advance(); self._emit(TokenKind.LT, start)
                continue
            if ch == ">":
                nxt = self._peek(1)
                if nxt == "=":
                    self.pos += 2; self._emit(TokenKind.GT_EQ, start)
                elif nxt == ">":
                    self.pos += 2; self._emit(TokenKind.GT_GT, start)
                else:
                    self._advance(); self._emit(TokenKind.GT, start)
                continue
            if ch == "&":
                nxt = self._peek(1)
                if nxt == "&":
                    self.pos += 2; self._emit(TokenKind.AND_AND, start)
                elif nxt == "!":
                    self.pos += 2; self._emit(TokenKind.AMP_BANG, start)
                else:
                    self._advance(); self._emit(TokenKind.AMP, start)
                continue
            if ch == "|":
                nxt = self._peek(1)
                if nxt == "|":
                    self.pos += 2; self._emit(TokenKind.OR_OR, start)
                elif nxt == ">":
                    self.pos += 2; self._emit(TokenKind.PIPE_ARROW, start)
                else:
                    self._advance(); self._emit(TokenKind.BAR, start)
                continue
            if ch == "/":
                # A bare `/` with no identifier char on either side is the SLASH
                # operator. An identifier that _starts_ with `/` is invalid —
                # path-qualified identifiers are handled during ident scanning.
                self._advance()
                self._emit(TokenKind.SLASH, start)
                continue
            if ch in _DIGIT:
                self._lex_number(start); continue
            if ch in _IDENT_START:
                self._lex_ident(start); continue

            raise self._error(f"unexpected character {ch!r}", start)

        self._emit(TokenKind.EOF, self.pos)
        return self.tokens

    # ---------- comment tiers ----------

    def _lex_comment(self) -> None:
        start = self.pos
        semis = 0
        while self._peek() == ";":
            self._advance()
            semis += 1
        # Skip rest of line.
        while not self._eof() and self._peek() != "\n":
            self._advance()
        kind = {
            1: TokenKind.COMMENT_INLINE,
            2: TokenKind.COMMENT_BLOCK,
            3: TokenKind.COMMENT_SECTION,
        }.get(semis, TokenKind.COMMENT_FILE)  # 4+ → file-level
        if self.keep_trivia:
            self._emit(kind, start, value=semis)

    # ---------- strings ----------

    def _lex_string(self, start: int) -> None:
        self._advance()  # opening "
        chars: list[str] = []
        while True:
            if self._eof():
                raise self._error("unterminated string literal", start)
            ch = self._advance()
            if ch == '"':
                break
            if ch == "\\":
                if self._eof():
                    raise self._error("unterminated escape in string literal", start)
                esc = self._advance()
                mapped = {
                    "n": "\n",
                    "t": "\t",
                    "r": "\r",
                    "0": "\0",
                    "\\": "\\",
                    '"': '"',
                }.get(esc)
                if mapped is None:
                    if esc == "x":
                        hi = self._advance() if not self._eof() else ""
                        lo = self._advance() if not self._eof() else ""
                        if hi not in _HEX_DIGIT or lo not in _HEX_DIGIT:
                            raise self._error("invalid \\xHH escape", start)
                        chars.append(chr(int(hi + lo, 16)))
                        continue
                    raise self._error(f"unknown escape \\{esc}", start)
                chars.append(mapped)
                continue
            chars.append(ch)
        self._emit(TokenKind.STRING_LIT, start, value="".join(chars))

    # ---------- keyword literals ----------

    def _lex_keyword_literal(self, start: int) -> None:
        self._advance()  # consume ':'
        name_start = self.pos
        while not self._eof() and self._peek() in _IDENT_BODY:
            self._advance()
        name = self.src[name_start : self.pos]
        self._emit(TokenKind.KEYWORD_LIT, start, value=name)

    # ---------- numbers ----------

    def _lex_number(self, start: int) -> None:
        # Base prefixes (0x / 0b / 0o) — integer only, no suffix yet.
        if self._peek() == "0" and self._peek(1) in ("x", "X", "b", "B", "o", "O"):
            self._advance()  # 0
            base_ch = self._advance().lower()
            base, digits = {
                "x": (16, _HEX_DIGIT),
                "b": (2, _BIN_DIGIT),
                "o": (8, _OCT_DIGIT),
            }[base_ch]
            digit_start = self.pos
            while not self._eof() and (self._peek() in digits or self._peek() == "_"):
                self._advance()
            raw = self.src[digit_start : self.pos].replace("_", "")
            if not raw:
                raise self._error("numeric literal needs at least one digit", start)
            suffix = self._lex_numeric_suffix(start)
            self._emit(TokenKind.INT_LIT, start, value=int(raw, base), suffix=suffix)
            return

        # Decimal integer or float.
        is_float = False
        while not self._eof() and (self._peek() in _DIGIT or self._peek() == "_"):
            self._advance()
        # Fractional part — but only if not followed by an identifier start
        # (to avoid eating into e.g. method-style tokens; ., if present, is
        # its own punctuation already handled above).
        if self._peek() == "." and self._peek(1) in _DIGIT:
            is_float = True
            self._advance()  # .
            while not self._eof() and (self._peek() in _DIGIT or self._peek() == "_"):
                self._advance()
        # Exponent
        if self._peek() in ("e", "E"):
            # Only consume as exponent if a valid exponent follows.
            save = self.pos
            self._advance()
            if self._peek() in ("+", "-"):
                self._advance()
            if self._peek() in _DIGIT:
                is_float = True
                while not self._eof() and self._peek() in _DIGIT:
                    self._advance()
            else:
                self.pos = save  # rollback: not an exponent
        raw = self.src[start : self.pos].replace("_", "")
        suffix = self._lex_numeric_suffix(start)
        if suffix and suffix.startswith("f"):
            is_float = True
        if is_float:
            self._emit(TokenKind.FLOAT_LIT, start, value=float(raw), suffix=suffix)
        else:
            self._emit(TokenKind.INT_LIT, start, value=int(raw), suffix=suffix)

    def _lex_numeric_suffix(self, start: int) -> Optional[str]:
        if self._eof() or self._peek() not in _IDENT_START:
            return None
        save = self.pos
        while not self._eof() and self._peek() in _IDENT_BODY:
            self._advance()
        suffix = self.src[save : self.pos]
        if suffix not in _NUMERIC_SUFFIXES:
            raise self._error(
                f"unknown numeric suffix {suffix!r}",
                start,
                hint=f"valid suffixes: {', '.join(sorted(_NUMERIC_SUFFIXES))}",
            )
        return suffix

    # ---------- identifiers and keywords ----------

    def _lex_ident(self, start: int) -> None:
        # Consume the first segment.
        self._consume_ident_segment()
        # Path-qualified identifiers: foo/bar/baz — one or more `/segment`
        # chains, but only when followed immediately (no spaces) by another
        # ident start. Bare `/` with space on either side is caught by the
        # main loop as SLASH.
        while self._peek() == "/" and self._peek(1) in _IDENT_START:
            self._advance()
            self._consume_ident_segment()
        # Trailing `!` or `?` (Lisp-style) — `fn!`, `set!`, `empty?`.
        if self._peek() in ("!", "?"):
            # But only if it's the identifier terminator, not part of `!=` etc.
            # Since we're inside an ident, != can't appear — consume it.
            self._advance()

        text = self.src[start : self.pos]

        if text == "_":
            self._emit(TokenKind.UNDERSCORE, start)
            return

        if text in BOOL_LITERALS:
            self._emit(TokenKind.BOOL_LIT, start, value=BOOL_LITERALS[text])
            return

        kw = KEYWORDS.get(text)
        if kw is not None:
            self._emit(kw, start)
            return

        self._emit(TokenKind.IDENT, start, value=text)

    def _consume_ident_segment(self) -> None:
        if self._eof() or self._peek() not in _IDENT_START:
            return
        self._advance()
        while not self._eof() and self._peek() in _IDENT_BODY:
            self._advance()


def lex(file: SourceFile, *, keep_trivia: bool = False) -> list[Token]:
    """Convenience wrapper: produce tokens for an entire source file."""
    return Lexer(file, keep_trivia=keep_trivia).lex()
