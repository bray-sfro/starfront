# Install the lab's build of the Starfront N.I.N.A. plugin on this PC and point
# it at the lab server. The lab server serves this file, with its own address
# filled in, so on the PC that runs N.I.N.A. (the backyard rig, say) it is one
# line in a normal, not an administrator, PowerShell window:
#
#   irm http://omarchy.lan:8800/_local/install-nina-plugin.ps1 | iex
#
# It installs the build the lab server's /nina feed offers, checked against the
# manifest's SHA-256, in place of any Starfront plugin already installed, and
# sets the plugin's ServerUrl. Nothing else of N.I.N.A.'s is touched. Run it
# again after the lab server gets a newer build (fetch-binaries.sh --plugin,
# then terraform apply). To go back to the real server, set ServerUrl to ""
# in the settings file it names, or install the plugin from N.I.N.A.'s own
# plugin list.
#
# It is a script block rather than a script so that, run through iex, an error
# stops it without closing the window.
& {
    $ErrorActionPreference = "Stop"
    $ProgressPreference = "SilentlyContinue"   # Windows PowerShell's progress bar slows downloads tenfold
    $server = "__STARFRONT_SERVER__"
    if ($env:STARFRONT_LAB_SERVER) { $server = $env:STARFRONT_LAB_SERVER }
    if ($server -like "__*") {
        throw "run this as the lab server serves it: irm http://<server>:8800/_local/install-nina-plugin.ps1 | iex (or set STARFRONT_LAB_SERVER first)"
    }
    $server = $server.TrimEnd("/")
    $id = "896B4E28-7CEB-4731-BB97-8BADE7460268"   # the plugin's GUID (nina/Plugin.props)
    $nina = Join-Path $env:LOCALAPPDATA "NINA"
    if (-not (Test-Path $nina)) {
        throw "N.I.N.A. has not run for this Windows user ($nina is missing): install N.I.N.A. 3.2, start it once, close it, then run this again."
    }
    if (Get-Process NINA -ErrorAction SilentlyContinue) {
        throw "N.I.N.A. is running: close it, then run this again (a running N.I.N.A. holds the plugin's files)."
    }

    Write-Output "== the plugin build on $server"
    $manifests = Invoke-RestMethod -UseBasicParsing "$server/nina/plugins/manifests" -TimeoutSec 20
    $manifest = @($manifests) | Where-Object { $_.Identifier -eq $id } | Select-Object -First 1
    if (-not $manifest) {
        throw "$server offers no Starfront plugin build. On the Mac: deploy/home-lab/scripts/fetch-binaries.sh --plugin, then terraform apply."
    }
    $v = $manifest.Version
    $version = "$($v.Major).$($v.Minor).$($v.Patch).$($v.Build)"
    $name = ($manifest.Installer.URL -split "/")[-1]
    $zip = Join-Path $env:TEMP "starfront-lab-$name"
    Invoke-WebRequest -UseBasicParsing "$server/nina/download/$name" -OutFile $zip -TimeoutSec 120
    $hash = (Get-FileHash $zip -Algorithm SHA256).Hash
    if ($hash -ne "$($manifest.Installer.Checksum)".ToUpperInvariant()) {
        Remove-Item -Force $zip
        throw "$name does not match its manifest's checksum (got $hash); nothing was installed."
    }

    $dest = Join-Path $nina "Plugins\3.0.0\Starfront"
    Write-Output "== installing $version into $dest"
    if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
    Expand-Archive -Path $zip -DestinationPath $dest
    Remove-Item -Force $zip

    # The plugin keeps its server in a settings file of its own, beside its
    # sign-in (protected for this Windows user); only ServerUrl is changed.
    $folder = Join-Path $nina "PluginSettings\$id"
    $file = Join-Path $folder "settings.json"
    New-Item -ItemType Directory -Force $folder | Out-Null
    $pattern = '"ServerUrl"\s*:\s*"[^"]*"'
    $entry = '"ServerUrl": "' + $server + '"'
    $before = ""
    if (Test-Path $file) {
        $text = [IO.File]::ReadAllText($file)
        if ($text -match $pattern) {
            $before = ($Matches[0] -replace '^"ServerUrl"\s*:\s*"|"$', '')
            $text = $text -replace $pattern, $entry
        } else {
            $text = $text -replace '^\s*\{', ("{`r`n  " + $entry + ",")
        }
    } else {
        $text = "{`r`n  " + $entry + "`r`n}"
    }
    [IO.File]::WriteAllText($file, $text)
    if ($before -and $before -ne $server) {
        Write-Output "== ServerUrl was $before; it is now $server (sign in again in the plugin: a sign-in belongs to one server)"
    } else {
        Write-Output "== ServerUrl is $server"
    }
    Write-Output "   settings: $file"

    try {
        $null = Invoke-RestMethod -UseBasicParsing "$server/api/v1/health" -TimeoutSec 10
        Write-Output "== $server answers"
    } catch {
        Write-Output "!! $server did not answer its health check from this PC: $($_.Exception.Message)"
    }
    Write-Output ""
    Write-Output "Done. Start N.I.N.A.; Options > Plugins lists Starfront $version. Sign in from the"
    Write-Output "plugin (the lab's stand-in Discord asks only for a name: ncedelliott is the owner)."
}
