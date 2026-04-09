# Nyet Implementation Blueprint

The full project plan for implementing the Nyet language as specified in `main.no`.

---

## 0. Spec Patches (prerequisite)

Three small additions to `main.no` before anything else:
- Add `usize` to the primitives list (platform-width unsigned integer, the canonical indexing type)
- Define `:keyword` literals: immutable interned strings used as tags — e.g. `:digital`, `:read`. Cheap to compare, no allocation, no Display needed.
- Define `Self` in trait contexts: stands for "the type implementing this trait." Inside `impl Foo Bar`, `Self` resolves to `Bar`.

That closes every undefined reference in the spec.

---

## 1. Project Layout

The current `pynyet/` tree is far too small for what's needed. Proposed restructure:

```
pynyet/
  driver.py          # CLI entry: parses args, orchestrates pipeline
  source.py          # SourceFile, Span, line/column tracking
  diagnostic.py      # Error reporting with spans and hints

  lexer/
    token.py         # Token kinds (enum) and Token class
    lexer.py         # Hand-written scanner

  parser/
    parser.py        # Recursive descent
    special_forms.py # Dispatch table: let, var, fn, if, match, etc.

  ast/
    nodes.py         # All AST node classes
    visitor.py       # Base traversal
    pretty.py        # S-expr pretty printer (for debug)

  sema/
    expand.py        # Macro expansion
    resolve.py       # Name resolution / symbol tables
    typeck.py        # Type inference + checking
    traits.py        # Trait resolution, coherence
    borrow.py        # Ownership + borrow checker
    exhaust.py       # Pattern exhaustiveness

  ir/
    ir.py            # Custom mid-level IR (typed, desugared, SSA-ish)
    lower.py         # AST → IR
    monomorph.py     # Generic specialization

  codegen/
    codegen.py       # IR → LLVM via llvmlite
    runtime_decls.py # extern declarations for runtime calls

  interp/            # Tree-walk interpreter (for fast iteration + feature validation)
    interp.py
    value.py

runtime/             # C runtime, compiled separately, linked into output
  alloc.c
  string.c
  io.c
  async.c

tests/
  lexer/             # golden: source → tokens
  parser/            # golden: source → AST
  sema/              # type-check expected pass/fail
  codegen/           # end-to-end: source → binary → stdout
  runtime/           # C runtime unit tests

examples/            # real .no programs
main.no              # language spec (exists)
```

Keep `nyet_compiler/` (C++ reference) untouched as documentation of the old approach. It's reference material, not an active path.

---

## 2. Lexer

**Approach:** hand-written scanner, not rply. rply cannot cleanly handle numeric suffixes, path identifiers with `/`, multi-tier comments, or the `#(` tuple prefix without fighting it. A ~300-line hand-written scanner is simpler.

### Token kinds

**Structural:** `LPAREN LBRACKET LBRACE RPAREN RBRACKET RBRACE HASH_LPAREN COLON COMMA DOT_DOT ARROW`

**Operators (longest-match first):**
- Two-char: `EQ_EQ BANG_EQ LT_EQ GT_EQ LT_LT GT_GT AND_AND OR_OR AMP_BANG PIPE_ARROW`
- One-char: `PLUS MINUS STAR SLASH PERCENT EQ LT GT AMP BAR CARET TILDE BANG QUESTION DOT`

**Literals:** `INT_LIT FLOAT_LIT STRING_LIT BOOL_LIT KEYWORD_LIT` — each carries its value and (for numbers) an optional suffix.

**Keywords:** `LET VAR CONST FN FN_BANG MOVE IF MATCH WHEN DO LOOP BREAK RETURN PASS STRUCT TYPE NEWTYPE ALIAS TRAIT IMPL MACRO QUOTE MODULE USE PUB ASYNC AWAIT SPAWN SELF SELF_TYPE DYN`

