# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Nyet is a programming language with a Lisp-like, S-expression syntax and
a Rust-inspired ownership model, compiling to native code via LLVM. The
implementation is a Python compiler under `pynyet/`. As of milestone
v1.0 it supports primitives, structs/sum types/pattern matching,
generics with monomorphization, traits/operator overloading, move
semantics with a borrow checker, closures, hygienic macros, modules,
and file IO. See PLAN.md for the original design blueprint and
CONTINUATION_PLAN.md for what's left.

There is also an old C++/Bison/Flex prototype referenced in git history
(`nyet_compiler/`) — it has since been deleted from the tree and is not
part of the current path.

## Commands

```bash
# Run the driver directly
python3 -m pynyet.driver lex FILE      # tokenize, print tokens
python3 -m pynyet.driver parse FILE    # parse, print pretty-printed AST
python3 -m pynyet.driver check FILE    # macro-expand + resolve + typecheck + borrow-check
python3 -m pynyet.driver build FILE    # compile to a native binary (writes output.ll + output)
python3 -m pynyet.driver run FILE      # build, then execute

# Equivalent justfile wrappers (run `just` to list all)
just lex FILE
just parse FILE
just check FILE
just build FILE
just run FILE

# Tests (golden-file harnesses, see tests/README.md)
just test-lexer
just test-parser
just test-sema
just test-codegen
just test-all

# C runtime (currently unused by codegen — see CONTINUATION_PLAN.md Phase 3)
just runtime

just clean   # remove build/, output, output.ll, __pycache__
```

No `app.py`, no `input.toy`, no `rply`, no `llvmlite` — none of those
exist in the current pipeline. `clang` is the only external dependency
(for linking emitted `.ll` files); the compiler itself is stdlib-only
Python 3.10+.

## Architecture

```
FILE.no → lexer/scanner.py → parser/parser.py → ast/nodes.py
                                                       ↓
                                    sema/{expand,resolve,typeck,borrow}.py
                                                       ↓
                                          codegen/emit.py → LLVM IR text
                                                       ↓
                                              clang → native binary
```

- **Lexer** (`pynyet/lexer/scanner.py`) — hand-written scanner. Handles
  numeric suffixes (`42i64`, `255u8`), base prefixes (`0x`/`0b`/`0o`),
  path identifiers (`std/math/sqrt`), `!`/`?` identifier suffixes,
  `:keyword` literals, four comment tiers, and string escapes.
- **Parser** (`pynyet/parser/parser.py`) — recursive descent. A
  dispatch table maps the head token of each `(...)` form to a
  special-form parser (`let`, `fn`, `if`, `match`, `struct`, `trait`,
  `impl`, `macro`, `module`, etc.); anything else parses as a call.
- **AST** (`pynyet/ast/nodes.py`) — dataclass node types, one file.
  `pynyet/ast/pretty.py` round-trips nodes back to S-expression text
  (used by `driver parse` and the parser golden tests).
- **Sema** (`pynyet/sema/`) — four passes, run in order by the driver:
  `expand.py` (macro expansion, hygienic/variadic), `resolve.py` (name
  resolution, trait/impl registration), `typeck.py` (type checking —
  annotation-driven, not full inference), `borrow.py` (move/borrow
  checking; use-after-move and aliasing-exclusive-borrow errors are
  hard errors that block `build`).
- **Codegen** (`pynyet/codegen/emit.py`) — a single `Emitter` class
  that walks the typed/resolved AST and emits LLVM IR **as text**
  directly (no `llvmlite`, no separate IR layer — `pynyet/ir/` is an
  unused stub). Handles generics via monomorphization, closures via
  lambda lifting, structs/sum types/match, arrays, and operator/trait
  dispatch.
- **`pynyet/driver.py`** — CLI entry point (`lex`/`parse`/`check`/
  `build`/`run`); orchestrates the pipeline above and shells out to
  `clang` for the final link step.

## Nyet Language Syntax

```
;; single-line comment
;;; section comment
;;;; file-level comment
#| block comment |#

(struct Point x:i32 y:i32)

(fn area (w:i32 h:i32) -> i32
  (* w h))

(fn main () -> unit
  (let p (Point x:3 y:4))
  (let x:i32 42)
  (if (== x 42) (out "yes\n") (out "no\n")))
```

Primitive types: `i8..i64`, `u8..u64`, `usize`, `f32`, `f64`, `bool`,
`char` (a Unicode scalar value, stored as `i32`), `string`, `unit`.
Bindings are immutable by default (`let`); use `var` for mutable
bindings. Integer literals default to `i32`, floats to `f64`, unless
suffixed (`42i64`, `3.14f32`) or annotated — an unsuffixed integer
literal can also satisfy any integer or `char` annotation directly,
e.g. `(let ch:char 65)`. Explicit primitive casts use `(as expr type)`
— numeric widen/narrow and `char`/int conversions; an out-of-range
int→char cast panics at runtime (Unicode scalar bounds check).

`main.no` at the repo root is the full language specification (not
just an example) — it documents features well beyond what's
implemented today; treat it as aspirational in places, not as proof a
construct works. `examples/` and `tests/{lexer,parser,sema,codegen}/`
are what's actually verified.

## Dependencies

- **clang** — only external dependency, used to compile emitted `.ll`
  to a native binary.
- Python 3.10+ stdlib only for the compiler itself.
- `lsp/requirements.txt` (pygls) is only needed for the LSP server in
  `lsp/`, unrelated to the compiler.
