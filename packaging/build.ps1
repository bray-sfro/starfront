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
Write-Host "Share that file. People unzip it and double-click Starfront.exe."
