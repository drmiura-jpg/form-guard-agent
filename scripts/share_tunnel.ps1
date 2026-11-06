# Temporary public demo URL (Cloudflare quick tunnel) protected by a passphrase.
# Usage: pwsh scripts/share_tunnel.ps1 [-Port 8000] [-Key <letters and digits>] [-AllowLive]
# Open the printed "/start?key=..." link in a browser (it sets a 12-hour cookie), or use the Basic prompt
# (any user name, password = passphrase). Stop with Ctrl+C. Use only the built-in fictional demo forms while public.
param(
    [int]$Port = 8000,
    [ValidatePattern('^[A-Za-z0-9]*$')][string]$Key = "",
    [switch]$AllowLive  # without it the public server never calls the Anthropic API (demo mode only)
)
$ErrorActionPreference = "Stop"
Push-Location (Split-Path $PSScriptRoot -Parent)  # repository root, wherever the script is started from
if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
    Pop-Location
    throw "cloudflared が見つかりません。 winget install --id Cloudflare.cloudflared でインストールしてください。"
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    Pop-Location
    throw "ポート $Port は使用中です（別のサービスを公開しないため中止）。 -Port で別の番号を指定してください。"
}
if (-not $Key) {
    $bytes = [byte[]]::new(12)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $Key = -join ($bytes | ForEach-Object { "abcdefghjkmnpqrstuvwxyz23456789"[$_ % 31] })
}
$log = Join-Path $env:TEMP "form_guard_tunnel.log"
$cf = $null; $server = $null
$prevAccessKey = $env:FORM_GUARD_ACCESS_KEY
$prevAllowedHosts = $env:FORM_GUARD_ALLOWED_HOSTS
$prevForceDemo = $env:FORM_GUARD_FORCE_DEMO
try {
    $env:FORM_GUARD_ACCESS_KEY = $Key
    $env:FORM_GUARD_FORCE_DEMO = if ($AllowLive) { "0" } else { "1" }
    $server = Start-Process python -ArgumentList "-m form_guard.cli --serve --port $Port" -PassThru -NoNewWindow
    $ready = $false
    for ($i = 0; $i -lt 30 -and -not $ready; $i++) {
        Start-Sleep 1
        if ($server.HasExited) { throw "サーバーの起動に失敗しました（終了コード $($server.ExitCode)）。" }
        try { Invoke-WebRequest "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 2 | Out-Null }
        catch { if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 401) { $ready = $true } }
    }
    if (-not $ready) { throw "サーバーが合言葉つきで応答しません（公開を中止）。" }
    Remove-Item $log -ErrorAction SilentlyContinue
    $cf = Start-Process cloudflared -ArgumentList "tunnel --url http://127.0.0.1:$Port --no-autoupdate" `
        -RedirectStandardError $log -PassThru -WindowStyle Hidden
    $hostName = $null
    for ($i = 0; $i -lt 60 -and -not $hostName; $i++) {
        Start-Sleep 1
        if ($cf.HasExited) { throw "cloudflared が終了しました（$log を確認）。" }
        $m = Select-String -Path $log -Pattern 'https://([a-z0-9-]+\.trycloudflare\.com)' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($m) { $hostName = $m.Matches[0].Groups[1].Value }
    }
    if (-not $hostName) { throw "公開URLが発行されませんでした（$log を確認）。" }
    # the server re-reads FORM_GUARD_ALLOWED_HOSTS per request only from its own environment: restart it with the host
    Stop-Process -Id $server.Id -Force; $server.WaitForExit()
    $env:FORM_GUARD_ALLOWED_HOSTS = $hostName
    $server = Start-Process python -ArgumentList "-m form_guard.cli --serve --port $Port" -PassThru -NoNewWindow
    Start-Sleep 3
    if ($server.HasExited) { throw "サーバーの再起動に失敗しました。" }
    Write-Host "PCのブラウザで開くURL（合言葉入り・人に見せない）: https://$hostName/start?key=$Key"
    Write-Host "Public URL : https://$hostName   Passphrase : $Key   (user name: anything)"
    $server.WaitForExit()
}
finally {
    foreach ($p in @($server, $cf)) { if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } }
    if ($null -eq $prevAccessKey) { Remove-Item Env:FORM_GUARD_ACCESS_KEY -ErrorAction SilentlyContinue } else { $env:FORM_GUARD_ACCESS_KEY = $prevAccessKey }
    if ($null -eq $prevAllowedHosts) { Remove-Item Env:FORM_GUARD_ALLOWED_HOSTS -ErrorAction SilentlyContinue } else { $env:FORM_GUARD_ALLOWED_HOSTS = $prevAllowedHosts }
    if ($null -eq $prevForceDemo) { Remove-Item Env:FORM_GUARD_FORCE_DEMO -ErrorAction SilentlyContinue } else { $env:FORM_GUARD_FORCE_DEMO = $prevForceDemo }
    Pop-Location
}
