"""Unit tests for the scanner's `:` disambiguation rule.

`x:i32` (type annotation) and `:read` (keyword literal) both use `:`,
distinguished only by whether the preceding character is attached to
an identifier -- see pynyet/lexer/scanner.py's handling of TokenKind.COLON
vs the keyword-literal path.
"""

from pynyet.lexer.scanner import lex
from pynyet.lexer.token import TokenKind
from pynyet.source import SourceFile


def _kinds(src: str) -> list[TokenKind]:
    return [t.kind for t in lex(SourceFile("t.no", src))]


def test_annotation_after_identifier_is_colon_not_keyword():
    kinds = _kinds("x:i32")
    assert TokenKind.COLON in kinds
    assert TokenKind.KEYWORD_LIT not in kinds


def test_leading_colon_after_whitespace_is_keyword_literal():
    kinds = _kinds("(let mode :read)")
    assert TokenKind.KEYWORD_LIT in kinds
    assert TokenKind.COLON not in kinds


def test_leading_colon_after_open_paren_is_keyword_literal():
    kinds = _kinds("(:ok)")
    assert TokenKind.KEYWORD_LIT in kinds


def test_keyword_literal_value_excludes_the_colon():
    tokens = list(lex(SourceFile("t.no", ":digital")))
    kw = next(t for t in tokens if t.kind is TokenKind.KEYWORD_LIT)
    assert kw.value == "digital"


def test_struct_field_annotation_in_context():
    # (struct Point x:i32 y:i32) -- every ':' here is a type annotation.
    kinds = _kinds("(struct Point x:i32 y:i32)")
    assert kinds.count(TokenKind.COLON) == 2
    assert TokenKind.KEYWORD_LIT not in kinds