**Type keywords:** `I8 I16 I32 I64 U8 U16 U32 U64 USIZE F32 F64 BOOL_T STRING_T UNIT_T`

**Trivia (preserved for autodocs, skipped for parsing):** `COMMENT_INLINE COMMENT_BLOCK COMMENT_SECTION COMMENT_FILE` — distinguished by leading `;` count.

**Meta:** `IDENT UNDERSCORE EOF`

### Key lexing rules

- **Identifiers with `/`:** `io/def`, `std/math/sqrt` tokenize as a single `IDENT`. Rule: `[a-zA-Z_][a-zA-Z0-9_]*(/[a-zA-Z_][a-zA-Z0-9_]*)*`. A bare `/` (surrounded by whitespace/non-ident chars) is `SLASH`. You must write `(/ a b)` with spaces for division. No ambiguity.
- **Identifier suffix chars:** allow trailing `!` or `?` (Lisp-style). `set!`, `empty?`. This is how we get `fn!` as a distinct token.
- **Numeric suffixes:** `42i64`, `3.14f32`, `255u8` — lexer consumes the suffix and attaches it to the literal token.
- **Base prefixes:** `0x` hex, `0b` binary, `0o` octal.
- **String escapes:** `\n \t \r \\ \" \0 \xHH`.
- **Keyword literals:** `:foo` — single token, value is the interned name.
- **Comment tiers:** count leading `;`, emit the appropriate comment token with the count. Drop the rest for the parser; autodoc tool picks them up later from the token stream.

### Tests

Golden files: `tests/lexer/foo.no` → `tests/lexer/foo.tokens`. One file per feature. Diff-based CI.

---

## 3. Parser

**Approach:** recursive descent. S-expressions make the outer structure trivial — the hard part is dispatching on the head of each list to the right sub-parser.

### Core loop

```
parse_program() → list of top-level declarations
parse_expr():
  if LPAREN:
    consume (
    if head is a special form keyword → call specialized parser
    else → parse as function call: head + args until )
  elif LBRACKET: parse_array_lit
  elif HASH_LPAREN: parse_tuple_lit
  elif LBRACE: parse_map_lit
  else: parse_atom (literal, ident, keyword-lit)
```

### Special forms (dispatched on first token inside `(`)

| Head | Produces |
|---|---|
| `let`, `var`, `const` | `LetDecl` / `VarDecl` / `ConstDecl` |
| `fn`, `fn!` | `FnDecl` (if followed by ident) or `FnExpr` (if followed by param list) |
| `move` | `FnExpr` with `CaptureMode.MOVE` |
| `if` | `If` |
| `match` | `Match` |
| `do` | `Do` |
| `loop` | `Loop` |
| `break`, `return` | `Break` / `Return` |
| `pass` | `Pass` |
| `struct` | `StructDecl` |
| `type` | `TypeDecl` (sum type) |
| `newtype`, `alias` | `NewtypeDecl` / `AliasDecl` |
| `trait` | `TraitDecl` |
| `impl` | `ImplDecl` (trait optional) |
| `macro`, `quote` | `MacroDecl` / `Quote` |
| `module`, `use`, `pub` | `ModuleDecl` / `UseDecl` / public modifier |
| `async`, `await`, `spawn` | `FnDecl(is_async=true)` / `Await` / `Spawn` |
| `=` | `Assign` |
| `?` | `Try` |
| `io/def` | sugar — desugar during AST lowering |
| anything else | `Call(head, args)` |

### Type expressions

Type expressions appear after `:` in annotations and after `->` in function returns. Parse:
- `IDENT` → `NamedType` (or `PrimType` for reserved names like `i32`)
- `IDENT[T1 T2]` → `GenericType`
- `&T`, `&!T` → `RefType`
- `(fn T1 T2 -> R)` → `FnType`
- `#(T1 T2 T3)` → `TupleType`
- `dyn T` → `DynType`
- `Self` → `SelfType`

### Patterns (in `match` arms)

