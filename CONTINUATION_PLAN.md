# Nyet Continuation Plan (2026-07-08)

Written after a full repo survey (multi-agent) following ~10 weeks idle.
Source of truth for phase tracking; check off items as they land. See
PLAN.md for the original design blueprint — this file is the "what to
do next" companion, not a replacement.

## Headline finding

Seven unpushed commits on local branch `claude/naughty-jemison-82a949`
implemented PLAN.md milestones v0.4 through v1.0 (traits/operator overload,
a 476-line borrow checker, closures via lambda lifting, nested
patterns/guards/exhaustiveness, hygienic+variadic macros, modules, file
IO — +2312/-189 across 37 files). Verified green in a worktree: all
examples incl. v04-v10 demos pass, use-after-move correctly rejected.
It branched from the exact tip of the default branch and fast-forwarded
cleanly — merged 2026-07-08, tagged v0.4 through v1.0.

## Phase 0 — Protect and land the lost work — DONE (2026-07-08)

- [x] Push `claude/naughty-jemison-82a949` to the remote
- [x] Fast-forward merge it into the default branch
- [x] Tag each milestone commit (v0.4 ... v1.0)
- [x] Push updated default branch + tags
- [x] Delete fully-merged dead branches (local: gifted-joliot-be8093,
      lucid-yalow, nostalgic-heyrovsky, relaxed-engelbart, plus
      naughty-jemison-82a949 once merged; remote: lucid-yalow,
      nostalgic-heyrovsky, relaxed-engelbart — naughty-jemison-82a949
      kept on remote as a redundant safety copy)
- [x] Rename default branch `origin` -> `main` on GitHub

## Phase 1 — Rebuild the safety net — DONE (2026-07-08)

- [x] Implement `tests/sema/run.py` and `tests/codegen/run.py` dump
      functions
- [x] Seed sema goldens (uaf_let/uaf_branch/uaf_struct/borrow_ok/branch_ok,
      moved from loose `tests/*.no` into `tests/sema/`) and codegen
      goldens (closure_*/match_*, moved into `tests/codegen/`)
- [x] Add GitHub Actions: `just test-all` + hello-world e2e smoke
      (`.github/workflows/ci.yml`; originally `make`, migrated to
      `just` — see Phase 1.6 below)
- [x] Make `build` fail on all sema errors (expand/resolve/typeck/borrow),
      not just borrow errors (`pynyet/driver.py`) — verified no example
      regressions
- [x] Rewrite CLAUDE.md, docs/architecture.md, CONTRIBUTING.md,
      tests/README.md, examples/README.md against the real pipeline
- [x] Delete dead legacy files: pynyet/lexer/lexer.py,
      pynyet/ast/ast.py, pynyet/codegen/codegen.py
- [x] Delete stale .vscode/tasks.json; fix .claude/launch.json
- [x] Fix .gitignore: `scripts/*.no` was being silently excluded by the
      blanket `*.no` rule (no exception like examples/tests had) — six
      real scripts existed on disk but were never tracked in git. Added
      `!scripts/*.no` and staged them. Also removed the `.claude/`/
      `.vscode/` ignore lines that contradicted already-tracked files
      inside them.

