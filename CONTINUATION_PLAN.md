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
- [x] Array bounds checks (2026-07-09) — `_emit_bounds_check` compares
      the index against the length stored at offset 0 of the array's
      heap block and branches to the existing panic path on an
      out-of-range access; wired into both `_emit_array_index` and
      `_emit_array_assign`. Golden test: `tests/codegen/array_bounds.no`.

## Phase 3 — Resume the roadmap — DONE (2026-07-09)

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
- [x] Typed IR layer decision (2026-07-09): NOT building one. A full
      PLAN.md §6 typed IR (desugar pattern matching/closures/`?`,
      insert drops, then lower to LLVM) is the "correct" architecture
      for a stackless-coroutine state-machine transform, but it's a
      rewrite of the codegen boundary, not an incremental step, and
      the 2300+ line `emit.py` monolith already works and is
      golden-tested end to end. Chose to implement async pragmatically
      instead (OS threads for `spawn`, blocking join for `await` — see
      below) specifically to avoid needing this. Revisit if/when true
      stackless coroutines (cheap enough to spawn millions of) become
      a real requirement, not preemptively.
- [x] `_emit_expr` now raises `NotImplementedError` on an unhandled AST
      node instead of silently returning `None` (2026-07-09) — closes
      the silent-miscompile hole flagged all the way back in Phase 1
      (every one of this session's confirmed-then-fixed miscompiles
      -- keyword literals, field assignment, string concat -- was
      exactly this failure mode: an unhandled case falling through to
      None and emitting nothing). Flipped now that the major gaps
      (tuples, HOFs, Map/Set, dyn, drops, exhaustiveness) are closed;
      verified zero regressions across the full golden suite (33
      cases) and every example/script in the repo.
- [x] v1.1 async/spawn/await (2026-07-09) — pragmatic OS-thread
      implementation per the typed-IR-layer decision above, not
      stackless coroutines. New `runtime/async.c`: `nyet_spawn`
      wraps `pthread_create`, `nyet_await` wraps `pthread_join`.
      Scoped to `(spawn (fn_name arg))` where `fn_name` is a
      top-level function with exactly one `ptr`-typed param and
      `ptr`-typed return (covers string/struct/sum-type/array/tuple —
      everything except bare scalars): that signature matches
      pthread's own `void *(*)(void *)` start-routine convention
      exactly, so the target function runs directly as the thread
      body with no trampoline, and its result comes back through
      `pthread_join`'s own out-parameter. `_emit_spawn`/`_emit_await`
      in emit.py, transparent type pass-through in typeck.py
      (mirrors how `?`/`Try` already worked), linked into both
      `driver.py`'s build command and the codegen test harness
      alongside `runtime/map.c`.
      Verified two ways: a struct-returning spawn/await round-trip,
      and a 4-way concurrent CPU-bound spawn (`tests/codegen/
      spawn_await.no` covers the former as a golden test; the
      latter was a throwaway timing check, not committed as a golden
      since wall-clock isn't deterministic) — 158% CPU utilization
      and wall time well under total user time confirmed genuine
      OS-level parallelism, not synchronous inlining.
      While building the concurrency test, tripped over a real
      pre-existing miscompile with i64 arithmetic (see below) —
      fixed as part of this verification since async's whole point
      is CPU-bound work, which meant reaching for i64 loop counters
      for the first time in any golden test.
- [x] Fixed three width-inference bugs surfaced by testing spawn/await
      with i64 loop counters (2026-07-09), all in `pynyet/codegen/emit.py`:
      1. `_emit_arith`'s integer path hardcoded `add/sub/mul/sdiv/srem
         i32` regardless of operand width — same class of bug `_emit_cmp`
         was already fixed for (via `_common_cmp_type`/`_coerce_int_to`)
         but the fix never propagated to arithmetic. Now reuses the same
         helpers.
      2. `_emit_out`'s integer branch always printed via `%d`/`i32`,
         truncating any `i64` argument — added an `i64`/`%lld` path
         (`_get_fmt_i64`), and narrower ints (bool/char) now go through
         `_coerce_int_to` up to i32 instead of being passed at their
         native (potentially sub-32-bit) width.
      3. The root cause: `_infer_llvm_type`'s `N.IntLit` case
         unconditionally returned `"i32"` regardless of the literal's
         actual magnitude (the parser drops any `i64` numeric suffix, so
         magnitude is the only signal left). This made `_emit_let`/
         `_emit_arith` coerce large literals via `sext i32 <value> to
         i64` — LLVM's IR parser parses the literal as a 32-bit constant
         *first* (silently wrapping values outside i32's range), then
         sign-extends the already-wrong wrapped result. E.g.
         `(let x:i64 5000000000i64)` silently became `705032704`. Fixed
         by inferring `i64` for any literal outside i32's range. The
         existing `cmp_widths.no` golden test didn't catch this because
         it only checks equality between two identically-truncated
         values, never the actual printed magnitude — a real coverage
         gap, not just a coincidence.
      New golden test `tests/codegen/arith_widths.no` pins down all
      three (large i64 literal arithmetic, an i64 accumulator mutated
      in a loop via `(+ i 1)` — the exact pattern that first exposed
      this — and an i64 operand paired with a bare int literal).
      Verified zero regressions: full golden suite (35 cases across
      lexer/parser/sema/codegen), 33 unit tests, `just fmt`/`lint`/
      `typecheck` all clean, and every script/example in the repo
      rebuilds successfully.
- [x] Pattern exhaustiveness as a proper sema diagnostic pass (2026-07-09)
      — moved the exact same variant-coverage logic from codegen's ad
      hoc stderr print (deleted `_exhaustiveness_warn` in emit.py) into
      `TypeChecker._check_match_exhaustive`, which now appends a real
      `Diagnostic(Severity.WARNING, ...)`. Required fixing `driver.py`
      first: `_cmd_check`/`_cmd_build` previously gated on *any*
      diagnostic (`if errors:`), which would have turned every
      non-exhaustive match into a hard failure instead of an advisory
      warning; now gates on `Severity.ERROR` specifically, printing
      warnings either way. Verified `driver check` now actually
      surfaces the warning (it never did before) while `check: ok`
      still prints and the exit code stays 0.

All the language-feature and correctness work planned for this phase
is done. Two housekeeping items remain, deliberately left for a
dedicated session rather than folded in here:

- [ ] AST statement/expression split so `parser.parser` and
      `codegen.emit` can drop their mypy exemption (Phase 1.6) — give
      `do`-block statement forms (`LetDecl`/`ConstDecl` used as an
      expression-position value) a real `Stmt` union instead of
      returning the base `N.Node` and letting every downstream
      constructor claim `Expr`. This is a real refactor across both
      files, not a quick annotation fix — risks regressions if rushed
      alongside feature work.
- [ ] Check whether `pynyet/ast/visitor.py` is dead code (0% coverage,
      Phase 1.6) — delete or start using it

- North star progress: `check main.no` errors are down to **44** (was
      68 pre-merge, 61 as of Phase 1.5) — the tuple/HOF/Map/keyword
      work landed this session incidentally fixed a batch of these
      (e.g. `map`/`filter`/`fold` were "undefined name" errors in
      main.no's own HOF examples until they became real builtins).
      Not yet zero; remaining errors are still mostly undefined stdlib
      names and free-floating snippet variables in the spec's example
      sections.
