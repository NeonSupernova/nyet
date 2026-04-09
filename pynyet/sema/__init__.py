"""Semantic analysis passes.

This package is a placeholder for the v0.3 semantic pipeline per PLAN.md §5:

    expand.py   — macro expansion
    resolve.py  — name resolution and symbol tables
    typeck.py   — Hindley-Milner type inference and checking
    traits.py   — trait resolution, coherence, vtable layout
    borrow.py   — ownership and borrow checking
    exhaust.py  — pattern exhaustiveness

None of these modules exist yet. This package exists so
`import pynyet.sema` succeeds, and so the directory is tracked in git.
"""
