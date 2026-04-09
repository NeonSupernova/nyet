# Nyet examples

Small Nyet programs that exercise the v0.1 language subset. These lex cleanly today; parsing and codegen arrive in v0.2+.

- `hello.no` — canonical hello world; demonstrates `fn main`, the `unit` return type, and writing a string literal to the `out` channel.
- `arith.no` — immutable `let` bindings with explicit `i32` annotations and a `+` expression passed to `out`.
- `branch.no` — `if` used as an expression, with both arms producing the same `string` type and bound through `let`.

Run `python3 -m pynyet.driver lex examples/hello.no` to see the token stream once the driver lands.
