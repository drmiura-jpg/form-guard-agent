"""架空学術振興ポータル — a fictional grant / study-group portal that hands out the blank demo forms.

``GET /portal`` renders it (server-side HTML, no script) and ``GET /api/portal/forms`` returns the same list as JSON,
so a person, Claude in Chrome or Claude Code can pick a form, download the blank file
(``GET /api/portal/forms/{id}/download``, SHA-256 in the ``X-Form-Guard-SHA256`` header) and open it in Form-Guard.
The printed rules on every card are read from the forms themselves (scan / blank-cell discovery), never typed in by
hand, and every organisation, call and date on the portal is fictional.
"""

from __future__ import annotations

import hashlib
import html
from pathlib import Path
from typing import Any

from .samples import DEMOS, PDF_FORMS, UNMARKED_FORMS, demo_paths, pdf_paths, unmarked_paths

E = html.escape
PORTAL_TITLE_JA = "架空学術振興ポータル — 公募・研究会指定様式ダウンロード窓口"
PORTAL_TITLE_EN = "Fictional Academic Grant & Study Group Form Portal"

# The calls the portal advertises (fictional). Order = order on the portal: the fixed PDF first.
CALLS: dict[str, dict[str, str]] = {
    "form6": {"call_ja": "次世代AI・分散システム研究会 研究助成（令和8年度 第2回公募）",
              "call_en": "Study-group research grant — 2nd call, FY2026", "category_ja": "研究会 公募（固定PDF様式）",
              "category_en": "Study-group call (fixed PDF form)", "deadline": "2026年11月30日（月）17:00"},
    "form4": {"call_ja": "架空学術機関 共用施設・備品の一時使用（随時受付）",
              "call_en": "Shared facilities & equipment — temporary use (rolling)", "category_ja": "施設利用（白紙Word様式）",
              "category_en": "Facility use (unmarked Word form)", "deadline": "随時（利用開始の2週間前まで）"},
    "form5": {"call_ja": "架空市 防災備蓄品の貸出（地域防災訓練向け）",
              "call_en": "Disaster stockpile loans for community drills", "category_ja": "自治体 貸出申請（白紙Excel様式）",
              "category_en": "Municipal loan request (unmarked Excel form)", "deadline": "随時（使用日の10日前まで）"},
    "form1": {"call_ja": "架空学術大学 共同研究 計画変更の届出", "call_en": "Joint research — plan modification notice",
              "category_ja": "大学 届出（Word様式・記入欄指定）", "category_en": "University notice (Word form, marked fields)",
              "deadline": "変更予定日の30日前まで"},
    "form2": {"call_ja": "架空市 地域中小企業省エネ設備導入補助金（令和8年度）",
              "call_en": "Municipal SME energy-saving equipment subsidy (FY2026)",
              "category_ja": "自治体 補助金（Word様式・記入欄指定）", "category_en": "Municipal subsidy (Word form, marked fields)",
              "deadline": "2026年12月18日（金）17:00"},
    "form3": {"call_ja": "架空市立サンプル技術交流センター 月次設備安全点検報告",
              "call_en": "Monthly facility safety inspection report", "category_ja": "施設管理 報告（Excel様式・記入欄指定）",
              "category_en": "Facility report (Excel form, marked fields)", "deadline": "毎月10日まで"},
}


def _items(base: Path) -> list[tuple[dict, dict, str]]:
    """(demo, paths, flow) in portal order; flow = pdf | unmarked | marked."""
    by_id: dict[str, tuple[dict, dict, str]] = {}
    for d in DEMOS:
        by_id[d["id"]] = (d, demo_paths(d, base), "marked")
    for u in UNMARKED_FORMS:
        by_id[u["id"]] = (u, unmarked_paths(u, base), "unmarked")
    for p in PDF_FORMS:
        by_id[p["id"]] = (p, pdf_paths(p, base), "pdf")
    return [by_id[i] for i in CALLS if i in by_id]


def rule_text(fmt: str, max_chars: Any, c: dict) -> str:
    """One printed rule in plain Japanese (what the form itself states)."""
    parts: list[str] = []
    if c.get("checkbox"):
        opts = "／".join(c.get("allowed_values") or [])
        parts.append(f"□→■ で選択：{opts}" + ("（一つだけ）" if c.get("exclusive", True) else "（複数可）"))
        if c.get("requires_human_if_selected"):
            parts.append(f"{'・'.join(c['requires_human_if_selected'])}は追記が必要なため人が選択")
    elif fmt == "wareki_date":
        parts.append("和暦の年月日（曜日つき）")
    elif fmt == "wareki_period":
        parts.append("和暦の期間（曜日つき）")
    elif fmt == "date":
        parts.append("年月日（曜日つき）")
    elif fmt == "yen":
        parts.append("円金額" + (f"・上限{int(c['max_value']):,}円" if c.get("max_value") is not None else "")
                     + ("・千円未満切捨て" if c.get("multiple_of") == 1000 else ""))
    elif fmt == "digits":
        parts.append(f"{c.get('digits')}桁の数字（推測禁止）")
    elif fmt == "pattern":
        parts.append(f"書式 {c.get('example') or c.get('pattern')}" + ("・半角" if c.get("halfwidth") else ""))
    elif fmt == "choice":
        parts.append("選択肢：" + "／".join(c.get("allowed_values") or []))
    elif fmt == "person_name":
        parts.append("氏名（敬称・注記なし）")
    if max_chars:
        parts.append(f"{max_chars}字以内")
    return "・".join(parts) or "自由記述（メモの記載どおり）"


