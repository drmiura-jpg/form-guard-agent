"""Describe the Claude Code extensibility suite shipped with the repository (GET /api/claude-suite).

Everything is read from the files on disk — ``.claude/skills/*/SKILL.md``, ``.claude/agents/*.md``,
``.claude/settings.json`` with the hook guard ``scripts/hook_guard.py``, ``.mcp.json`` — plus the MCP tool list of ``form_guard.mcp_server`` and the model
routing policy of ``form_guard.mapper``, so the web UI always shows what the repository really contains.
Only known paths inside the project root are read; nothing is executed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import MODEL_HAIKU, MODEL_OPUS, MODEL_SONNET
from .mapper import MODEL_TIERS, ROUTING_POLICY, TIER_MODELS

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The two-stage guard lives in scripts/hook_guard.py; each check is listed only if its code is in that file.
# (needle in scripts/hook_guard.py, what it checks — en, ja), per stage of the hook command.
HOOK_SCRIPT = "scripts/hook_guard.py"
HOOK_CHECKS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "pre-tool-use": (
        ("def template_violation", "blank-template guard: no write, copy-over, move or delete of "
                                   "samples/blank_forms/*.docx|*.xlsx|*.pdf",
         "白紙様式（samples/blank_forms/*.docx・*.xlsx・*.pdf）の書き込み・上書きコピー・移動・削除を阻止"),
        ("API_KEY_RE", "Anthropic API key pattern (before it is written)", "書き込む前に API キーらしき文字列を検出"),
        ("KEY_ASSIGN_RE", "literal API key assignment (before it is written)", "書き込む前に API キーの直書きを検出"),
        ("WIN_PROFILE_RE", "local user-profile paths (before they are written)", "書き込む前にローカルのユーザーフォルダのパスを検出"),
        ("EMAIL_RE", "e-mail addresses outside the reserved example domains (before they are written)",
         "書き込む前に例示用ドメイン以外のメールアドレスを検出"),
    ),
    "post-tool-use": (
        ("py_compile.compile", "Python syntax (py_compile) and an import smoke test of the edited module (.py)",
         "Python 構文（py_compile）と編集したモジュールの import 確認（.py）"),
        ("def _json_check", "JSON validity (.json)", "JSON の妥当性（.json）"),
        ("def hygiene_findings", "hygiene re-scan of the written file (keys, local paths, e-mail addresses)",
         "書き込んだファイルの再点検（API キー・ローカルパス・メールアドレス）"),
    ),
}
HOOK_STAGES = {
    "pre-tool-use": ("before the tool runs — exit code 2 + permissionDecision deny, so nothing reaches the disk",
                     "ツール実行前 — 終了コード 2 と permissionDecision: deny で、ディスクには何も書かれない"),
    "post-tool-use": ("after the write — decision block sends the finding back to Claude to fix at once",
                      "書き込み後 — decision: block で指摘を Claude に返し、その場で直させる"),
}


# How the pieces work together in a Claude Code session; every name refers to an asset listed by describe_suite().
FLOW: list[dict[str, Any]] = [
    {"step": 1, "ja": "「この様式には何を書く？」— 欄とルールを読む。マーカーなし様式・固定PDFは空欄候補を出し、記入する欄は人が確定。"
                      "公募ポータル（/portal）からの様式取得も一言で",
     "en": "\"What does this form need?\" — list slots and printed rules; for an unmarked form or a fixed PDF propose "
           "empty boxes, and the user confirms which ones to fill; a form can be fetched from the grant portal (/portal)",
     "uses": [("skill", "form-scan"), ("skill", "portal-form-fill"), ("mcp", "form_guard_list_demos"),
              ("mcp", "form_guard_scan"), ("mcp", "form_guard_discover_blank_cells")]},
    {"step": 2, "ja": "「メモから記入して」— 原文引用だけを抽出する別エージェントが独立に読み、外科的記入で未承認の下書きを作る",
     "en": "\"Fill it from my notes\" — a read-only subagent extracts verbatim evidence independently; the surgical "
           "fill writes an unapproved draft",
     "uses": [("agent", "memo-extractor"), ("agent", "field-mapper"), ("skill", "surgical-fill"),
              ("skill", "form-fill-verify"), ("mcp", "form_guard_fill")]},
    {"step": 3, "ja": "「確認して」— 下書きを作っていない監査役が 21 項目＋VIS-01 で検査し直し、人が確認すべき点を挙げる",
     "en": "\"Check it\" — an auditor that did not produce the draft re-runs the 21 checks (+ VIS-01) and lists what "
           "the human must confirm",
     "uses": [("agent", "structure-auditor"), ("agent", "form-auditor"), ("skill", "structural-audit"),
              ("skill", "visual-diff-audit"), ("mcp", "form_guard_verify")]},
    {"step": 4, "ja": "開発中は 2 段のフックが働く：書き込む前に白紙様式の改変と秘密情報・個人パス・メールアドレスを阻止し、"
                      "書き込んだ後に構文・import・JSON を検査",
     "en": "During development a two-stage hook guard runs: before a write it blocks template mutation and secret / "
           "personal-path / e-mail leaks; after a write it checks syntax, imports and JSON",
     "uses": [("hook", "PreToolUse"), ("hook", "PostToolUse")]},
    {"step": 5, "ja": "名前のある確認者が Web 画面で全項目を確認して承認。Claude Code から承認する手段はありません",
     "en": "A named reviewer signs off in the web app; nothing in Claude Code can approve a file",
     "uses": [("human", "web UI sign-off")]},
]


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    head = text.split("---", 2)[1]
    out: dict[str, str] = {}
    for line in head.strip().splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] == '"':  # a double-quoted YAML scalar
                v = v[1:-1].replace('\\"', '"')
            out[k.strip()] = v
    return out


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _skills(root: Path) -> list[dict[str, Any]]:
    out = []
    for p in sorted((root / ".claude" / "skills").glob("*/SKILL.md")):
        fm = _frontmatter(p)
        out.append({"name": fm.get("name", p.parent.name), "description": fm.get("description", ""), "path": _rel(p, root)})
    return out


def _agents(root: Path) -> list[dict[str, Any]]:
    out = []
    for p in sorted((root / ".claude" / "agents").glob("*.md")):
        fm = _frontmatter(p)
        out.append({"name": fm.get("name", p.stem), "description": fm.get("description", ""),
                    "tools": [t.strip() for t in fm.get("tools", "").split(",") if t.strip()],
                    "model": fm.get("model", ""), "path": _rel(p, root)})
    return out


def _settings(root: Path) -> dict[str, Any]:
    p = root / ".claude" / "settings.json"
    if not p.is_file():
        return {"path": _rel(p, root), "installed": False, "hooks": [], "deny": []}
    data = json.loads(p.read_text(encoding="utf-8"))
    script = root / HOOK_SCRIPT
    code = script.read_text(encoding="utf-8") if script.is_file() else ""
    hooks = []
    for event, entries in (data.get("hooks") or {}).items():
        for entry in entries or []:
            for hk in entry.get("hooks") or []:
                cmd = str(hk.get("command", ""))
                stage = next((st for st in HOOK_CHECKS if HOOK_SCRIPT in cmd and cmd.rstrip().endswith(st)), "")
                checks = [{"en": en, "ja": ja} for needle, en, ja in HOOK_CHECKS.get(stage, ()) if needle in code]
                when_en, when_ja = HOOK_STAGES.get(stage, ("", ""))
                hooks.append({
                    "event": event, "matcher": entry.get("matcher", ""), "type": hk.get("type", ""),
                    "timeout": hk.get("timeout"), "runner": cmd.split(" ", 1)[0], "command": cmd,
                    "script": HOOK_SCRIPT if stage and code else "", "stage": stage, "when": {"en": when_en, "ja": when_ja},
                    "checks": checks,
                    # PreToolUse refuses with exit code 2; PostToolUse returns decision "block" — both reach Claude
                    "feeds_back_to_claude": bool(checks) and ("return 2" in code if stage == "pre-tool-use"
                                                              else '"decision": "block"' in code),
                })
    return {"path": _rel(p, root), "installed": True, "hooks": hooks, "env": dict(data.get("env") or {}),
            "deny": list((data.get("permissions") or {}).get("deny") or []),
            "bypass_permissions": "bypassPermissions" in json.dumps(data)}


def _mcp(root: Path) -> dict[str, Any]:
    from .mcp_server import TOOLS

    p = root / ".mcp.json"
    servers = []
    if p.is_file():
        for name, s in (json.loads(p.read_text(encoding="utf-8")).get("mcpServers") or {}).items():
            servers.append({"name": name, "type": s.get("type", ""), "command": s.get("command", ""),
                            "args": list(s.get("args") or [])})
    return {"path": _rel(p, root), "installed": p.is_file(), "servers": servers,
            "tools": [{"name": t["name"], "description": t["description"],
                       "required": list(t["inputSchema"].get("required") or [])} for t in TOOLS]}


def describe_suite(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    root = Path(root)
    suite = {"skills": _skills(root), "agents": _agents(root), "settings": _settings(root), "mcp": _mcp(root)}
    names = {"skill": {s["name"] for s in suite["skills"]}, "agent": {a["name"] for a in suite["agents"]},
             # the tools exist in form_guard.mcp_server, but Claude Code only sees them once .mcp.json registers it
             "mcp": {t["name"] for t in suite["mcp"]["tools"]} if suite["mcp"]["servers"] else set(),
             "hook": {h["event"] for h in suite["settings"]["hooks"]}}
    flow = []
    for f in FLOW:
        uses = [{"kind": k, "name": n, "present": k == "human" or n in names.get(k, set())} for k, n in f["uses"]]
        flow.append({"step": f["step"], "ja": f["ja"], "en": f["en"], "uses": uses})
    return {
        **suite,
        "flow": flow,
        "routing": {"tiers": list(MODEL_TIERS),
                    "models": {"haiku": MODEL_HAIKU, "sonnet": MODEL_SONNET, "opus": MODEL_OPUS},
                    "policy": [{**p, "model": TIER_MODELS[p["tier"]]} for p in ROUTING_POLICY]},
    }
