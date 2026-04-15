#!/usr/bin/env python3
"""
Nyet Language Server — provides LSP features for .no files.

Features:
  - Diagnostics (syntax errors via pynyet lexer/parser)
  - Hover docs for keywords, types, and builtins
  - Completions for keywords, types, builtins, and local identifiers
  - Document symbols (functions, structs, types, let/const)

Usage:
  python3 server.py            # stdio transport (VS Code, JetBrains)
  python3 server.py --debug    # with verbose logging
"""

import sys
import os
import re
import logging
import argparse
from typing import Optional
from urllib.parse import unquote, urlparse

# ---------------------------------------------------------------------------
# Resolve project root so we can import pynyet
# ---------------------------------------------------------------------------

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Attempt to import pynyet pipeline (graceful fallback if unavailable)
# ---------------------------------------------------------------------------

_PYNYET_AVAILABLE = False
try:
    from pynyet.lexer.lexer import Lexer as NyetLexer
    from pynyet.parser.parser import get_parser as get_nyet_parser
    _PYNYET_AVAILABLE = True
except Exception:
    pass

# ---------------------------------------------------------------------------
# pygls / lsprotocol imports
# ---------------------------------------------------------------------------

from pygls.server import LanguageServer
from lsprotocol import types as lsp

# ---------------------------------------------------------------------------
# Language knowledge
# ---------------------------------------------------------------------------

KEYWORDS_CONTROL = ["if", "match", "when", "loop", "break", "return", "do"]
KEYWORDS_DECL = ["let", "var", "const", "fn", "fn!", "struct", "type",
                 "newtype", "alias", "trait", "impl"]
KEYWORDS_MODULE = ["pub", "use", "module", "macro", "quote"]
KEYWORDS_OTHER = ["self", "Self", "dyn", "move", "async", "await", "spawn",
                  "pass"]
BUILTIN_FUNCTIONS = ["out", "err", "in", "fmt", "str", "len", "push", "pop",
                     "append", "parse", "gensym"]
PRIMITIVE_TYPES = ["i8", "i16", "i32", "i64", "u8", "u16", "u32", "u64",
                   "usize", "f32", "f64", "bool", "string", "unit"]
GENERIC_TYPES = ["Option", "Result", "Array"]
ALL_KEYWORDS = (KEYWORDS_CONTROL + KEYWORDS_DECL + KEYWORDS_MODULE +
                KEYWORDS_OTHER)
ALL_TYPES = PRIMITIVE_TYPES + GENERIC_TYPES

