<#
.SYNOPSIS
  Adds this folder to your user PATH so `nyet` works from any new
  terminal window, without needing admin rights.

  Uses [Environment]::SetEnvironmentVariable rather than `setx`
  deliberately -- setx silently truncates PATH if the combined value
  is over 1024 characters, which is a real risk on a managed lab
  machine whose PATH is already long, and a truncated PATH is a much
  worse failure mode than this script just doing nothing.
#>
$ErrorActionPreference = "Stop"
$dir = $PSScriptRoot

$old = [Environment]::GetEnvironmentVariable("Path", "User")
$parts = @()
if ($old) { $parts = $old -split ";" }

if ($parts -contains $dir) {
    Write-Host "$dir is already on your PATH."
} else {
    $new = if ($old) { "$old;$dir" } else { $dir }
    [Environment]::SetEnvironmentVariable("Path", $new, "User")
    Write-Host "Added $dir to your user PATH."
    Write-Host "Close and reopen your terminal (Command Prompt / PowerShell) for it to take effect."
}
