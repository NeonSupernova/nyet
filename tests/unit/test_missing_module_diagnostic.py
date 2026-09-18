"""An unresolved `(use ...)` must fail loudly, not compile without it.

`_load_program_with_deps`'s `load()` helper no-ops on a path that isn't a
file. Applied to a `(use ...)` target, that silently dropped the module:
its macros were never registered, so calls to them survived expansion and
reached codegen, which emitted an illegal LLVM identifier and left clang to
report `expected '(' in call` against IR the user never wrote.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _build(source: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    entry = tmp_path / "prog.no"
    entry.write_text(source)
    return subprocess.run(
        [sys.executable, "-m", "pynyet.driver", "build", "-o", str(tmp_path / "prog"), str(entry)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


def test_missing_use_target_is_an_error(tmp_path: Path) -> None:
    result = _build('(use no_such_module)\n\n(fn main () -> unit (out! "hi\\n"))\n', tmp_path)
    assert result.returncode != 0
    assert "module 'no_such_module' not found" in result.stderr
    assert "no_such_module.no" in result.stderr


def test_present_module_still_builds(tmp_path: Path) -> None:
    (tmp_path / "helper.no").write_text("(pub fn helper () -> i32 42)\n")
    result = _build('(use helper)\n\n(fn main () -> unit (out! (helper)) (out! "\\n"))\n', tmp_path)
    assert result.returncode == 0, result.stderr
