# Form-Guard Agent

**Fill only the designated fields of a Word / Excel form, prove that nothing else changed, and keep a human in the loop.**
**指定欄だけを記入し、様式の変更を検査する書類記入エージェント**

Built for **Built with Claude: Japan 2026** (development started Monday, 2026-11-02 / 2026-11-02（月）). [日本語版はこちら ↓](#日本語)

![Visual diff: naive regeneration vs Form-Guard](docs/screenshots/01_visual_diff.png)

---

## Why this matters & Who keeps using it after the hackathon

**Concrete pain in Japan's workplaces.** University and research-institute administration (共同研究の計画変更届,
研究設備の利用申請), municipal subsidy and disaster-stockpile counters (補助金の交付申請, 備蓄品の貸出申請) and
facility safety inspections (月次点検報告) all run on official `.docx` / `.xlsx` templates with strict cell grids,
merged cells, `vMerge`, formulas, drop-down lists and printed legal boilerplate. One broken border, one paraphrased
clause or one number the notes never stated means immediate administrative rejection (差し戻し) and another round
trip. The people who fill these forms every month — research administrators, municipal applicants and back-office
staff, mostly not engineers — cannot safely use chat AI for it: asked to "fill in this form", it **re-creates the
document**, so merged cells split, borders vanish, legal text is paraphrased, the page size changes and numbers
appear that are in nobody's notes — and nobody can tell what changed.

- **Theme 1 — depth only someone from the domain sees:** the dangerous failure is not "the AI wrote a wrong
  sentence" but "the AI silently changed the form". Form-Guard checks the form itself (ZIP, XML skeleton, borders,
  merges, boilerplate) and refuses to guess facts (`法人番号` stays empty for the human).
- **Theme 2 — what should reach everyone:** non-engineers get AI help without having to trust it: every value shows
  its verbatim source, office-only cells are locked, and the download stays locked until a named person signs off.

**Measured quantitative proof** (`python docs/measure_workflow.py` — offline engine, fictional sample forms; the
left-hand columns are counted from the files, **not** a timed study of a person):

| Form | Fields to set up by hand | Characters to transcribe | Printed elements to re-check line by line | Form-Guard wall-clock (measured) | Automated checks | Left for human sign-off |
|---|---|---|---|---|---|---|
| form1 `form1_research_plan_change.docx` | 8 (markers / coordinates already designated) | 325 | 492 XML tags · 35 printed text blocks · 19 merged cells/ranges · 9 border definitions · 18 package parts | 189 ms | 21/21 (PASS) | 10 checklist items |
| form2 `form2_municipal_equipment_subsidy.docx` | 11 (markers / coordinates already designated) | 174 | 519 XML tags · 34 printed text blocks · 7 merged cells/ranges · 12 border definitions · 18 package parts | 183 ms | 21/21 (NEEDS_HUMAN_INPUT) | 13 checklist items, 1 field(s) to enter |
| form3 `form3_facility_safety_inspection.xlsx` | 10 (markers / coordinates already designated) | 180 | 323 XML tags · 34 printed text blocks · 40 merged cells/ranges · 350 border definitions · 9 package parts | 246 ms | 21/21 (PASS) | 12 checklist items |
| form4 `form4_unmarked_blank.docx` | 7 (locate 11 empty cells, decide office vs applicant) | 113 | 503 XML tags · 38 printed text blocks · 24 merged cells/ranges · 10 border definitions · 18 package parts | 120 ms | 21/21 (PASS) | 9 checklist items |
| form5 `form5_unmarked_blank.xlsx` | 5 (locate 6 empty cells, decide office vs applicant) | 52 | 282 XML tags · 30 printed text blocks · 34 merged cells/ranges · 269 border definitions · 9 package parts | 154 ms | 21/21 (PASS) | 7 checklist items |

Across the 5 built-in forms Form-Guard filled 41 designated fields (40 values, 844 characters written) and verified
2,119 XML tags, 171 printed text blocks, 124 merged cells/ranges and 650 border definitions in about 0.1–0.25 s per
form. What is left for the person is the 51-item source-linked sign-off checklist and 1 unstated fact — `法人番号` in
form2 — which stays empty for the human instead of being hallucinated.

