"""Standalone HTML reports (no external assets) and the naive-regeneration comparison pages."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .verifier import flag_label

E = html.escape

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#14171c;--muted:#5b6472;--line:#e3e6ea;--ok:#047857;--okbg:#ecfdf5;
--bad:#b91c1c;--badbg:#fef2f2;--warn:#b45309;--warnbg:#fffbeb;--info:#1d4ed8;--infobg:#eff6ff;--mark:#fde68a}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#171b21;--ink:#e8ebef;--muted:#9aa3af;--line:#2a3038;
--okbg:#06281e;--badbg:#2a0f12;--warnbg:#2a1d07;--infobg:#0c1b33;--mark:#7c5d0a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 system-ui,'Yu Gothic UI','Hiragino Sans','Noto Sans JP',sans-serif}
header{padding:28px 32px 18px;border-bottom:1px solid var(--line);background:var(--card)}
h1{margin:0 0 4px;font-size:22px}h2{font-size:17px;margin:28px 0 10px}h3{font-size:14px;margin:16px 0 6px}
main{padding:8px 32px 48px;max-width:1500px;margin:0 auto}.muted{color:var(--muted)}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:700;font-size:12px;letter-spacing:.02em}
.PASS,.ok{background:var(--okbg);color:var(--ok)}.FAIL,.bad{background:var(--badbg);color:var(--bad)}
.NEEDS_HUMAN_INPUT,.warn{background:var(--warnbg);color:var(--warn)}.info{background:var(--infobg);color:var(--info)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:18px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.tile b{display:block;font-size:24px;line-height:1.2}.tile span{color:var(--muted);font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:12px 0}
.grid3{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}
.grid3 figure{margin:0;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px}
.grid3 figcaption{font-weight:700;margin:2px 4px 8px}.grid3 svg{width:100%;height:auto;display:block}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{border-bottom:1px solid var(--line);padding:7px 8px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px}td.v{font-weight:600}code,.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
.chip{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:0 8px;margin:1px 2px;font-size:11px;color:var(--muted)}
.memo{white-space:pre-wrap;font-family:ui-monospace,Consolas,'BIZ UDGothic',monospace;font-size:12.5px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;line-height:1.75}
.memo .ln{color:var(--muted);user-select:none;display:inline-block;width:3.2em}mark{background:var(--mark);color:inherit;border-radius:3px;padding:0 1px}
details summary{cursor:pointer;color:var(--info)}.pass{color:var(--ok);font-weight:700}.fail{color:var(--bad);font-weight:700}
footer{padding:20px 32px;color:var(--muted);font-size:12px;border-top:1px solid var(--line)}
@media print{header,footer{background:#fff}.grid3{grid-template-columns:1fr 1fr 1fr}}
"""


def _status_label(s: str) -> str:
    return {"PASS": "PASS — ready for human sign-off", "NEEDS_HUMAN_INPUT": "NEEDS HUMAN INPUT",
            "FAIL": "FAIL — do not use"}.get(s, s)


def _memo_html(memo: str, mappings: list[dict]) -> str:
    marks: list[tuple[int, int, str]] = []
    for m in mappings:
        for sp in m.get("source_spans", []):
            marks.append((sp["start_char"], sp["end_char"], m["slot_id"]))
    bounds = sorted({0, len(memo)} | {a for a, _, _ in marks} | {b for _, b, _ in marks})
    out, line_no = [], 1
    out.append(f'<span class="ln">L{line_no:03d}</span>')
    for a, b in zip(bounds, bounds[1:]):
        seg = memo[a:b]
        ids = sorted({sid for s, e, sid in marks if s <= a and b <= e})
        pieces = seg.split("\n")
        for i, piece in enumerate(pieces):
            if i > 0:
                line_no += 1
                out.append(f'\n<span class="ln">L{line_no:03d}</span>')
            if piece:
                esc = E(piece)
                out.append(f'<mark title="{E(", ".join(ids))}">{esc}</mark>' if ids else esc)
    return "".join(out)


def _checks_table(checks: list[dict]) -> str:
    rows = []
    for c in checks:
        mark = '<span class="pass">✓</span>' if c["passed"] else '<span class="fail">✗</span>'
        rows.append(f"<tr><td>{mark}</td><td class='mono'>{E(c['check_id'])}</td><td>{E(c['description'])}</td>"
                    f"<td>{E(c['details'])}</td></tr>")
    return "<table><tr><th></th><th>ID</th><th>Check</th><th>Evidence</th></tr>" + "".join(rows) + "</table>"


