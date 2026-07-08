# Contributing to Nyet

Thanks for your interest in hacking on Nyet. See
[PLAN.md](PLAN.md) for the original compiler blueprint,
[CONTINUATION_PLAN.md](CONTINUATION_PLAN.md) for current status and
what's left, and [main.no](main.no) for the language specification
(note: the spec is aspirational in places — `main.no` itself doesn't
fully pass `driver check` yet).

## Development setup

- **Python 3.10+** is required. The compiler uses `from __future__ import
  annotations`, PEP 604 union syntax, and `list[int]`-style generics.
- The compiler has **no external Python dependencies** — it's pure
  stdlib. `llvmlite` is **not** used; codegen emits LLVM IR as text
  directly (`pynyet/codegen/emit.py`).
- **clang** is required to link generated `.ll` files into a native
  binary (`driver build` / `driver run`, and the codegen test harness).
- `rply`/`llvmlite` were used by an earlier prototype, remnants of
  which still live at `pynyet/lexer/lexer.py`, `pynyet/ast/ast.py`,
  and `pynyet/codegen/codegen.py`. Nothing in the live pipeline
  imports them; they're scheduled for deletion. Don't build on them.

## Running tests

Four golden-file harnesses, one per compiler phase:

```bash
python3 tests/lexer/run.py              # check every case
python3 tests/lexer/run.py hello        # check just tests/lexer/hello.no
python3 tests/lexer/run.py --update     # rewrite all golden files
```

Same interface for `tests/parser/run.py`, `tests/sema/run.py`, and
`tests/codegen/run.py` — or run everything with `make test-all`. See
[tests/README.md](tests/README.md) for what each phase's golden format
looks like.

## Adding a test

1. Drop a new `.no` source file in the relevant `tests/<phase>/`
   directory.
2. Run `python3 tests/<phase>/run.py`. The runner writes the golden
   file next to it on first run.
3. Open the generated golden and sanity-check the output — don't just
   trust whatever the compiler currently emits; compare against what
   the fixture's own comments say should happen.
4. Commit both the `.no` and the golden file.

To update goldens after an intentional compiler change, run with
`--update`, inspect the diff, and commit.

## Coding conventions

- **Python 3.10+ syntax.** Use `from __future__ import annotations` at the
  top of every module and prefer `list[int]` / `dict[str, T]` over the
  `typing` aliases.
- **Stdlib only.** No third-party dependencies in the compiler itself.
- **Every token, AST node, and IR node carries a `Span`.** Spans come
  from `pynyet.source` and are half-open byte ranges into a `SourceFile`.
- **Every error message routes through `pynyet.diagnostic`.** Do not
  `raise ValueError` or `print` errors directly from a compiler pass —
  build a `Diagnostic` with a span and attach a hint when possible.
- **Hand-written scanner and parser.** No parser generators; see
  PLAN.md §2 for why `rply` was abandoned.
- **`@dataclass` for data-carrying classes.** Use `frozen=True` for
  nodes that should stay immutable after construction.
- Keep modules focused. The layout in `docs/architecture.md` is the
  current shape.

## Commit style

Terse, present-tense, imperative commit messages. Milestone prefixes are
encouraged but not required:

```
v1.1: async fn state-machine lowering
fix: array bounds checks in codegen
docs: update architecture.md pipeline walkthrough
```

When a change crosses multiple phases, pick the dominant one.

## Pointers

- [PLAN.md](PLAN.md) — the original implementation blueprint. Every
  design decision is justified there.
- [CONTINUATION_PLAN.md](CONTINUATION_PLAN.md) — current status, known
  gaps, and phased next steps.
- [main.no](main.no) — the language specification. The source of truth
  for intended syntax and semantics (not all of it is implemented yet).
- [docs/architecture.md](docs/architecture.md) — the current module
  layout and pipeline walkthrough.
