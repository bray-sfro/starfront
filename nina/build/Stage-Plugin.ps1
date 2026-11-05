# Copy the files that ship into a clean staging folder.
#
# Driven by the StagePlugin target: MSBuild writes the list of
# "source|destination" pairs and this puts each file where its destination
# says, refusing anything missing, duplicated, or pointing outside the folder.
# Only a folder this script made is ever cleared, so a wrong -Destination can
# never erase somebody's work.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$FileList,
    [Parameter(Mandatory)][string]$Destination
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$marker = '.starfront-stage'
$destinationRoot = [IO.Path]::GetFullPath($Destination)
$entries = foreach ($line in Get-Content -LiteralPath $FileList) {
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    $parts = $line.Split('|')
    if ($parts.Count -ne 2 -or [string]::IsNullOrWhiteSpace($parts[1])) { throw "Invalid package file entry: $line" }
    $target = [IO.Path]::GetFullPath([IO.Path]::Combine($destinationRoot, $parts[1]))
    $inside = $target.StartsWith($destinationRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
    if ([IO.Path]::IsPathRooted($parts[1]) -or -not $inside) { throw "Package path escapes staging: $($parts[1])" }
    if (-not (Test-Path -LiteralPath $parts[0] -PathType Leaf)) { throw "Missing package file: $($parts[0])" }
    [pscustomobject]@{ Source = $parts[0]; Target = $target; Relative = $parts[1] }
}
$entries = @($entries)
if ($entries.Count -eq 0) { throw 'Nothing to stage.' }
if (@($entries.Target | Sort-Object -Unique).Count -ne $entries.Count) { throw 'Duplicate package destination.' }
# N.I.N.A. supplies its own copies of everything it exports, and a second copy
# of one of them in a plugin folder breaks composition; nothing of N.I.N.A.'s
# may ever be staged, whatever the package list says.
$hostOwned = @($entries | Where-Object { (Split-Path -Leaf $_.Relative) -like 'NINA.*' })
if ($hostOwned.Count) { throw "Refusing to ship N.I.N.A.'s own assemblies: $($hostOwned.Relative -join ', ')" }

if (Test-Path -LiteralPath $destinationRoot) {
    if (-not (Test-Path -LiteralPath (Join-Path $destinationRoot $marker))) {
        throw "Refusing to clear a staging folder this script did not make: $destinationRoot"
    }
    Remove-Item -LiteralPath $destinationRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $destinationRoot -Force | Out-Null
Set-Content -LiteralPath (Join-Path $destinationRoot $marker) -Value 'Starfront plugin staging' -Encoding ascii
foreach ($entry in $entries) {
    New-Item -ItemType Directory -Path (Split-Path $entry.Target) -Force | Out-Null
    Copy-Item -LiteralPath $entry.Source -Destination $entry.Target
}
Write-Host "Staged $($entries.Count) files in $destinationRoot"
