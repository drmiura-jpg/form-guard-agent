# Temporary public demo URL (Cloudflare quick tunnel) protected by a passphrase.
# Usage: pwsh scripts/share_tunnel.ps1 [-Port 8000] [-Key <passphrase>]
# Any user name works in the browser prompt; the password is the passphrase printed below.
# Stop with Ctrl+C. Only the built-in fictional demo forms should be used while it is public.
param([int]$Port = 8000, [string]$Key = "")
$ErrorActionPreference = "Stop"
if (-not $Key) { $Key = -join ((48..57) + (97..122) | Get-Random -Count 12 | ForEach-Object { [char]$_ }) }
$log = Join-Path $env:TEMP "form_guard_tunnel.log"
Remove-Item $log -ErrorAction SilentlyContinue
$cf = Start-Process cloudflared -ArgumentList "tunnel --url http://127.0.0.1:$Port --no-autoupdate" `
    -RedirectStandardError $log -PassThru -WindowStyle Hidden
$hostName = $null
for ($i = 0; $i -lt 60 -and -not $hostName; $i++) {
    Start-Sleep 1
    if (Test-Path $log) {
        $m = Select-String -Path $log -Pattern 'https://([a-z0-9-]+\.trycloudflare\.com)' | Select-Object -First 1
        if ($m) { $hostName = $m.Matches[0].Groups[1].Value }
    }
}
if (-not $hostName) { Stop-Process -Id $cf.Id -Force; throw "tunnel URL not issued (see $log)" }
$env:FORM_GUARD_ALLOWED_HOSTS = $hostName
$env:FORM_GUARD_ACCESS_KEY = $Key
Write-Host "Public URL : https://$hostName"
Write-Host "Passphrase : $Key   (user name: anything)"
try { python -m form_guard.cli --serve --port $Port }
finally { Stop-Process -Id $cf.Id -Force -ErrorAction SilentlyContinue }