def build_portal(base: Path, root: Path) -> list[dict[str, Any]]:
    """The portal entries, with the printed rules and office-only boxes read from each blank form."""
    from .scanner import discover_blank_cells, scan

    out = []
    for n, (d, p, flow) in enumerate(_items(base), start=1):
        blob = p["form"].read_bytes()
        disc = discover_blank_cells(p["form"])
        office = [c["slot_id"] for c in disc["candidates"] if c["office_only"]]
        if flow == "marked":
            sc = scan(p["form"])
            rules = [{"field": s.label, "rule": rule_text(s.required_format or "text", s.max_chars, s.constraints)}
                     for s in sc.slots]
            designation, next_tool = "記入欄が指定済み（{{マーカー}}・[記入欄: …]・.slots.json の座標）", "form_guard_scan"
        else:
            rules = [{"field": c["label"], "rule": rule_text(c["required_format"], c["max_chars"], c["constraints"])}
                     for c in disc["candidates"] if c["recommended"]]
            designation = ("固定PDF：罫線から記入枠を自動発見（人が確定）" if flow == "pdf"
                           else "マーカーなし白紙：空欄を自動発見（人が確定）")
            next_tool = "form_guard_discover_blank_cells"

        def rel(path: Path) -> str:
            try:
                return path.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                return path.name

        open_url = {"pdf": f"/#pdf={d['id']}", "unmarked": f"/#unmarked={d['id']}", "marked": f"/#demo={d['id']}"}[flow]
        out.append({
            "id": d["id"], "number": n, "title": d["title"], "title_en": d["title_en"], "org": d["org"],
            "file_type": d["file_type"], "flow": flow, **CALLS[d["id"]],
            "form": p["form"].name, "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest(),
            "download_url": f"/api/portal/forms/{d['id']}/download", "open_url": open_url,
            "designation": designation, "next_tool": next_tool, "mcp_form_path": rel(p["form"]),
            "memo_path": rel(p["memo"]), "rules": rules, "office_only": office,
            "highlights": list(d.get("highlights", [])), "fictional": True,
        })
    return out


_CSS = """
:root{--ink:#16202a;--muted:#5a6573;--line:#d9dee5;--bg:#f3f5f8;--card:#fff;--brand:#0f4c81;--brand2:#e8f1fa;--ok:#0a7a35}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.65 system-ui,'Yu Gothic UI','Hiragino Sans','Noto Sans JP',sans-serif}
header{background:var(--brand);color:#fff;padding:22px 36px 18px}header h1{margin:0;font-size:22px;letter-spacing:.02em}
header .en{opacity:.85;font-size:13px}header nav{margin-top:10px;font-size:13px}header nav a{color:#fff;margin-right:18px}
.fiction{background:#fff7e0;border-bottom:1px solid #f0d58c;padding:8px 36px;font-size:12.5px;color:#6b4b00}
main{max-width:1180px;margin:0 auto;padding:22px 24px 48px}
.howto{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--brand);border-radius:10px;
padding:14px 18px;margin-bottom:18px}.howto h2{font-size:15px;margin:0 0 6px}.howto ol{margin:6px 0 0 18px;padding:0}
.howto code,.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(440px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;display:flex;flex-direction:column}
.card.pdf{border:2px solid var(--brand)}.badges{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px}
.b{font-size:11px;font-weight:700;border-radius:999px;padding:1px 9px;background:var(--brand2);color:var(--brand)}
.b.pdf{background:#fde8e8;color:#9b1c1c}.b.no{background:#eef0f3;color:#3d4652}
.card h3{margin:2px 0 2px;font-size:16px}.card .org{color:var(--muted);font-size:12.5px}
.call{margin:8px 0 4px;font-weight:600}.dl{font-size:12.5px;color:var(--muted)}
.rules{margin:8px 0;padding:0;list-style:none;font-size:12.5px}
.rules li{border-top:1px dashed var(--line);padding:3px 0;display:grid;grid-template-columns:11em 1fr;gap:10px}
.locked{font-size:12px;color:#5b6472;margin:2px 0 8px}
.sha{font-size:11px;color:var(--muted);word-break:break-all;margin:4px 0 10px}
.actions{margin-top:auto;display:flex;flex-direction:column;gap:8px}
.btn{display:block;text-align:center;text-decoration:none;border-radius:8px;padding:9px 12px;font-weight:700}
.btn.dl{background:#fff;border:1px solid var(--brand);color:var(--brand)}.btn.go{background:var(--brand);color:#fff}
footer{color:var(--muted);font-size:12px;text-align:center;padding:24px}
"""