HOVER_DOCS: dict[str, str] = {
    # Control flow
    "if":     "```nyet\n(if condition then-branch else-branch)\n```\nConditional expression.",
    "match":  "```nyet\n(match scrutinee\n  (pattern body) ...)\n```\nPattern matching.",
    "when":   "```nyet\n((pattern when guard) body)\n```\nMatch-arm guard clause.",
    "loop":   "```nyet\n(loop body)\n```\nInfinite loop. Use `(break)` or `(break value)` to exit.",
    "break":  "```nyet\n(break) | (break value)\n```\nExit the innermost `loop`, optionally with a value.",
    "return": "```nyet\n(return value)\n```\nEarly return from a function.",
    "do":     "```nyet\n(do expr1 expr2 ...)\n```\nSequence expressions; evaluates to the last.",
    # Declarations
    "let":    "```nyet\n(let name:type value)\n```\nImmutable binding.",
    "var":    "```nyet\n(var name:type value)\n```\nMutable binding.",
    "const":  "```nyet\n(const NAME:type value)\n```\nCompile-time constant.",
    "fn":     "```nyet\n(fn name (params) -> rettype body)\n(fn name [T] (x:T) -> T body)\n```\nNamed function definition. Supports generics.",
    "fn!":    "```nyet\n(fn! name (params) -> rettype body)\n```\nFunction with mutable-capture semantics.",
    "struct": "```nyet\n(struct Name (field1:Type1 field2:Type2))\n```\nProduct type (struct) definition.",
    "type":   "```nyet\n(type Name (Variant1 Type) (Variant2))\n```\nSum type (enum) definition.",
    "newtype":"```nyet\n(newtype Name InnerType)\n```\nNewtype wrapper around an existing type.",
    "alias":  "```nyet\n(alias Name TargetType)\n```\nType alias.",
    "trait":  "```nyet\n(trait Name\n  (fn method (self) -> rettype body))\n```\nTrait (interface) definition.",
    "impl":   "```nyet\n(impl TraitName TypeName\n  (fn method ...))\n```\nImplement a trait for a type.",
    # Module
    "pub":    "`pub` — make the following declaration public.",
    "use":    "```nyet\n(use std/io)\n(use path (name1 name2))\n```\nImport from a module path.",
    "module": "```nyet\n(module name)\n```\nDeclare a module.",
    "macro":  "```nyet\n(macro name (params) body)\n```\nMacro definition.",
    # Other keywords
    "pass":   "The unit value `()`. No-op / placeholder expression.",
    "self":   "`self` — receiver in method definitions.",
    "Self":   "`Self` — the implementing type inside a `trait` or `impl` block.",
    "dyn":    "```nyet\n(dyn TraitName)\n```\nDynamic trait object.",
    "move":   "```nyet\n(move fn () -> rettype body)\n```\nClosure that moves its captures.",
    "async":  "`async` — mark a function as asynchronous.",
    "await":  "```nyet\n(await expr)\n```\nAwait an async value.",
    "spawn":  "```nyet\n(spawn expr)\n```\nSpawn a concurrent task.",
    # Builtins
    "out":    "```nyet\n(out value)\n(out key:value)\n```\nPrint to stdout.",
    "err":    "```nyet\n(err message)\n```\nPrint to stderr.",
    "in":     "```nyet\n(in Type)\n(in key:\"prompt\")\n```\nRead from stdin.",
    "fmt":    "```nyet\n(fmt template arg ...)\n```\nFormat a string.",
    "str":    "```nyet\n(str value)\n```\nConvert a value to its string representation.",
    "len":    "```nyet\n(len collection)\n```\nReturn the length of a string, array, or map.",
    "push":   "```nyet\n(push! array value)\n```\nAppend a value to a mutable array.",
    "pop":    "```nyet\n(pop! array)\n```\nRemove and return the last element of a mutable array.",
    "append": "```nyet\n(append array value)\n```\nReturn a new array with value appended.",
    "parse":  "```nyet\n(parse Type string)\n```\nParse a string into a value of the given type.",
    # Types
    "i8":  "`i8` — 8-bit signed integer.",
    "i16": "`i16` — 16-bit signed integer.",
    "i32": "`i32` — 32-bit signed integer.",
    "i64": "`i64` — 64-bit signed integer.",
    "u8":  "`u8` — 8-bit unsigned integer.",
    "u16": "`u16` — 16-bit unsigned integer.",
    "u32": "`u32` — 32-bit unsigned integer.",
    "u64": "`u64` — 64-bit unsigned integer.",
    "usize": "`usize` — pointer-sized unsigned integer.",
    "f32": "`f32` — 32-bit IEEE 754 float.",
    "f64": "`f64` — 64-bit IEEE 754 float.",
    "bool": "`bool` — Boolean. Values: `true`, `false`.",
    "string": "`string` — UTF-8 string.",
    "unit": "`unit` — The unit type. Its only value is `()`/`pass`.",
    "Option": "```nyet\n(type Option[T]\n  (Some T)\n  (None))\n```\nOptional value. Use `(? expr)` to propagate `None`.",
    "Result": "```nyet\n(type Result[T E]\n  (Ok T)\n  (Err E))\n```\nError-handling type. Use `(? expr)` to propagate `Err`.",
    "Array":  "`Array[T]` — homogeneous array type. Literal syntax: `[1 2 3]`.",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def uri_to_path(uri: str) -> str:
    return unquote(urlparse(uri).path)


def word_at(text: str, offset: int) -> str:
    """Return the word (identifier) at byte offset in text."""
    start = offset
    while start > 0 and re.match(r'[\w!?/]', text[start - 1]):
        start -= 1
    end = offset
    while end < len(text) and re.match(r'[\w!?/]', text[end]):
        end += 1
    return text[start:end]


def offset_of(text: str, line: int, col: int) -> int:
    lines = text.splitlines(keepends=True)
    return sum(len(lines[i]) for i in range(min(line, len(lines)))) + col


def extract_local_names(text: str) -> list[str]:
    """Regex-based scan for locally defined names (fn, let, var, const, struct, type)."""
    patterns = [
        r'\(fn!?\s+([a-zA-Z_][a-zA-Z0-9_/]*[!?]?)',
        r'\((?:let|var|const)\s+([a-zA-Z_][a-zA-Z0-9_]*)',
        r'\((?:struct|type|newtype|alias|trait)\s+([A-Z][a-zA-Z0-9_]*)',
    ]
    names = []
    for pat in patterns:
        for m in re.finditer(pat, text):
            names.append(m.group(1))
    return names


def extract_document_symbols(
    text: str, uri: str
) -> list[lsp.DocumentSymbol]:
    symbol_patterns = [
        (r'\(fn!?\s+([a-zA-Z_][a-zA-Z0-9_/]*[!?]?)', lsp.SymbolKind.Function),
        (r'\(struct\s+([A-Z][a-zA-Z0-9_]*)', lsp.SymbolKind.Struct),
        (r'\(type\s+([A-Z][a-zA-Z0-9_]*)', lsp.SymbolKind.Enum),
        (r'\(trait\s+([A-Z][a-zA-Z0-9_]*)', lsp.SymbolKind.Interface),
        (r'\(impl\s+[A-Z][a-zA-Z0-9_]*\s+([A-Z][a-zA-Z0-9_]*)', lsp.SymbolKind.Class),
        (r'\(const\s+([a-zA-Z_][a-zA-Z0-9_]*)', lsp.SymbolKind.Constant),
        (r'\(let\s+([a-zA-Z_][a-zA-Z0-9_]*)', lsp.SymbolKind.Variable),
    ]
    symbols = []
    lines = text.splitlines()

    for pat, kind in symbol_patterns:
        for m in re.finditer(pat, text):
            name = m.group(1)
            start_offset = m.start(1)
            line_no = text[:start_offset].count('\n')
            line_start = text.rfind('\n', 0, start_offset) + 1
            col = start_offset - line_start
            end_col = col + len(name)
            pos = lsp.Position(line=line_no, character=col)
            end_pos = lsp.Position(line=line_no, character=end_col)
            rng = lsp.Range(start=pos, end=end_pos)
            symbols.append(lsp.DocumentSymbol(
                name=name,
                kind=kind,
                range=rng,
                selection_range=rng,
            ))
    return symbols


def parse_error_position(msg: str) -> tuple[int, int]:
    """Try to extract (line, col) from an rply or generic error message."""
    # rply: "Unexpected `X` at (line N, col M)"
    m = re.search(r'line\s+(\d+),\s*col(?:umn)?\s+(\d+)', msg, re.IGNORECASE)
    if m:
        return int(m.group(1)) - 1, int(m.group(2)) - 1
    # "line N" alone
    m = re.search(r'line\s+(\d+)', msg, re.IGNORECASE)
    if m:
        return int(m.group(1)) - 1, 0
    return 0, 0


def get_diagnostics(text: str) -> list[lsp.Diagnostic]:
    if not _PYNYET_AVAILABLE:
        return []
    try:
        lexer = NyetLexer().get_lexer()
        tokens = lexer.lex(text)
        parser = get_nyet_parser()
        parser.parse(tokens)
        return []
    except Exception as exc:
        msg = str(exc)
        line, col = parse_error_position(msg)
        start = lsp.Position(line=line, character=col)
        end = lsp.Position(line=line, character=max(col + 1, col))
        return [lsp.Diagnostic(
            range=lsp.Range(start=start, end=end),
            message=msg,
            severity=lsp.DiagnosticSeverity.Error,
            source="nyet",
        )]

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

server = LanguageServer("nyet-language-server", "v0.1")

# In-memory document store
_documents: dict[str, str] = {}


def _publish(ls: LanguageServer, uri: str) -> None:
    text = _documents.get(uri, "")
    diags = get_diagnostics(text)
    ls.publish_diagnostics(uri, diags)


@server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
def did_open(ls: LanguageServer, params: lsp.DidOpenTextDocumentParams):
    _documents[params.text_document.uri] = params.text_document.text
    _publish(ls, params.text_document.uri)


@server.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls: LanguageServer, params: lsp.DidChangeTextDocumentParams):
    for change in params.content_changes:
        _documents[params.text_document.uri] = change.text
    _publish(ls, params.text_document.uri)


