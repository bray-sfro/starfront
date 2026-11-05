# One-time administrator set-up of the lab's Windows PC. Run it AT that PC, in
# an elevated PowerShell, signed in as the account N.I.N.A. runs under:
#
#   powershell -ExecutionPolicy Bypass -File bootstrap-windows.ps1 `
#       -MacPublicKey "ssh-ed25519 AAAA... you@mac" [-Subnet 192.168.1.0/24] `
#       [-NasHost DXP8800PLUS-76B2 -NasUser USER]
#
# It:
#   1. installs and starts OpenSSH Server, with PowerShell as its shell;
#   2. lets SSH in on the Private profile from the home subnet only, and makes
#      the home network's profile Private (a Public one blocks the lab);
#   3. authorises the Mac's key (administrators_authorized_keys for an admin
#      account, as Windows' OpenSSH requires);
#   4. stores the NAS credential for this user (cmdkey), so the worker's logon
#      task can read \\NAS\XPSD without a mapped drive.
# Nothing inbound is needed for the worker or N.I.N.A.: they only call out.
#Requires -RunAsAdministrator
param(
    [Parameter(Mandatory)] [string] $MacPublicKey,
    [string] $Subnet = "192.168.1.0/24",
    [string] $NasHost = "DXP8800PLUS-76B2",
    [string] $NasUser = ""
)
$ErrorActionPreference = "Stop"

Write-Output "== OpenSSH Server"
$cap = Get-WindowsCapability -Online -Name "OpenSSH.Server*"
if ($cap.State -ne "Installed") { Add-WindowsCapability -Online -Name $cap.Name | Out-Null }
Set-Service sshd -StartupType Automatic
Start-Service sshd
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell -PropertyType String -Force `
    -Value "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" | Out-Null

Write-Output "== network profile and firewall"
$prefix = ($Subnet -split "/")[0] -replace "\.0$", "."
$lan = Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress.StartsWith($prefix) } | Select-Object -First 1
if (-not $lan) { throw "no adapter has an address in $Subnet" }
$netProfile = Get-NetConnectionProfile -InterfaceIndex $lan.InterfaceIndex
if ($netProfile.NetworkCategory -ne "Private") {
    Set-NetConnectionProfile -InterfaceIndex $lan.InterfaceIndex -NetworkCategory Private
    Write-Output "   $($netProfile.Name) is now Private"
}
$rule = Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue
if ($rule) {
    Set-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -Profile Private -RemoteAddress $Subnet
} else {
    New-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -DisplayName "OpenSSH Server (lab)" -Direction Inbound `
        -Protocol TCP -LocalPort 22 -Action Allow -Profile Private -RemoteAddress $Subnet | Out-Null
}

Write-Output "== the Mac's key"
$isAdmin = (New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) {
    # For a member of Administrators, Windows' sshd reads only this file, and only
    # if Administrators and SYSTEM alone can touch it.
    $keys = "$env:ProgramData\ssh\administrators_authorized_keys"
    if (-not (Test-Path $keys) -or -not (Select-String -LiteralPath $keys -SimpleMatch $MacPublicKey -Quiet)) {
        Add-Content -LiteralPath $keys -Value $MacPublicKey
    }
    icacls $keys /inheritance:r /grant "*S-1-5-32-544:F" /grant "SYSTEM:F" | Out-Null
} else {
    $dir = Join-Path $env:USERPROFILE ".ssh"
    New-Item -ItemType Directory -Force $dir | Out-Null
    Add-Content -LiteralPath (Join-Path $dir "authorized_keys") -Value $MacPublicKey
}

if ($NasUser) {
    Write-Output "== the NAS credential for \\$NasHost (you are asked for its password)"
    cmdkey /add:$NasHost /user:$NasUser /pass
}

Write-Output "== done. From the Mac: ssh $env:USERNAME@$($env:COMPUTERNAME).lan 'hostname'"
