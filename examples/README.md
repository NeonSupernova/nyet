# Nyet examples

Runnable `.no` programs exercising each milestone through v1.0. Run any
of them with:

```bash
python3 -m pynyet.driver run examples/<name>.no
# or
just run examples/<name>.no
```

Everything directly in `examples/` is self-contained and needs no
stdin (`v10_demo.no` writes and reads a file of its own). Two
subdirectories hold the rest:

- **`tour/`** — the guided five-program tour, in reading order, that
  ships as the `demo\` folder of the Windows bundle.
- **`interactive/`** — programs that read stdin, so they are run by
  hand rather than in any harness.

Complete programs rather than single-feature examples — the arcade
suite — live in `demos/`, and the language reference is `main.no` at
the repo root.

## v0.1 — primitives, let/if/do/fn, arithmetic

- `hello.no` — canonical hello world.
- `arith.no` — immutable `let` bindings with explicit `i32` annotations.
- `branch.no` — `if` as an expression.
- `v01_demo.no` — exercises the full v0.1 subset in one program.

## v0.2 — structs, sum types, match, arrays

- `shapes.no` — shape-area calculator (structs, sum types, match, float
  arithmetic).
- `shapes_match.no` — same, via a sum type + `match`.
- `v02_demo.no` — exercises the full v0.2 subset.

## v0.3 — generics, monomorphization, Option/Result, `?`

- `generic_first.no`, `generic_pair.no`, `generic_struct.no`,
  `generic_test.no` — generic functions/structs over one or two type
  parameters.
- `generic_option.no`, `generic_result.no` — `Option[T]` / `Result[T E]`.
- `try_option.no`, `try_chain.no` — the `?` early-return operator,
  including chained calls.

## v0.4 — traits, operator overloading

- `v04_demo.no` — inherent methods, `+`/`==`/`!=` overloads on a
  struct, and `display` dispatch via `out`.
- `vec2.no` — `Vec2` with an `impl` block, `&T` self params, and
  methods returning structs.

## v0.5 — move semantics, borrow checker

- `v05_demo.no` — moves and borrows accepted by the checker.
- `v05_use_after_move.no` — a **negative** example: the borrow checker
  correctly rejects this program.

## v0.6 — closures

- `v06_demo.no` — closures and higher-order functions.

## v0.7 — nested patterns, guards, exhaustiveness

- `v07_demo.no` — nested `match` patterns and `when` guards.

## v0.8 — macros

- `macro_unless.no` — basic macro expansion: `(unless cond body)` →
  `(if (! cond) body pass)` before the rest of sema runs.
- `macro_hygiene.no` — hygienic macro expansion (no accidental capture).
- `macro_variadic.no` — variadic macros and quote-based source capture.

## v0.9 — modules

- `v09_demo.no` / `v09_lib.no` — multi-file compilation via `(use ...)`.

## v1.0 — file IO

- `v10_demo.no` — reads a text file, transforms its contents, writes it
  back out.

## tour/ — the guided set shipped on Windows

- `01_hello.no`, `02_structs_and_match.no`, `03_ownership.no`,
  `03_ownership_bad.no` (rejected on purpose),
  `04_generics_and_option.no`, `05_closures.no`.

## interactive/ — reads stdin, run by hand

- `inp.no` — reads an integer and reports whether it is even.
- `isbn.no` — validates an ISBN typed at the prompt.
- `funky_numbers.no` — a small number-classification prompt.

## Other self-contained examples

- `cast.no` — `(as expr type)` casts and `char`/int conversions.
- `dropwhile.no` — `drop_while` over an array.
- `gen.no` — Euler's pentagonal recurrence with an `Array[i32]` memo.
- `seq.no` — sequence functions.
- `x.no` — a minimal `if` expression.

See `tests/{lexer,parser,sema,codegen}/` for the golden-file regression
suite (a subset of these behaviors, wired into `just test-all`).
