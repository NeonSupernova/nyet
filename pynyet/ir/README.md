# pynyet.ir

Mid-level intermediate representation and lowering passes. This package is
currently a placeholder; see PLAN.md §6 for the authoritative specification of
the v0.4 IR pipeline.

## Planned modules

- `ir.py` — typed, desugared, SSA-ish mid-level IR node definitions.
- `lower.py` — lowering from the semantic-analysis AST down to mid-level IR.
- `monomorph.py` — generic specialization and name mangling.

None of these modules exist yet. The package is tracked in git so that
`import pynyet.ir` succeeds.
