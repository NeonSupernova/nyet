# Nyet -- Windows demo bundle

Everything in this folder is self-contained: the `nyet` compiler
(`nyet.exe`), a bundled clang toolchain to compile with, and a handful
of demo programs. Nothing needs to be installed, and no admin rights
are needed for any of it.

The zip is around 390MB (almost all of it the bundled clang/LLVM
toolchain) -- fine for a USB stick or shared drive, but budget a
minute or two to copy/unzip per machine.

## One-time setup on each lab computer

1. Copy this whole folder onto the machine (USB stick, shared drive,
   wherever) -- **keep it as one folder**, don't pull just `nyet.exe`
   out on its own, it needs `clang\` and `_internal\` next to it.
2. Double-click `add-to-path.bat`. It adds this folder to your PATH
   for your Windows user account (no admin needed) so you can type
   `nyet` from any terminal. Close and reopen any terminal windows
   after running it.
3. Open a **new** Command Prompt or PowerShell window and check it
   worked:

   ```
   nyet run demo\01_hello.no
   ```

   You should see `hello from Nyet!`. If `nyet` isn't found, either
   the PATH change didn't take (try step 2 again, or just `cd` into
   this folder and run `.\nyet.exe` instead of `nyet`), or the
   terminal window was open before you ran step 2 -- open a new one.

### The first time you run *any* freshly-compiled .exe, Windows will complain

The first time Windows SmartScreen sees `nyet.exe` (or any `.exe` that
`nyet` compiles for you), you'll likely get a blue "Windows protected
your PC" screen. This is expected -- these are unsigned binaries, not
a bug. Click **More info**, then **Run anyway**.

**Test this on the actual lab machines at least a day before the
demo, not right before you present.** Some lab imaging / antivirus
setups flag or quarantine freshly-built unsigned `.exe` files more
aggressively than a home machine would, and that's not something you
want to discover live. If a compiled demo binary vanishes or won't
run and there's no SmartScreen prompt, check whether antivirus
quarantined it, and get it allowlisted (or ask lab IT to) ahead of
time.

**If the lab computers reset on reboot/logoff** (common with imaged
lab machines), the PATH change from step 2 won't survive a reset.
Either redo step 2 each session, or skip it entirely and just `cd`
into this folder and run `.\nyet.exe ...` -- works identically, just
without the shorter `nyet` name.

## The arcade -- the main event

`nyet run arcade\main.no` (or double-click-build it first with
`nyet -o arcade.exe arcade\main.no`, then `.\arcade.exe`) launches a
menu that links five small game/utility programs into one binary and
lets you jump between them -- a live demonstration of Nyet's module
system, not five separate demos glued together after the fact.

| # | Game | Shows |
|---|---|---|
| 1 | `minibase` | A CRUD console database: structs, an `impl` block, macros, file save/load. |
| 2 | `adventure` | A tiny text adventure: sum types + `match` encoding a room graph. |
| 3 | `game of life` | Conway's Game of Life as an animated terminal UI, with a cursor-based board editor. |
| 4 | `pipe dreams` | A number-transformation puzzle built entirely around the `\|>` pipe operator. |
| 5 | `hangman` | Classic word-guessing -- ASCII gallows art, one letter at a time. |

Each also runs completely standalone (same code, just linked a second
way): `nyet run minibase\main.no`, `nyet run adventure\main.no`, etc.

## The demo programs

Smaller, single-feature programs in `demo\`, run with
`nyet run demo\<file>`:

| File | Shows |
|---|---|
| `01_hello.no` | Hello world -- proves the toolchain works. |
| `02_structs_and_match.no` | Structs, sum types, pattern matching (a shape-area calculator). |
| `03_ownership.no` | Move semantics + borrows -- the *accepted* case. |
| `03_ownership_bad.no` | The borrow checker *rejecting* a use-after-move bug at compile time. Run with `nyet check demo\03_ownership_bad.no` (not `run` -- it's supposed to fail to compile). |
| `04_generics_and_option.no` | Generic `Option[T]` and the `?` early-return operator -- no nulls. |
| `05_closures.no` | Closures / higher-order functions. |

`demo\features\` has a few more, doubling as this repo's own
regression tests for the features they show off:

| File | Shows |
|---|---|
| `ansi_lib.no` | `std/ansi.no`: SGR colors, 256-color mode, truecolor, hex-to-ANSI conversion. |
| `char_printing.no` | Printing a `char` correctly (as the letter, not its numeric codepoint). |
| `file_read_lines.no` | Reading a file back as one string per line. |
| `string_len.no` | `len` on a `string` (as opposed to an `Array[T]`). |

A natural live order: `01` to prove it works, `02` for the language
feel, `03` then `03_bad` as the "watch it catch a real bug" beat,
`04` and `05` for two more distinctive features, then straight into
the arcade as the finale.

## main.no -- the language reference

`main.no`, copied into this folder's root, is this project's own
language specification, written as one big Nyet program with
extensive comments -- worth browsing (in a text editor, or
`nyet parse main.no` to see it round-tripped) but **not guaranteed to
build**: it deliberately documents features somewhat ahead of what's
implemented, and isn't something this bundle promises to compile.

## Compiling your own file

```
nyet -o main.exe path\to\file.no
.\main.exe
```

`nyet` (no subcommand) means "build". Other subcommands, if you want
to show them: `nyet lex`, `nyet parse`, `nyet check` (type/borrow
check without producing a binary), `nyet run` (build + immediately
execute).

## If something's wrong with the bundle itself

This bundle is rebuilt with `packaging\build_windows.ps1` in the
`nyet` repo (also wired into `.github/workflows/windows-package.yml`
as a manual/`workflow_dispatch` job) -- rerun that if you need to pick
up compiler changes before the demo.
