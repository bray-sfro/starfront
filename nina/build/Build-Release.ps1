# Build, package and validate a release from a version tag, without uploading.
#
#     ./build/Build-Release.ps1 -Tag v1.0.0.0 -Repository bray-sfro/starfront
#
# The tag's version is passed into the build, so the DLL, the zip's name and
# the manifest all carry it. The installer URL is the GitHub release asset the
# tag will publish; -InstallerBase points it somewhere else, such as the
# collaboration server's own download folder for a closed test.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Tag,
    [Parameter(Mandatory)][string]$Repository,
    [ValidateSet('Release', 'Beta')][string]$Channel = 'Release',
    [string]$InstallerBase
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. "$PSScriptRoot/Release-Settings.ps1"
$settings = Get-ReleaseSettings -Tag $Tag -Repository $Repository -Channel $Channel

function Invoke-Dotnet([string[]]$Arguments) {
    & dotnet @Arguments
    if ($LASTEXITCODE) { throw "dotnet failed with exit code $LASTEXITCODE." }
}
$root = Split-Path $PSScriptRoot
# The plugin project by name: src/ also holds RapidStackerCollab.Core, the
# upload engine the plugin references and ships, so "the one csproj under src"
# no longer picks it out.
$project = Join-Path $root 'src/Starfront.Collab/Starfront.Collab.csproj'
if (-not (Test-Path -LiteralPath $project -PathType Leaf)) { throw "Plugin project not found: $project" }
$repositoryUrl = "https://github.com/$Repository"

Invoke-Dotnet @('restore', $project)
Invoke-Dotnet @('build', $project, '-c', 'Release', '--no-restore', "-p:Version=$($settings.Version)", "-p:RepositoryUrl=$repositoryUrl")
$assemblyName = (& dotnet msbuild $project -getProperty:AssemblyName).Trim()
if ($LASTEXITCODE -or [string]::IsNullOrWhiteSpace($assemblyName)) { throw 'Could not read AssemblyName.' }
$archive = "$assemblyName.$($settings.Version).zip"
if ([string]::IsNullOrWhiteSpace($InstallerBase)) {
    $url = "$repositoryUrl/releases/download/$Tag/$archive"
} else {
    $url = "$($InstallerBase.TrimEnd('/'))/$archive"
}
Invoke-Dotnet @('msbuild', $project, '-t:PackagePlugin', '-p:Configuration=Release', "-p:Version=$($settings.Version)", "-p:RepositoryUrl=$repositoryUrl", "-p:PluginInstallerUrl=$url", "-p:PluginChannel=$Channel")

$manifestPath = Join-Path $root "artifacts/release/$assemblyName.$($settings.Version).manifest.json"
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.Repository -ne $repositoryUrl -or $manifest.Installer.URL -ne $url) { throw 'Release metadata does not match the repository and tag.' }
Write-Host "Validated release $Tag ($Channel). Assets: $root/artifacts/release"