- Literal: `0`, `"foo"`, `true`
- Var: `x` (IDENT in pattern position)
- Wildcard: `_`
- Variant unit: `Point`, `North`
- Variant with fields: `(Circle r)`, `(Rect w h)`
- Nested: `(Node (Leaf a) (Leaf b))`
- Tuple: `#(a b)`
- Struct destructure: `(Person name:n age:a)`
- Guarded: `((Some v) when (> v 0))` — an extra pair wrapping pattern + `when` + expr

### Generics in function declarations

`(fn name[T] ...)` — after the function name, check for `[`, parse type params until `]`. Each param can have bounds: `[T: Add Zero]`.

### Modifier handling

`(pub fn ...)` — `pub` is parsed first, then the inner declaration is parsed and its `public` flag set to true.

### Tests

Golden files: `tests/parser/foo.no` → pretty-printed AST. Every special form, every type shape, every pattern.

---

## 4. AST

### Design principles

- Immutable after construction
- Every node carries a `Span`
- Visitor pattern for traversal (single-dispatch via `visit_NodeName` methods)
- Strict tree at parse time; semantic phases *annotate* nodes but don't restructure

### Node families

**Types:** `PrimType NamedType GenericType RefType FnType TupleType DynType SelfType UnitType`

**Patterns:** `WildPat VarPat LitPat VariantPat StructPat TuplePat GuardedPat`

**Expressions:** `IntLit FloatLit StringLit BoolLit UnitLit KeywordLit ArrayLit TupleLit MapLit Ident Path Call FieldAccess If Match Do Loop Break Return Pass Assign FnExpr Await Spawn Quote Try`

**Declarations:** `LetDecl ConstDecl FnDecl StructDecl TypeDecl NewtypeDecl AliasDecl TraitDecl ImplDecl MacroDecl ModuleDecl UseDecl`

**Support:** `Span Param GenericParam CaptureMode(BORROW|BORROW_MUT|MOVE)`

### Annotation slots (filled by later passes)

Each node has optional fields populated during sema:
- `resolved_def_id` (name resolution) on `Ident`/`Path`
- `inferred_type` (type check) on every `Expr`
- `trait_impl` (trait resolution) on `Call` with overloaded operators
- `borrow_info` (borrow check) on bindings and references
- `monomorphized_name` (IR lowering) on generic calls

### Pretty printer

Round-trips AST back to S-expression form. Critical for parser debugging.

---

## 5. Semantic Analysis

The biggest phase, roughly 60% of compiler complexity. Broken into seven passes. Run in order:

### 5a. Macro expansion
Walks the AST. For each `Call` whose head resolves to a macro, replaces it with the macro's expansion. Uses `gensym` for hygienic binding introduction. Bounded recursion depth.

### 5b. Name resolution
Builds nested symbol tables (module → function → block → pattern). For each `Ident`/`Path`, finds the declaration it refers to and attaches a `def_id`. Reports unresolved names. Handles module imports.

### 5c. Type inference + checking
Hindley-Milner with extensions. Constraint generation pass, then unification. For each expression, compute its type. For each function, check all paths return the declared type. Handle:
- Explicit annotations as constraints
- Numeric literal defaulting (`42` → `i32`, `3.14` → `f64`)
- Generic instantiation
- Ref/deref rules
- Arrays and tuples

### 5d. Trait resolution
For each call whose target is an operator or trait method, find the applicable impl. Monomorphize generic calls (create a specialized signature for each concrete type combo). Check coherence (no overlapping impls). For `dyn Trait`, generate vtable layout.

### 5e. Borrow checker
Dataflow analysis. For each program point, compute the set of live bindings and their borrow state (owned/borrowed/moved). Enforce:
- One owner per value
- After move, original is dead
- Shared `&T` borrows may coexist
- Exclusive `&!T` borrows cannot coexist with any other borrow
- Borrow cannot outlive owner

