# Runs ON the Windows PC (copied there and started over SSH by worker-up.sh),
# after the binary is in %USERPROFILE%\starfront-lab\bin\collab-worker.exe.
# Code "keep" means already enrolled: the token is kept, only settings change.
# (Not "-" as on POSIX: powershell -File exits 5, silently, on a bare "-".)
#
# The worker is installed as the logon Scheduled Task (collab-worker's
# default), which runs as this user and so reaches the NAS share with the
# credential bootstrap-windows.ps1 stored. It pauses while NINA.exe runs.
param(
    [Parameter(Mandatory)] [string] $Id,
    [Parameter(Mandatory)] [string] $Hub,
    [Parameter(Mandatory)] [string] $Code,
    [string] $Mode = "synthetic",
    [int] $ScratchGb = 150,
    [int] $RamGb = 0,
    [string] $Xpsd = ""
)
$ErrorActionPreference = "Stop"
# Short paths: the Scheduled Task's command line is limited to 261 characters.
$lab = Join-Path $env:USERPROFILE "starfront-lab"
$dir = Join-Path $lab "worker"
$bin = Join-Path $lab "bin\collab-worker.exe"
$scratch = Join-Path $lab "scratch"
$noXpsd = Join-Path $lab "no-xpsd"

New-Item -ItemType Directory -Force $scratch, $noXpsd | Out-Null
if ($Code -ne "keep") {
    # enrol checks the configuration it writes: the catalogue settings are given
    # in the environment so it checks what this worker will run with (a hub with
    # no catalogue, in synthetic mode, hands out no xpsd_dir).
    if ($Mode -eq "synthetic") { $env:COLLAB_LOCAL_CATALOG = "synthetic"; $env:COLLAB_XPSD_DIR = $noXpsd }
    else { $env:COLLAB_XPSD_DIR = $Xpsd }
    & $bin enrol $Hub --code $Code --worker-id $Id --dir $dir
    $enrolled = $LASTEXITCODE
    Remove-Item Env:COLLAB_LOCAL_CATALOG, Env:COLLAB_XPSD_DIR -ErrorAction SilentlyContinue
    if ($enrolled -ne 0) { throw "collab-worker enrol failed ($enrolled)" }
}

# Replace or add top-level keys of collab-worker.toml; $null removes a key.
function Set-TomlKeys([string] $Path, [hashtable] $Keys) {
    $lines = @(Get-Content -LiteralPath $Path | Where-Object { $Keys.Keys -notcontains ($_ -split "=", 2)[0].Trim() })
    $first = 0
    while ($first -lt $lines.Count -and -not $lines[$first].TrimStart().StartsWith("[")) { $first++ }
    $added = foreach ($k in $Keys.Keys) {
        $v = $Keys[$k]
        if ($null -eq $v) { continue }
        if ($v -is [bool]) { "$k = $($v.ToString().ToLower())" }
        elseif ($v -is [int]) { "$k = $v" }
        else { "$k = $($v | ConvertTo-Json)" }   # a TOML basic string: backslashes doubled
    }
    $head = if ($first -gt 0) { @($lines[0..($first - 1)]) } else { @() }
    $out = $head + @($added) + @($lines | Select-Object -Skip $first)
    [System.IO.File]::WriteAllText($Path, (($out -join "`n") + "`n"))
}

$keys = @{
    scratch_dir      = $scratch
    scratch_cap_gb   = $ScratchGb
    pause_while_nina = $true
}
if ($RamGb -gt 0) { $keys["cap_ram_gb"] = $RamGb }
if ($Mode -eq "synthetic") {
    $keys["local_catalog"] = "synthetic"
    $keys["xpsd_dir"] = $noXpsd
} else {
    $keys["local_catalog"] = $null
    $keys["xpsd_dir"] = $Xpsd
}
Set-TomlKeys (Join-Path $dir "collab-worker.toml") $keys

# Windows PowerShell 5.1 turns a native command's stderr into an error, which
# "Stop" makes fatal; uninstall complains when nothing is installed yet.
$ErrorActionPreference = "Continue"
& $bin service uninstall --dir $dir *> $null
& $bin service install --dir $dir
if ($LASTEXITCODE -ne 0) { throw "collab-worker service install failed ($LASTEXITCODE)" }
Write-Output "worker ${Id}: installed ($Mode catalogue), hub $Hub"
