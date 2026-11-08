@echo off
setlocal
chcp 65001 >nul
title Form-Guard Agent - Built with Claude: Japan 2026 Live Demo
cd /d "%~dp0.."

echo ============================================================================
echo  Form-Guard Agent ^| Live Demo Session
echo  1. Verifying local Form-Guard Web Server on http://127.0.0.1:8000/start ...
echo  2. Launching Claude Code (.mcp.json enabled) ...
echo  If start_url cannot load the draft, stop the running web server and start it again without FORM_GUARD_FORCE_DEMO=1.
echo ============================================================================

if not exist ".venv\Scripts\python.exe" (
    echo [NG] .venv\Scripts\python.exe not found. Run scripts\setup_remote_demo.ps1 -AllowLive first.
    pause
    exit /b 1
)

set "URI_START=http://127.0.0.1:8000/start"
set "URI_HEALTH=http://127.0.0.1:8000/api/health"

set "PS_CHECK=function Test-FGServer { try { $h = Invoke-RestMethod -Uri '%URI_HEALTH%' -TimeoutSec 2 -ErrorAction Stop; $s = Invoke-WebRequest -Uri '%URI_START%' -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop; return ($h.status -eq 'ok' -and $h.live_available -eq $false -and $h.default_mode -eq 'demo' -and $s.StatusCode -eq 200 -and $s.Content -match 'Form-Guard') } catch { return $false } }"

set "FG_BOOT=import os, runpy, sys; [os.environ.pop(k, None) for k in ('FORM_GUARD_FORCE_DEMO', 'FORM_GUARD_API_KEY', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'FG_BOOT')]; import form_guard.mapper as m; m.load_dotenv = lambda *a, **k: None; sys.argv = ['form_guard.cli', '--serve', '--port', '8000']; runpy.run_module('form_guard.cli', run_name='__main__')"

set "PS_MAIN=$expPy = [IO.Path]::GetFullPath('.\.venv\Scripts\python.exe'); $needStart = $true; $conns = @(Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction Ignore); if ($conns.Count -gt 0) { $pids = @($conns | Select-Object -ExpandProperty OwningProcess -Unique); foreach ($targetPid in $pids) { $w = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $targetPid) -ErrorAction Ignore; if (-not $w -or -not $w.CommandLine -or ($w.CommandLine.IndexOf($expPy, [StringComparison]::OrdinalIgnoreCase) -lt 0)) { Write-Host '[NG] Port 8000 is in use by another process or repository.'; exit 1 } }; if (Test-FGServer) { $needStart = $false } else { foreach ($targetPid in $pids) { Stop-Process -Id $targetPid -Force -ErrorAction Ignore }; Start-Sleep -Milliseconds 500 } }; if ($needStart) { Remove-Item Env:FORM_GUARD_FORCE_DEMO, Env:FORM_GUARD_API_KEY, Env:ANTHROPIC_API_KEY, Env:ANTHROPIC_AUTH_TOKEN -ErrorAction SilentlyContinue; $q = [char]34; $p = Start-Process -FilePath $expPy -ArgumentList ('-c ' + $q + $env:FG_BOOT + $q) -WorkingDirectory (Get-Location).Path -WindowStyle Hidden -PassThru -ErrorAction Stop; $ok = $false; for ($i=0; $i -lt 20; $i++) { Start-Sleep -Seconds 1; if ($p.HasExited) { break }; if (Test-FGServer) { $ok = $true; break } }; if (-not $ok) { exit 1 } }"

powershell -NoProfile -Command "try { %PS_CHECK%; %PS_MAIN% } catch { Write-Host ('[NG] ' + $_.Exception.Message); exit 1 }"
set "FG_BOOT="
if errorlevel 1 (
    echo [NG] Failed to start or verify Form-Guard Web Server on port 8000.
    pause
    exit /b 1
)

rem Only Claude Code / MCP get FORCE_DEMO=1. On the web server it disables start_url draft import (form_guard/server.py:443).
set FORM_GUARD_FORCE_DEMO=1

if exist "%USERPROFILE%\.claude-hackathon\claude-hackathon.cmd" goto :run_isolated
if /I "%~1"=="--remote" goto :run_standard
if defined CLAUDE_CONFIG_DIR goto :run_standard

echo [NG] Isolated launcher not found and CLAUDE_CONFIG_DIR is not set.
echo      Set CLAUDE_CONFIG_DIR or run: scripts\launch_clean_demo.cmd --remote
pause
exit /b 1

:run_isolated
cls
call "%USERPROFILE%\.claude-hackathon\claude-hackathon.cmd"
set "demo_exit=%ERRORLEVEL%"
if not "%demo_exit%"=="0" pause
exit /b %demo_exit%

:run_standard
where claude >nul 2>&1
if errorlevel 1 (
    echo [NG] 'claude' command not found in PATH.
    pause
    exit /b 1
)
cls
call claude
set "demo_exit=%ERRORLEVEL%"
if not "%demo_exit%"=="0" pause
exit /b %demo_exit%
