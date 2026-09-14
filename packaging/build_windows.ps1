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
& "$PSScriptRoot\windows\fetch_clang.ps1" -Dest "$bundle\clang"

Write-Host "== 2/4: installing PyInstaller =="
python -m pip install --quiet --upgrade pyinstaller

Write-Host "== 3/4: building nyet.exe with PyInstaller =="
python -m PyInstaller packaging\nyet.spec --distpath "$OutDir\pyi" --workpath "$OutDir\build" -y

Write-Host "== 4/4: assembling bundle =="
Copy-Item "$OutDir\pyi\nyet\*" $bundle -Recurse -Force
Copy-Item "$RepoRoot\demo" "$bundle\demo" -Recurse -Force
Copy-Item "$PSScriptRoot\windows\add-to-path.ps1" "$bundle\add-to-path.ps1" -Force
Copy-Item "$PSScriptRoot\windows\add-to-path.bat" "$bundle\add-to-path.bat" -Force
Copy-Item "$PSScriptRoot\WINDOWS_DEMO.md" "$bundle\README.md" -Force

$zipPath = "$OutDir\nyet-windows-demo.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -Path "$bundle\*" -DestinationPath $zipPath

Write-Host ""
Write-Host "Bundle folder: $bundle"
Write-Host "Zip:           $zipPath"