This is the hardest single thing in the compiler. Budget extra time.

### 5f. Pattern exhaustiveness
For each `match`, build a usefulness matrix. Check:
- Every possible value covered (otherwise: error)
- No arm is unreachable (otherwise: warn)

### 5g. Effect checking
Simple pass. `await` only valid inside `async fn`. `break` only valid inside `loop`. `return` exits the enclosing function.

---

## 6. IR Lowering

Bridge between typed AST and LLVM. A custom typed IR makes codegen tractable.

Responsibilities:
1. **Monomorphize generics** — one IR function per concrete type combination. Mangled names.
2. **Lower closures** — convert `FnExpr` with captures into a struct + function pair. The struct holds captured values; the function takes the struct as an extra argument.
3. **Lower pattern matching** — `Match` becomes a decision tree of compares and branches.
4. **Lower `|>` and `?`** — pipe becomes nested calls; `?` becomes early-return control flow.
5. **Insert drops** — for each owned value, insert explicit drop calls at the end of its scope.
6. **Lower async** — `async fn` becomes a state machine (one enum variant per suspension point).
7. **Desugar string interpolation** — `(fmt "x = {}" n)` becomes a call to the runtime format function.

The IR should be typed (no inference), explicit (all coercions visible), and close to SSA.

---

## 7. Code Generation (LLVM via llvmlite)

### Type mapping

| Nyet type | LLVM type |
|---|---|
| `i8..i64 / u8..u64 / usize` | `i8..i64` (signedness handled at op level) |
| `f32 / f64` | `float / double` |
| `bool` | `i1` |
| `unit` | `void` (or `{}` in ABI positions) |
| `string` | `{ i8*, i64 }` (ptr + len, fat pointer) |
| `Array[T]` | `{ T*, i64 len, i64 cap }` |
| `&T / &!T` | `T*` |
| struct | LLVM struct type |
| sum type | `{ i64 tag, [N x i8] payload }` sized to largest variant |
| tuple | LLVM struct |
| `fn T -> R` | function pointer (bare fn) or `{ fnptr, env* }` (closure) |
| `dyn Trait` | `{ i8* data, vtable* }` |

### Function emission

Each monomorphized IR function → one LLVM function. Parameters passed by value (primitives, small structs) or by pointer (large structs, borrowed refs). Returns by value or via sret for large types.

### Operation lowering

- Field access → `getelementptr` + `load`
- Indexed access → bounds check runtime call + `getelementptr` + `load`
- Match → compare tag, branch to variant block, extract payload
- Assignment → `store`
- Closure call → load fn pointer, load env pointer, call with env as first arg
- Trait dispatch (static) → direct call to mono'd impl
- Trait dispatch (dyn) → load vtable entry, indirect call
- Drops → call to type's drop function (or skip for Copy types)
- IO ops → call to runtime function

### Runtime externs

The codegen declares these LLVM externs; the runtime library provides the bodies:

```
nyet_alloc(size) -> ptr
nyet_free(ptr)
nyet_panic(msg)
nyet_string_new(ptr, len) -> String
nyet_string_concat(a, b) -> String
nyet_format(template, argc, args...) -> String
nyet_array_new / grow / push / pop / index
nyet_io_stdout_write / stdin_read / stderr_write
nyet_io_file_open / read / write / close
nyet_task_spawn / await
```

---

## 8. Runtime Library

Written in C. Compiled to a static lib, linked with generated object files.

**Phase 1 (minimal):** alloc (wrap `malloc`/`free`), string primitives, stdout write, basic panic.

**Phase 2:** arrays, full IO (file, stdin, stderr), format.

**Phase 3:** map/set hash tables, async runtime (task queue, executor), shared/mpsc.

**Phase 4:** hardware IO (pins — platform dependent, probably stubbed unless targeting embedded).

---

## 9. Testing Strategy

Every phase has its own test tier:

