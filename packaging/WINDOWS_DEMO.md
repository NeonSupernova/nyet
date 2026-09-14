# Nyet -- Windows demo bundle

Everything in this folder is self-contained: the `nyet` compiler
(`nyet.exe`), a bundled clang toolchain to compile with, and a handful
of demo programs. Nothing needs to be installed, and no admin rights
are needed for any of it.

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

## The demo programs

All in `demo\`, run with `nyet run demo\<file>`:

| File | Shows |
|---|---|
| `01_hello.no` | Hello world -- proves the toolchain works. |
| `02_structs_and_match.no` | Structs, sum types, pattern matching (a shape-area calculator). |
| `03_ownership.no` | Move semantics + borrows -- the *accepted* case. |
| `03_ownership_bad.no` | The borrow checker *rejecting* a use-after-move bug at compile time. Run with `nyet check demo\03_ownership_bad.no` (not `run` -- it's supposed to fail to compile). |
| `04_generics_and_option.no` | Generic `Option[T]` and the `?` early-return operator -- no nulls. |
| `05_closures.no` | Closures / higher-order functions. |

A natural live order: `01` to prove it works, `02` for the language
feel, `03` then `03_bad` as the "watch it catch a real bug" beat,
`04` and `05` for two more distinctive features.

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