@server.feature(lsp.TEXT_DOCUMENT_DID_CLOSE)
def did_close(ls: LanguageServer, params: lsp.DidCloseTextDocumentParams):
    _documents.pop(params.text_document.uri, None)
    ls.publish_diagnostics(params.text_document.uri, [])


@server.feature(lsp.TEXT_DOCUMENT_HOVER)
def hover(
    ls: LanguageServer, params: lsp.HoverParams
) -> Optional[lsp.Hover]:
    uri = params.text_document.uri
    text = _documents.get(uri, "")
    pos = params.position
    offset = offset_of(text, pos.line, pos.character)
    word = word_at(text, offset)
    if not word:
        return None
    doc = HOVER_DOCS.get(word)
    if doc is None:
        return None
    return lsp.Hover(
        contents=lsp.MarkupContent(kind=lsp.MarkupKind.Markdown, value=doc)
    )


@server.feature(
    lsp.TEXT_DOCUMENT_COMPLETION,
    lsp.CompletionOptions(trigger_characters=["(", ":", "["])
)
def completion(
    ls: LanguageServer, params: lsp.CompletionParams
) -> lsp.CompletionList:
    uri = params.text_document.uri
    text = _documents.get(uri, "")
    local_names = extract_local_names(text)

    items: list[lsp.CompletionItem] = []

    for kw in KEYWORDS_CONTROL:
        items.append(lsp.CompletionItem(
            label=kw,
            kind=lsp.CompletionItemKind.Keyword,
            detail="control flow",
        ))
    for kw in KEYWORDS_DECL:
        items.append(lsp.CompletionItem(
            label=kw,
            kind=lsp.CompletionItemKind.Keyword,
            detail="declaration",
        ))
    for kw in KEYWORDS_MODULE + KEYWORDS_OTHER:
        items.append(lsp.CompletionItem(
            label=kw,
            kind=lsp.CompletionItemKind.Keyword,
        ))
    for fn in BUILTIN_FUNCTIONS:
        items.append(lsp.CompletionItem(
            label=fn,
            kind=lsp.CompletionItemKind.Function,
            detail="builtin",
            documentation=lsp.MarkupContent(
                kind=lsp.MarkupKind.Markdown,
                value=HOVER_DOCS.get(fn, "")
            ),
        ))
    for t in ALL_TYPES:
        items.append(lsp.CompletionItem(
            label=t,
            kind=lsp.CompletionItemKind.Class,
            detail="type",
            documentation=lsp.MarkupContent(
                kind=lsp.MarkupKind.Markdown,
                value=HOVER_DOCS.get(t, "")
            ),
        ))
    seen = set()
    for name in local_names:
        if name not in seen:
            seen.add(name)
            items.append(lsp.CompletionItem(
                label=name,
                kind=lsp.CompletionItemKind.Variable,
                detail="defined in file",
            ))

    return lsp.CompletionList(is_incomplete=False, items=items)


@server.feature(lsp.TEXT_DOCUMENT_DOCUMENT_SYMBOL)
def document_symbol(
    ls: LanguageServer, params: lsp.DocumentSymbolParams
) -> list[lsp.DocumentSymbol]:
    uri = params.text_document.uri
    text = _documents.get(uri, "")
    return extract_document_symbols(text, uri)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nyet Language Server")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)
    else:
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    server.start_io()
