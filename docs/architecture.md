# Nyet Compiler Architecture

## Overview

Nyet is an S-expression language with Rust-inspired ownership. The
implementation is a Python compiler (`pynyet/`) that targets native
code by emitting LLVM IR **as text directly** (no `llvmlite`) and
linking with `clang`. The pipeline is the conventional one:

```
source → lexer → parser → AST → sema → codegen (LLVM IR text) → clang → binary
```

See [PLAN.md](../PLAN.md) for the original design document,
[CONTINUATION_PLAN.md](../CONTINUATION_PLAN.md) for current status and
what's left, and [main.no](../main.no) for the language spec (which
compiles and runs — see below).

## Current status

Nyet is at milestone **v1.0** (PLAN.md §10), plus the later work tracked
in CONTINUATION_PLAN.md: tuples, Map, `dyn` trait objects, drops,
exhaustiveness diagnostics, async `spawn`/`await`, closures that capture
their environment, and a `std/` written in Nyet. Lexer, parser, AST, four
sema passes, and codegen are covered by golden tests in
`tests/{lexer,parser,sema,codegen}/`.

`main.no`'s claim "This file is valid Nyet source. It compiles." holds:
the codegen harness builds and runs it against
`tests/codegen/main_no.golden`, and its asserts check the results its
comments state. Designs that aren't implemented yet (IO channel values,
GPIO/TCP channels, http, `Shared`, `mpsc`) sit in the "Planned" block
comment at the end of the file.

Three files under `pynyet/` are dead legacy from the original
`rply`/`llvmlite` prototype and are slated for deletion — nothing in
the live pipeline imports them: `pynyet/lexer/lexer.py`,
`pynyet/ast/ast.py`, `pynyet/codegen/codegen.py`.

## Module layout

```
pynyet/
  source.py            SourceFile and Span (byte-range spans)
  diagnostic.py        Diagnostic, Severity, NyetError reporting
  driver.py            CLI entry point (lex/parse/check/build/run)
  __main__.py          `python -m pynyet` entry point
  lexer/
    token.py           TokenKind enum, Token dataclass, keyword table
    scanner.py         Hand-written scanner, lex() entry point
    lexer.py           (dead legacy rply lexer)
  parser/
    parser.py          Recursive-descent parser
  ast/
    nodes.py           AST node dataclasses
    pretty.py          AST -> S-expression pretty printer
    ast.py             (dead legacy rply/eval-based AST)
  sema/
    expand.py          Macro expansion (hygienic, variadic)
    resolve.py         Name resolution, trait/impl registration
    typeck.py          Type checking (annotation-driven, not full inference)
    borrow.py          Move/borrow checking
  codegen/
    emit.py            LLVM-IR-as-text emitter (class Emitter)
    codegen.py         (dead legacy llvmlite-based codegen)
  ir/                  unused stub — PLAN.md §6 typed IR, never built
  interp/              unused stub — tree-walk interpreter, never built

runtime/               C runtime sources; compiles via `just runtime`
                        but is not linked by the driver/codegen yet

tests/
  lexer/               golden token dumps
  parser/              golden AST dumps + main.no smoke parse
  sema/                golden diagnostic dumps (`ok` or formatted errors)
  codegen/             golden build+run stdout dumps (+ build stderr)
```

## Pipeline walkthrough

1. **Load source.** `pynyet.source.SourceFile` wraps the path and text.
   Every later stage attaches `Span` values back to this `SourceFile`
   so diagnostics can render the exact line and column.
2. **Lex.** `pynyet.lexer.scanner.lex(source)` walks the byte stream and
   produces a list of `Token`s. The scanner handles numeric suffixes
   (`42i64`), base prefixes (`0x`, `0b`, `0o`), identifier paths
   (`std/math/sqrt`), `!`/`?` identifier suffixes, keyword literals
   (`:foo`), multi-tier comments, and string escapes. Trivia tokens
   (comments) can optionally be preserved for autodocs.
3. **Parse.** `pynyet.parser.parser.parse(tokens)` consumes the token
   stream and produces an AST. S-expressions make the outer shape
   trivial; a dispatch table handles special forms like `let`, `fn`,
   `if`, `match`, `struct`, `trait`, `impl`, `macro`, `module`, and so on.
4. **Sema.** Four passes run in order, each returning a list of
   `Diagnostic`s, wired together by `driver.py`:
   - `expand.py` — macro expansion (hygienic, variadic)
   - `resolve.py` — name resolution, trait/impl registration
   - `typeck.py` — type checking (annotation-driven)
   - `borrow.py` — move/borrow checking; the only pass whose errors
     block `build` today (other sema errors are reported but don't
     yet gate compilation)
5. **Codegen.** `pynyet.codegen.emit.emit_ir(program)` walks the
   checked AST directly and emits LLVM IR as text — generics are
   monomorphized, closures are lambda-lifted, structs/sum
   types/matches/arrays/trait dispatch are lowered inline. There is no
   separate typed IR layer; `pynyet/ir/` is an unused stub.
6. **Link.** `clang` compiles the emitted `.ll` to a native binary.
   `runtime/` (C sources for alloc/string/io) is not currently linked
   in — codegen calls libc (`printf`, `fgets`, `exit`, ...) directly.

## Key design decisions

### Hand-written scanner

The lexer is hand-written rather than generated by `rply`. Nyet has
several constructs that fight LR-style lexers: numeric suffixes
attached to literals, path identifiers containing `/`, comment tiers
keyed off the number of leading `;`, and the `#(` tuple prefix. A
straightforward character-at-a-time scanner handles all of these
cleanly and produces precise spans for diagnostics. PLAN.md §2 walks
through the motivation.

### S-expression syntax

Nyet's S-expression surface syntax gives the parser a trivial outer
grammar: everything is either an atom or a parenthesized list. All
language constructs — `let`, `fn`, `if`, `match`, `struct`, `trait` —
live as special forms dispatched on the head of a list. The same
uniformity makes hygienic macros natural: a macro rewrites one tree into
another, with no custom parser extensions needed.

### Ownership without annotations at call sites

Like Rust, Nyet distinguishes owned values, shared borrows (`&T`), and
exclusive borrows (`&!T`). Unlike Rust, borrow information at call sites
is inferred from the callee's signature rather than written explicitly
at every call. `pynyet/sema/borrow.py` implements one-owner-per-value,
no-use-after-move, and no-aliasing-of-exclusive-borrows checks as
dataflow analysis; violations are hard errors.

### Codegen skipped the planned IR layer

PLAN.md §6 called for a typed mid-level IR between sema and codegen
(desugaring pattern matching, closures, `?`, and inserting explicit
drops before LLVM emission). In practice `pynyet/codegen/emit.py`
lowers directly from the checked AST to LLVM IR text — simpler to get
working, but it means monomorphization, lambda lifting, and match
lowering all live inside one 2000+ line `Emitter` class instead of
being separable passes. This is worth revisiting before v1.1
(async/await), which wants state-machine lowering — see
CONTINUATION_PLAN.md Phase 3.

## Pointers

- [PLAN.md](../PLAN.md) — the original implementation blueprint
- [CONTINUATION_PLAN.md](../CONTINUATION_PLAN.md) — current status,
  known gaps, and phased next steps
- [main.no](../main.no) — the language specification, built and run by
  the test suite; unimplemented designs are in its "Planned" block (see
  "Current status" above)
- [tests/README.md](../tests/README.md) — golden-file test harness
