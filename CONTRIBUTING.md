# Contributing to Nyet

Thanks for your interest in hacking on Nyet. See
[PLAN.md](PLAN.md) for the original compiler blueprint,
[CONTINUATION_PLAN.md](CONTINUATION_PLAN.md) for current status and
what's left, and [main.no](main.no) for the language specification
(it compiles and runs, and the test suite checks it; designs that
aren't implemented yet are in its "Planned" block).

## Development setup

- **Python 3.10+** is required. The compiler uses `from __future__ import
  annotations`, PEP 604 union syntax, and `list[int]`-style generics.
- The compiler has **no external Python dependencies** — it's pure
  stdlib. `llvmlite` is **not** used; codegen emits LLVM IR as text
  directly (`pynyet/codegen/emit.py`).
- **clang** is required to link generated `.ll` files into a native
  binary (`driver build` / `driver run`, and the codegen test harness).
- **[`just`](https://github.com/casey/just)** runs the dev commands
  (see the [justfile](justfile), or `just --list`).
- **Node/bun** (for `bun install` or `npm install`) is only needed for
  the commit-message git hook — unrelated to the compiler itself.
- **Dev tooling** (ruff, mypy, pytest, coverage — only needed for
  `just fmt`/`lint`/`typecheck`/`unit-test`/`coverage`, not for running
  the compiler itself):
  ```bash
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements-dev.txt
  ```

## Running tests

Four golden-file harnesses, one per compiler phase:

```bash
python3 tests/lexer/run.py              # check every case
python3 tests/lexer/run.py hello        # check just tests/lexer/hello.no
python3 tests/lexer/run.py --update     # rewrite all golden files
```

Same interface for `tests/parser/run.py`, `tests/sema/run.py`, and
`tests/codegen/run.py` — or run everything with `just test-all`. See
[tests/README.md](tests/README.md) for what each phase's golden format
looks like, and the [justfile](justfile) (`just --list`) for every
available command.

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

## Unit tests

`tests/unit/` (pytest) complements the golden harnesses above: it
covers individual functions in isolation (e.g. `Span.line_col()`,
`TypeChecker._compatible()`) rather than full pipeline behavior. Add a
unit test when you're testing pure logic that doesn't need a full
`.no` file round-trip; add a golden-file fixture when you're testing
observable compiler behavior end-to-end.

```bash
just unit-test          # or: python3 -m pytest tests/unit/
```

## Coding conventions

- **Python 3.10+ syntax.** Use `from __future__ import annotations` at the
  top of every module and prefer `list[int]` / `dict[str, T]` over the
  `typing` aliases.
- **Stdlib only** in the compiler itself. `ruff`/`mypy`/`pytest` are
  dev-only tooling (see above), never a runtime dependency.
- **`just fmt` / `just lint` / `just typecheck`** before committing —
  the pre-commit hook runs format+lint automatically if ruff is
  installed (see Development setup), CI always does regardless.
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
- **mypy is not yet fully strict everywhere** — `pynyet.parser.parser`
  and `pynyet.codegen.emit` are exempted in `pyproject.toml`
  (`[[tool.mypy.overrides]]`) pending an AST redesign to distinguish
  expression-like and statement-like nodes properly. See
  CONTINUATION_PLAN.md. Don't add new files to that exemption list.

## Commit style

Commit messages must follow [Conventional Commits](https://www.conventionalcommits.org/):

```
type(scope): subject
```

`type` is one of `feat`, `fix`, `docs`, `style`, `refactor`, `perf`,
`test`, `build`, `ci`, `chore`, `revert`. `scope` is optional but
encouraged — e.g. `lang`, `codegen`, `sema`, `lsp`, `repo`. Examples:

```
feat(lang): add char primitive type and (as expr type) casts
fix(codegen): array bounds checks
docs: update architecture.md pipeline walkthrough
chore(repo): rebuild test harness after v0.4-v1.0 rescue
```

This is enforced by a `commit-msg` git hook (Husky + commitlint) — a
non-conforming message is rejected at commit time. First-time setup:

```bash
bun install   # or npm install — installs husky + commitlint, wires up hooks via `prepare`
```

If you don't have the hook tooling installed, `git commit` still works
(hooks just won't run) — but CI/reviewers expect the convention
regardless, so follow it by hand in that case.

## Pointers

- [PLAN.md](PLAN.md) — the original implementation blueprint. Every
  design decision is justified there.
- [CONTINUATION_PLAN.md](CONTINUATION_PLAN.md) — current status, known
  gaps, and phased next steps.
- [main.no](main.no) — the language specification. The source of truth
  for intended syntax and semantics. Everything outside its "Planned"
  block is implemented and runs in the test suite; when you change the
  language, update it and `tests/codegen/main_no.golden`.
- [docs/architecture.md](docs/architecture.md) — the current module
  layout and pipeline walkthrough.
