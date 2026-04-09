# pynyet.sema

Semantic analysis passes for the Nyet compiler. This package is currently a
placeholder; see PLAN.md §5 for the authoritative specification of the v0.3
semantic pipeline.

## Planned modules

- `expand.py` — macro expansion.
- `resolve.py` — name resolution and symbol table construction.
- `typeck.py` — Hindley-Milner type inference and checking.
- `traits.py` — trait resolution, coherence, and vtable layout.
- `borrow.py` — ownership and borrow checking.
- `exhaust.py` — pattern match exhaustiveness checking.

None of these modules exist yet. The package is tracked in git so that
`import pynyet.sema` succeeds and downstream work has a stable home.
