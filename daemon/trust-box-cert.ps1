<#
.SYNOPSIS
    Trust the Achievement Box root CA on this Windows machine, so the box's
    https:// URLs open with a clean padlock (real PWA install, unflagged push)
    instead of a certificate warning.

.DESCRIPTION
    Downloads the box's root CA over plain http (you can't validate the box's
    own https until you trust it -- chicken and egg) and imports it into the
    Windows Trusted Root store.

    Run elevated  -> imports machine-wide (LocalMachine\Root), fully silent.
    Run normally  -> imports for your user (CurrentUser\Root); Windows shows
                     one "install this certificate?" confirmation. Click Yes.

    The box has a persistent CA that re-issues its server cert as LAN IPs drift,
    so you only need to run this once.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File daemon\trust-box-cert.ps1

.EXAMPLE
    # Testing against a daemon on this same machine:
    powershell -ExecutionPolicy Bypass -File daemon\trust-box-cert.ps1 -BoxHost localhost
#>
[CmdletBinding()]
param(
    [string]$BoxHost = 'achievementbox.local',
    [int]$HttpPort   = 8000
)

$ErrorActionPreference = 'Stop'

$url = "http://${BoxHost}:${HttpPort}/rootca.crt"
$tmp = Join-Path $env:TEMP 'achievementbox-rootca.crt'
# The box serves https on 443 (best effort) and :8443; point at whichever the
# banner showed. We can't know it from the http port, so suggest the common one.
$httpsHint = "https://${BoxHost}  (or https://${BoxHost}:8443)"

Write-Host "Fetching CA from $url ..."
try {
    Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
} catch {
    Write-Host "Could not reach the box at $url" -ForegroundColor Red
    Write-Host "  - Is the daemon running and on the network?" -ForegroundColor Red
    Write-Host "  - Try -BoxHost <the box's IP> if achievementbox.local doesn't resolve." -ForegroundColor Red
    exit 1
}

$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2 $tmp
$thumb = $cert.Thumbprint
Write-Host "CA subject : $($cert.Subject)"
Write-Host "Thumbprint : $thumb"

# Elevated? -> machine-wide + silent. Otherwise per-user (one Yes prompt).
$elevated = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$storeLoc = if ($elevated) { 'LocalMachine' } else { 'CurrentUser' }
$storePath = "Cert:\$storeLoc\Root"

# Idempotent: skip if this exact CA is already trusted in the target store.
if (Test-Path (Join-Path $storePath $thumb)) {
    Write-Host "Already trusted in $storePath -- nothing to do." -ForegroundColor Green
    Remove-Item $tmp -ErrorAction SilentlyContinue
    Write-Host "Open  $httpsHint  -- it should show a clean padlock."
    exit 0
}

Write-Host "Importing into $storePath ($storeLoc)..."
if (-not $elevated) {
    Write-Host "(not elevated) Windows will ask you to confirm -- click Yes." -ForegroundColor Yellow
}
Import-Certificate -FilePath $tmp -CertStoreLocation $storePath | Out-Null

if (Test-Path (Join-Path $storePath $thumb)) {
    Write-Host "Trusted. The Achievement Box CA is now installed." -ForegroundColor Green
    Write-Host "Open  $httpsHint  -- it should show a clean padlock."
    if (-not $elevated) {
        Write-Host "(Tip: run this from an elevated PowerShell to trust it for the whole machine and skip the prompt.)"
    }
} else {
    Write-Host "Import did not register the CA -- was the confirmation dialog dismissed?" -ForegroundColor Red
    exit 1
}

Remove-Item $tmp -ErrorAction SilentlyContinue
