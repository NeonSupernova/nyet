"""Semantic analysis passes.

v0.3 modules:
    types.py    — NyetType hierarchy (semantic types, not AST)
    resolve.py  — name resolution and symbol tables
    typeck.py   — type inference and checking

Planned (v0.4+):
    expand.py   — macro expansion
    traits.py   — trait resolution, coherence, vtable layout
    borrow.py   — ownership and borrow checking
    exhaust.py  — pattern exhaustiveness
"""
