# Built with Claude Code / Claude Code での開発

Form-Guard Agent was written from scratch during **Built with Claude: Japan 2026** (development started Monday,
2026-11-02 / 2026-11-02（月）) with **Claude Code** running `claude-opus-5-5`: the application code, the tests, the
generators of the fictional sample forms and these documents. This page explains which Claude Code features ship with
the repository, why each mechanism was chosen, and how the running application divides its work between three Claude
models. [日本語 ↓](#日本語)

## 1. The Claude Code extensibility suite in this repository

| Mechanism | Files | What it does | Why this mechanism |
|---|---|---|---|
| **Skills** (6) | `.claude/skills/form-scan/SKILL.md`, `.claude/skills/surgical-fill/SKILL.md`, `.claude/skills/structural-audit/SKILL.md`, `.claude/skills/form-fill-verify/SKILL.md`, `.claude/skills/visual-diff-audit/SKILL.md`, `.claude/skills/portal-form-fill/SKILL.md` | Step-by-step procedures for scanning a form (and discovering the empty cells of an unmarked form), filling it from a memo with verbatim evidence, auditing a filled copy with the 21 checks + VIS-01 and the HITL checklist, a one-pass fill-and-verify that explains PASS / NEEDS_HUMAN_INPUT / FAIL, the visual / layout audit against a naive regeneration, and the end-to-end grant/study-group portal workflow (`/portal` and `GET /api/portal/forms` via **Claude in Chrome** + MCP for fixed PDF, Word and Excel forms). | A procedure that is always done the same way (commands, what to report, what never to do) belongs in a skill: Claude loads it only when the task matches, instead of carrying it in every prompt. A test checks that every CLI flag a skill names really exists. |
| **Subagents** (4) | `.claude/agents/memo-extractor.md` (tools: Read, Grep, Glob · model: sonnet), `.claude/agents/field-mapper.md` (tools: Read, Glob, Grep, Bash · model: claude-opus-5-5), `.claude/agents/structure-auditor.md` (tools: Read, Glob, Grep, Bash · model: sonnet), `.claude/agents/form-auditor.md` (tools: Read, Grep, Glob, Bash · model: opus) | `memo-extractor` lists every fact of a memo as verbatim quotes **before** it reads the slot list, then assigns them or answers `not_found`. `field-mapper` proposes values under the form's printed rules (和暦 with weekday, caps, condition-safe condensation). `structure-auditor` runs `--verify`, an independent ZIP hash script and the `audit_log.json` cross-check. `form-auditor` re-verifies adversarially, re-reads every value against the memo and lists what the human must confirm. | Separation of duties in fresh contexts: proposing (Opus for rule-heavy mapping, Sonnet for blind extraction) is never the same agent as checking (Sonnet for the fast mechanical audit, Opus for critical review). **No subagent has a file-editing tool (`Edit`/`Write`)**: `memo-extractor` uses only `Read, Grep, Glob`. `field-mapper`, `structure-auditor` and `form-auditor` do have `Bash`, for read-only verification commands: that `Bash` is restricted by their prompt instructions and by the `PreToolUse` hook (`scripts/hook_guard.py`), which blocks any write, move or deletion targeting `samples/blank_forms/` — the hook, not the tool list, is what stops a shell write. |
| **Hooks** (two-stage) | `.claude/settings.json` → `scripts/hook_guard.py` — `PreToolUse` on `Write\|Edit\|MultiEdit\|NotebookEdit\|Bash\|PowerShell`, `PostToolUse` on `Write\|Edit\|MultiEdit` | **Before** a tool runs, `PreToolUse` refuses (exit code 2 + `permissionDecision: "deny"`) any write, copy-over, move or delete of `samples/blank_forms/*.docx\|*.xlsx\|*.pdf` (redirection, cp/mv/rm, Set-Content, Copy-Item, sed -i, inline python / node / .NET writes …) — including deleting, moving or wildcard-removing the `samples/blank_forms` folder itself or a folder above it (`rm -rf samples/blank_forms`, `Remove-Item -LiteralPath samples/blank_forms -Recurse`, `rm samples/blank_forms/*`); only the real generator command (`python -m form_guard.cli --generate-samples`, never a flag inside a `# comment`) is exempt — and any write whose new text (`content`, `new_string`, `edits`, `new_source`) carries an Anthropic API key, a literal `ANTHROPIC_API_KEY = "sk-…"`, a personal user-profile path or an e-mail address outside the reserved example domains — so nothing reaches the disk. **After** a write, `PostToolUse` runs `py_compile` and an import smoke test of the edited module (< 1.8 s), validates JSON and re-scans the file; a finding goes back to Claude as `decision: "block"`. | A prompt can be ignored, a hook cannot: the blank templates are the "before" of every proof, and secrets must never be written in the first place. The commands — `python scripts/hook_guard.py pre-tool-use` / `post-tool-use` — contain no `$`, `%` or backtick, so they run identically in Git Bash, PowerShell, cmd, macOS and Linux; `CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR=1` keeps Claude at the project root so the relative path always resolves. Only a human can override the template guard (`FORM_GUARD_ALLOW_TEMPLATE_WRITE=1` in Claude Code's own environment); the git-ignored `.env` is the one place a key may go. No `bypassPermissions` anywhere. |
| **MCP server** | `.mcp.json` → `python -m form_guard.mcp_server` (`form_guard/mcp_server.py`) | Five tools over stdio JSON-RPC 2.0: `form_guard_list_demos` (all six fictional demos: marked, unmarked and the fixed PDF), `form_guard_scan`, `form_guard_discover_blank_cells`, `form_guard_fill`, `form_guard_verify` (+ VIS-01), each with a full JSON Schema `inputSchema`. Newline-delimited and Content-Length framed messages; no dependencies (the official `mcp` SDK can be used with `--fastmcp`). File descriptor 1 is pointed at stderr while the server runs and every message is UTF-8 bytes, so neither a stray print nor a cp932 console can corrupt the stream. | Claude Code (or any MCP client) calls the deterministic engine directly and gets structured results. `form_guard_fill` only writes an **unapproved draft** under `output/mcp_runs/` (never inside `samples/`, never over a blank form), with `approval_status: UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF` in the result, in `APPROVAL_STATUS.json` and in `audit_log.json` — not stamped into the document, which would break the structural proof. Its `review_url` (`http://127.0.0.1:8000/?run_id=…`, via `web_handoff.json`) opens that exact draft in the web app for the named reviewer's sign-off. Reviewer-entered values cannot be sent through MCP, office-only cells can be neither confirmed nor designated, `mode` defaults to `demo`, and paths must stay inside the project (never a `.env*`, `.git`, `.ssh` or `.claude-hackathon` component). |

The web UI makes the suite inspectable without opening an editor: the tab **Claude Code 連携 · Architecture**
(`GET /api/claude-suite`, `form_guard/claude_suite.py`) reads these files at request time — skill and subagent front
matter (with each subagent's model and tools), each hook stage with the checks found in `scripts/hook_guard.py`, the
permission denials, the MCP server registration and its tools — and shows how they work together, next to this run's
per-field model routing, the routing policy and the measured workflow numbers of section 3.

A typical Claude Code session with these assets:

1. *"What does this form need?"* → skill `form-scan` (MCP `form_guard_list_demos`, `form_guard_scan`, or
   `form_guard_discover_blank_cells` for an unmarked form). The **user** confirms which empty cells to fill.
2. *"Fill it from my notes."* → subagent `memo-extractor` reads the memo blind, `field-mapper` proposes values under the
   printed rules; skill `surgical-fill` / `form-fill-verify` runs the pipeline (MCP `form_guard_fill` → unapproved
   draft).
3. *"Check it."* → subagent `structure-auditor` (fast) or `form-auditor` (adversarial) re-verifies the draft (MCP
   `form_guard_verify`, skill `structural-audit` / `visual-diff-audit`) and lists what the human must confirm.
   Throughout, the `PreToolUse` hook keeps the blank templates and the repository's hygiene intact.
4. A named human reviewer signs off in the web app (`python -m form_guard.cli --serve`). Nothing in Claude Code can
   approve a file.

## 2. Multi-model routing inside the application

In live mode (`ANTHROPIC_API_KEY` set), `--model-tier auto` (the default) sends each task to the model it needs
(`form_guard/mapper.py`):

| Task | Model | Why |
|---|---|---|
| Structured fields — `wareki_date`, `date`, `wareki_period`, `digits`, `pattern`, `choice` | `claude-haiku-4-5-20251001` | Short, checkable tokens; the deterministic validator re-checks the format and the quote anyway. |
| Blank-cell label classification (live discovery, stage 2) | `claude-haiku-4-5-20251001` | Labels are short; the rules always win towards safety (Claude can only make a cell *less* fillable). |
| Standard text, yen amounts, person names | `claude-sonnet-5` | Needs reading comprehension but no condensation. |
| Long-text condensation — the memo passage is longer than the printed limit (or the limit is 150+ characters) | `claude-opus-5-5` | Deletion-only condensation that must not drop a condition is the hardest step. |
| Every self-correction pass after a validator rejection | `claude-opus-5-5` | Continues the conversation that proposed the field, with the validator's findings. |

A Haiku or Sonnet draft that fails grounding or a printed rule is **escalated**: its self-correction runs on
`claude-opus-5-5` with adaptive thinking, in the conversation that proposed it, and `usage.escalations` records the
field, the tier it came from and the verifier's reason. `--model-tier opus|sonnet|haiku` (or `--model-tiering off`,
which means one model — Opus — for every call) disables routing; `--model` or `FORM_GUARD_MODEL` pins any model id.

**Model availability fallback.** A model id that does not exist and one an API organisation cannot use both return
`404 not_found_error`. `ClaudeEngine` then retries along `MODEL_FALLBACK_CHAIN` — `claude-opus-5-5` → `claude-opus-5`
→ `claude-opus-4-6`, `claude-sonnet-5` → `claude-sonnet-4-5`, `claude-haiku-4-5-20251001` → `claude-haiku-4-5` —
rebuilding each request from the fallback model's capabilities. The
first id that answers serves the rest of the run, and the audit log keeps both: `usage.model_fallbacks` (tier,
requested id, unavailable ids, the id used), `usage.calls[].attempted_models`, and a `404 fallback` note in each
field's `routed_model` reason.

Request parameters are adapted per model via `model_capabilities()`, an explicit table of known model ids
(`KNOWN_MODEL_VERSIONS`: `opus` / `sonnet` / `haiku` 4.5, 4.6, 4.7, 5, 5.5 and `fable` 5 / 5.1): adaptive thinking
(`thinking={"type": "adaptive", "display": "summarized"}`) and `output_config.effort` are sent only to the table's
4.6-and-later entries; legacy models such as `claude-haiku-4-5` — the Haiku tier `claude-haiku-4-5-20251001`
included — get a plain structured-output request, and an
unrecognised family (`claude-unrecognized-5-5`) or future version (`claude-opus-99-9`) gets no capability flags at all.
Effort is clamped to a level the model supports, and the server-side refusal fallback is only requested where the
table says it exists. Request formatting is verified against mock/transport tests; live API availability and
model-specific features depend on the caller's Anthropic account tier. The audit log records which model handled which
field and why, every served model id, per-call stop reasons and tokens, and the thinking summaries the API returned
(labelled "API-returned thinking summary" — a summary written by the API, not the model's raw reasoning). The HTML
report shows the routing table.

Every `FieldMapping` carries `routed_model` and `route_reason`. In live mode `routed_model` is the model id the API
actually served — `claude-haiku-4-5-20251001` for a date, `claude-sonnet-5 → claude-opus-5-5 (revision)` for a field
the validator sent back — and a server-side fallback to another model is recorded as
`(served by …; requested …)`. Offline, the deterministic engine records the plan live auto mode would follow
(`offline (claude-haiku-4-5-20251001 in live auto)`, routing groups marked `planned_in_live_auto: True`), so a judge without an
API key still sees the routing design while the UI, the report and the CLI never present a plan as a call. The web UI
shows a model badge and the reason on every field (Fields & sources, with the API-returned thinking summary of the
call that handled the field when there is one) and the whole run in the Claude Code tab; the top-bar **Model tier**
selector (`Auto: Haiku / Sonnet / Opus`, `Opus 5.5 only`, `Sonnet 5 only`, `Haiku 4.5 only`) is sent with every run.
A live run that fails part-way in `auto` mode falls back to the offline engine and keeps the calls that were made:
`usage.live_attempt_failed`, the call list and the token counts stay in the audit log, and the UI and the report say
that live API calls happened before the fallback instead of "no model was called".

## 3. Measured workflow comparison

Measured on 2026-11-02（月） on Windows 11 with Python 3.13, using the offline demo engine (no network; live Claude
calls add API latency that is not included). The left-hand columns count the work a person would do by hand for each
fictional form — they are counted from the files by the same script, **not** a timed study of a person. The
Form-Guard columns are measured. Reproduce with `python docs/measure_workflow.py`.

| Form | Fields to set up by hand | Characters to transcribe | Printed elements to re-check line by line | Form-Guard wall-clock (measured) | Automated checks | Left for human sign-off |
|---|---|---|---|---|---|---|
| form1 `form1_research_plan_change.docx` | 8 (markers / coordinates already designated) | 325 | 492 XML tags · 35 printed text blocks · 19 merged cells/ranges · 9 border definitions · 18 package parts | 189 ms | 21/21 (PASS) | 10 checklist items |
| form2 `form2_municipal_equipment_subsidy.docx` | 11 (markers / coordinates already designated) | 174 | 519 XML tags · 34 printed text blocks · 7 merged cells/ranges · 12 border definitions · 18 package parts | 183 ms | 21/21 (NEEDS_HUMAN_INPUT) | 13 checklist items, 1 field(s) to enter |
| form3 `form3_facility_safety_inspection.xlsx` | 10 (markers / coordinates already designated) | 180 | 323 XML tags · 34 printed text blocks · 40 merged cells/ranges · 350 border definitions · 9 package parts | 246 ms | 21/21 (PASS) | 12 checklist items |
| form4 `form4_unmarked_blank.docx` | 7 (locate 11 empty cells, decide office vs applicant) | 113 | 503 XML tags · 38 printed text blocks · 24 merged cells/ranges · 10 border definitions · 18 package parts | 120 ms | 21/21 (PASS) | 9 checklist items |
| form5 `form5_unmarked_blank.xlsx` | 5 (locate 6 empty cells, decide office vs applicant) | 52 | 282 XML tags · 30 printed text blocks · 34 merged cells/ranges · 269 border definitions · 9 package parts | 154 ms | 21/21 (PASS) | 7 checklist items |

Across the five forms, Form-Guard wrote 40 values (844 characters) into 41 designated fields and compared 2,119 XML
tags, 171 printed text blocks, 124 merged cells/ranges and 650 border definitions, in about 0.1–0.25 s per form
(wall-clock varies with machine load; the counted columns do not). What is left for the person is the 51-item sign-off
checklist (each value next to its verbatim source) and the one fact the memo does not contain (法人番号 in form2),
which Form-Guard leaves empty instead of guessing. The same numbers are measured live in the web UI's Claude Code tab.

## 4. Safety by design

- Discovery never fills anything: the human picks the cells, office-only cells (事務局・受付印・受付番号・審査欄・決裁・検印)
  are refused, and the confirmed slot spec is bound to the SHA-256 of the blank form — a changed form is refused before
  any fill. The web UI makes this interactive for the built-in unmarked forms form4 (`.docx`) and form5 (`.xlsx`) under
  **マーカーなし白紙様式・空欄自動発見 / Unmarked blank forms**, and for uploads (**🔍 Discover blank cells**): the
  4-stage Blank-Cell Discovery Inspector pre-checks applicant cells, locks office-only cells, and
  `POST /api/runs/from-discovery` accepts nothing but candidate ids of the discovery stored on the server. One click on
  デモ4 / デモ5 (`POST /api/run-demo`) also shows an auto-preview of the recommended cells filled, but that run can never
  be approved: only the reviewer's confirmation (**✅ 検出した空欄を承認して外科的差込を実行**) produces a run to sign off.
- Every package passes one archive gate (`form_guard/ziputil.py`) before anything parses it — scanner, writer, verifier
  and web upload alike: path traversal in entry names, duplicate names, more than 2,000 entries, more than 100 MB
  uncompressed or an extreme compression ratio (ZIP bombs) and DTD / entity declarations (XXE) are refused, and uploads
  are capped at 15 MB. A hostile filled copy fails ZIP-01 before anything is decompressed.
- Coordinate-designated cells are checked strictly: a Word cell may gain one run with one `<w:t>` and the blank cell's
  paragraph-mark formatting (no drawing, field, hyperlink or table); an Excel cell may only become an inline string
  with its style id kept, and reverting the designated cells must give back the blank sheet byte for byte. The rows
  that hold them are checked before that revert: a created row carries only `r`, an existing row keeps its
  attributes (`hidden`, `ht`, `s`, `outlineLevel` …) and its non-`<c>` content, and `<sheetData>` is unchanged.
- The approval in the web app is bound to the revision on screen and to the SHA-256 of the verified bytes; approve and
  download *require* both `filled_sha256` and `revision` (422 when missing or malformed) and refuse a stale hash or
  revision (409) and a file that changed on disk. A run's state is only read and written under its re-entrant lock, so
  every response is one consistent snapshot.
- A confirmed discovery is re-checked against a fresh deterministic discovery of the actual blank form: coordinates
  that are not a candidate, office-only cells and `not_a_field` cells are refused, and the spec is rebuilt from the
  fresh candidates — editing the discovery JSON cannot point a fill at 受付番号 or 審査欄. A slot spec (uploaded, via
  MCP `slot_spec_path` or a sidecar `.slots.json`) that designates an office-only cell is refused before writing, and
  a candidate the live classifier leaves out or lists twice is never recommended.
- **MCP draft → web sign-off.** `form_guard_fill` writes `web_handoff.json` next to its unapproved draft
  (`output/mcp_runs/<run id>/`: a byte copy of the blank form, the memo, the effective slot spec, the mappings, the
  draft's SHA-256) and returns `review_url` = `http://127.0.0.1:8000/?run_id=<run id>`. The web app (`python -m
  form_guard.cli --serve`) loads that exact file on first use (`GET /api/runs/<run id>` or `POST /api/runs/import`)
  without running a model, re-runs the 21 checks and VIS-01 against the recorded blank form and refuses a draft whose
  SHA-256 differs from `audit_log.json` or that fails; the reviewer then inspects, edits, approves and downloads it.
- The MCP server defaults to `mode: "demo"` (offline; `live` / `auto` send data to the Claude API and are an explicit
  opt-in, marked with `openWorldHint: true`), caps every message at 10 MB (a larger or negative `Content-Length` or a
  truncated body is a JSON-RPC parse error, never a blocking read) and refuses paths with a `.env*`, `.git`, `.ssh` or
  `.claude-hackathon` component.
- **Using the Claude Code setup.** `.claude/` and `.mcp.json` are not self-contained: they run
  `python scripts/hook_guard.py …` and `python -m form_guard.mcp_server` from this repository. Clone the repository,
  create the virtual environment and install the dependencies (`python -m venv .venv`, then
  `pip install -r requirements.txt`), and launch Claude Code from the repository root — or, to work in another folder,
  point `.mcp.json` and `.claude/settings.json` at that installed environment and at `scripts/hook_guard.py`. Copying
  only `.mcp.json` and `.claude/` into a document folder is not enough.
- The hooks run as `python scripts/hook_guard.py …`: start Claude Code from a shell where the project's `.venv` is
  activated (`.venv\Scripts\Activate.ps1` on Windows PowerShell, `.venv\Scripts\activate` in cmd, `source
  .venv/bin/activate` elsewhere). The script itself needs only the standard library; its import check prefers
  `.venv` (or `FORM_GUARD_HOOK_PYTHON`) whichever `python` started it. User-profile paths are found whatever the user
  name (Japanese included), e-mail domains are compared label by label (only `example.com` / `example.jp` /
  `example.org` and their subdomains, `users.noreply.github.com`, plus exactly `noreply@anthropic.com`), and `.PY` /
  `.JSON` files are checked like `.py` / `.json`.
- All sample forms, memos, names and organisations were written from scratch during the hackathon and are fictional
  and non-medical.

## 5. Iterating with Claude Code

The git history shows the iterations, each one done with Claude Code and each one adding regression tests:

1. **Core pipeline** — scanner, surgical writer, 21-check verifier, visual diff, web app with the HITL gate.
2. **Audit fixes (R1)** — strict grounding, condition-safe condensation, approval bound to the verified bytes; one
   test per finding that fails on the pre-fix code.
3. **Phase 2** — the Claude Code suite (skills, subagents, hook, MCP server), multi-model routing, unmarked blank-form
   discovery and strict checks of coordinate-designated cells.
4. **Phase 3 (judging-criteria polish)** — MIT licence, interactive discovery of unmarked forms in the web UI,
   `routed_model` / `route_reason` on every field, the Claude Code tab (`GET /api/claude-suite`), the Japanese-first
   bilingual UI with the judge walkthrough, and this documentation.
5. **Phase 2 integration** — the two-stage hook guard (`scripts/hook_guard.py`), four read-only subagents and five
   skills, the fifth MCP tool `form_guard_list_demos` with fd-level stdout isolation and UTF-8 stdio, the 404 model
   fallback chain and recorded escalations, all five demos in the web UI with one-click discovery + auto-preview, and
   the archive gate against path traversal, ZIP bombs and XXE.
6. **Phase 4 (fixed PDF forms, text checkboxes `□` → `■`, and the grant portal)** — `form_guard/pdf_support.py` for
   fixed PDF forms (`form6_study_group_grant.pdf`), rebuilding ruled boxes, locking office-only boxes, writing values
   and ticking text checkboxes (`□` → `■`, with `requires_human_if_selected` for options that require a date or manual
   entry) via a pure-Python PDF incremental update (増分更新) that leaves the blank PDF as an exact byte prefix and
   verifies all 21 checks + `VIS-01`; plus the local grant/study-group portal (`/portal` and `GET /api/portal/forms`)
   and the `portal-form-fill` skill connecting **Claude in Chrome**, Claude Code and MCP.

The suite covers fake and mock-transport Claude clients (including the 404 fallback chain), tampering attacks, the
archive gate, the web API's human gate, the MCP server over stdio on a cp932 console, and both hook stages. Claude Code made it practical to add regression tests in every
iteration and, in Phase 3, to drive the web UI end to end in a headless browser before committing.

## 6. Real-machine verification (2026-11-03（火）)

Beyond the offline suite, two checks were run on real software on 2026-11-03（火）.

**Live Claude Code CLI MCP verification.** In a live Claude Code session started with the hackathon CLI wrapper
(`claude-hackathon.cmd`) in this repository, the `form-guard` server registered in `.mcp.json` answered through all
five tools:

| MCP tool | Called on | Result |
|---|---|---|
| `form_guard_list_demos` | — | 6 demos (3 marked, 2 unmarked Word/Excel, 1 fixed PDF) |
| `form_guard_scan` | form1 | 8 slots |
| `form_guard_discover_blank_cells` | form4 · form6 (fixed PDF) | form4: 11 candidates (7 applicant, 4 office-only) · form6: 19 boxes in 2 tables (7 applicant incl. `□ 合意済み　□ 協議中`, 2 office-only locked) |
| `form_guard_fill` | form1 + memo1 · form6 + memo6 (`mode: demo`) | `PASS` drafts (`UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF`) with their `review_url` (`form6` run `20261103-110647-a585b1`, `filled_sha256: dbb27d227ac7cc102aacecbc2cba5ce87e96034191878c2d0e169d2a3e3d1678`, `□ 合意済み` → `■ 合意済み`) |
| `form_guard_verify` | blank form1 / form6 vs. those drafts | `PASS` — 21/21 checks + VIS-01 (0 px drift outside slot boxes, blank PDF 3,742 bytes preserved as exact byte prefix) |

`GET /api/runs/<run_id>` — the draft's `review_url` — then loaded the exact same draft (same SHA-256) into the web UI
without any AI regeneration, re-verified it against the blank form and left it unapproved for the named reviewer.

**Live Microsoft Word 16.0 & Excel 16.0 COM verification.** The blank and filled copies of all five forms (form1–form5)
were opened through COM automation in real Microsoft Word 16.0 and Excel 16.0: 0 warnings, exact page counts preserved
(1 page → 1 page for form1, form2 and form4), clean PDF export, and Excel recalculated the formulas to the expected
values — form3 `D13 =COUNTIF($F$8:$F$12,"良")` → 3, `F13 =COUNTIF($F$8:$F$12,"要注意")` → 1,
`H13 =COUNTIF($F$8:$F$12,"要是正")` → 1; form5 `E16 =SUM(E13:E15)` → 17. This complements APP-01 (an independent OOXML
library opens every file) and the built-in previews, which are close to, but not identical with, Word/Excel rendering.

The 3-minute Japanese demo video follows [DEMO_VIDEO_SCRIPT_JA.md](DEMO_VIDEO_SCRIPT_JA.md): 175 seconds, one step per
guided-tour deep link `#tour=1` … `#tour=5` of the web UI.

---

<a id="日本語"></a>
## 日本語

Form-Guard Agent は **Built with Claude: Japan 2026**（開発開始 2026-11-02（月））の会期中に、**Claude Code**
（`claude-opus-5-5`）でゼロから作りました。アプリのコード、テスト、架空サンプル様式の生成コード、このドキュメントも含みます。
ここでは、リポジトリに同梱した Claude Code の拡張機能と、それぞれを選んだ理由、そして実行時に 3 つの Claude モデルを
どう使い分けているかを説明します。

### 1. リポジトリ同梱の Claude Code 拡張

| 仕組み | ファイル | 役割 | この仕組みを選んだ理由 |
|---|---|---|---|
| **Skills**（6 種） | `.claude/skills/form-scan/`・`surgical-fill/`・`structural-audit/`・`form-fill-verify/`・`visual-diff-audit/`・`portal-form-fill/`（各 `SKILL.md`） | 様式のスキャン（マーカーなし様式では空欄候補の発見）、メモの原文引用に基づく記入、21 項目＋VIS-01 の検査と人による最終確認、記入と検証を一度に行い PASS / NEEDS_HUMAN_INPUT / FAIL を説明する手順、生成AIによる作り直し版との見た目の比較、公募ポータル（`/portal`・`GET /api/portal/forms`）から **Claude in Chrome** と MCP で固定PDF・Word・Excel様式を取得し記入・承認画面までつなぐ手順。 | 毎回同じ手順（コマンド、報告すること、してはいけないこと）はスキルにまとめ、必要なときだけ読み込ませます。スキルに書いた CLI オプションが実在することもテストで確認します。 |
| **サブエージェント**（4 種） | `memo-extractor`（Read・Grep・Glob／sonnet）、`field-mapper`（Read・Glob・Grep・Bash／claude-opus-5-5）、`structure-auditor`（Read・Glob・Grep・Bash／sonnet）、`form-auditor`（Read・Grep・Glob・Bash／opus） | `memo-extractor` は欄の一覧を見る**前に**メモの事実を原文引用で洗い出し、その後で欄に割り当てるか `not_found` を返します。`field-mapper` は様式に印刷されたルール（曜日つき和暦・上限額・条件を落とさない要約）に沿って値を提案します。`structure-auditor` は `--verify`・独自の ZIP ハッシュ照合・`audit_log.json` の突き合わせを高速に行い、`form-auditor` は敵対的に検査し直し、各値をメモと照合して人が確認すべき点を挙げます。 | 「提案する役」と「確かめる役」を新しい文脈で分けます。**どのサブエージェントもファイル編集ツール（`Edit`/`Write`）を持ちません**。`memo-extractor` は `Read, Grep, Glob` のみです。`field-mapper`・`structure-auditor`・`form-auditor` は読み取り専用の検証コマンドのために `Bash` を持ちますが、その `Bash` はプロンプトの指示と PreToolUse フック（`scripts/hook_guard.py`）で制限され、`samples/blank_forms/` を対象とする書き込み・移動・削除はフックが阻止します（シェルからの書き込みを止めるのはツール一覧ではなくフックです）。 |
| **Hooks**（2 段） | `.claude/settings.json` → `scripts/hook_guard.py`（`Write\|Edit\|MultiEdit\|NotebookEdit\|Bash\|PowerShell` 前の `PreToolUse`、`Write\|Edit\|MultiEdit` 後の `PostToolUse`） | **実行前**の PreToolUse は、白紙様式（`samples/blank_forms/*.docx・*.xlsx・*.pdf`）の書き込み・上書きコピー・移動・削除（リダイレクト、cp/mv/rm、Set-Content、Copy-Item、sed -i、python・node・.NET での直接書き込みなど。`samples/blank_forms` フォルダ自体やその上位フォルダの削除・移動、`rm samples/blank_forms/*` のようなワイルドカード削除も含みます。例外は本物の生成コマンド `python -m form_guard.cli --generate-samples` だけで、`# コメント` に書いた文字列では例外になりません）と、書き込もうとしている文字列（`content`・`new_string`・`edits`・`new_source`）に含まれる API キー・`ANTHROPIC_API_KEY = "sk-…"` の直書き・個人のユーザーフォルダのパス・例示用ドメイン以外のメールアドレスを、終了コード 2（`permissionDecision: "deny"`）で阻止します。ディスクには何も書かれません。**書き込み後**の PostToolUse は、`py_compile` と編集したモジュールの import 確認（1.8 秒以内）、JSON の妥当性、ファイルの再点検を行い、問題を `decision: "block"` で Claude に返します。 | プロンプトは無視され得ますが、フックは無視できません。白紙様式はすべての検査の「変更前」で、秘密情報はそもそも書かせないことが大切です。コマンド（`python scripts/hook_guard.py pre-tool-use` / `post-tool-use`）は `$`・`%`・バッククォートを含まず、Git Bash・PowerShell・cmd・macOS・Linux で同じように動きます。`CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR=1` で作業ディレクトリをプロジェクト直下に保つので、相対パスが常に解決されます。白紙様式の保護を解除できるのは、`FORM_GUARD_ALLOW_TEMPLATE_WRITE=1` で Claude Code を起動した人だけです。`bypassPermissions` は使いません。 |
| **MCP サーバー** | `.mcp.json` → `python -m form_guard.mcp_server`（`form_guard/mcp_server.py`） | stdio の JSON-RPC 2.0 で 5 つのツール（`form_guard_list_demos`（架空デモ6件、固定PDFを含む）・`form_guard_scan`・`form_guard_discover_blank_cells`・`form_guard_fill`・`form_guard_verify`（VIS-01 つき））を JSON Schema 付きで提供します。改行区切りと Content-Length 形式の両方に対応し、追加の依存はありません（`--fastmcp` で公式 `mcp` SDK も利用可）。実行中はファイル記述子 1 を stderr に向け、メッセージは常に UTF-8 なので、余計な出力や cp932 のコンソールでも通信が壊れません。 | Claude Code などの MCP クライアントが決定論的な検証エンジンを直接呼び、構造化された結果を受け取れます。`form_guard_fill` が書くのは `output/mcp_runs/` の**未承認の下書き**だけで（`samples/` の中や白紙様式の上には書きません）、承認状態（`UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF`）は結果・`APPROVAL_STATUS.json`・`audit_log.json` に記録し、文書本体には書き込みません。結果の `review_url`（`web_handoff.json` 経由）で、その同じ下書きを Web 画面の承認へ引き継ぎます。担当者入力値の送信、事務局欄の確定・指定、プロジェクト外のパスや `.env`・`.git`・`.ssh`・`.claude-hackathon` を含むパスは受け付けず、`mode` の既定は `demo` です。 |

Web 画面の「Claude Code 連携 · Architecture」タブ（`GET /api/claude-suite`、`form_guard/claude_suite.py`）は、これらの
ファイルをリクエストのたびに読み込み（Skills とサブエージェントの定義、各サブエージェントのモデルとツール、2 段のフックの
対象と `scripts/hook_guard.py` に実装された検査項目、読み取り禁止の権限、MCP サーバーの登録とツール）、連携の流れを、この実行の欄ごとのモデル割り当て・ルーティング方針・
第3節の計測値と並べて表示します。

### 2. アプリ内のモデルの使い分け

ライブモードで `--model-tier auto`（既定）のとき、作業ごとにモデルを選びます。

| 作業 | モデル | 理由 |
|---|---|---|
| 定型の欄（和暦日付・期間・桁数・書式・選択肢） | `claude-haiku-4-5-20251001` | 短く検証しやすい値で、形式と引用は検証器が必ず確認します。 |
| 空欄候補のラベル分類（発見の第 2 段階） | `claude-haiku-4-5-20251001` | ラベルは短く、判定は常に安全側（Claude は欄を「記入しない」側にしか変えられません）。 |
| 通常の文章・金額・氏名 | `claude-sonnet-5` | 読解は必要ですが、要約はいりません。 |
| 字数制限を超える長文の要約 | `claude-opus-5-5` | 条件を落とさずに削除だけで縮める、最も難しい作業です。 |
| 検証器に差し戻された後の自己修正 | `claude-opus-5-5` | その欄を提案した会話を、検証結果を添えて続けます。 |

Haiku・Sonnet の下書きが根拠確認や印刷されたルールで却下されると、その欄の自己修正は `claude-opus-5-5`（adaptive thinking）に
引き上げられ、`usage.escalations` に欄・元のモデル・検証器の指摘が記録されます。`--model-tier opus|sonnet|haiku`
（または「1 つのモデル（Opus）で通す」`--model-tiering off`）で振り分けを止められ、`--model` や `FORM_GUARD_MODEL` で任意の
モデルを指定できます。

**モデルが使えない場合のフォールバック。** 存在しないモデル ID と、その API 組織で使えないモデル ID は、どちらも
`404 not_found_error` になります。そのとき `ClaudeEngine` は `MODEL_FALLBACK_CHAIN`（`claude-opus-5-5` → `claude-opus-5` →
`claude-opus-4-6`、`claude-sonnet-5` → `claude-sonnet-4-5`、`claude-haiku-4-5-20251001` → `claude-haiku-4-5`）に沿って、
各モデルの機能に合わせたリクエストで再試行します。最初に応答した
ID がその実行の残りを担当し、監査ログには要求したモデルと実際に応答したモデルの両方（`usage.model_fallbacks`、
`usage.calls[].attempted_models`、各欄の `routed_model` の注記）が残ります。
adaptive thinking（`display: "summarized"`）と effort は、`model_capabilities()` の既知モデル表（`KNOWN_MODEL_VERSIONS`：
`opus`・`sonnet`・`haiku` の 4.5・4.6・4.7・5・5.5 と `fable` 5・5.1）で対応しているモデルにだけ送り、`claude-haiku-4-5`
などの旧モデル（Haiku の層の `claude-haiku-4-5-20251001` を含む）には送りません。表に無いファミリーや将来のバージョンには、どの機能フラグも付けません。
モデル別に送信パラメータを切り替えます。確認済みなのはモックによるリクエスト形式で、実APIでの利用可否・対応機能はアカウント権限に依存します。
どの欄をどのモデルが担当したか（理由つき）、実際に応答したモデル、
API が返した思考の要約（「API-returned thinking summary」と明記）は監査ログに記録され、HTML レポートにも表示されます。

すべての `FieldMapping` に `routed_model` と `route_reason` を記録します。ライブでは API が実際に返したモデル ID
（日付なら `claude-haiku-4-5-20251001`、検証器に差し戻された欄なら `claude-sonnet-5 → claude-opus-5-5 (revision)`）で、サーバー側の
フォールバックで別のモデルが応答した場合も `(served by …; requested …)` と残ります。オフラインでは、ライブ auto 時の割り当て
計画を `offline (claude-haiku-4-5-20251001 in live auto)`（ルーティングには `planned_in_live_auto: True`）として記録するので、API キーの
無い審査員にも設計が見え、計画を「呼び出した」と表示することはありません。Web 画面では「記入値と根拠」で欄ごとにモデルの
バッジと理由（その欄を処理した呼び出しの思考の要約があれば併記）を、Claude Code タブで実行全体の割り当てを表示し、右上の
「モデル」選択（自動：Haiku / Sonnet / Opus、Opus 5.5 のみ、Sonnet 5 のみ、Haiku 4.5 のみ）は毎回の実行に送られます。
`auto` モードでライブ実行が途中で失敗した場合はオフラインエンジンで続けますが、それまでの呼び出しは残します
（`usage.live_attempt_failed`、呼び出し一覧とトークン数を監査ログに記録し、画面とレポートでも「モデルは呼び出していない」とは
表示せず、ライブの API 呼び出しの後に切り替えたことを示します）。

### 3. 作業量の比較（計測）

2026-11-02（月）に Windows 11・Python 3.13 で、オフラインのデモエンジン（ネットワークなし）を使って計測しました。
ライブモードでは API の応答時間が加わります（表には含みません）。左側の列は、架空の各様式を人が手作業で処理する場合の
作業量を同じスクリプトでファイルから数えたもので、人の作業時間を計ったものではありません。右側の列が Form-Guard の
計測値です。`python docs/measure_workflow.py` で再現できます（表は英語版と同じ）。

5 つの様式で、Form-Guard は 41 の指定欄に 40 件（844 文字）を記入し、XML タグ 2,119 個・定型文 171 か所・結合セル 124・
罫線定義 650 を 1 様式あたり約 0.1〜0.25 秒で照合しました（時間は PC の負荷で変わりますが、数えた列は変わりません）。
人に残るのは、各値を原文と並べて確認する計 51 項目の最終確認と、メモに無い事実（様式 2 の法人番号）の入力だけです。
Form-Guard はその欄を推測で埋めません。同じ数値は Web 画面の Claude Code タブでもその場で計測して表示します。

### 4. 安全のための設計

- 空欄の発見は記入を一切しません。記入する欄は人が選び、事務局欄（事務局・受付印・受付番号・審査欄・決裁・検印）は拒否し、
  確定した欄の定義は白紙様式の SHA-256 に結び付けます。様式が変わっていれば記入前に止めます。Web 画面では左の
  「マーカーなし白紙様式・空欄自動発見」の form4（.docx）・form5（.xlsx）と、アップロードした様式（「🔍 マーカーなし白紙から
  空欄を自動発見」）でこれを対話的に行えます。4段階の空欄発見インスペクターは申請者欄を最初からチェックし、事務局専用欄を
  ロックします。`POST /api/runs/from-discovery` が受け取るのは、サーバーに保存した発見結果の候補 ID だけです。
  デモ4・デモ5 を押すと（`POST /api/run-demo`）推奨の欄に仮記入した自動プレビューも表示しますが、その実行は承認できず、
  人が「✅ 検出した空欄を承認して外科的差込を実行」で確定した欄の実行だけが承認の対象になります。
- すべてのファイルは、解析する前に 1 つの関門（`form_guard/ziputil.py`）を通ります（スキャン・書き込み・検査・アップロード
  のどれでも）。ZIP のパストラバーサル、重複名、2,000 を超えるエントリ、展開後 100 MB 超や極端な圧縮率（ZIP 爆弾）、
  DTD・実体宣言（XXE）は拒否し、アップロードは 15 MB までです。悪意ある記入済みファイルは、展開前に ZIP-01 で不合格になります。
- 座標で指定した欄は厳しく検査します。Word の欄に追加できるのは、元の段落記号の書式を持つ 1 つの run と 1 つの `<w:t>` だけ
  （図・フィールド・ハイパーリンク・表は不可）、Excel の欄は書式番号 `s` を保ったインライン文字列だけで、指定欄を元に戻すと
  白紙のシートとバイト単位で一致しなければなりません。指定欄を含む行は元に戻す前に検査し、新しく作った行は `r` 以外の属性を
  持てず、既存の行は属性（`hidden`・`ht`・`s`・`outlineLevel` など）と `<c>` 以外の中身を保ち、`<sheetData>` も変えられません。
- Web 画面の承認は、画面上の版（revision）と検査済みファイルの SHA-256 に結び付けます。承認とダウンロードでは
  `filled_sha256` と `revision` の両方が必須で（欠けている・形式が違う場合は 422）、古いハッシュや版（409）、ディスク上で
  変更されたファイルは、承認もダウンロードも拒否します。実行の状態は再入可能なロックの下でだけ読み書きするので、
  応答は常に一貫したスナップショットです。
- 確定した空欄は、実際の白紙様式でもう一度決定論的に発見し直して照合します。候補に無い座標・事務局欄・`not_a_field` の欄は
  拒否し、スロット定義は新しい発見結果から作り直すので、発見結果の JSON を書き換えても受付番号や審査欄には記入できません。
  スロット定義（アップロード・MCP の `slot_spec_path`・付属の `.slots.json`）が事務局欄を指している場合も記入前に拒否し、
  ライブ分類で答えが無かった（または重複した）候補は推奨しません。
- **MCP の下書き → Web で承認。** `form_guard_fill` は未承認の下書きの隣（`output/mcp_runs/<run id>/`）に `web_handoff.json`
  （白紙様式の複製・メモ・スロット定義・対応付け・下書きの SHA-256）を書き、結果に `review_url`
  （`http://127.0.0.1:8000/?run_id=<run id>`）を返します。Web 画面（`python -m form_guard.cli --serve`）は初回の
  `GET /api/runs/<run id>`（または `POST /api/runs/import`）でその同じファイルをモデルを動かさずに読み込み、21項目と VIS-01 を
  白紙様式に対して再検査し、`audit_log.json` の SHA-256 と違う下書きや不合格の下書きは拒否します。確認者はそのまま確認・修正・
  承認・ダウンロードできます。
- MCP サーバーの `mode` の既定は `demo`（オフライン）で、Claude API にデータを送る `live`・`auto` は明示した場合だけです
  （`openWorldHint: true`）。メッセージは 10 MB まで（それより大きい・負の `Content-Length` や途中で切れた本文は JSON-RPC の
  解析エラーで、読み込みで止まることはありません）、`.env`・`.git`・`.ssh`・`.claude-hackathon` を含むパスは拒否します。
- **Claude Code での使い方。** `.claude/` と `.mcp.json` だけでは動きません。どちらもこのリポジトリの
  `python scripts/hook_guard.py …` と `python -m form_guard.mcp_server` を実行するからです。リポジトリを clone し、仮想環境を
  作って依存関係を入れ（`python -m venv .venv` のあと `pip install -r requirements.txt`）、リポジトリ直下から Claude Code を
  起動してください。別のフォルダで使う場合は、`.mcp.json` と `.claude/settings.json` をその環境と `scripts/hook_guard.py` に
  向けてください。`.mcp.json` と `.claude/` を書類フォルダに置くだけでは足りません。
- フックは `python scripts/hook_guard.py …` で動くので、プロジェクトの `.venv` を有効にしたシェル（PowerShell は
  `.venv\Scripts\Activate.ps1`、cmd は `.venv\Scripts\activate`、macOS/Linux は `source .venv/bin/activate`）から Claude Code を
  起動してください。スクリプト自体は標準ライブラリだけで動き、import 検査は `.venv`（または `FORM_GUARD_HOOK_PYTHON`）を優先します。
  ユーザーフォルダのパスはユーザー名（日本語を含む）に関係なく検出し、メールのドメインはラベル単位で照合し（`example.com`・
  `example.jp`・`example.org` とそのサブドメイン、`users.noreply.github.com`、それ以外は `noreply@anthropic.com` だけ）、
  `.PY`・`.JSON` も `.py`・`.json` と同じく検査します。
- サンプルの様式・メモ・人名・組織は、すべて本ハッカソン期間中にゼロから自作した架空のもので、医療とは無関係です。

### 5. Claude Code による反復開発

git の履歴に、Claude Code で進めた各段階が残っています。どの段階も回帰テストを追加しています。

1. **基本パイプライン** — スキャン、外科的な書き込み、21 項目の検査、見た目の比較、人による承認ゲートつきの Web 画面。
2. **監査指摘の修正（R1）** — 根拠の厳格化、条件を落とさない要約、検査済みバイト列に結び付けた承認。指摘ごとに、修正前の
   コードでは失敗するテストを追加。
3. **Phase 2** — Claude Code 拡張一式（Skills・サブエージェント・フック・MCP サーバー）、モデルの使い分け、マーカーなし白紙
   様式の空欄発見、座標指定欄の厳格な検査。
4. **Phase 3（審査基準に沿った仕上げ）** — MIT ライセンス、Web 画面でのマーカーなし様式の対話的な空欄発見、全欄の
   `routed_model` / `route_reason`、Claude Code タブ（`GET /api/claude-suite`）、日本語を既定とする 2 言語 UI と審査員向け
   ガイドツアー、このドキュメント。
5. **Phase 2 の統合** — 2 段のフック（`scripts/hook_guard.py`）、読み取り専用のサブエージェント 4 種とスキル 5 種、5 つ目の
   MCP ツール `form_guard_list_demos` とファイル記述子レベルの出力分離・UTF-8 の入出力、404 時のモデルフォールバックと
   引き上げの記録、Web 画面での 5 デモと 1 クリックの空欄発見＋自動プレビュー、パストラバーサル・ZIP 爆弾・XXE を防ぐ関門。
6. **Phase 4（固定PDF様式・文字型チェックボックス `□` → `■`・公募ポータル連携）** — 固定PDF様式（`form6_study_group_grant.pdf`）
   の罫線矩形から空欄と事務局欄を検出し、元のPDFバイト列を完全な先頭一致（増分更新 / incremental update）のまま保持して
   テキストと `□` → `■`（`requires_human_if_selected` 対応）をオーバーレイし、21項目＋`VIS-01` で検証する `pdf_support.py`、
   および公募ポータル（`/portal`・`GET /api/portal/forms`）と **Claude in Chrome**・Claude Code を結ぶ `portal-form-fill` スキル。

テストは、偽の Claude クライアントとモックの通信（404 フォールバックを含む）、改ざん攻撃、ファイルの関門、Web API の承認ゲート、
cp932 環境での stdio 経由の MCP サーバー、2 段のフックを対象にしています。Claude Code によって、どの段階でも回帰テストを追加し、Phase 3 ではコミット前にヘッドレス
ブラウザーで Web 画面を端から端まで操作して確認する進め方を無理なく続けられました。

### 6. 実機での検証（2026-11-03（火））

オフラインのテストに加えて、2026-11-03（火）に実際のソフトウェアで次の 2 つを確認しました。

**Claude Code CLI からの MCP 実行。** このリポジトリでハッカソン用 CLI（`claude-hackathon.cmd`）から起動した Claude Code の
セッションで、`.mcp.json` に登録した `form-guard` サーバーの 5 ツールをすべて呼び出しました。`form_guard_list_demos` はデモ 6 件
（マーカーあり 3・マーカーなし Word/Excel 2・固定PDF 1）、`form_guard_scan` は form1 で 8 欄、`form_guard_discover_blank_cells` は form4 で候補 11（申請者 7・
事務局 4）および固定PDFの form6 で 2 表 19 枠（申請者 7 枠〔`□ 合意済み　□ 協議中` を含む〕・事務局 2 枠ロック）、`form_guard_fill` は form1 および form6（run `20261103-110647-a585b1`、`filled_sha256: dbb27d227ac7cc102aacecbc2cba5ce87e96034191878c2d0e169d2a3e3d1678`、`□ 合意済み` → `■ 合意済み`）で `PASS` の未承認下書き（`UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF`）と `review_url`、
`form_guard_verify` はそれらの下書きで `PASS`（21/21 ＋ VIS-01、枠外の画素差分 0 px・はみ出しなし・白紙PDF先頭3,742バイト完全一致）を返しました。続けて `review_url` の
`GET /api/runs/<run_id>` で、同じ SHA-256 の下書きを AI で作り直すことなく Web 画面に読み込み、白紙様式に対して再検査したうえで、
確認者の承認待ちのまま表示しました。

**Microsoft Word 16.0・Excel 16.0 での COM 検証。** form1〜form5 の白紙と記入済みを COM 経由で実機の Word 16.0・Excel 16.0 で
開き、警告 0、ページ数不変（form1・form2・form4 は 1 ページ → 1 ページ）、PDF への出力、計算式の再計算が期待値と一致すること
（form3：`D13 =COUNTIF($F$8:$F$12,"良")` → 3、`F13 =COUNTIF($F$8:$F$12,"要注意")` → 1、`H13 =COUNTIF($F$8:$F$12,"要是正")` → 1、
form5：`E16 =SUM(E13:E15)` → 17）を確認しました。独立した OOXML ライブラリで開けることを確かめる APP-01 と、Word/Excel の描画に
近いものの同一ではない内蔵プレビューを補う確認です。

3分のデモ動画（日本語ナレーション・175秒）は [DEMO_VIDEO_SCRIPT_JA.md](DEMO_VIDEO_SCRIPT_JA.md) の台本と絵コンテに沿って、
Web 画面のガイドツアー `#tour=1`〜`#tour=5` を順にたどります。
