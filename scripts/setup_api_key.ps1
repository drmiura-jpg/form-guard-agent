# Form-Guard だけが使う API キーを .env に保存する（.env / .env.* は .gitignore 済み・GitHub には上がらない）
# 使い方: pwsh scripts/setup_api_key.ps1 [-UseSaved]
# - Claude Code 本体（claude-hackathon）は今までどおり Max プランのまま。キーは Form-Guard の Web 画面と MCP サーバーだけが読む。
# - 退避済みのユーザー環境変数 ANTHROPIC_API_KEY_SAVED があれば、確認のうえそれを使える（-UseSaved 指定時は確認なしで反映）。無ければ貼り付けて入力する。
# - 既存の .env がある場合は、他の設定行やコメントを消さずに FORM_GUARD_API_KEY の行だけを更新し、更新前を .env.bak に退避する。
param(
    [switch]$UseSaved
)
$ErrorActionPreference = "Stop"
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    $envPath = Join-Path (Get-Location) ".env"
    $existingLines = @()
    if (Test-Path $envPath) {
        if (-not $UseSaved -and (Read-Host ".env はすでにあります。FORM_GUARD_API_KEY の行だけを更新しますか？（他の設定は保持されます） (y/N)") -ne "y") {
            Write-Host "中止しました。"; exit 0
        }
        $existingLines = [IO.File]::ReadAllLines($envPath, [Text.Encoding]::UTF8)
        Copy-Item -Path $envPath -Destination "$envPath.bak" -Force
    }
    $key = $null
    $saved = [Environment]::GetEnvironmentVariable("ANTHROPIC_API_KEY_SAVED", "User")
    if ($saved -and ($UseSaved -or (Read-Host "退避済みのキー（ANTHROPIC_API_KEY_SAVED・$($saved.Length)文字）を Form-Guard 用に使いますか？ (y/N)") -eq "y")) {
        $key = $saved.Trim()
    } else {
        $sec = Read-Host "Anthropic の API キーを貼り付けてください（画面には表示されません）" -AsSecureString
        $key = ([System.Net.NetworkCredential]::new("", $sec).Password).Trim()
    }
    if (-not $key -or -not $key.StartsWith("sk-ant-")) { throw "API キーの形式ではありません（sk-ant- で始まります）。" }

    $outLines = [System.Collections.Generic.List[string]]::new()
    if ($existingLines.Count -eq 0) {
        $outLines.Add("# Form-Guard 専用（git-ignored）。ANTHROPIC_API_KEY ではなく FORM_GUARD_API_KEY を使うため Claude Code 本体の Max 定額枠に影響しない。")
    }
    $keyUpdated = $false
    $hasTier = $false
    foreach ($ln in $existingLines) {
        if ($ln -match '^\s*FORM_GUARD_API_KEY\s*=') {
            if (-not $keyUpdated) { $outLines.Add("FORM_GUARD_API_KEY=$key"); $keyUpdated = $true }
        } else {
            if ($ln -match '^\s*FORM_GUARD_MODEL_TIER\s*=') { $hasTier = $true }
            $outLines.Add($ln)
        }
    }
    if (-not $keyUpdated) { $outLines.Add("FORM_GUARD_API_KEY=$key") }
    if (-not $hasTier) { $outLines.Add("FORM_GUARD_MODEL_TIER=auto") }

    [IO.File]::WriteAllLines($envPath, $outLines, (New-Object Text.UTF8Encoding $false))
    $hasKeyLine = Select-String -Path $envPath -Pattern '^FORM_GUARD_API_KEY=sk-ant-' -Quiet
    $userAnthropicKey = [Environment]::GetEnvironmentVariable("ANTHROPIC_API_KEY", "User")
    Write-Host "保存しました: $envPath （中身は表示しません）"
    Write-Host "[セルフチェック OK] FORM_GUARD_API_KEY 設定済み（$($key.Length)文字・検証=$hasKeyLine） / ユーザー環境変数 ANTHROPIC_API_KEY=$(if ($userAnthropicKey) { 'あり' } else { '未設定（Claude Code Max定額枠を維持）' })"
    Write-Host "次: python -m form_guard.cli --serve を起動し直すと、画面の STEP 1 で「Claude（本番の AI）」も選べます。"
}
finally {
    Pop-Location
}
