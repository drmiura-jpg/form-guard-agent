# Form-Guard Agent

**Fill only the designated fields of a Word / Excel form, prove that nothing else changed, and keep a human in the loop.**
**指定欄だけを記入し、様式の変更を検査する書類記入エージェント**

Built for **Built with Claude: Japan 2026** (development started 2026-11-02). [日本語版はこちら ↓](#日本語)

![Visual diff: naive regeneration vs Form-Guard](docs/screenshots/01_visual_diff.png)

---

## The problem

Ask a generative AI to "fill in this form" and it usually **re-creates the document**. The result looks plausible but
the merged cells are split, the borders are gone, the printed legal text is paraphrased, the page size has changed,
and sometimes a number that appears nowhere in your notes ends up in the form. On an official form, any of those can
get it rejected or make it wrong.

## What Form-Guard does

```
 blank form (.docx/.xlsx) + free-text memo
        │
 1 Scan ............ find {{markers}} / [記入欄: …] / coordinate-designated empty cells,
        │            read the rules printed on the form (200字以内, 和暦, 13桁, 上限1,000,000円, drop-down lists)
 2 Map & self-correct  Claude (live) or a deterministic offline engine proposes a value + VERBATIM memo quotes;
        │            an independent validator checks quotes, numbers, dates and printed rules; failures are
        │            repaired (和暦 conversion, 483万6千円 → 4,836,000円, half-width IDs, deletion-only
        │            condensation) or sent back to Claude with the validator's findings
 3 Surgical write .... replace ONLY the marker characters inside the XML bytes; every other ZIP entry is copied
        │            as raw compressed bytes (not even re-compressed)
 4 Verify ............ 21 deterministic checks: ZIP/SHA-256, XML skeleton, tables/borders/merges, boilerplate text,
        │            grounding, constraints, application load, rendered-layout drift
 5 Human sign-off .... checklist with the source quote for every value; the download unlocks only after a named
                     reviewer confirms every item (enforced by the server, bound to the file's SHA-256)
```

Anything Form-Guard cannot ground in the memo is **never guessed**: it is left for the reviewer
(see 法人番号 in demo 2).

## Quick start (no API key needed)

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt

python -m form_guard.cli --all-demos     # runs the 3 fictional demos -> output/index.html
python -m form_guard.cli --serve         # web UI on http://127.0.0.1:8000
pytest -v                                # 43 tests
```

Requires Python 3.10+. Without `ANTHROPIC_API_KEY` everything runs on the deterministic **offline demo engine**, so the
whole pipeline is reproducible with zero setup.

## Live Claude mode

Set the key in your shell or in a git-ignored `.env` file in the project root:

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | enables live mode (`--mode auto` picks it up) |
| `FORM_GUARD_MODEL` | `claude-opus-5-5` | Claude model used for field mapping |
| `FORM_GUARD_EFFORT` | `medium` | `output_config.effort` (`low` … `max`) |
| `FORM_GUARD_FALLBACKS` | `1` | server-side refusal fallback opt-in; set `0` to disable |

```bash
python -m form_guard.cli --all-demos --mode live
python -m form_guard.cli --form my_form.docx --memo notes.txt --out-dir out --mode live
```

How Claude is used at runtime (`form_guard/mapper.py`):

* **Structured outputs** (`output_config.format` = JSON schema): one entry per field with `value`,
  `source_quotes` (verbatim, single-line), line numbers, confidence and a short reason — or `not_found`.
* **Adaptive thinking** with an explicit **effort** level, streamed with `get_final_message()`.
* **Self-correction loop**: the validator's findings ("quote not found verbatim", "212 chars exceeds the 200-char
  limit", "1,612,000円 exceeds the printed cap") are sent back in the same conversation; only the failing fields
  are revised. Values that still fail are *not* written; they go to the reviewer.
* **Refusals** are handled (`stop_reason == "refusal"`), with the server-side fallback opt-in
  (`fallbacks="default"`, beta `server-side-fallback-2026-07-01`). In `auto` mode, a failed live call falls back
  to the offline engine and the report says so.
* The memo is treated as data, never as instructions (prompt-injection guard in the system prompt).

The whole project — code, tests, sample generators and docs — was written with **Claude Code** during the hackathon.

## The demos (100% fictional, non-medical)

| Demo | File | Shows |
|---|---|---|
| 共同研究計画変更届出書（様式第3号） — 架空学術大学 | `.docx` | split-run markers, Western date → 和暦, full-width ID → `JR-2026-047`, 257 → 181 chars by deletion only, merged label cells (vMerge) |
| 地域中小企業省エネ設備導入補助金 交付申請書（様式第1号） — 架空市 | `.docx` | `483万6千円` → `4,836,000円`, printed cap `上限1,000,000円` forces re-extraction (1,612,000円 → 1,000,000円), **missing 法人番号 left for the human**, empty cell filled by table coordinates |
| 月次設備安全点検および改善報告書（様式A-4） — 架空施設 | `.xlsx` | drop-down list values, `COUNTIF` formulas and 40 merged ranges preserved, 173 → 115 chars, year of 「12月8日（火）」 inferred and flagged |

Each demo also ships a scripted **naive regeneration** (`samples/broken_before/`) that reproduces the usual failure
modes — US-Letter page, borders and merged cells lost, legal clauses paraphrased, invented numbers such as
「約50万円」 or a fabricated 13-digit 法人番号 — and Form-Guard's verifier flags every one of them.

All sample forms, memos, names and organisations were written from scratch during the hackathon and are fictional.
E-mail addresses use the reserved `example.jp` domain.

## What the verifier proves

| ID | Check |
|---|---|
| ZIP-01…05 | archive integrity (CRC), same entries/order, identical ZipInfo metadata, every non-target entry byte-identical (raw compressed stream + SHA-256), only slot-bearing parts modified |
| XML-01…03 | well-formed (DTD/entities rejected), namespace declarations identical, **markup skeleton byte-identical** — every tag and attribute, with text-node contents blanked |
| LAYOUT-01…04 | tables/rows/cells, gridSpan/vMerge, tblGrid · borders & shading · pPr/rPr · page setup (for .xlsx: merged ranges, cell styles, widths/heights, formulas, data validation, print area) |
| TEXT-01…03 | all boilerplate text unchanged · each slot holds exactly its approved value · no marker left except fields awaiting human input |
| GRD-01…02 | every AI value backed by verbatim memo quotes · every number/date in a value appears in the quoted evidence |
| CON-01…03 | printed character limits · formats (和暦, yen, ID patterns) · options, caps and rounding |
| APP-01 | the file opens in an independent OOXML library |
| VIS-01 | rendered layout outside the designated fields has zero drift (added only if drift is found) |

`python -m form_guard.cli --verify ORIGINAL FILLED` audits *any* filled copy — including one filled by hand or by another tool.

## Designating fields in your own forms

* Type `{{項目名}}` or `[記入欄: 項目名]` where a value belongs (markers split across Word runs are handled).
* For official forms you must not edit, designate **empty table cells by coordinates** in `<form>.slots.json`:
  `{"targets": [{"slot_id": "担当者連絡先", "table": 1, "row": 8, "col": 1}]}`.
* Rules printed on the form are discovered automatically (`（200字以内）`, `（13桁）`, `例：JR-2026-001`, `上限1,000,000円`,
  `千円未満切捨て`, `A／B／Cのうち`, Excel drop-down lists). `.slots.json` can add aliases and descriptions.

## Human-in-the-loop by design

The web UI shows every value next to its verbatim source (click a value in the preview to jump to the memo line),
the self-correction history, and the full structural audit. The **download stays locked on the server** until
there are no pending fields, verification passed, and a named reviewer has confirmed every checklist item. The
approval record (reviewer, time, checked items, SHA-256 of the exact file) is written next to the audit log.

## Scope and safety

* Form-Guard checks that each value is **supported by the memo** and that **the form itself was not altered**.
  It does not establish whether the memo is true; that, and the decision to submit, stays with the human reviewer.
* Intended for general administrative, research-administration and municipal paperwork. No medical use.
* Do not upload real personal data. The server binds to `127.0.0.1`, rejects DTDs and entity declarations,
  limits upload and decompressed sizes (zip-bomb guard), and serves the UI with a strict Content-Security-Policy.

## Project layout

```
form_guard/
  scanner.py      slot discovery (+ printed-rule inference in constraints.py)
  mapper.py       Claude / offline engines + self-correction loop (grounding.py, textnorm.py)
  writer.py       surgical byte-range injection (ooxml.py indexer, ziputil.py raw ZIP copy)
  verifier.py     ZIP / XML / layout / text / grounding / constraint checks + HITL checklist
  visual_diff.py  deterministic SVG (and optional PNG) previews, layout-drift measurement
  pipeline.py     orchestration; report.py standalone HTML; cli.py; server.py (FastAPI)
  static/         vanilla HTML/CSS/JS web UI
samples/          blank forms, slot specs, memos, naive regenerations (generated by samples.py)
tests/            pytest suite (fake + mock-transport Claude, tampering attacks, API gate)
```

## Limitations

* Previews come from a built-in layout engine. They are close to, but not identical with, Word/Excel rendering.
* The offline engine is rule-based and works best with `label：value` or `■ heading` style memos. Use live mode
  for free-form text.
* In `.docx` slots, line breaks in a value are folded into one paragraph, so the run structure stays unchanged.
* ZIP64 archives and UTF-16 XML parts are not supported (the tool refuses them rather than risk damage).

License: to be decided by the author before publication.

---

<a id="日本語"></a>
## 日本語

### 何をするツールか

生成AIに「この様式に記入して」と頼むと、多くの場合は文書を**作り直します**。見た目はそれらしくても、
結合セルが分かれ、罫線が消え、注意事項や誓約文の文言が言い換えられ、用紙サイズまで変わります。
メモにない数字が紛れ込むこともあります。Form-Guard Agent は次の流れで、**指定欄だけ**を書き換えます。

1. **スキャン** — `{{項目名}}`・`[記入欄: 項目名]`・座標指定の空欄を見つけ、様式に印刷された記入ルール（字数・和暦・桁数・上限額・選択肢）も読み取ります。
2. **対応付けと自己修正** — Claude（ライブ）またはオフラインの決定論エンジンが、値と**メモ原文そのままの引用**を返します。独立した検証器が引用・数値・日付・記入ルールを確認し、和暦変換・金額表記の統一・半角化・削除のみの要約で直すか、指摘内容を添えて Claude に再依頼します。
3. **外科的な書き込み** — XML 内のマーカー文字だけを置き換えます。それ以外の ZIP 内パーツは圧縮済みバイト列のまま複製します。
4. **構造検査** — ZIP・SHA-256、XML骨格、表構造・罫線・結合セル、定型文、根拠、記入ルール、描画位置のずれを含む 21 項目を決定論的に検査します。
5. **人による最終確認** — 各値をその根拠と並べて確認します。確認者名の入力と全項目のチェックが済むまで、ダウンロードはサーバー側でロックされます。承認はファイルの SHA-256 に結び付けて記録します。

メモに根拠がない項目は**推測で埋めません**（デモ2の法人番号）。人が入力します。

### すぐ試す（APIキー不要）

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m form_guard.cli --all-demos     # 架空デモ3件を実行 → output/index.html
python -m form_guard.cli --serve         # Web画面 http://127.0.0.1:8000
pytest -v
```

`ANTHROPIC_API_KEY` を設定すると（`.env` も可、Git には含まれません）、`--mode auto` / `--mode live` で Claude（既定 `claude-opus-5-5`）を使います。
構造化出力（JSON スキーマ）、adaptive thinking と effort 指定、検証器の指摘による再依頼、拒否時のフォールバック設定に対応しています。

### デモ（すべて架空・医療以外）

- **共同研究計画変更届出書（様式第3号）**：分割された run 上のマーカー、西暦→和暦、全角ID→半角、200字への削除のみ要約
- **省エネ設備導入補助金 交付申請書（様式第1号）**：483万6千円→4,836,000円、印刷された上限額による再抽出、法人番号は人が入力、座標指定の空欄への記入
- **月次設備安全点検および改善報告書（様式A-4, .xlsx）**：プルダウン・COUNTIF式・結合セルを保持、120字への要約、年を推定した日付には確認フラグ

各デモには「生成AIで作り直した場合」を再現した比較用ファイル（`samples/broken_before/`）も付いています。
すべてのサンプル様式とテストデータは、本ハッカソン期間中にゼロから自作した架空のものです。

### 位置付けと注意

- 本ツールが確認するのは「各値がメモに裏付けられていること」と「様式そのものが改変されていないこと」です。メモの内容が事実かどうかの確認と、提出の最終判断は人が行います。
- 一般事務・研究事務・自治体手続き向けです。医療用途には使いません。
- 実在の個人情報はアップロードしないでください。
