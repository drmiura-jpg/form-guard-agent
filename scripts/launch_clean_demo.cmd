@echo off
chcp 65001 >nul
title Form-Guard Agent - Built with Claude: Japan 2026 Live Demo
cd /d C:\hackathon\form-guard-agent
prompt C:\hackathon\form-guard-agent$G 

echo ============================================================================
echo  Form-Guard Agent ^| Live Demo Session
echo  1. Starting local Form-Guard Web Server on http://127.0.0.1:8000 ...
echo  2. Launching Claude Code (.mcp.json enabled) ...
echo ============================================================================

powershell -NoProfile -Command "if (-not (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)) { Start-Process -FilePath 'C:\hackathon\form-guard-agent\.venv\Scripts\python.exe' -ArgumentList '-m','form_guard.cli','--serve','--port','8000' -WorkingDirectory 'C:\hackathon\form-guard-agent' -WindowStyle Hidden; Start-Sleep -Seconds 2 }"

cls
call claude
