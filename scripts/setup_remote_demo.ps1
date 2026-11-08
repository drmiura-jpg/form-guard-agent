# ==============================================================================
# Form-Guard Agent - Remote Demo Setup & Optional Claude Desktop MCP Connector
# ==============================================================================
# Usage (after extracting or cloning to C:\hackathon\form-guard-agent):
#   cd C:\hackathon\form-guard-agent
#   powershell -ExecutionPolicy Bypass -File .\scripts\setup_remote_demo.ps1
#
# Optional switches:
#   -ConfigureClaudeDesktop : Also register form-guard MCP in %APPDATA%\Claude\claude_desktop_config.json
#   -AllowLive              : Do not force FORM_GUARD_FORCE_DEMO=1 (use when importing MCP drafts via review_url or testing live API)
#   -UpgradePip             : Force pip upgrade and requirements install even when .venv already exists
# ==============================================================================

param(
    [string]$RepoDir = "",
    [switch]$ConfigureClaudeDesktop,
    [switch]$AllowLive,
    [switch]$UpgradePip
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if ([string]::IsNullOrWhiteSpace($RepoDir)) {
    $Candidate = Split-Path -Parent $MyInvocation.MyCommand.Path
    if ((Split-Path -Leaf $Candidate) -eq "scripts") {
        $Candidate = Split-Path -Parent $Candidate
    }
    $RepoDir = $Candidate
}
$RepoDir = [System.IO.Path]::GetFullPath($RepoDir)

$ReqFile = Join-Path $RepoDir "requirements.txt"
$McpModule = Join-Path $RepoDir "form_guard\mcp_server.py"
if (-not (Test-Path -LiteralPath $ReqFile) -or -not (Test-Path -LiteralPath $McpModule)) {
    Write-Host "[NG] Repository files (requirements.txt / form_guard\mcp_server.py) not found in: $RepoDir" -ForegroundColor Red
    throw "Repository files not found in $RepoDir"
}

Set-Location -LiteralPath $RepoDir

Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host " Form-Guard Agent | Remote Demo Setup & Optional Claude Desktop MCP" -ForegroundColor Cyan
Write-Host " Working Directory: $RepoDir" -ForegroundColor Cyan
Write-Host "======================================================================" -ForegroundColor Cyan

# 0. Check Python 3.10+
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "[NG] 'python' command not found. Please install Python 3.10+." -ForegroundColor Red
    throw "Python 3.10+ not found"
}
& python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[NG] Python 3.10 or newer is required." -ForegroundColor Red
    throw "Python 3.10+ check failed (exit code: $LASTEXITCODE)"
}

# 1. Create or reuse Python virtual environment (.venv) and install requirements
$VenvPython = Join-Path $RepoDir ".venv\Scripts\python.exe"
$CreatedNewVenv = $false

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host "[1/4] Creating Python virtual environment (.venv)..." -ForegroundColor Yellow
    & python -m venv .venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
        Write-Host "[NG] Failed to create .venv (exit code: $LASTEXITCODE)." -ForegroundColor Red
        throw "venv creation failed (exit code: $LASTEXITCODE)"
    }
    $CreatedNewVenv = $true
} else {
    Write-Host "[1/4] Found existing .venv." -ForegroundColor Green
}

& $VenvPython -c "import fastapi, uvicorn, docx, openpyxl, lxml, PIL, pydantic" 2>$null
$DepsReady = ($LASTEXITCODE -eq 0)

if ($CreatedNewVenv -or $UpgradePip -or -not $DepsReady) {
    Write-Host "[2/4] Installing requirements (requires internet on first run)..." -ForegroundColor Yellow
    if ($CreatedNewVenv -or $UpgradePip) {
        & $VenvPython -m pip install --upgrade pip --disable-pip-version-check --quiet
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[NG] pip upgrade failed (exit code: $LASTEXITCODE)." -ForegroundColor Red
            throw "pip upgrade failed (exit code: $LASTEXITCODE)"
        }
    }
    & $VenvPython -m pip install -r $ReqFile --disable-pip-version-check --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[NG] pip install -r requirements.txt failed (exit code: $LASTEXITCODE)." -ForegroundColor Red
        throw "pip install -r requirements.txt failed (exit code: $LASTEXITCODE)"
    }
    Write-Host "      [OK] Requirements installed." -ForegroundColor Green
} else {
    Write-Host "[2/4] Existing .venv dependencies verified (skipping pip network call; use -UpgradePip to force)." -ForegroundColor Green
}

