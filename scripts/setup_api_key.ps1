# Form-Guard だけが使う API キーを .env に保存する（.env は .gitignore 済み・GitHub には上がらない）
# 使い方: pwsh scripts/setup_api_key.ps1
# - Claude Code 本体（claude-hackathon）は今までどおり Max プランのまま。キーは Form-Guard の Web 画面と MCP サーバーだけが読む。
# - 退避済みのユーザー環境変数 ANTHROPIC_API_KEY_SAVED があれば、確認のうえそれを使える。無ければ貼り付けて入力する。
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$envPath = Join-Path (Get-Location) ".env"
if (Test-Path $envPath) {
    if ((Read-Host ".env はすでにあります。上書きしますか？ (y/N)") -ne "y") { Write-Host "中止しました。"; exit 0 }
}
$key = $null
$saved = [Environment]::GetEnvironmentVariable("ANTHROPIC_API_KEY_SAVED", "User")
if ($saved -and (Read-Host "退避済みのキー（ANTHROPIC_API_KEY_SAVED・$($saved.Length)文字）を Form-Guard 用に使いますか？ (y/N)") -eq "y") {
    $key = $saved
} else {
    $sec = Read-Host "Anthropic の API キーを貼り付けてください（画面には表示されません）" -AsSecureString
    $key = [System.Net.NetworkCredential]::new("", $sec).Password
}
if (-not $key -or -not $key.StartsWith("sk-ant-")) { throw "API キーの形式ではありません（sk-ant- で始まります）。" }
$lines = @(
    "# Form-Guard 専用（git-ignored）。Claude Code 本体はこのファイルを読まない。",
    "ANTHROPIC_API_KEY=$key",
    "FORM_GUARD_MODEL_TIER=auto"
)
[IO.File]::WriteAllLines($envPath, $lines, (New-Object Text.UTF8Encoding $false))
Write-Host "保存しました: $envPath （中身は表示しません）"
Write-Host "次: python -m form_guard.cli --serve を起動し直すと、画面の STEP 1 で「Claude（本番の AI）」が選べます。"
