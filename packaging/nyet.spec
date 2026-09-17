# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for the standalone `nyet` CLI. Build from the repo
# root with:
#
#   pyinstaller packaging/nyet.spec
#
# (packaging/build_windows.ps1 does this as one step of the full demo
# bundle build.) Produces an onedir bundle at dist/nyet/ -- nyet.exe
# plus support files -- with std/ and runtime/ bundled as data so
# `(use std/...)` and the C runtime link step work standalone, without
# the source checkout being present on the target machine.
#
# Also bundles every repo-root `_lib`/`_repl`/`prelude_*` module the
# arcade demo suite (minibase/adventure/game_of_life/pipe_dreams/
# hangman + the arcade launcher) needs via `(use ...)` -- same
# mechanism as std/, since pynyet/driver.py's `_repo_root()` resolves
# to this same bundled data directory when frozen. Without these, a
# frozen nyet.exe can still run/build any single-file script, but
# `(use adventure_lib)` etc. would fail to resolve once the source
# checkout isn't there to fall back to.
#
# Paths below are built from SPECPATH (this file's own directory,
# injected by PyInstaller) rather than left relative, since PyInstaller
# resolves a bare relative script/data path against SPECPATH -- not the
# invoking shell's cwd -- which is easy to get wrong silently.

import os

repo_root = os.path.dirname(SPECPATH)

ARCADE_LIB_MODULES = [
    "minibase_core.no",
    "minibase_repl.no",
    "adventure_lib.no",
    "game_of_life_lib.no",
    "pipe_dreams_lib.no",
    "hangman_lib.no",
    "prelude_option.no",
    "prelude_rng.no",
]

a = Analysis(
    [os.path.join(SPECPATH, "nyet_entry.py")],
    pathex=[repo_root],
    datas=[
        (os.path.join(repo_root, "std"), "std"),
        (os.path.join(repo_root, "runtime"), "runtime"),
    ]
    + [(os.path.join(repo_root, name), ".") for name in ARCADE_LIB_MODULES],
    hiddenimports=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="nyet",
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="nyet",
)