def build_report_html(d: dict[str, Any]) -> str:
    rep = d["report"]
    naive = d.get("naive_report")
    slots = {s["slot_id"]: s for s in d["slots"]}
    maps = d["mappings"]
    filled = sum(1 for m in maps if m["status"] in ("filled", "human_entered"))
    pending = [m for m in maps if m["status"] == "needs_human_input"]
    untouched = [e for e in rep["entry_hashes"] if e["role"] == "untouched"]
    # count only parts that really are byte-identical (content and raw compressed stream)
    untouched_identical = [e for e in untouched if e.get("identical") and e.get("raw_identical")]
    n_changed = len(untouched) - len(untouched_identical)
    identical_tile = (str(len(untouched_identical)) if not n_changed
                      else f"{len(untouched_identical)}/{len(untouched)} (changed: {n_changed})")
    stats = rep.get("stats", {})
    prev = d.get("previews", {})
    figs = []
    for key, cap in (("blank", "① Blank original / 原本"), ("naive", "② Naive AI regeneration (simulated) / 生成AIで作り直し"),
                     ("filled", "③ Form-Guard Agent / 指定欄だけ記入")):
        if key in prev:
            figs.append(f"<figure><figcaption>{E(cap)}</figcaption>{prev[key]}</figure>")
    rows = []
    for m in maps:
        s = slots.get(m["slot_id"], {})
        val = m["filled_value"] or (f"（未記入）suggestion: {m['suggested_value']}" if m["suggested_value"] else "（未記入 / awaiting input）")
        lim = f"{m['char_count']}/{s.get('max_chars')}" if s.get("max_chars") else str(m["char_count"] or "")
        flags = "".join(f'<span class="chip">{E(flag_label(f))}</span>' for f in m["normalizations"])
        lr = m.get("source_line_range")
        quote = (f"<span class='muted'>L{lr[0]}{'–' + str(lr[1]) if lr and lr[1] != lr[0] else ''}</span> 「{E(m['source_quote'])}」"
                 if m["source_quote"] else "—")
        hist = "".join(f"<li>#{a['attempt']} {'✓' if a['passed'] else '✗'} {E(a['action'])} → «{E(a['value'][:80])}»"
                       + (f" <span class='muted'>[{E(', '.join(i['code'] for i in a['issues']))}]</span>" if a["issues"] else "")
                       + "</li>" for a in m["attempts"])
        status_cls = {"filled": "ok", "human_entered": "info", "needs_human_input": "warn"}.get(m["status"], "warn")
        warn = "".join(f"<div class='muted'>⚠ {E(w)}</div>" for w in m["warnings"][:3])
        model = (f"<div class='mono muted' title='{E(m.get('route_reason', ''))}'>{E(m['routed_model'])}</div>"
                 if m.get("routed_model") else "")
        rows.append(f"<tr><td><b>{E(s.get('label', m['slot_id']))}</b><div class='muted'>{E(s.get('context_label', ''))}</div></td>"
                    f"<td class='v'>{E(val)}{warn}</td><td>{E(lim)}</td><td><span class='badge {status_cls}'>{E(m['status'])}</span>{flags}{model}</td>"
                    f"<td>{quote}<details><summary>self-correction ({len(m['attempts'])})</summary><ol>{hist}</ol></details></td></tr>")
    entries = "".join(f"<tr><td class='mono'>{E(e['name'])}</td><td>{E(e['role'])}</td><td class='mono'>{E(e['original_sha256'][:16])}…</td>"
                      f"<td>{'✓ identical' if e['identical'] and e['raw_identical'] else ('modified (target)' if e['role'] == 'target' else '✗ changed')}</td></tr>"
                      for e in rep["entry_hashes"])
    hitl = "".join(f"<li><b>{'□'}</b> {E(h['prompt'])}" + (f" <span class='muted'>— 「{E(h['source_quote'][:60])}」</span>" if h.get("source_quote") else "")
                   + "".join(f'<span class="chip">{E(flag_label(f))}</span>' for f in h.get("flags", [])) + "</li>"
                   for h in rep["hitl_checklist"])
    patches = "".join(f"<tr><td class='mono'>{E(p['part_name'])}</td><td>{E(p['slot_id'])}</td><td class='mono'>{p['byte_start']}–{p['byte_end']}</td>"
                      f"<td>{E(p['kind'])}</td><td class='mono'>«{E(p['old_text'])}» → «{E(p['new_text'][:60])}»</td></tr>" for p in d["patches"])
    naive_html = ""
    if naive:
        naive_html = f"<h2>Naive regeneration audit / 作り直し版の検査 <span class='badge FAIL'>{naive['failed_count']} failures</span></h2>" \
                     f"<div class='card'>{_checks_table(naive['checks'])}</div>"
    eng = d["engine"]
    fb = f" — <b>fallback:</b> {E(eng['fallback_reason'])}" if eng.get("fallback_reason") else ""
    routing = (eng.get("usage") or {}).get("routing") or []
    routing_html = ""
    if routing:  # which model handled which field, and why (offline: the plan live auto mode would follow)
        planned = all(r.get("planned_in_live_auto") for r in routing)
        trs = "".join(f"<tr><td>{E(r['task'])}</td><td class='mono'>{E(r['model'])}</td><td>{E('、'.join(r['slots']))}</td>"
                      f"<td class='muted'>{E(' / '.join(dict.fromkeys(r.get('reasons', {}).values())))}</td></tr>"
                      for r in routing)
        head = ("<h2>Planned model routing (live auto mode) / ライブ（auto）時のモデル割り当て計画</h2><p class='muted'>"
                "This run used the offline engine — no model was called. The table shows where live <code>auto</code> "
                "mode would send each field. ｜ この実行はオフラインエンジンで、モデルは呼び出していません。</p>"
                if planned else "<h2>Model routing / モデルの使い分け</h2>")
        routing_html = (f"{head}<div class='card'><table><tr><th>Task</th><th>Model</th>"
                        f"<th>Fields</th><th>Why</th></tr>{trs}</table></div>")
    # Never claim an uploaded form, an edited memo or reviewer-typed values are fictional.
    footer = ("This report is generated from the supplied form and memo. Reviewing the facts and deciding to submit "
              "is the human reviewer's responsibility. ｜ このレポートは入力された様式・メモに基づきます。"
              "記入内容の最終確認と提出判断は人が行います。")
    if d.get("is_builtin_demo"):
        footer += "（Built-in demo: all sample forms, memos and data are 100% fictional / 付属デモの様式・メモ・データはすべて架空です）"
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Form-Guard report — {E(d['form']['title'])}</title><style>{CSS}</style></head><body>
<header><div class="muted">Form-Guard Agent · verification report · {E(d['created_at'])}</div>
<h1>{E(d['form']['title'])}</h1><div class="muted">{E(d['form'].get('title_en', ''))} · {E(d['form']['name'])} → {E(d['filled']['name'])}</div>
<p><span class="badge {E(rep['overall_status'])}">{E(_status_label(rep['overall_status']))}</span>
<span class="muted"> Engine: {E(eng.get('label', ''))}{fb}</span></p></header><main>
<div class="tiles">
<div class="tile"><b>{filled}/{len(maps)}</b><span>fields filled / 記入済み</span></div>
<div class="tile"><b>{len(pending)}</b><span>awaiting human input / 人の入力待ち</span></div>
<div class="tile"><b>{rep['passed_count']}/{rep['passed_count'] + rep['failed_count']}</b><span>checks passed / 検査合格</span></div>
<div class="tile"><b>{E(identical_tile)}</b><span>ZIP entries byte-identical / 無変更パーツ</span></div>
<div class="tile"><b>{stats.get('boilerplate_blocks', '–')}</b><span>boilerplate blocks unchanged / 定型文</span></div>
<div class="tile"><b>{rep['ungrounded_claims_count']}</b><span>ungrounded claims / 根拠なし</span></div>
</div>
<h2>Visual comparison / 見た目の比較</h2><div class="grid3">{''.join(figs)}</div>
<h2>Field mapping &amp; verbatim sources / 記入値と根拠</h2><div class="card"><table>
<tr><th>Field</th><th>Value</th><th>Chars</th><th>Status</th><th>Source quote · self-correction</th></tr>{''.join(rows)}</table></div>
{routing_html}
<h2>Source memo / 入力メモ（黄色＝引用箇所）</h2><div class="memo">{_memo_html(d['memo']['text'], maps)}</div>
<h2>Structural audit / 構造検査</h2><div class="card">{_checks_table(rep['checks'])}</div>
<h3>ZIP entries (SHA-256)</h3><div class="card"><table><tr><th>Entry</th><th>Role</th><th>SHA-256</th><th>Result</th></tr>{entries}</table></div>
{naive_html}
<h2>Human-in-the-Loop checklist / 人による最終確認</h2><div class="card"><p class="muted">Sign-off and download happen in the
web app (<code>python -m form_guard.cli --serve</code>); the download stays locked until every item is confirmed.</p><ul>{hitl}</ul></div>
<h2>Surgical patch manifest / 変更箇所一覧</h2><div class="card"><table><tr><th>Part</th><th>Slot</th><th>Bytes</th><th>Kind</th><th>Change</th></tr>{patches}</table></div>
</main><footer>{footer}</footer></body></html>"""


def build_index_html(summaries: list[dict[str, Any]]) -> str:
    cards = []
    for s in summaries:
        cards.append(f"<div class='card'><h3>{E(s['title'])}</h3><div class='muted'>{E(s.get('title_en', ''))}</div>"
                     f"<p><span class='badge {E(s['status'])}'>{E(_status_label(s['status']))}</span> "
                     f"{s['filled']} filled · {s['pending']} awaiting input · {s['checks_passed']}/{s['checks_total']} checks passed"
                     f"{' · naive version: ' + str(s['naive_failures']) + ' failures' if s.get('naive_failures') is not None else ''}</p>"
                     f"<p><a href='{E(s['dir'])}/report.html'>report.html</a> · <a href='{E(s['dir'])}/{E(s['filled_name'])}'>{E(s['filled_name'])}</a>"
                     f" · <a href='{E(s['dir'])}/audit_log.json'>audit_log.json</a></p></div>")
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>Form-Guard Agent — demo outputs</title>
<style>{CSS}</style></head><body><header><h1>Form-Guard Agent — demo outputs</h1>
<div class="muted">Scan → Map &amp; Self-Correct → Surgical XML Write → Structural Verify → Visual Diff &amp; HITL</div></header>
<main>{''.join(cards)}</main><footer>100% fictional sample forms and memos. The final check and decision are made by a human.</footer></body></html>"""


