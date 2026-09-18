<#
.SYNOPSIS
  Builds the standalone Windows demo bundle: nyet.exe (via
  PyInstaller), a bundled clang/mingw toolchain (so target machines
  need no installs), the demo/ programs, and the PATH-setup scripts --
  all assembled into one folder and zipped.

  Runs in CI (.github/workflows/windows-package.yml) but is also
  meant to be run by hand on any Windows machine with Python 3.10+ on
  PATH, e.g. to rebuild closer to a demo date without going through
  GitHub Actions:

    powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#>
param(
    [string]$OutDir = "packaging\dist"
)
$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path "$PSScriptRoot\.."
Set-Location $RepoRoot

$bundle = "$OutDir\bundle"
if (Test-Path $bundle) { Remove-Item $bundle -Recurse -Force }
New-Item -ItemType Directory -Path $bundle -Force | Out-Null

Write-Host "== 1/4: fetching portable clang/mingw toolchain =="
# -ManifestPath: records which WinLibs release the bundle ships, for
# the release notes' GPL source-code pointer. Lands outside $bundle so
# it isn't zipped into the download.
& "$PSScriptRoot\windows\fetch_clang.ps1" -Dest "$bundle\clang" -ManifestPath "$OutDir\toolchain.json"

Write-Host "== 2/4: installing PyInstaller =="
python -m pip install --quiet --upgrade pyinstaller

Write-Host "== 3/4: building nyet.exe with PyInstaller =="
python -m PyInstaller packaging\nyet.spec --distpath "$OutDir\pyi" --workpath "$OutDir\build" -y

Write-Host "== 4/4: assembling bundle =="
Copy-Item "$OutDir\pyi\nyet\*" $bundle -Recurse -Force
Copy-Item "$RepoRoot\examples\tour" "$bundle\demo" -Recurse -Force
Copy-Item "$PSScriptRoot\windows\add-to-path.ps1" "$bundle\add-to-path.ps1" -Force
Copy-Item "$PSScriptRoot\windows\add-to-path.bat" "$bundle\add-to-path.bat" -Force
Copy-Item "$PSScriptRoot\WINDOWS_DEMO.md" "$bundle\README.md" -Force

# The arcade demo suite -- each game's own directory (a two-line
# `main.no` wrapper around a module in the repo's demos/lib/, which
# nyet.spec already bundled as data so `(use ...)` resolves standalone)
# plus the combined launcher. The bundle keeps these at its top level:
# `nyet run arcade\main.no` is what the README tells people to type.
foreach ($dir in @("minibase", "adventure", "game_of_life", "pipe_dreams", "hangman", "arcade")) {
    Copy-Item "$RepoRoot\demos\$dir" "$bundle\$dir" -Recurse -Force
}

# The arcade suite's actual logic: each game's `main.no` wrapper is
# just two lines, so the interesting, language-feature-showing code
# lives in these demos/lib/ modules instead.
# nyet.spec bundles them too (ARCADE_LIB_MODULES, kept in sync with
# this list) so `(use ...)` resolves at runtime from the frozen exe --
# but that copy lands wherever PyInstaller's internals put it, not
# somewhere a person browsing the demo folder would ever find. Half
# the point of the demo is showing off the language, so give them a
# second, plainly-visible copy here.
New-Item -ItemType Directory -Path "$bundle\lib" -Force | Out-Null
foreach ($name in @("minibase_core", "minibase_repl", "adventure_lib", "game_of_life_lib", "pipe_dreams_lib", "hangman_lib", "prelude_option", "prelude_rng")) {
    Copy-Item "$RepoRoot\demos\lib\$name.no" "$bundle\lib\$name.no" -Force
}

# A handful of feature-focused programs (also this repo's own
# regression tests, written to double as short, documented demos of
# one language feature each) alongside the curated demo/ set.
New-Item -ItemType Directory -Path "$bundle\demo\features" -Force | Out-Null
foreach ($name in @("ansi_lib", "char_printing", "file_read_lines", "string_len")) {
    Copy-Item "$RepoRoot\tests\codegen\$name.no" "$bundle\demo\features\$name.no" -Force
}

# main.no -- the language spec, kept as reference/browsing material,
# not something guaranteed to build (see the repo's own CLAUDE.md: it
# documents features beyond what's implemented and is aspirational in
# places).
Copy-Item "$RepoRoot\main.no" "$bundle\main.no" -Force

$zipPath = "$OutDir\nyet-windows-demo.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -Path "$bundle\*" -DestinationPath $zipPath

Write-Host ""
Write-Host "Bundle folder: $bundle"
Write-Host "Zip:           $zipPath"
