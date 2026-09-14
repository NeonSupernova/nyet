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

a = Analysis(
    ["packaging/nyet_entry.py"],
    pathex=["."],
    datas=[("std", "std"), ("runtime", "runtime")],
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
