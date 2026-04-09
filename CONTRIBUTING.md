# Contributing to Nyet

Thanks for your interest in hacking on Nyet. This guide covers the v0.1
development loop. See [PLAN.md](PLAN.md) for the full compiler blueprint
and [main.no](main.no) for the language specification.

## Development setup

- **Python 3.10+** is required. The compiler uses `from __future__ import
  annotations`, PEP 604 union syntax, and `list[int]`-style generics.
- The v0.1 compiler has **no external Python dependencies** — the lexer
  and diagnostic modules are pure stdlib.
- **llvmlite** will be needed once the codegen phase lands (v0.4+):
  ```bash
  pip install llvmlite
  ```
  You do not need it to work on the lexer, parser, AST, or sema phases.
- **clang** will be needed to link generated LLVM IR against the C
  runtime. Again, not required for v0.1.
- `rply` was used by the legacy pipeline in `pynyet/lexer/lexer.py` and
  `pynyet/parser/parser.py`. That code is kept as reference but is **not**
  part of the v0.1 path and is scheduled for removal.

## Running tests

The lexer has a golden-file test harness:

```bash
python3 tests/lexer/run.py              # check every case
python3 tests/lexer/run.py hello        # check just tests/lexer/hello.no
python3 tests/lexer/run.py --update     # rewrite all golden files
```

Every `.no` file under `tests/lexer/` is tokenized and diffed against its
sibling `.tokens` file. A missing `.tokens` file is written on the first
run so you can inspect and commit it.

Other test tiers (`tests/parser/`, `tests/sema/`, `tests/codegen/`) are
placeholders for future milestones.

## Adding a lexer test

1. Drop a new `.no` source file in `tests/lexer/`, e.g.
   `tests/lexer/my_case.no`.
2. Run `python3 tests/lexer/run.py`. The runner writes
   `tests/lexer/my_case.tokens` next to it.
3. Open the generated `.tokens` file and sanity-check the output.
4. Commit both the `.no` and the `.tokens` file.

To update goldens after an intentional lexer change, run with `--update`,
inspect the diff, and commit.

## Coding conventions

- **Python 3.10+ syntax.** Use `from __future__ import annotations` at the
  top of every module and prefer `list[int]` / `dict[str, T]` over the
  `typing` aliases.
- **Stdlib only** in the compiler, with llvmlite as the sole exception
  once codegen lands. No other third-party dependencies.
- **Every token, AST node, and IR node carries a `Span`.** Spans come
  from `pynyet.source` and are half-open byte ranges into a `SourceFile`.
- **Every error message routes through `pynyet.diagnostic`.** Do not
  `raise ValueError` or `print` errors directly from a compiler pass —
  build a `Diagnostic` with a span and attach a hint when possible.
- **Hand-written scanner and parser.** No parser generators; see PLAN.md
  §2 for why rply was abandoned. A recursive-descent parser is planned
  for v0.2.
- **`@dataclass` for data-carrying classes.** Use `frozen=True` for
  nodes that should stay immutable after construction.
- Keep modules focused. The layout in PLAN.md §1 is the target shape.

## Commit style

Terse, present-tense, imperative commit messages. Milestone prefixes are
encouraged but not required:

```
v0.1: add hand-written scanner
v0.1: lexer golden tests for comments and strings
v0.2: parser MVP for let and fn
```

When a change crosses multiple phases, pick the dominant one.

## Pointers

- [PLAN.md](PLAN.md) — the full implementation blueprint. Every design
  decision is justified there.
- [main.no](main.no) — the language specification. The source of truth
  for syntax and semantics.
- [docs/architecture.md](docs/architecture.md) — the current module
  layout and pipeline walkthrough.
