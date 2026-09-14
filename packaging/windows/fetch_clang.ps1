<#
.SYNOPSIS
  Downloads a portable, no-install mingw-w64 toolchain (clang + lld +
  a posix-threads winpthreads runtime) from the WinLibs project and
  extracts it to -Dest as <Dest>\bin\clang.exe.

  This is what lets the packaged nyet.exe compile .no files on a
  Windows machine with nothing else installed: pynyet/driver.py's
  _find_clang() looks for exactly this layout (clang\bin\clang.exe)
  next to a frozen nyet.exe before falling back to a system `clang`.

  Posix threads specifically (not win32 threads) because
  runtime/async.c uses <pthread.h>.
#>
param(
    [Parameter(Mandatory)] [string]$Dest
)
$ErrorActionPreference = "Stop"

if (Test-Path "$Dest\bin\clang.exe") {
    Write-Host "clang already present at $Dest, skipping download."
    return
}

Write-Host "Querying latest WinLibs mingw-w64 release..."
$release = Invoke-RestMethod `
    -Uri "https://api.github.com/repos/brechtsanders/winlibs_mingw/releases/latest" `
    -Headers @{ "User-Agent" = "nyet-packaging-script" }

# WinLibs publishes several flavors per release (gcc-only, clang, 32 vs
# 64-bit, win32 vs posix threads, msvcrt vs ucrt, .7z vs .zip). We want
# 64-bit + posix threads + the clang variant, packaged as .zip so
# Expand-Archive can unpack it with no extra tooling.
$asset = $release.assets | Where-Object {
    $_.name -match "(?i)x86_64" -and
    $_.name -match "(?i)posix" -and
    $_.name -match "(?i)clang" -and
    $_.name -like "*.zip"
} | Select-Object -First 1

if (-not $asset) {
    Write-Host "Available assets in $($release.tag_name):"
    $release.assets | ForEach-Object { Write-Host "  $($_.name)" }
    throw "No x86_64/posix/clang .zip asset matched in the latest winlibs_mingw release. " +
          "Pick one of the names above and adjust the filter in this script."
}

Write-Host "Downloading $($asset.name) ($([math]::Round($asset.size / 1MB))MB)..."
$zipPath = Join-Path $env:TEMP $asset.name
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath

Write-Host "Extracting..."
$extractDir = Join-Path $env:TEMP "winlibs_extract"
if (Test-Path $extractDir) { Remove-Item $extractDir -Recurse -Force }
Expand-Archive -Path $zipPath -DestinationPath $extractDir

# WinLibs zips extract to a single top-level mingw64\ (or mingw32\) folder.
$inner = Get-ChildItem $extractDir -Directory | Select-Object -First 1
if (-not $inner) { throw "Extracted archive had no top-level folder -- unexpected layout." }

$parent = Split-Path $Dest
if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
if (Test-Path $Dest) { Remove-Item $Dest -Recurse -Force }
Move-Item $inner.FullName $Dest -Force

Remove-Item $zipPath -Force
Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "clang ready at $Dest\bin\clang.exe"
