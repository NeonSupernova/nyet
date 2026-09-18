# Welcome to Nyet

Nyet is a small programming language -- Lisp-like syntax, a Rust-style
ownership model that catches memory bugs at compile time, and a real
compiler that produces native `.exe` files. Everything you need to
try it is in this folder: the compiler itself, a toolchain to compile
with, and a set of programs and games written in Nyet, ready to run.

Nothing here needs to be installed, and you don't need admin rights
for any of it -- unzip it and go. Programs compiled by `nyet` use
color in the terminal (the arcade especially), and that's set up to
just work in a normal Command Prompt or PowerShell window -- no extra
setup needed there either.

(The zip is around 390MB, almost all of it the compiler toolchain that
lets `nyet` build `.exe` files entirely on its own. If you're copying
it around on a USB stick or shared drive, budget a minute or two per
machine.)

## Getting set up

1. Copy this whole folder onto the machine -- **keep everything
   together in one folder**, don't pull `nyet.exe` out on its own,
   it needs the `clang\` and `_internal\` folders sitting right next
   to it.
2. Double-click `add-to-path.bat`. This lets you type `nyet` from any
   terminal window instead of having to `cd` into this folder every
   time. Close and reopen any terminal windows you already had open
   after running it.
3. Open a **new** Command Prompt or PowerShell window and try:

   ```
   nyet run demo\01_hello.no
   ```

   You should see `hello from Nyet!`. If `nyet` isn't recognized,
   either open a fresh terminal window (it only picks up the PATH
   change on windows opened *after* step 2), or just `cd` into this
   folder and run `.\nyet.exe` instead.

### A heads-up about Windows security warnings

The first time Windows notices `nyet.exe` -- or any program `nyet`
compiles for you -- it will likely show a blue "Windows protected
your PC" screen. This is completely normal and expected: it just
means these are freshly built programs that haven't been digitally
signed, not that anything is wrong. Click **More info**, then
**Run anyway**, and it won't ask again for that same file.

If a compiled program seems to vanish or refuses to run with no
warning at all, antivirus software may have quietly quarantined it --
worth checking, especially on managed/school computers.

If the computer you're on resets everything on logoff or restart, the
PATH shortcut from step 2 won't stick around. That's fine -- just
`cd` into this folder and run `.\nyet.exe ...` instead of `nyet ...`;
it does exactly the same thing.

## The arcade -- start here

The best way to see what Nyet can do: run the arcade, a menu that
links five small games and tools together into one program.

```
nyet run arcade\main.no
```

(or build it once with `nyet -o arcade.exe arcade\main.no` and just
run `.\arcade.exe` after that -- faster if you're going to launch it
more than once.)

| # | Game | What it shows off |
|---|---|---|
| 1 | minibase | A tiny console database -- add, rename, delete, save/load, search. |
| 2 | adventure | A pocket text adventure -- explore a few connected rooms. |
| 3 | game of life | Conway's Game of Life, animated right in the terminal, with an editor so you can draw your own starting board. |
| 4 | pipe dreams | A number puzzle built around Nyet's `\|>` pipe operator -- chain simple steps together to hit a target number. |
| 5 | hangman | Classic word-guessing, complete with ASCII gallows art. |

Each of these also runs completely on its own, same code either way:
`nyet run minibase\main.no`, `nyet run adventure\main.no`, and so on.

**Curious how any of it is actually written?** The real source code
for each game lives in the `lib\` folder -- structs, pattern matching,
the borrow checker catching mistakes, `|>` pipelines, all of it, in
plain readable Nyet. (Each game's `main.no` imports these as
`(use demos/lib/...)`, the path they have in the Nyet source repo;
`nyet.exe` carries its own copy, so the imports resolve wherever you
put the folder. The copies here are for reading.) Open any of these in
a text editor:

| File | The game it belongs to |
|---|---|
| `minibase_core.no` / `minibase_repl.no` | minibase |
| `adventure_lib.no` | adventure |
| `game_of_life_lib.no` | game of life |
| `pipe_dreams_lib.no` | pipe dreams |
| `hangman_lib.no` | hangman |
| `prelude_option.no` / `prelude_rng.no` | small shared helpers a few of the games above use |

## Smaller demo programs

The `demo\` folder has shorter programs, each focused on one language
feature. Run any of them with `nyet run demo\<file>`:

| File | What it shows off |
|---|---|
| `01_hello.no` | Hello world -- the simplest possible Nyet program. |
| `02_structs_and_match.no` | Structs and pattern matching, via a shape-area calculator. |
| `03_ownership.no` | Nyet's ownership model in action -- moves and borrows. |
| `03_ownership_bad.no` | The same idea, but broken on purpose -- watch the compiler catch a real bug before the program ever runs. Use `nyet check demo\03_ownership_bad.no` here (not `run`) -- it's *supposed* to fail. |
| `04_generics_and_option.no` | Generics and an `Option[T]` type that makes "forgot to handle the empty case" bugs impossible. |
| `05_closures.no` | Closures and functions that take other functions as arguments. |

`demo\features\` has a few more, each centered on one specific piece
of syntax:

| File | What it shows off |
|---|---|
| `ansi_lib.no` | Terminal colors -- the same coloring the arcade uses. |
| `char_printing.no` | Printing individual characters. |
| `file_read_lines.no` | Reading a text file back in, one line at a time. |
| `string_len.no` | Getting the length of a string. |

A natural order to try them in: `01` to see it work, `02` to get a
feel for the syntax, `03` then `03_bad` to watch the compiler catch a
mistake, `04` and `05` for two more highlights, then finish with the
arcade.

## main.no -- the whole language, in one file

`main.no`, right here in this folder, is Nyet's own language
reference -- one big, heavily commented program that walks through
everything the language offers. Worth a browse in a text editor. A
few things in it describe where the language is headed rather than
what's finished today, so it isn't guaranteed to compile start to
finish -- treat it as a tour, not a test.

## Trying your own ideas

Write any `.no` file and compile it the same way:

```
nyet -o myprogram.exe path\to\myfile.no
.\myprogram.exe
```

A few other things `nyet` can do, if you want to show them off:
`nyet check file.no` checks a program for errors without producing an
`.exe`, and `nyet run file.no` builds and immediately runs it in one
step. `nyet --version` prints which build this is -- worth including
if you report a problem.

## Questions or something not working?

If anything about this bundle itself seems broken (as opposed to a
mistake in a program you wrote), open an issue:

  https://github.com/NeonSupernova/nyet-releases/issues

Include what `nyet --version` prints, the `.no` program, and the exact
output you got. That's also where newer builds are published, so it's
worth a look before reporting anything -- it may already be fixed.