def portal_html(forms: list[dict[str, Any]]) -> str:
    cards = []
    for f in forms:
        ext = f".{f['file_type']}"
        badges = (f"<span class='b{' pdf' if f['file_type'] == 'pdf' else ''}'>{E(ext)}</span>"
                  f"<span class='b'>{E(f['category_ja'])}</span>"
                  + ("<span class='b no'>空欄自動発見</span>" if f["flow"] != "marked" else ""))
        rules = "".join(f"<li><b>{E(r['field'])}</b>{E(r['rule'])}</li>" for r in f["rules"])
        locked = (f"<div class='locked'>🔒 事務局専用欄（記入不可）：{E('、'.join(f['office_only']))}</div>"
                  if f["office_only"] else "")
        cards.append(
            f"<article class='card{' pdf' if f['file_type'] == 'pdf' else ''}' id='{E(f['id'])}' data-form-id='{E(f['id'])}' "
            f"data-file-type='{E(f['file_type'])}' data-sha256='{E(f['sha256'])}' data-download='{E(f['download_url'])}' "
            f"data-open='{E(f['open_url'])}' data-mcp-form-path='{E(f['mcp_form_path'])}'>"
            f"<div class='badges'>{badges}</div><h3>{E(f['title'])}</h3>"
            f"<div class='org'>{E(f['org'])} · {E(f['title_en'])}</div>"
            f"<div class='call'>{E(f['call_ja'])}</div><div class='dl'>締切：{E(f['deadline'])} · {E(f['call_en'])}</div>"
            f"<ul class='rules' aria-label='様式に印刷された記入ルール'>{rules}</ul>{locked}"
            f"<div class='sha mono' title='SHA-256 of the blank form'>{E(f['form'])} · {f['size']:,} bytes · SHA-256 "
            f"{E(f['sha256'])}</div>"
            f"<div class='actions'><a class='btn dl' href='{E(f['download_url'])}' download>⬇ 白紙様式をダウンロード "
            f"({E(ext)})</a><a class='btn go' href='{E(f['open_url'])}'>🚀 Form-Guard で自動空欄発見・外科的差込を開く</a>"
            f"</div></article>")
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{E(PORTAL_TITLE_JA)}</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect x='3' y='5' width='26' height='22' rx='3' fill='%230f4c81'/%3E%3Cpath d='M9 12h14M9 17h14M9 22h8' stroke='%23fff' stroke-width='2'/%3E%3C/svg%3E">
<style>{_CSS}</style></head><body>
<header><h1>{E(PORTAL_TITLE_JA)}</h1><div class="en">{E(PORTAL_TITLE_EN)} — calls for proposals &amp; designated forms</div>
<nav><a href="/">🛡 Form-Guard（指定欄だけ記入・様式の変更を検査）へ戻る</a><a href="/api/portal/forms">JSON 一覧（/api/portal/forms）</a></nav></header>
<div class="fiction">※このポータル・公募・組織・様式・締切はすべて Form-Guard のデモ用にハッカソン期間中に作成した架空のものです（Fictional portal for the demo — no real organisation, call or person）。</div>
<main>
<section class="howto" aria-label="使い方"><h2>Claude Code ＋ Claude in Chrome で使う / With Claude Code and Claude in Chrome</h2>
<div>Claude Code に「公募ポータルから研究会のPDF様式をダウンロードしてメモの内容で埋めて確認画面を開いて」と一言で頼めます（スキル <code>portal-form-fill</code>）。</div>
<ol><li>このページ（または <code>/api/portal/forms</code>）から様式と SHA-256 を確認し、白紙様式をダウンロード</li>
<li><code>form_guard_discover_blank_cells</code> で記入枠を自動発見（事務局専用欄はロック）→ 人が記入する枠を確定</li>
<li><code>form_guard_fill</code> でメモの原文根拠つきの未承認ドラフトを作成（原本のバイト列は変更しない）</li>
<li><code>form_guard_verify</code> で 21 項目＋VIS-01 を再検査</li>
<li>返ってきた <code>review_url</code> を Chrome で開き、名前のある確認者が全項目を確認して承認（AI は承認できません）</li></ol></section>
<div class="grid">{''.join(cards)}</div></main>
<footer>架空学術振興ポータル（Form-Guard demo）· 記入内容の事実確認と提出の最終判断は人が行います / A person checks the facts and decides to submit.</footer>
</body></html>"""
