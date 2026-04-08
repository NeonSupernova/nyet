# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Nyet is a programming language with a Lisp-like, S-expression syntax and a Rust-inspired ownership model. It has two implementations: a primary Python/LLVM compiler (`pynyet/`) and a reference C++ compiler (`nyet_compiler/`) using Bison/Flex.

## Commands

```bash
# Generate LLVM IR from input.toy
python3 app.py

# Full build pipeline
make all          # runs app.py then compiles .ll to binary
make output.ll    # only runs app.py (Python pipeline)
make output       # compiles output.ll to binary via clang
make clean        # removes generated output files

# Compile LLVM IR manually
clang -o output output.ll
```

There are no automated tests — `tests/` contains `.no` source files used as manual input samples.

## Architecture

The Python compiler pipeline lives entirely in `pynyet/`:

```
app.py  →  pynyet/lexer/lexer.py  →  pynyet/parser/parser.py  →  pynyet/codegen/codegen.py
                                               ↓
                                    pynyet/ast/ast.py  (AST nodes)
```

- **Lexer** (`pynyet/lexer/lexer.py`) — tokenizes source using `rply`; tokens include `PRINT`, `NUMBER`, `SUM`, `SUB`, etc.
- **Parser** (`pynyet/parser/parser.py`) — builds AST via `rply` LR parser; calls `get_parser()`
- **AST** (`pynyet/ast/ast.py`) — node classes (`Number`, `BinaryOp`, `Sum`, `Sub`, `Print`) each implement `.eval()` for tree-walk interpretation
- **CodeGen** (`pynyet/codegen/codegen.py`) — uses `llvmlite` IRBuilder to emit LLVM IR; manages module, builder, and execution engine
- **`app.py`** — orchestrates the full pipeline; reads `input.toy`, runs lex → parse → codegen → writes `output.ll`

The C++ implementation in `nyet_compiler/` uses Flex (`lexer.l`) and Bison (`parser.y`/`bison.y`) with AST nodes declared in `node.h`. It supports a broader feature set (functions, if statements, variables) than the current Python implementation.

## Nyet Language Syntax

```
; single-line comment
;; section comment
;;; block comment

(fn sum:int (arg1, arg2, arg3) (return (+ arg1 arg2 arg3)))
(let x:i32 42)
(= x 10)
(if (== x 10) (out "yes") (pass))
(out "Hello World")
```

Types: `int`, `double`, `string`, `bool`. Mutable variants are prefixed with `m` (e.g. `mi32`, `mstring`).

Input files use the `.no` extension. The current Python pipeline reads from `input.toy` (hardcoded in `app.py`).

## Dependencies

- `rply` — lexer/parser generator
- `llvmlite` — LLVM Python bindings
- `clang` — for compiling generated `.ll` files to native binaries