Not done (lower priority, didn't block anything): `_emit_expr` still
returns `None` silently on unhandled AST nodes instead of raising —
left as-is since flipping it now would surface every remaining
unimplemented-feature gap as a hard crash mid-session; revisit
alongside Phase 3 feature work instead.

## Phase 1.5 — Landed the `char`/`as` cast feature — DONE (2026-07-08)

Found in-progress, uncommitted work on disk (another concurrent Claude
Code session) implementing a `char` primitive type and `(as expr type)`
explicit cast — tokenizer, parser, AST, typeck, emit, pretty-printer.
It predated and overlapped the Phase 0 merge, so it was stashed first
to protect it, then re-verified and finished directly against the
merged tree once the concurrent session went idle:

- [x] Fixed `_compatible()` in `pynyet/sema/typeck.py` so a bare
      (unsuffixed) integer literal satisfies any integer or `char`
      annotation, not just float — this also fixes the pre-existing
      known bug where `(let s:usize 0)` / `(let d:u64 100000)` failed
      to typecheck (main.no's own §0 spec-patch lines). `check main.no`
      error count dropped 68 -> 61.
- [x] Fixed `_emit_cast`'s int->char bounds-check panic in
      `pynyet/codegen/emit.py`, which called an undefined
      `@nyet_panic` extern (not backed by anything in `runtime/`,
      inconsistent with the rest of the codebase) — replaced with the
      same printf+exit(1) pattern `_emit_panic` already uses elsewhere.
      Verified the panic path actually fires on an invalid surrogate
      value.
- [x] Verified end-to-end via `scripts/cast.no` (the feature author's
      own test script) and added proper golden coverage:
      `tests/lexer/casts.no`, `tests/parser/casts.no`,
      `tests/codegen/cast.no`.
- [x] Documented `char` and `as` in CLAUDE.md's type list.

The stash from before the merge (`git stash list`) is now superseded
by this work and safe to ignore/drop — every file's net diff against
HEAD was independently verified clean.

## Phase 1.6 — Dev tooling — DONE (2026-07-08)

- [x] Replaced Makefile with justfile (`just --list`); all docs/CI
      updated from `make` to `just`
- [x] Conventional Commits enforced via Husky `commit-msg` hook
      (commitlint, `.commitlintrc.json`) — verified rejecting a
      non-conforming message and accepting a conforming one. Also
      enforced in CI (`wagoid/commitlint-github-action`) so a
      contributor without the local hook installed can't slip through.
- [x] Added `ruff` (format + lint), `mypy` (type check), `pytest`
      (`tests/unit/`, complementing the golden harnesses), and
      `coverage` (combined across unit tests + all four golden
      harnesses — the golden harnesses exercise far more of the
      compiler than the unit tests alone, so `just coverage`
      accumulates both rather than reporting pytest-only numbers,
      which would be misleadingly low). Config lives in
      `pyproject.toml`; dev deps in `requirements-dev.txt`.
- [x] Fast pre-commit hook (`.husky/pre-commit`) runs `ruff format
      --check` + `ruff check`, skipping gracefully with a warning if
      ruff isn't installed rather than blocking every commit.
- [x] Formatted the whole `pynyet/` tree with `ruff format` (one
      isolated commit, zero behavior change, verified via full test
      suite before/after) and fixed every `ruff check` finding by hand
      (not blind auto-fix) — mostly unused imports, a few genuine
      `Optional`-typed parameters annotated as required (the functions
      already null-checked at runtime; only the annotation was wrong),
      and a couple of mutually-exclusive-branch variable-name reuses
      that confused mypy's flow analysis. `E702` (semicolon-joined
      statements) is allow-listed — it's a deliberate compact-dispatch
      style in the lexer/parser, not worth a rewrite.
- [x] `mypy` is fully clean on `pynyet.source`, `pynyet.diagnostic`,
      `pynyet.ast.*`, `pynyet.lexer.*`, `pynyet.sema.*` (including
      `borrow.py`, `resolve.py`, `typeck.py` — all newly clean this
      session). `pynyet.parser.parser` and `pynyet.codegen.emit` are
      exempted (`[[tool.mypy.overrides]]` in pyproject.toml): both hit
      the same root cause — `parse_expr()` is typed to return the base
      `N.Node` because it can legitimately yield a `LetDecl`/`ConstDecl`
      (statement-like forms inside a `do` block) alongside a real
      `Expr`, but nearly every downstream AST field/constructor
      declares `Expr`, so ~95 call sites trip a type error. Fixing this
      properly means giving the AST a real statement/expression
      distinction (e.g. a `Stmt` union), not a quick annotation tweak —
      tracked as a Phase 3 candidate below.
- [x] Combined coverage (unit + golden harnesses): 60% of `pynyet/`.
      Notably low spots: `pynyet/codegen/emit.py` at 45% (the largest,
      riskiest file, and where the known miscompiles below live —
      corroborates that these are real, unexercised paths, not edge
      cases), `pynyet/driver.py` at 0% (the golden harnesses call
      compiler passes directly in-process, bypassing the CLI entry
      point entirely — driver.py itself has no test coverage at all),
      `pynyet/ast/visitor.py` at 0% (looks unused — worth checking if
      it's dead code).
- [x] Evaluated and explicitly skipped (not a fit / not worth it right
      now, per discussion): structured logging, health endpoints,
      `.env.example`, OpenAPI generation (no service/API surface in a
      CLI compiler), Dockerfile, dev container, Renovate/Dependabot,
      automatic semver releases (conflicts with the existing
      milestone-based v0.x/v1.x tagging).

## Phase 2 — Fix surviving miscompiles, revive the LSP — DONE (2026-07-08)

- [x] Pin `lsp/requirements.txt` to `pygls>=1.3,<2` — verified pygls
      1.3.1 installs and the server starts cleanly on stdio
- [x] Rewire LSP diagnostics to `pynyet.lexer.scanner.lex` /
      `pynyet.parser.parser.parse` (was importing the now-deleted rply
      path, silently returning zero diagnostics forever) — now returns
      real spans instead of the regex-based `parse_error_position`
      heuristic; verified against both a malformed and a clean document
- [x] Unsigned/char literal defaulting fixed as part of Phase 1.5 above
- [x] `let` immutability enforcement — assigning to a `let` (not `var`)
      binding is now a check-time error. Caught and fixed a real bug in
      the first version of this fix: `TypeChecker.mutable` wasn't
      scoped per-function like `env` already is, so a stale entry from
      an unrelated `let` elsewhere in the file could leak into a
      same-named function parameter. Verified by diffing
      `check main.no`'s exact error list before/after, not just the
      count — `mutate`'s `x:&!i32` param was a real false positive
      until the scoping fix landed.
- [x] Field assignment codegen — `(= (. p x) 9)` previously no-op'd
      silently; now correctly updates the field
      (`_emit_field_ptr`/`_emit_assign`)
- [x] String concat codegen — `(+ "a" "b")` previously emitted invalid
      LLVM IR; now does malloc+strcpy+strcat (`_emit_string_concat`),
      with `_infer_llvm_type` also fixed so `(out (+ a b))` and
      `(let x (+ a b))` pick the right printf/store type
- [x] Real `Keyword` type distinct from `string` — `:read` no longer
      typechecks as a string or falls through codegen's silent-None
      default. Keywords intern to a small integer ID (allocation-free,
      compared by identity per main.no's own description); verified
      `==` correctly distinguishes different keyword names
- [ ] Array bounds checks — not attempted this session; length is
      already stored at offset 0 of the array's heap block, needs a
      compare + branch to the existing panic path
      (`pynyet/codegen/emit.py`, array indexing)

## Phase 3 — Resume the roadmap

- [ ] AST statement/expression split so `parser.parser` and
      `codegen.emit` can drop their mypy exemption (Phase 1.6) — give
      `do`-block statement forms (`LetDecl`/`ConstDecl` used as an
      expression-position value) a real `Stmt` union instead of
      returning the base `N.Node` and letting every downstream
      constructor claim `Expr`
- [ ] Check whether `pynyet/ast/visitor.py` is dead code (0% coverage,
      Phase 1.6) — delete or start using it
- [x] Tuple codegen (2026-07-09) — construction, `(t i)` indexing
      matching main.no's own documented call-syntax, `#(T1 T2)`
      annotations on lets/params/returns. Gaps: destructuring
      `(let #(a b) expr)` still just binds a dummy `_destructure` name
      (parser discards the pattern -- separate feature); generic
      functions returning a tuple built from type params (`#(B A)`)
      don't substitute correctly in typeck's generic instantiation.
- [x] stdlib HOFs (2026-07-09): map/filter/fold/any/all over Array[T],
      plus zip (pairs into an array of tuples). All match main.no's own
      call syntax (array/arrays last). flat_map deliberately not
      implemented (dynamic-size result, needs more machinery) and not
      registered as a builtin, so it fails cleanly as "undefined name".
- [x] Map/Set runtime + codegen (2026-07-09) — see the runtime-fate
      decision below. Map[string V] fully works: `{k v ...}`
      construction, `(m key)` lookup, `(= (m key) v)` insert/update,
      matching main.no's own documented example exactly. Values are
      generic 8-byte slots (sext/trunc for ints, ptrtoint/inttoptr for
      pointers, bitcast for f64), the real type tracked statically per
      binding the same way Array[T] tracks its element type. Only
      string keys are supported (the runtime hashes/compares C
      strings) -- Map[i32 V] etc. isn't recognized. Set[T] has runtime
      support (nyet_set_new/add/contains/count) but main.no defines no
      construction syntax for it at all (no SetLit AST node, no `{...}`
      equivalent) -- not a codegen gap, a language-design gap; skipped
      rather than invent syntax unilaterally.
- [x] `dyn` trait objects + vtables (2026-07-09) — `&dyn Trait` params
      work with true dynamic dispatch: `dyn Trait` lowers to a 2-word
      fat pointer `{data, vtable}` (`_get_or_register_dyn_type`); each
      `(concrete_type, trait)` pair gets a synthesized global vtable
      constant (`_get_or_register_vtable`) built from `_method_impls`
      in the trait's declared method order (now tracked via
      `_traits`, populated from `TraitDecl` -- codegen never needed
      this for static dispatch before). Verified genuine dynamic
      dispatch, not accidental static resolution: two different struct
      types (Point2, Circle) both implementing Display are passed
      through the *same* function and each calls its own impl.
      Coercion happens at the call site (`_emit_user_call`, guided by
      `_fn_param_dyn_traits`) when an argument is passed to a
      dyn-typed parameter. Scoped to that one pattern -- the only
      concretely demonstrated one in main.no; local `let x:&dyn Trait`
      bindings and primitive types implementing a trait (main.no's own
      `(log_value &42)` example) aren't attempted. `Self`/`&Self` in a
      trait method's signature is treated as `ptr`, matching how every
      impl actually passes structs regardless of ownership.
- [x] Drop insertion (2026-07-09) -- narrowly and deliberately scoped
      given the correctness stakes (a wrong drop is a use-after-free or
      double-free, strictly worse than the leak-everything status quo
      it replaces). Struct-typed locals only (arrays/tuples/sum
      types/dyn objects/closures still leak); only bindings declared
      unconditionally at a function's own top level (not inside an
      if/match/loop branch); only freed at the function's natural
      end-of-body fallthrough, never at an explicit `return` or the
      `?` operator's early-return (a binding declared later in the
      body wouldn't be initialized yet at an earlier exit, and this
      pass doesn't do position-aware liveness analysis). Reuses the
      borrow checker's own move-tracking (`compute_drop_names` in
      pynyet/sema/borrow.py) rather than reimplementing it, via a
      second BorrowChecker pass kept isolated from the
      diagnostic-producing path.
      Caught and fixed a real soundness bug while writing the unit
      tests: an all-primitive-field struct is legitimately Copy per
      the borrow checker's language-level semantics, so passing it by
      value to another function doesn't mark it `moved` -- but codegen
      always passes structs as an aliased pointer, never a true
      bitwise copy, regardless of Copy-ness. The first version trusted
      `_Binding.moved` alone and would have freed a struct the callee
      still held a live alias to. Fixed with a second, independent
      check (`_walk_by_value_call_args`) that excludes anything ever
      passed by value to a non-builtin call, regardless of what the
      borrow checker's Copy/Move reasoning concluded.
      Verified two ways: 9 unit tests pin down the exact boundary
      cases (returned/moved/borrowed/param/builtin-call/non-struct),
      and an end-to-end run of 20 million allocate-and-drop cycles
      held peak memory at ~1.3MB (`/usr/bin/time -l`) -- would be
      300MB+ if leaking, confirming drops actually fire at scale with
      no accumulation.