1. **Lexer tests** — `.no` source → token list (golden files)
2. **Parser tests** — `.no` source → AST pretty-print (golden files)
3. **Sema positive tests** — `.no` source → passes check, produces expected typed AST
4. **Sema negative tests** — `.no` source → expected error message
5. **Codegen tests** — `.no` source → compiled binary → expected stdout
6. **Runtime unit tests** — C unit tests for runtime functions

Golden-file infrastructure: a simple `pytest` harness that reads expected output from a sibling file, diffs, auto-updates on a flag.

The **tree-walk interpreter** in `interp/` gives us a fast feedback loop before codegen is ready. Each feature can be prototyped in the interpreter first, then translated to codegen.

---

## 10. Incremental Milestones

Do not try to build all of this at once. Ship working compilers at each stage.

| Version | Scope | Goal program |
|---|---|---|
| **v0.1** | Primitives, `let`/`var`, arithmetic, `if`, `do`, `fn`, `out` — no ownership, no generics | `(fn main () -> unit (out "hello, world"))` |
| **v0.2** | Structs, sum types, basic `match`, arrays, tuples | Shape-area calculator |
| **v0.3** | Generics, monomorphization, `Option`/`Result`, `?` | Generic `fold`, `first` |
| **v0.4** | Traits, `impl`, operator overloading, `dyn` | `Vec2` with full ops |
| **v0.5** | Move semantics, borrow checker, drops | Programs that correctly reject use-after-move |
| **v0.6** | Closures, capture modes, Fn/FnMut/FnOnce | `map`/`filter`/`fold` with arbitrary closures |
| **v0.7** | Nested patterns, guards, exhaustiveness | Complex `match` with deep destructuring |
| **v0.8** | Macros, hygiene, variadic macros | `unless`, `log/debug`, routing DSL |
| **v0.9** | Modules, `use`, `pub`, multi-file | Split stdlib across files |
| **v1.0** | IO channels, `io/def`, file IO runtime | Read/transform/write files |
| **v1.1** | Async, `spawn`, `await`, state machine lowering | Concurrent file IO |
| **v1.2+** | Stdlib in Nyet, rewrite compiler passes in Nyet, self-host | Compiler compiling compiler |

Each milestone gets a git tag. Each milestone's demo program lives in `examples/`.

---

## 11. Risk Areas

- **Borrow checker** — the single hardest thing. Escape hatch: start with a "no references, everything owned, all types Copy" model for v0.1–v0.4 so we can defer this until v0.5. Programs will be inefficient but semantically correct.
- **Macro hygiene** — study the Racket syntax-case model. Getting renaming right is subtle; `gensym` alone isn't enough for multi-level expansion.
- **Trait coherence** — for v0.4, restrict to non-overlapping impls only. Add specialization later.
- **Async state machine** — defer to v1.1. It's a localized transformation once the rest works.
- **LLVM quirks** — llvmlite has sharp edges. Budget debug time for getting the first function call lowered correctly; after that the pattern repeats.
- **Type inference for closures** — inferring capture mode automatically is tricky. For v0.6, require explicit `fn`/`fn!`/`move fn` and infer only the closure's signature.

---

## 12. Immediate Next Steps

1. Patch `main.no`: add `usize`, `:keyword`, `Self`
2. Create the new directory layout under `pynyet/`
3. Stub out `diagnostic.py` and `source.py` (spans, errors) — everything depends on these
4. Start v0.1 lexer: only the tokens needed for the hello-world subset (primitives, `let`, `fn`, `if`, `do`, `out`, arithmetic, string literals)
5. Write lexer golden tests for v0.1
6. v0.1 parser (tiny subset)
7. v0.1 AST + pretty printer
8. v0.1 codegen (reuse the existing `pynyet/codegen/codegen.py` as a starting point)
9. First end-to-end test: `(fn main () -> unit (out "hello, world"))` → binary → "hello, world"
10. Tag v0.1, iterate to v0.2