**Post-hackathon continued use & deployment roadmap**

1. **Daily administrative paperwork via the web UI / CLI.** Research administrators, municipal applicants and
   back-office staff register their recurring `.docx` / `.xlsx` templates once (unmarked forms: the web UI's
   blank-cell discovery or `--discover-blanks`, then a person confirms the cells) and fill monthly / quarterly reports
   from meeting notes — with zero template drift proven on every run and an auditable trail (`approval.json`,
   `audit_log.json`, `report.html`). It runs locally on `127.0.0.1` and works offline without an API key.
2. **Direct integration into Claude Code workflows via `.mcp.json` and Skills.** Anyone using Claude Code can drop
   `.mcp.json` and `.claude/` into their document folder: their assistant then fills forms only through Form-Guard's
   tools, gets **unapproved drafts** back, and can never produce an unverified or auto-approved official form. Other
   MCP clients (e.g. Claude Desktop) can register the same server command, `python -m form_guard.mcp_server`.

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
pytest -v                                # 120 tests
```

Requires Python 3.10+. Without `ANTHROPIC_API_KEY` everything runs on the deterministic **offline demo engine**, so the
whole pipeline is reproducible with zero setup.

The web UI is Japanese first (`日本語 / EN` toggle in the top bar). The **🎬 審査員向け 90秒ガイドツアー / Judge Quick
Walkthrough** bar jumps straight to the five proof points: ① visual diff against a naive regeneration, ② blank-cell
discovery on an unmarked form, ③ verbatim grounding and no guessing (`483万6千円 → 4,836,000円`, the printed cap,
the missing `法人番号`), ④ Claude usage — per-field model routing and the Claude Code suite, ⑤ the 21-check audit and
the human approval lock. The left rail lists the 3 marked demos, the 2 **unmarked blank forms** (form4 `.docx`, form5
`.xlsx`) for interactive discovery, and an upload form with **🔍 Discover blank cells** for your own unmarked forms.
Deep links for recordings: `/#tour=1` … `/#tour=5`, `/#unmarked=form4`, `/#demo=form2&tab=hitl`, `/#view=claude`,
`/#lang=en`.

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
* **Every field records its model**: each mapping carries `routed_model` / `route_reason` — the id the API actually
  served (`claude-haiku-5-5`, or `claude-sonnet-5-5 → claude-opus-5-5 (revision)` after a self-correction; a
  server-side fallback to another model shows up as such). Offline runs show the plan live auto mode would follow —
  `offline (claude-haiku-5-5 in live auto)` — and their routing groups are marked `planned_in_live_auto`, so a plan is
  never mistaken for a call. The web UI shows the routing per field (Fields & sources, with any API-returned thinking
  summary) and per run (tab **Claude Code 連携 · Architecture**); the top-bar **Model tier** selector
  (`Auto: Haiku / Sonnet / Opus`, `Opus 5.5 only`, `Sonnet 5.5 only`, `Haiku 5.5 only`) is sent with every run.
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
| Web UI tab **Claude Code 連携 · Architecture** | `GET /api/claude-suite` reads the files above (skills, subagents with their models and tools, the hook's checks, the permission denials, the MCP tools) and shows how they work together, this run's per-field model routing, the routing policy and the measured workflow numbers |

![Claude Code 連携 · Architecture tab: per-field Haiku / Sonnet / Opus routing and the routing policy](docs/screenshots/07_claude_code_architecture.png)

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

**In the web UI** pick form4 / form5 under **マーカーなし白紙様式・空欄自動発見 / Unmarked blank forms** — or upload your
own form and press **🔍 Discover blank cells** — and the **4-stage Blank-Cell Discovery Inspector** opens: the blank
form's SHA-256, the candidate / recommended / rejected counts, and every candidate cell with its id, location, printed
label, inferred format and limit, role and reason. Applicant cells (`✓ 申請者記入欄 / Applicant field`) are pre-checked;
office-only cells (`✗ 事務局専用欄（除外） / Office-only — locked`) can never be checked. The memo stays editable, and
**確定した空欄だけに外科的記入＆21項目検査を実行 / Fill confirmed cells & run 21 checks** continues into the usual
visual diff, fields & sources, structural audit and human approval screens.

Web API: `GET /api/unmarked-demos`; `POST /api/discover/demo` (built-in form4 / form5) or `POST /api/discover` (upload);
then `POST /api/runs/from-discovery` (JSON: `discovery_id`, `confirmed_targets` = candidate ids or `"recommended"`,
`memo_text`, `mode`, `model_tier`) or `POST /api/runs/upload` (multipart, with `discovery_id` and `confirmed_targets`).
The discovery stays on the server: the client only names candidate ids, and any other field is refused.

![4-stage blank-cell discovery inspector: applicant cells pre-checked, office-only cells locked](docs/screenshots/06_unmarked_discovery.png)

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
  claude_suite.py reads .claude/ and .mcp.json for GET /api/claude-suite; measure.py measured workflow numbers
  static/         vanilla HTML/CSS/JS web UI (Japanese first, 日本語 / EN)
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

License: MIT (see [LICENSE](LICENSE)).

---

<a id="日本語"></a>
## 日本語

### 誰のどんな課題を解決し、ハッカソン後にどこで使われ続けるか

**日本の職場の具体的な困りごと**：大学・研究機関の研究事務（共同研究の計画変更届、研究設備の利用申請）、自治体の補助金や
防災備蓄品の窓口、施設の安全点検では、罫線・結合セル・`vMerge`・計算式・プルダウン・印刷された定型文を持つ公式の
`.docx` / `.xlsx` 様式を使います。罫線が1本崩れる、文言が1つ言い換わる、メモに無い数字が入る——それだけで差し戻しになり、
やり直しが発生します。毎月これらに記入しているのは、多くがエンジニアではない研究事務・申請者・バックオフィスの担当者です。
生成AIのチャットは様式ごと作り直し、根拠の無い数字を作り、何が変わったのか誰にも分からないため、安全に使えませんでした。

- **テーマ1（その現場にいた人にしか見えない深さ）**：本当に危ないのは「AIの文章が間違う」ことより「AIが様式を黙って変える」
  ことです。Form-Guard は様式そのもの（ZIP・XML骨格・罫線・結合セル・定型文）を検査し、事実は推測しません（法人番号は人が入力）。
- **テーマ2（すべての人に届けるべきもの）**：AIを信用しなくても使えます。各値に原文の根拠を示し、事務局欄はロックし、
  名前のある確認者が承認するまでダウンロードはロックされます。

**計測による定量的な裏付け**（`python docs/measure_workflow.py`、オフラインエンジン、架空のサンプル様式。様式ごとの表は
英語版を参照。手作業の列はファイルから数えた作業量で、人の作業時間を計ったものではありません）：5つの付属様式で、
Form-Guard は 41 の指定欄に 40 件（844 文字）を記入し、XML タグ 2,119 個・定型文 171 か所・結合セル／範囲 124・罫線定義 650 を
1 様式あたり約 0.1〜0.25 秒で照合しました。人に残るのは、各値を原文と並べて確認する計 51 項目の最終確認と、メモに無い事実
1 件（様式2の法人番号）の入力だけです。Form-Guard はその欄を推測で埋めません。

**ハッカソン後の継続利用場面**

1. **Web 画面・CLI での日常の事務作業**：研究事務・申請者・バックオフィスの担当者が、毎月・四半期ごとの `.docx` / `.xlsx`
   様式を一度登録し（マーカーの無い様式は Web 画面の空欄発見か `--discover-blanks` で、記入する欄を人が確定）、会議メモから
   記入します。毎回「様式は変わっていない」ことを検査で示し、`approval.json`・`audit_log.json`・`report.html` の監査証跡が
   残ります。ローカル（`127.0.0.1`）で動き、API キーが無くてもオフラインで使えます。
2. **`.mcp.json` と Skills による Claude Code からの直接利用**：Claude Code の利用者は `.mcp.json` と `.claude/` を書類フォルダに
   置くだけで、AIアシスタントが Form-Guard のツール経由でしか様式に記入しなくなり、返ってくるのは**未承認の下書き**だけです。
   検証されていない様式や、自動で承認された様式は生まれません（Claude Desktop など他の MCP クライアントにも、同じ
   `python -m form_guard.mcp_server` を登録できます）。

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

Web 画面は日本語が既定です（右上で EN に切替）。上部の「🎬 審査員向け 90秒ガイドツアー」の①〜⑤から、①生成AIの作り直し版との
視覚比較、②マーカーなし白紙からの空欄発見、③原文根拠のハイライトと推測拒否（483万6千円→4,836,000円、印刷された上限、
法人番号は人の入力待ち）、④Claude のモデル使い分けと Claude Code 連携、⑤21項目の構造検査と人間承認ロック、の各画面へ
直接移動できます。

`ANTHROPIC_API_KEY` を設定すると（`.env` も可、Git には含まれません）、`--mode auto` / `--mode live` で Claude を使います。
既定の `--model-tier auto` では、定型の欄（和暦日付・期間・桁数・書式・選択肢）と空欄ラベルの分類を `claude-haiku-5-5`、
通常の文章・金額・氏名を `claude-sonnet-5-5`、長文の要約と検証器に差し戻された後の自己修正を `claude-opus-5-5` が担当します
（`--model-tier opus|sonnet|haiku` で固定も可）。構造化出力（JSON スキーマ）、adaptive thinking（要約表示）と effort 指定は
対応モデルにだけ送り、旧モデルでもエラーになりません。どの欄をどのモデルが担当したかは監査ログとレポートに残ります。
各欄の `routed_model` / `route_reason` には実際に応答したモデル（自己修正後は `claude-sonnet-5-5 → claude-opus-5-5 (revision)`）を
記録し、オフライン実行では `offline (claude-haiku-5-5 in live auto)` のように「ライブ auto 時の計画」であることを明示します
（API を呼んだとは表示しません）。Web 画面の「記入値と根拠」で欄ごとに、右上の「モデル」選択（自動：Haiku / Sonnet / Opus、
Opus 5.5 のみ など）で実行ごとに切り替えられます。

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

Web 画面では、左の「マーカーなし白紙様式・空欄自動発見」から form4（.docx）／form5（.xlsx）を選ぶか、自分の様式を
アップロードして「🔍 マーカーなし白紙から空欄を自動発見」を押すと、4段階の空欄発見インスペクターが開きます（白紙様式の
SHA-256、候補・推奨・除外の件数、候補ごとの ID・位置・印字ラベル・推定ルール・役割・理由）。申請者欄は最初からチェック済みで、
事務局専用欄はロックされて選べません。メモを確認・編集して「確定した空欄だけに外科的記入＆21項目検査を実行」を押すと、
通常の確認画面（見た目の比較・記入値と根拠・構造検査・人による最終確認）へ進みます。API：`GET /api/unmarked-demos`、
`POST /api/discover/demo`（付属様式）・`POST /api/discover`（アップロード）、`POST /api/runs/from-discovery`。

### Claude Code との連携

リポジトリには Claude Code 用の Skills（`.claude/skills/`：スキャン・外科的記入・構造検査）、サブエージェント
（`.claude/agents/`：原文引用だけを抽出する `memo-extractor`、独立に検査し直す `form-auditor`）、編集後に構文と秘密情報・
ローカルパス・メールアドレスを点検するフック（`.claude/settings.json`）、MCP サーバー（`.mcp.json` →
`python -m form_guard.mcp_server`、記入は**未承認の下書き**のみ）を同梱しています。Web 画面の
「Claude Code 連携 · Architecture」タブ（`GET /api/claude-suite`）は、これらのファイルを読み込んで連携の流れ、この実行の
欄ごとのモデル割り当て、ルーティング方針、計測した作業量を表示します。詳しくは
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

ライセンス：MIT（[LICENSE](LICENSE) を参照）。