def write_naive_comparisons(samples_dir: Path) -> dict[str, Path]:
    """Render blank vs naive-regeneration comparisons into samples/broken_before/."""
    from .pipeline import naive_overlays
    from .samples import DEMOS, read_memo
    from .scanner import scan
    from .verifier import compare_naive
    from .visual_diff import layout_docx, layout_xlsx, svg_document

    written: dict[str, Path] = {}
    for demo in DEMOS:
        form = samples_dir / "blank_forms" / demo["form"]
        naive = samples_dir / "broken_before" / demo["naive"]
        memo = read_memo(samples_dir / "memos" / demo["memo"])
        sc = scan(form)
        rep = compare_naive(sc, naive, memo)
        lay = (lambda p: layout_docx(p)[0]) if sc.file_type == "docx" else layout_xlsx
        blank_l, naive_l = lay(form), lay(naive)
        ovs, banner = naive_overlays(sc, blank_l, naive_l, rep)
        naive_svg = svg_document(naive_l, ovs, "Naive AI regeneration (simulated)", banner, "bad")
        blank_svg = svg_document(blank_l, [], "Blank original form", "Blank original / 原本", "info")
        stem = Path(demo["naive"]).stem
        sp = samples_dir / "broken_before" / f"{stem}.svg"
        sp.write_text(naive_svg, encoding="utf-8", newline="\n")
        hp = samples_dir / "broken_before" / f"comparison_{demo['id']}.html"
        fails = "".join(f"<li><b>{E(c['check_id'])}</b> {E(c['description'])} — {E(c['details'])}</li>"
                        for c in json.loads(rep.model_dump_json())["checks"] if not c["passed"])
        hp.write_text(f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>Naive regeneration vs original — {E(demo['title'])}</title>
<style>{CSS}</style></head><body><header><h1>Before: naive AI regeneration (simulated)</h1>
<div class="muted">{E(demo['title'])} — what happens when a form is re-generated from scratch instead of filled in place.
The naive file is scripted to reproduce common failure modes; it is not output of any specific product.</div></header>
<main><div class="card"><h3>Verifier findings ({rep.failed_count} failed checks)</h3><ul>{fails}</ul></div>
<div class="grid3"><figure><figcaption>Original blank form / 原本</figcaption>{blank_svg}</figure>
<figure><figcaption>Naive regeneration / 作り直し版</figcaption>{naive_svg}</figure></div></main>
<footer>100% fictional sample. Run <code>python -m form_guard.cli --all-demos</code> for the Form-Guard result.</footer></body></html>""",
                      encoding="utf-8", newline="\n")
        written[sp.name] = sp
        written[hp.name] = hp
    return written
