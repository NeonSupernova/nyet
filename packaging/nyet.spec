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
# Paths below are built from SPECPATH (this file's own directory,
# injected by PyInstaller) rather than left relative, since PyInstaller
# resolves a bare relative script/data path against SPECPATH -- not the
# invoking shell's cwd -- which is easy to get wrong silently.

import os

repo_root = os.path.dirname(SPECPATH)

a = Analysis(
    [os.path.join(SPECPATH, "nyet_entry.py")],
    pathex=[repo_root],
    datas=[
        (os.path.join(repo_root, "std"), "std"),
        (os.path.join(repo_root, "runtime"), "runtime"),
    ],
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
