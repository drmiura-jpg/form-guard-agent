# Form-Guard Agent

**Fill only the designated fields of a Word / Excel form, prove that nothing else changed, and keep a human in the loop.**
**指定欄だけを記入し、様式の変更を検査する書類記入エージェント**

Built for **Built with Claude: Japan 2026** (development started Monday, 2026-11-02 / 2026-11-02（月）). [日本語版はこちら ↓](#日本語)

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
 1 Scan ............ find {{markers}} / [記入欄: …] / coordinate-designated empty cells (Word table cells and
        │            Excel cells), read the rules printed on the form (200字以内, 和暦, 13桁, 上限1,000,000円,
        │            drop-down lists); an UNMARKED blank form gets a 4-stage blank-cell discovery instead
        │            (applicant vs office-only cells) and a human confirms the cells to fill
 2 Map & self-correct  Claude (live, Haiku / Sonnet / Opus routed per field) or a deterministic offline engine
        │            proposes a value + VERBATIM memo quotes; an independent validator checks quotes, numbers,
        │            dates (real calendar date, era, weekday), names, IDs, options and printed rules; failures are
        │            repaired (和暦 with weekday, 483万6千円 → 4,836,000円, half-width IDs, deletion-only
        │            condensation that never silently drops a condition) or sent back to Claude (Opus) with the
        │            validator's findings
 3 Surgical write .... replace ONLY the marker characters inside the XML bytes; every other ZIP entry is copied
        │            as raw compressed bytes (not even re-compressed)
 4 Verify ............ 21 deterministic checks (plus VIS-01 if layout drift or overflow is detected): ZIP/SHA-256,
        │            XML skeleton, tables/borders/merges, boilerplate text, grounding, constraints, application load
 5 Human sign-off .... checklist with the source quote for every value (and the full text of anything condensed
                     away); any field can be corrected; the download unlocks only after a named reviewer confirms
                     every item (enforced by the server, bound to the revision on screen and the file's SHA-256)
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
pytest -v                                # 109 tests
```

Requires Python 3.10+. Without `ANTHROPIC_API_KEY` everything runs on the deterministic **offline demo engine**, so the
whole pipeline is reproducible with zero setup.

## Live Claude mode

Set the key in your shell or in a git-ignored `.env` file in the project root:

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | enables live mode (`--mode auto` picks it up) |
| `FORM_GUARD_MODEL_TIER` | `auto` | `auto` routes per field (below); `opus` / `sonnet` / `haiku` use one model (CLI: `--model-tier`) |
| `FORM_GUARD_MODEL` | – | pins one model id for every call (CLI: `--model`) |
| `FORM_GUARD_EFFORT` | `medium` | `output_config.effort` (`low` … `max`; clamped to what the model supports) |
| `FORM_GUARD_FALLBACKS` | `1` | server-side refusal fallback opt-in (only where the model supports it); set `0` to disable |

```bash
python -m form_guard.cli --all-demos --mode live
python -m form_guard.cli --form my_form.docx --memo notes.txt --out-dir out --mode live --model-tier auto
```

How Claude is used at runtime (`form_guard/mapper.py`):

* **Multi-model routing** (`--model-tier auto`): structured fields (和暦 dates, periods, digits, ID patterns,
  choices) and blank-cell label classification → `claude-haiku-5-5`; standard text, yen amounts and names →
  `claude-sonnet-5-5`; long-text condensation and every self-correction pass after a validator rejection →
  `claude-opus-5-5`. The audit log and the report show which model handled which field and why.
* **Structured outputs** (`output_config.format` = JSON schema): one entry per field with `value`,
  `source_quotes` (verbatim, single-line), line numbers, confidence and a short reason — or `not_found`.
* **Adaptive thinking** (`display: "summarized"`) with an explicit **effort** level, streamed with
  `get_final_message()` — sent only to models that support them (a capability table keeps legacy models such as
  `claude-haiku-4-5` on plain requests, so no call fails with a 400). Thinking summaries returned by the API are
  kept in the audit log, labelled as API-returned summaries.
* **Self-correction loop**: the validator's findings ("quote not found verbatim", "212 chars exceeds the 200-char
  limit", "1,612,000円 exceeds the printed cap") are sent back in the same conversation; only the failing fields
  are revised. Values that still fail are *not* written; they go to the reviewer.
* **Refusals** are handled (`stop_reason == "refusal"`), with the server-side fallback opt-in
  (`fallbacks="default"`, beta `server-side-fallback-2026-07-01`). In `auto` mode, a failed live call falls back
  to the offline engine and the report says so.
* The memo is treated as data, never as instructions (prompt-injection guard in the system prompt).

The whole project — code, tests, sample generators and docs — was written with **Claude Code** during the hackathon.

## Claude Code integration: skills, subagents, hooks, MCP

The repository ships its own Claude Code setup (details and rationale: [docs/BUILT_WITH_CLAUDE_CODE.md](docs/BUILT_WITH_CLAUDE_CODE.md)):

| | |
|---|---|
| `.claude/skills/` | `form-scan`, `surgical-fill`, `structural-audit` — the scan → fill → audit procedures |
| `.claude/agents/` | `memo-extractor` (read-only verbatim evidence, no guessing) and `form-auditor` (independent re-verification) |
| `.claude/settings.json` | `PostToolUse` hook on `Edit`/`Write`: Python syntax + secret / local-path / e-mail hygiene check, cross-platform `python -c` |
| `.mcp.json` | registers the `form-guard` MCP server: `python -m form_guard.mcp_server` (stdio JSON-RPC 2.0, no extra dependencies) with `form_guard_scan`, `form_guard_discover_blank_cells`, `form_guard_fill` (writes only an **unapproved draft**) and `form_guard_verify` |

## Unmarked blank forms: discover → confirm → fill

A form without `{{markers}}` and without a `.slots.json` can still be filled — after a human confirms the cells:

```bash
python -m form_guard.cli --discover-blanks samples/blank_forms/form5_unmarked_blank.xlsx --mode demo --out-dir output
python -m form_guard.cli --form samples/blank_forms/form5_unmarked_blank.xlsx --memo samples/memos/memo5_stockpile_loan.txt \
    --discovery output/form5_unmarked_blank.discovery.json --confirm recommended --out-dir output/form5 --mode demo
```

1. **Heuristics** — empty cells with a printed label immediately to the left (or above, in a column-header table).
2. **Classification** — applicant field vs office-only (`事務局`, `受付印`, `受付番号`, `審査欄`, `決裁`, `検印` …: never
   recommended); duplicate labels get unique ids (`研究代表者_氏名` / `事務担当者_氏名`); format and character limit are
   inferred from the label. In live mode Claude (Haiku) also classifies the labels, but can only make a cell *less* fillable.
3. **Safety** — formula cells, merged continuations and cells with content are rejected.
4. **Human gate** — `--confirm c01,c02,…` (or `recommended`) builds the slot spec only if the blank form still has the
   SHA-256 it had at discovery, at least one cell is confirmed, and no office-only cell is included.
   Web API: `POST /api/discover`, then `POST /api/runs/upload` with `discovery_id` and `confirmed_targets`.

## The demos (100% fictional, non-medical)

| Demo | File | Shows |
|---|---|---|
| 共同研究計画変更届出書（様式第3号） — 架空学術大学 | `.docx` | split-run markers, `2026年11月2日（月）` → `令和8年11月2日（月）`, full-width ID → `JR-2026-047`, 257 → 181 chars by deletion only (deleted text listed for the reviewer), merged label cells (vMerge) |
| 地域中小企業省エネ設備導入補助金 交付申請書（様式第1号） — 架空市 | `.docx` | `483万6千円` → `4,836,000円`, printed cap `上限1,000,000円` forces re-extraction (1,612,000円 → 1,000,000円), **missing 法人番号 left for the human**, empty cell filled by table coordinates |
| 月次設備安全点検および改善報告書（様式A-4） — 架空施設 | `.xlsx` | drop-down list values, `COUNTIF` formulas and 40 merged ranges preserved, 179 → 116 chars keeping the interim measure 「交換完了までは…」, year of 「12月8日（火）」 inferred and flagged |

Each demo also ships a scripted **naive regeneration** (`samples/broken_before/`) that reproduces the usual failure
modes — US-Letter page, borders and merged cells lost, legal clauses paraphrased, invented numbers such as
「約50万円」 or a fabricated 13-digit 法人番号 — and Form-Guard's verifier flags every one of them.

Two **unmarked** blank forms (no markers, no `.slots.json`) show blank-cell discovery:

| Form | File | Shows |
|---|---|---|
| 研究設備共用利用申請書 — 架空学術機関 | `form4_unmarked_blank.docx` | 7 applicant cells found next to printed labels, duplicate 「氏名」 → `研究代表者_氏名` / `事務担当者_氏名`, 4 office-only cells (受付番号・受付日・審査欄・事務局受付欄) excluded, vertically merged stamp continuation rejected |
| 地域防災備蓄品貸出申請書 — 架空市 | `form5_unmarked_blank.xlsx` | 5 applicant cells in merged ranges (style ids kept), `SUM` formula cell and merged continuation rejected, 「※事務局使用欄（決裁印・受付番号）」 excluded |

All sample forms, memos, names and organisations were written from scratch during the hackathon and are fictional.
E-mail addresses use the reserved `example.jp` domain.

## What the verifier proves

| ID | Check |
|---|---|
| ZIP-01…05 | archive integrity (CRC), same entries/order, identical ZipInfo metadata, every non-target entry byte-identical (raw compressed stream + SHA-256), only slot-bearing parts modified |
| XML-01…03 | well-formed (DTD/entities rejected), namespace declarations identical, **markup skeleton byte-identical** — every tag, attribute (incl. `xml:space`) and non-slot text, with only the slot text nodes blanked |
| LAYOUT-01…04 | tables/rows/cells, gridSpan/vMerge, tblGrid · borders & shading · pPr/rPr · page setup (for .xlsx: merged ranges, cell styles, widths/heights, formulas, data validation, print area) |
| TEXT-01…03 | all boilerplate text unchanged · each slot holds exactly its approved value · no marker left except fields awaiting human input |
| GRD-01…02 | every AI value backed by verbatim memo quotes, with names, IDs and selected options matching those quotes · every number/date in a value appears in the quoted evidence (no sub-amounts of 483万6千円, matching weekday) |
| CON-01…03 | printed character limits · formats (real calendar dates inside their era, matching （曜）, periods in order, yen, ID patterns) · options, caps and rounding |
| APP-01 | the file opens in an independent OOXML library |
| VIS-01 | rendered layout outside the designated fields has zero drift and no Excel value overflows its cell, on every sheet (added only if drift or overflow is found) |

XML-03 keeps the exact start tag (including `xml:space`) and text of every non-slot text node, and TEXT-01 compares
boilerplate run by run, so moving one character between a bold and a normal run is caught.

Coordinate-designated empty cells are checked strictly. A Word cell may gain exactly one run with one `<w:t>`: a
drawing, picture, object, field (`fldSimple` / `fldChar` / `instrText`), hyperlink, table or second run fails XML-03,
and run formatting that differs from the blank cell's paragraph-mark formatting (font size, bold, colour, a removed
`rPr`) or a changed `pPr` fails LAYOUT-03. An Excel cell may only become an inline string: a formula fails XML-03 and
LAYOUT-04, a changed style id `s` fails LAYOUT-02, and reverting the designated cells must reproduce the blank sheet
byte for byte, so any other cell that changed fails XML-03 / TEXT-01.

`python -m form_guard.cli --verify ORIGINAL FILLED` audits *any* filled copy — including one filled by hand or by another
tool. It always checks structure and printed constraints; memo grounding is checked when `--memo MEMO` is given or a
matching `audit_log.json` (same SHA-256) sits next to the filled file. Without either, the report says
"structural verification only (no memo provided — grounding not verified)". Reviewer-entered values can be declared
with `--set SLOT=VALUE`.

## Designating fields in your own forms

* Type `{{項目名}}` or `[記入欄: 項目名]` where a value belongs (markers split across Word runs are handled).
* For official forms you must not edit, designate **empty cells by coordinates** in `<form>.slots.json` — a Word
  table cell `{"targets": [{"slot_id": "担当者連絡先", "table": 1, "row": 8, "col": 1}]}` or an Excel cell
  `{"targets": [{"slot_id": "申請日", "sheet": 0, "cell": "C5"}]}` (`sheet` = index or name). Excel cells that do not
  exist yet are created in column order with the row / column style Excel would show; formula cells, merged
  continuations and cells with content are refused. Or let `--discover-blanks` propose the cells.
* Excel workbooks may have several sheets; every sheet is scanned, rendered and checked. Worksheets larger than
  2,000 rows × 200 columns (or 100,000 cells) are refused rather than processed.
* Rules printed on the form are discovered automatically (`（200字以内）`, `（13桁）`, `例：JR-2026-001`, `上限1,000,000円`,
  `千円未満切捨て`, `A／B／Cのうち`, Excel drop-down lists). `.slots.json` can add aliases and descriptions.

## Human-in-the-loop by design

The web UI shows every value next to its verbatim source (click a value in the preview to jump to the memo line),
the self-correction history, and the full structural audit. The reviewer can **correct any field**, including
AI-filled ones; each edit is written surgically, re-verified and clears any earlier approval. When a value had to be
shortened, the checklist shows the **full text that was deleted**; if shortening would delete a condition
(ただし, 場合に限り, 禁止, 除く, 不可, 条件 …) the value is not written at all and is offered only as a suggestion.
The **download stays locked on the server** until there are no pending fields, verification passed, and a named
reviewer has confirmed every checklist item. The approval is bound to the revision on screen and to the SHA-256 of
the exact bytes the verifier checked; the download re-checks the file and serves those bytes. The approval record
(reviewer, time, checked items, revision, SHA-256) is written next to the audit log.

## Scope and safety

* Form-Guard checks that each value is **traceable to verbatim memo quotes** (numbers, dates, names, IDs and choices must match them) and that **the form itself was not altered**. The wording of free-text values is condensed/checked mechanically, and the human reviewer confirms the final meaning and decides whether to submit.
* It does not establish whether the memo is true; that, and the decision to submit, stays with the human reviewer.
* Intended for general administrative, research-administration and municipal paperwork. No medical use.
* Do not upload real personal data. The server binds to `127.0.0.1`, refuses requests whose `Host` is not local
  (DNS rebinding) and state-changing requests from another `Origin` (cross-site forms), rejects DTDs and entity
  declarations, limits upload and decompressed sizes (zip-bomb guard), sanitises every colour taken from a form
  before it reaches an SVG preview, and serves the UI with a strict Content-Security-Policy.
* Reports only call the data fictional for an unmodified built-in demo; for your own forms the footer says the
  report is based on the supplied form and memo.

## Project layout

```
form_guard/
  scanner.py      slot discovery, 4-stage blank-cell discovery (+ printed-rule inference in constraints.py)
  mapper.py       Claude (multi-model routing) / offline engines + self-correction loop (grounding.py, textnorm.py)
  writer.py       surgical byte-range injection (ooxml.py indexer, ziputil.py raw ZIP copy)
  verifier.py     ZIP / XML / layout / text / grounding / constraint checks + HITL checklist
  visual_diff.py  deterministic SVG (and optional PNG) previews, layout-drift measurement
  pipeline.py     orchestration; report.py standalone HTML; cli.py; server.py (FastAPI)
  mcp_server.py   MCP server (stdio JSON-RPC 2.0) with the scan / discover / fill / verify tools
  static/         vanilla HTML/CSS/JS web UI
.claude/          project skills, subagents and hook (settings.json); .mcp.json registers the MCP server
docs/             BUILT_WITH_CLAUDE_CODE.md, measure_workflow.py, screenshots
samples/          blank forms (3 marked + 2 unmarked), slot specs, memos, naive regenerations (samples.py)
tests/            pytest suite (fake + mock-transport Claude, tampering attacks, API gate, MCP over stdio)
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

1. **スキャン** — `{{項目名}}`・`[記入欄: 項目名]`・座標指定の空欄（Word の表のセル、Excel のセル）を見つけ、様式に印刷された記入ルール（字数・和暦・桁数・上限額・選択肢）も読み取ります。マーカーの無い白紙様式では、空欄候補を4段階で発見し（申請者欄か事務局欄かも判定）、記入する欄を人が確定します。
2. **対応付けと自己修正** — Claude（ライブ。欄の種類に応じて Haiku・Sonnet・Opus を使い分け）またはオフラインの決定論エンジンが、値と**メモ原文そのままの引用**を返します。独立した検証器が引用・数値・日付・記入ルールを確認し、和暦変換・金額表記の統一・半角化・削除のみの要約で直すか、指摘内容を添えて Claude（Opus）に再依頼します。
3. **外科的な書き込み** — XML 内のマーカー文字だけを置き換えます。それ以外の ZIP 内パーツは圧縮済みバイト列のまま複製します。
4. **構造検査** — ZIP・SHA-256、XML骨格、表構造・罫線・結合セル、定型文、根拠、記入ルールなど21項目を決定論的に検査します（描画位置のずれやセルはみ出しが検出された場合は VIS-01 を追加）。
5. **人による最終確認** — 各値をその根拠と並べて確認し、AIが記入した欄も含めてどの欄でも修正できます（修正すると再検査され、承認は取り消されます）。字数制限のために削除した文は全文を表示し、条件（ただし・場合に限り・禁止など）が欠ける要約は記入せず候補として人に回します。確認者名の入力と全項目のチェックが済むまで、ダウンロードはサーバー側でロックされます。承認は画面上の版（revision）と、検査したファイルの SHA-256 に結び付けて記録します。

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

`ANTHROPIC_API_KEY` を設定すると（`.env` も可、Git には含まれません）、`--mode auto` / `--mode live` で Claude を使います。
既定の `--model-tier auto` では、定型の欄（和暦日付・期間・桁数・書式・選択肢）と空欄ラベルの分類を `claude-haiku-5-5`、
通常の文章・金額・氏名を `claude-sonnet-5-5`、長文の要約と検証器に差し戻された後の自己修正を `claude-opus-5-5` が担当します
（`--model-tier opus|sonnet|haiku` で固定も可）。構造化出力（JSON スキーマ）、adaptive thinking（要約表示）と effort 指定は
対応モデルにだけ送り、旧モデルでもエラーになりません。どの欄をどのモデルが担当したかは監査ログとレポートに残ります。

### マーカーなしの白紙様式（発見 → 人が確定 → 記入）

```bash
python -m form_guard.cli --discover-blanks samples/blank_forms/form4_unmarked_blank.docx --mode demo --out-dir output
python -m form_guard.cli --form samples/blank_forms/form4_unmarked_blank.docx --memo samples/memos/memo4_equipment_use.txt \
    --discovery output/form4_unmarked_blank.discovery.json --confirm recommended --out-dir output/form4 --mode demo
```

印字されたラベルの右（または列見出しの下）の空欄を候補にし、事務局・受付印・受付番号・審査欄・決裁・検印などの欄は推奨せず、
同じラベル（「氏名」など）には区分名を付けて区別します（`研究代表者_氏名`／`事務担当者_氏名`）。計算式のセルや結合セルの続きは
除外します。記入する欄は人が `--confirm` で確定し、白紙様式の SHA-256 が発見時と違う場合・0 件の場合・事務局欄を含む場合は
記入前に拒否します。

### Claude Code との連携

リポジトリには Claude Code 用の Skills（`.claude/skills/`：スキャン・外科的記入・構造検査）、サブエージェント
（`.claude/agents/`：原文引用だけを抽出する `memo-extractor`、独立に検査し直す `form-auditor`）、編集後に構文と秘密情報・
ローカルパス・メールアドレスを点検するフック（`.claude/settings.json`）、MCP サーバー（`.mcp.json` →
`python -m form_guard.mcp_server`、記入は**未承認の下書き**のみ）を同梱しています。詳しくは
[docs/BUILT_WITH_CLAUDE_CODE.md](docs/BUILT_WITH_CLAUDE_CODE.md) を参照してください。

### デモ（すべて架空・医療以外）

- **共同研究計画変更届出書（様式第3号）**：分割された run 上のマーカー、西暦→和暦（曜日つき：令和8年11月2日（月））、全角ID→半角、200字への削除のみ要約
- **省エネ設備導入補助金 交付申請書（様式第1号）**：483万6千円→4,836,000円、印刷された上限額による再抽出、法人番号は人が入力、座標指定の空欄への記入
- **月次設備安全点検および改善報告書（様式A-4, .xlsx）**：プルダウン・COUNTIF式・結合セルを保持、120字への要約（暫定措置「交換完了までは…」を残す）、年を推定した日付には確認フラグ
- **研究設備共用利用申請書（マーカーなし, .docx）**：空欄を発見して申請者欄7か所を推奨、事務局欄4か所を除外、「氏名」の重複を区別
- **地域防災備蓄品貸出申請書（マーカーなし, .xlsx）**：結合セルの空欄5か所に書式番号を保ったまま記入、SUM式と結合セルの続き、事務局使用欄を除外

各デモには「生成AIで作り直した場合」を再現した比較用ファイル（`samples/broken_before/`）も付いています。
すべてのサンプル様式とテストデータは、本ハッカソン期間中にゼロから自作した架空のものです。

### 位置付けと注意

- 本ツールが機械的に確認するのは「各値がメモの原文引用に紐づき、数値・日付・氏名・ID・選択肢が一致すること」と「様式そのものが改変されていないこと」です。自由記述の文意やメモの内容が事実かどうかの確認と、提出の最終判断は人が行います。
- `python -m form_guard.cli --verify 原本 記入済み` は構造と記入ルールを常に検査し、`--memo` か同じ SHA-256 の `audit_log.json` がある場合だけ根拠も検査します（無い場合は「根拠は未検証」と表示します）。
- 一般事務・研究事務・自治体手続き向けです。医療用途には使いません。
- 実在の個人情報はアップロードしないでください。
