"""Unit tests for `(in)` codegen (Emitter._emit_in).

Regression coverage for two bugs in reading stdin, found while turning
minibase/main.no into an interactive console REPL (see that file's own
comments for the user-facing story). End-to-end behavior is covered by
tests/codegen/stdin_repeat.no and stdin_across_return.no (which build
and run a real binary); these tests instead pin the generated IR's
*structure* directly, since that's what actually distinguishes the fix
from the bug -- both goldens print the same empty-string-on-EOF output
whether or not the stdin handle is cached, because the harness pipes no
stdin at all.

1. `(in)` re-opened a brand new stdio handle (`fdopen(0, "r")`) on every
   call -- not just every call *site*, but every runtime execution of a
   `loop` body containing one. A fresh stdio buffer's first `fgets`
   reads ahead past the current line, and that buffered remainder was
   discarded the instant the FILE* was abandoned, silently dropping
   every line after the first. Fixed by caching the FILE* returned by
   `fdopen` in a module-level global (`@__nyet_stdin`) behind a
   null-check, so it's opened at most once per process.
2. The read buffer was stack-allocated (`alloca [256 x i8]`), so the
   `string` `(in)` returns was a dangling pointer to a popped stack
   frame the instant the declaring function returned -- e.g. any
   ordinary `(fn read_line () -> string (in))` prompt helper handed
   back corrupted bytes to its caller. Fixed: `malloc(256)` instead.
"""

import re

from pynyet.codegen.emit import Emitter
from pynyet.lexer.scanner import lex
from pynyet.parser.parser import parse
from pynyet.sema.expand import expand_macros
from pynyet.sema.resolve import resolve_names
from pynyet.sema.typeck import check_types
from pynyet.source import SourceFile


def _ir_for(src: str) -> str:
    sf = SourceFile("t.no", src)
    program = parse(lex(sf))
    program, _ = expand_macros(program)
    resolve_names(program)
    check_types(program)
    return Emitter().emit(program)


def test_stdin_handle_is_cached_behind_a_null_check():
    ir = _ir_for("(fn main () -> unit (let a:string (in)))")
    assert "@__nyet_stdin = internal global ptr null" in ir
    # Every call site must guard its `fdopen` call behind "is the cached
    # handle still null" -- not call it unconditionally.
    assert re.search(r"load ptr, ptr @__nyet_stdin", ir)
    assert re.search(r"icmp eq ptr %\w+, null\n\s*br i1", ir)


def test_stdin_read_buffer_is_heap_allocated_not_stack():
    ir = _ir_for("(fn main () -> unit (let a:string (in)))")
    assert "call ptr @malloc(i64 256)" in ir
    assert "alloca [256 x i8]" not in ir


def test_fgets_eof_is_checked_and_yields_empty_string():
    ir = _ir_for("(fn main () -> unit (let a:string (in)))")
    fgets_result = re.search(r"(%t\d+) = call ptr @fgets\(", ir)
    assert fgets_result is not None
    reg = fgets_result.group(1)
    # The fgets return value must be compared to null (EOF), not used
    # unconditionally -- and the stored fallback on that path is an
    # empty (nul-terminated) buffer, "store i8 0".
    assert re.search(rf"icmp eq ptr {re.escape(reg)}, null", ir)
    assert "store i8 0" in ir


def test_trailing_line_ending_is_stripped():
    # fgets keeps '\n' (and a preceding '\r' on CRLF input); a line
    # compared with `(== line "quit")` could never match actual typed
    # input without this.
    ir = _ir_for("(fn main () -> unit (let a:string (in)))")
    assert "call i64 @strcspn(" in ir
