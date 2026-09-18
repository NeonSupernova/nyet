#!/usr/bin/env python3
"""Stage the example Nyet sources that get mirrored into the public repo.

This compiler repo is private. `NeonSupernova/nyet-releases` is the
public front door: it carries the download, the docs, the issue
tracker, and -- so the language can be read on the web without
fetching a 400MB bundle -- a copy of the demo programs.

This script is the single definition of *which* files that copy
contains. The release workflow runs it and pushes the result, so the
mirror cannot drift from what ships inside the bundle. It only ever
copies Nyet sources; nothing under `pynyet/` is public.

    python3 packaging/stage_public_examples.py --out /tmp/examples
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The curated tour, in reading order. Mirrors bundle demo\.
DEMO_PROGRAMS = sorted(p.name for p in (REPO_ROOT / "examples" / "tour").glob("*.no"))

# Regression tests that double as single-feature demos. build_windows.ps1
# copies these same four into the bundle's demo\features\ -- keep the
# two lists in sync.
FEATURE_DEMOS = ("ansi_lib", "char_printing", "file_read_lines", "string_len")

# Where the arcade suite's real logic lives: each game's main.no is a
# two-line wrapper around one of these (repo demos/lib/). Same list as
# build_windows.ps1's bundle lib\ copy.
LIB_MODULES = (
    "minibase_core",
    "minibase_repl",
    "adventure_lib",
    "game_of_life_lib",
    "pipe_dreams_lib",
    "hangman_lib",
    "prelude_option",
    "prelude_rng",
)

PROGRAM_DIRS = ("minibase", "adventure", "game_of_life", "pipe_dreams", "hangman", "arcade")


def _copy(src: Path, dest: Path) -> None:
    if not src.is_file():
        raise SystemExit(
            f"error: expected to mirror {src.relative_to(REPO_ROOT)}, but it is missing"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)


def stage(out: Path) -> int:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    count = 0
    for name in DEMO_PROGRAMS:
        _copy(REPO_ROOT / "examples" / "tour" / name, out / "demo" / name)
        count += 1
    for name in FEATURE_DEMOS:
        _copy(
            REPO_ROOT / "tests" / "codegen" / f"{name}.no", out / "demo" / "features" / f"{name}.no"
        )
        count += 1
    for name in LIB_MODULES:
        _copy(REPO_ROOT / "demos" / "lib" / f"{name}.no", out / "lib" / f"{name}.no")
        count += 1
    for name in PROGRAM_DIRS:
        _copy(REPO_ROOT / "demos" / name / "main.no", out / "programs" / name / "main.no")
        count += 1

    # main.no is the language specification. Renamed on the way out
    # because "main.no" reads like an entry point rather than a
    # reference, and the public README links it by the clearer name.
    _copy(REPO_ROOT / "main.no", out / "language-reference.no")
    count += 1

    print(f"staged {count} Nyet sources into {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=Path, help="directory to write the staged tree to")
    args = ap.parse_args(argv)
    return stage(args.out.resolve())


if __name__ == "__main__":
    sys.exit(main())
