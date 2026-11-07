# ==============================================================================
# Form-Guard Agent — リモートPC用 ワンクリック環境構築＆ClaudeアプリMCP接続スクリプト
# ==============================================================================
# 使い方（リモートPCの PowerShell で実行）:
#   powershell -ExecutionPolicy Bypass -File .\setup_remote_demo.ps1
# ==============================================================================

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if ((Split-Path -Leaf $RepoDir) -eq "scripts") {
    $RepoDir = Split-Path -Parent $RepoDir
}
Set-Location $RepoDir

Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host " Form-Guard Agent | Remote Demo Setup & Claude Desktop MCP Connector" -ForegroundColor Cyan
Write-Host " Working Directory: $RepoDir" -ForegroundColor Cyan
Write-Host "======================================================================" -ForegroundColor Cyan

# 1. Python 仮想環境 (.venv) の作成とパッケージ導入
$VenvPython = Join-Path $RepoDir ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "[1/4] Creating Python virtual environment (.venv)..." -ForegroundColor Yellow
    python -m venv .venv
} else {
    Write-Host "[1/4] Found existing .venv." -ForegroundColor Green
}

Write-Host "[2/4] Installing requirements..." -ForegroundColor Yellow
& $VenvPython -m pip install --upgrade pip --quiet
& $VenvPython -m pip install -r (Join-Path $RepoDir "requirements.txt") --quiet
Write-Host "      Requirements installed." -ForegroundColor Green

# 2. Claude Desktop (クローラのデスクトップアプリ) の MCP 設定を自動登録
$ClaudeConfigDir = Join-Path $env:APPDATA "Claude"
$ClaudeConfigPath = Join-Path $ClaudeConfigDir "claude_desktop_config.json"

Write-Host "[3/4] Configuring Claude Desktop MCP ($ClaudeConfigPath)..." -ForegroundColor Yellow
if (-not (Test-Path $ClaudeConfigDir)) {
    New-Item -ItemType Directory -Path $ClaudeConfigDir -Force | Out-Null
}

if (Test-Path $ClaudeConfigPath) {
    $BackupPath = "$ClaudeConfigPath.bak-" + (Get-Date -Format "yyyyMMdd-HHmmss")
    Copy-Item $ClaudeConfigPath $BackupPath -Force
    Write-Host "      Backed up existing config to $BackupPath" -ForegroundColor Gray
}

& $VenvPython -X utf8 -c @"
import json, os

config_path = r'$ClaudeConfigPath'
repo_dir = r'$RepoDir'
venv_py = r'$VenvPython'

data = {}
if os.path.exists(config_path):
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = {}

if 'mcpServers' not in data or not isinstance(data['mcpServers'], dict):
    data['mcpServers'] = {}

data['mcpServers']['form-guard'] = {
    'command': venv_py,
    'args': ['-m', 'form_guard.mcp_server'],
    'cwd': repo_dir,
    'env': {
        'PYTHONUTF8': '1',
        'PYTHONIOENCODING': 'utf-8'
    }
}

with open(config_path, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write('\n')
print('      Updated claude_desktop_config.json with form-guard MCP server.')
"@

# 3. ローカル Web UI (http://127.0.0.1:8000) の起動確認・バックグラウンド起動
Write-Host "[4/4] Starting Form-Guard Web Server on http://127.0.0.1:8000 ..." -ForegroundColor Yellow
$PortListening = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if (-not $PortListening) {
    Start-Process -FilePath $VenvPython -ArgumentList "-m","form_guard.cli","--serve","--port","8000" -WorkingDirectory $RepoDir -WindowStyle Hidden
    Start-Sleep -Seconds 2
}
Write-Host "      Web Server is running at http://127.0.0.1:8000/start" -ForegroundColor Green

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Green
Write-Host " ✅ セットアップ完了！" -ForegroundColor Green
Write-Host "  1. Claude デスクトップアプリを再起動すると 'form-guard' MCP 5ツールが使えます。" -ForegroundColor White
Write-Host "  2. Claude Code で実演する場合はこのフォルダで 'claude' を起動してください。" -ForegroundColor White
Write-Host "  3. 実演プロンプトと台本は docs\REMOTE_CLAUDE_LIVE_DEMO_MASTER_JA.md を開いてください。" -ForegroundColor White
Write-Host "======================================================================" -ForegroundColor Green
