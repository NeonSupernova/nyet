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

# WinLibs publishes far more GCC-only refreshes than combined
# GCC+LLVM/Clang/LLD builds -- as of when this was written, the most
# recent releases on the /latest endpoint had no clang variant at all,
# and the newest one that did was ~30 releases back. So: page through
# releases (newest first) until one has a matching asset, rather than
# assuming /latest has it.
#
# Within a release we want: 64-bit, posix threads (runtime/async.c
# uses <pthread.h>), the llvm/clang build, packaged as .zip (so
# Expand-Archive can unpack it with no extra tooling), preferring UCRT
# over MSVCRT (the modern default mingw-w64 recommends).
$candidates = @()
$headers = @{ "User-Agent" = "nyet-packaging-script" }
for ($page = 1; $page -le 10 -and $candidates.Count -eq 0; $page++) {
    Write-Host "Searching winlibs_mingw releases (page $page) for an x86_64/posix/llvm build..."
    $uri = "https://api.github.com/repos/brechtsanders/winlibs_mingw/releases?per_page=100&page=$page"
    $releases = Invoke-RestMethod -Uri $uri -Headers $headers
    if ($releases.Count -eq 0) { break }

    # Collect every match across the whole page before picking one, not
    # just the first release that has any -- the newest matching release
    # here happens to only ship an msvcrt build, four days after an
    # (older, but still recent) release that ships ucrt. Scanning the
    # whole page first lets the sort below prefer ucrt overall rather
    # than settling for whatever the single newest release happened to
    # include.
    foreach ($release in $releases) {
        $matches = $release.assets | Where-Object {
            $_.name -match "(?i)x86_64" -and
            $_.name -match "(?i)posix" -and
            $_.name -match "(?i)llvm" -and
            $_.name -like "*.zip"
        }
        foreach ($m in $matches) {
            $candidates += [PSCustomObject]@{
                Asset       = $m
                PublishedAt = [DateTime]$release.published_at
                IsUcrt      = $m.name -match "(?i)ucrt"
            }
        }
    }
}

if ($candidates.Count -eq 0) {
    throw "No x86_64/posix/llvm .zip asset found in the last several hundred winlibs_mingw releases. " +
          "Check https://github.com/brechtsanders/winlibs_mingw/releases by hand and adjust the filter in this script."
}

$best = $candidates | Sort-Object -Property @{Expression = "IsUcrt"; Descending = $true }, @{Expression = "PublishedAt"; Descending = $true } | Select-Object -First 1
$asset = $best.Asset
Write-Host "Selected $($asset.name) (published $($best.PublishedAt))"

Write-Host "Downloading $($asset.name) ($([math]::Round($asset.size / 1MB))MB)..."
$tempDir = [System.IO.Path]::GetTempPath()
$zipPath = Join-Path $tempDir $asset.name
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath

Write-Host "Extracting..."
$extractDir = Join-Path $tempDir "winlibs_extract"
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