# 2. Configure Claude Desktop（Claude のデスクトップアプリ）MCP only when -ConfigureClaudeDesktop is specified
if ($ConfigureClaudeDesktop) {
    $ClaudeConfigDir = Join-Path $env:APPDATA "Claude"
    $ClaudeConfigPath = Join-Path $ClaudeConfigDir "claude_desktop_config.json"

    Write-Host "[3/4] Configuring Claude Desktop MCP ($ClaudeConfigPath)..." -ForegroundColor Yellow
    if (-not (Test-Path -LiteralPath $ClaudeConfigDir)) {
        New-Item -ItemType Directory -Path $ClaudeConfigDir -Force | Out-Null
    }

    $ConfigureCode = @'
import json, os, shutil, sys, tempfile, time

config_path, repo_dir, venv_py = sys.argv[1:4]
data = {}

if os.path.exists(config_path):
    try:
        with open(config_path, "r", encoding="utf-8-sig") as f:
            raw = f.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        print(f"[NG] Failed to parse existing claude_desktop_config.json (left untouched): {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print("[NG] Root of claude_desktop_config.json is not a JSON object (left untouched).", file=sys.stderr)
        sys.exit(1)
    if "mcpServers" in data and not isinstance(data["mcpServers"], dict):
        print("[NG] 'mcpServers' in claude_desktop_config.json is not a JSON object (left untouched).", file=sys.stderr)
        sys.exit(1)
    bak_path = f"{config_path}.bak-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    shutil.copy2(config_path, bak_path)
    print(f"      Backed up existing config to {bak_path}")

mcp_servers = data.setdefault("mcpServers", {})
mcp_servers["form-guard"] = {
    "command": venv_py,
    "args": ["-m", "form_guard.mcp_server", "--root", repo_dir],
    "cwd": repo_dir,
    "env": {
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": repo_dir,
        "FORM_GUARD_MCP_ROOT": repo_dir
    }
}

cfg_dir = os.path.dirname(os.path.abspath(config_path))
fd, tmp_path = tempfile.mkstemp(prefix="claude_cfg_", suffix=".tmp", dir=cfg_dir)
try:
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open(tmp_path, "r", encoding="utf-8") as vf:
        verified = json.load(vf)
    if not isinstance(verified, dict) or "form-guard" not in verified.get("mcpServers", {}):
        raise ValueError("Verification of temporary config failed")
    os.replace(tmp_path, config_path)
finally:
    if os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
        except OSError:
            pass

print("      [OK] Updated claude_desktop_config.json with form-guard MCP server.")
'@

    & $VenvPython -X utf8 -c $ConfigureCode $ClaudeConfigPath $RepoDir $VenvPython
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[NG] Failed to update Claude Desktop MCP config (exit code: $LASTEXITCODE)." -ForegroundColor Red
        throw "Claude Desktop MCP config update failed (exit code: $LASTEXITCODE)"
    }
} else {
    Write-Host "[3/4] Skipping Claude Desktop global config modification (pass -ConfigureClaudeDesktop to register)." -ForegroundColor DarkGray
}

# 3. Verify or launch local Web UI on http://127.0.0.1:8000
Write-Host "[4/4] Starting Form-Guard Web Server on http://127.0.0.1:8000 ..." -ForegroundColor Yellow

function Test-FormGuardHealth {
    try {
        $Resp = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/health" -Method Get -TimeoutSec 2 -ErrorAction Stop
        $StartResp = Invoke-WebRequest -Uri "http://127.0.0.1:8000/start" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        if ($null -ne $Resp -and $Resp.status -eq "ok" -and $null -ne $Resp.models -and $StartResp.StatusCode -eq 200) {
            return $Resp
        }
    } catch {
        return $null
    }
    return $null
}

$DesiredMode = if ($AllowLive) { $null } else { "demo" }
$Health = Test-FormGuardHealth
$StartedProc = $null

if ($null -ne $Health -and $null -ne $DesiredMode -and $Health.default_mode -ne $DesiredMode) {
    Write-Host "[NG] An existing Form-Guard server is already running on port 8000 in '$($Health.default_mode)' mode (expected '$DesiredMode'). Stop it first or pass -AllowLive." -ForegroundColor Red
    throw "Existing Form-Guard server mode mismatch ($($Health.default_mode) != $DesiredMode)"
}

if ($null -eq $Health) {
    $PortListening = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
    if ($PortListening) {
        Write-Host "[NG] Port 8000 is already in use by another application (/api/health did not respond)." -ForegroundColor Red
        throw "Port 8000 is already in use by another application"
    }

    $PrevForceDemo = $env:FORM_GUARD_FORCE_DEMO
    try {
        if (-not $AllowLive) {
            $env:FORM_GUARD_FORCE_DEMO = "1"
        }
        $StartedProc = Start-Process -FilePath $VenvPython -ArgumentList "-m","form_guard.cli","--serve","--port","8000" -WorkingDirectory $RepoDir -WindowStyle Hidden -PassThru
    } finally {
        if ($null -eq $PrevForceDemo) {
            Remove-Item Env:FORM_GUARD_FORCE_DEMO -ErrorAction SilentlyContinue
        } else {
            $env:FORM_GUARD_FORCE_DEMO = $PrevForceDemo
        }
    }

    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        if ($StartedProc.HasExited) {
            Write-Host "[NG] Web server process exited immediately (exit code: $($StartedProc.ExitCode))." -ForegroundColor Red
            throw "Web server process exited immediately (exit code: $($StartedProc.ExitCode))"
        }
        $Health = Test-FormGuardHealth
        if ($null -ne $Health) {
            break
        }
    }
}

if ($null -eq $Health) {
    Write-Host "[NG] Web server health check (http://127.0.0.1:8000/api/health and /start) timed out after 20 seconds." -ForegroundColor Red
    throw "Web server health check timed out"
}

Write-Host "      [OK] Web Server is running and verified at http://127.0.0.1:8000/start (version: $($Health.version), default_mode: $($Health.default_mode))" -ForegroundColor Green

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Green
Write-Host " [OK] Setup finished." -ForegroundColor Green
Write-Host "  1. Open http://127.0.0.1:8000/start or http://127.0.0.1:8000/#tour=1 in your browser." -ForegroundColor White
Write-Host "  2. To stop the background server on port 8000:" -ForegroundColor White
Write-Host "     Get-NetTCPConnection -LocalPort 8000 -State Listen | ForEach-Object { Stop-Process -Id `$_.OwningProcess -Force }" -ForegroundColor Gray
Write-Host "  3. For temporary tunnel (requires pwsh + cloudflared): stop port 8000 first or run 'pwsh scripts/share_tunnel.ps1 -Port 8001'." -ForegroundColor White
Write-Host "  4. Timeline: docs/HANDOFF.md section 5" -ForegroundColor White
Write-Host "======================================================================" -ForegroundColor Green
