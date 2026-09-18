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
just an example), and it compiles and runs: each section's examples
live in a `*_examples` function called from `main`, and results the
comments state are `assert`ed. The codegen harness builds and runs it
against `tests/codegen/main_no.golden`, so `just test-all` fails if it
breaks — update it (`python3 tests/codegen/run.py --update main`) when
the language changes. Designs that aren't implemented yet sit in the
"Planned" block comment at its end, the only part that isn't compiled.

## Where the `.no` files live

`.no` is gitignored by default; `.gitignore` lists back exactly what
the repo keeps, grouped the same way as below.

| Path | What it is |
| --- | --- |
| `main.no` | The language reference. Source of truth, compiled and asserted by the codegen harness. |
| `std/` | The standard library — `ansi`, `bench`, `collections`, `io`, `math`. Written in Nyet, ships with the compiler. |
| `demos/` | Complete programs: the arcade suite. One directory per game, each a two-line `main.no` wrapper, plus `demos/lib/` for the shared modules they import. |
| `examples/` | Short programs, one feature each. `examples/tour/` is the guided set that becomes the Windows bundle's `demo\` folder; `examples/interactive/` reads stdin and is run by hand. |
| `tests/{lexer,parser,sema,codegen}/` | Golden-file fixtures. Nothing here may read stdin. |

Two rules worth knowing before moving any of these:

- A program imports a shared module by its repo-relative path
  (`(use demos/lib/hangman_lib)`, `(use std/ansi)`). `(use ...)` resolves
  against the importing file's own directory first, then the repo root —
  so modules that sit next to each other (everything in `demos/lib/`)
  import each other by bare name.
- `packaging/nyet.spec` bundles `std/` and `demos/lib/` into the frozen
  `nyet.exe` under those same names, because that is what the shipped
  programs import. `packaging/build_windows.ps1` and
  `packaging/stage_public_examples.py` map these paths onto the bundle's
  and the public mirror's own layouts, which are deliberately flatter and
  are what the public README documents. Changing a path here means
  changing all three.

## Distribution

This repo is private and stays private. Public downloads come from a
separate public repo, **[NeonSupernova/nyet-releases][rel]**, which
holds the README, the binary license and third-party notices, the
issue tracker, a mirror of the demo sources, and the release assets —
no compiler code.

[rel]: https://github.com/NeonSupernova/nyet-releases

`.github/workflows/windows-package.yml` (manual trigger) builds the
Windows bundle here, smoke-tests it, and publishes it there. See
**packaging/RELEASING.md** for the full runbook, including the
`RELEASE_TOKEN` secret it needs and the GPL source-offer obligation
that comes with redistributing the bundled WinLibs toolchain.

`__version__` in `pynyet/__init__.py` is the single source of truth for
the release version — the workflow reads it to name the tag, and
`nyet --version` prints it. `packaging/stage_public_examples.py`
defines exactly which `.no` files get mirrored publicly.

## Dependencies

- **clang** — only external dependency, used to compile emitted `.ll`
  to a native binary.
- Python 3.10+ stdlib only for the compiler itself.
- `lsp/requirements.txt` (pygls) is only needed for the LSP server in
  `lsp/`, unrelated to the compiler.