- [x] Decided the fate of the orphaned C runtime (2026-07-09): the
      original runtime/{alloc,string,io}.c used a length-prefixed fat
      `nyet_string` struct that never matched what codegen actually
      emits (plain null-terminated C strings) -- those three files
      remain unlinked/orphaned. Added a new, separate
      runtime/map.c using the representations codegen actually uses;
      `driver.py`'s build command and the codegen test harness both
      link it into every build now (tiny, unconditional, unused
      symbols cost nothing if a program doesn't touch Map/Set).
- [ ] Consider introducing the typed IR layer (PLAN.md §6) before
      attempting v1.1 async/spawn/await
- [ ] v1.1 async/spawn/await
- [ ] Pattern exhaustiveness as a proper sema diagnostic pass (currently
      an ad hoc warning printed from inside codegen's match lowering,
      not through the diagnostic pipeline — `driver check` never sees it)
- [ ] `_emit_expr` should raise on unhandled AST nodes instead of
      silently returning `None` (`pynyet/codegen/emit.py`) — deferred
      from Phase 1, do this alongside closing the feature gaps above so
      it doesn't just turn "missing feature" into "crash" for no gain
- [ ] North star: `check main.no` reaches zero errors (was 68 pre-merge,
      61 as of Phase 1.5 — mostly undefined stdlib names and
      free-floating snippet variables in the spec's example section)
