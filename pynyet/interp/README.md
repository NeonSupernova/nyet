# pynyet.interp

Tree-walk interpreter for the Nyet language. This is a developer-facing
validation and iteration tool, **not** the main execution path — production
execution goes through LLVM codegen in `pynyet.codegen`. See PLAN.md §1 for
where the interpreter fits into the overall project layout.

## Planned modules

- `interp.py` — tree-walking evaluator over the AST.
- `value.py` — runtime value representation used by the evaluator.

None of these modules exist yet. The package is tracked in git so that
`import pynyet.interp` succeeds.
