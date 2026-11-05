# Build the downloadable Starfront: a folder with Starfront.exe in it, zipped.
#
#     powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#
# Needs the same Python the program runs under, with PyInstaller and Pillow
# installed in it (python -m pip install pyinstaller pillow) - Pillow only to
# turn packaging\logo.png into the icon. Writes:
#
#     packaging\dist\Starfront\               the folder people run
#     packaging\dist\Starfront-<version>-windows.zip   what people download
#
# Nothing here reads or writes anybody's settings; it only packs the program.
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $root

$version = (python -c "import astrocontrol; print(astrocontrol.__version__)").Trim()
$build = Join-Path $PSScriptRoot "build"
$dist = Join-Path $PSScriptRoot "dist"
New-Item -ItemType Directory -Force $build | Out-Null
New-Item -ItemType Directory -Force $dist | Out-Null

# An Anaconda Python, or a venv made from one, keeps the DLLs its own modules
# load (libffi for _ctypes, OpenSSL, sqlite, lzma) in the base install's
# Library\bin, and PyInstaller does not look there. Left off PATH they are left
# out of the build, which finishes cleanly and then dies at start with "DLL load
# failed while importing _ctypes": 0.2.44's first build did. A python.org
# Python has no Library\bin, so this changes nothing there.
$basePrefix = (python -c "import sys; print(sys.base_prefix)").Trim()
$condaBin = Join-Path $basePrefix "Library\bin"
if (Test-Path $condaBin) {
    Write-Host "== Anaconda's DLLs from $condaBin"
    $env:PATH = "$condaBin;$env:PATH"
}

Write-Host "== icon"
python (Join-Path $PSScriptRoot "make_icon.py") (Join-Path $build "starfront.ico")

Write-Host "== pyinstaller (Starfront $version)"
if (Test-Path (Join-Path $dist "Starfront")) { Remove-Item -Recurse -Force (Join-Path $dist "Starfront") }
python -m PyInstaller --noconfirm --clean `
    --workpath (Join-Path $build "work") `
    --distpath $dist `
    (Join-Path $PSScriptRoot "starfront.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Write-Host "== extras"
Copy-Item (Join-Path $PSScriptRoot "READ ME FIRST.txt") (Join-Path $dist "Starfront\READ ME FIRST.txt") -Force
Set-Content -Path (Join-Path $dist "Starfront\VERSION.txt") -Value "Starfront $version" -Encoding ascii

Write-Host "== zip"
$zip = Join-Path $dist "Starfront-$version-windows.zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
Compress-Archive -Path (Join-Path $dist "Starfront") -DestinationPath $zip -CompressionLevel Optimal
$size = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host ""
Write-Host "Built $zip  ($size MB)"

# The installer: one file that puts Starfront under the person's own Programs
# folder with Start-menu and desktop shortcuts and an uninstaller, with no
# administrator prompt. Needs Inno Setup 6 (winget install JRSoftware.InnoSetup);
# without it the zip above is still a complete download.
Write-Host "== installer"
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
          "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
    & $iscc /Q "/DVersion=$version" (Join-Path $PSScriptRoot "starfront.iss")
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
    $setup = Join-Path $dist "Starfront-Setup-$version.exe"
    $setupSize = [math]::Round((Get-Item $setup).Length / 1MB, 1)
    Write-Host "Built $setup  ($setupSize MB)"
    # Stage it for the collaboration server, which offers it at /download.
    $downloads = Join-Path $root "server\downloads"
    New-Item -ItemType Directory -Force $downloads | Out-Null
    Copy-Item $setup $downloads -Force
    Write-Host "Copied to server\downloads. Run 'Deploy the Starfront server.cmd' to put it on the download page."
} else {
    Write-Host "Inno Setup is not installed, so no installer was made: winget install JRSoftware.InnoSetup"
    Write-Host "Share the zip. People unzip it and double-click Starfront.exe."
}
