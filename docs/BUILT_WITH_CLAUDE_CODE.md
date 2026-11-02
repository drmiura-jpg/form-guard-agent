# Built with Claude Code / Claude Code での開発

Form-Guard Agent was written from scratch during **Built with Claude: Japan 2026** (development started Monday,
2026-11-02 / 2026-11-02（月）) with **Claude Code** running `claude-opus-5-5`: the application code, the tests, the
generators of the fictional sample forms and these documents. This page explains which Claude Code features ship with
the repository, why each mechanism was chosen, and how the running application divides its work between three Claude
models. [日本語 ↓](#日本語)

## 1. The Claude Code extensibility suite in this repository

| Mechanism | Files | What it does | Why this mechanism |
|---|---|---|---|
| **Skills** | `.claude/skills/form-scan/SKILL.md`, `.claude/skills/surgical-fill/SKILL.md`, `.claude/skills/structural-audit/SKILL.md` | Step-by-step procedures for scanning a form (and discovering the empty cells of an unmarked form), filling it from a memo with verbatim evidence, and auditing a filled copy with the 21 checks + VIS-01 and the HITL checklist. | A procedure that is always done the same way (commands, what to report, what never to do) belongs in a skill: Claude loads it only when the task matches, instead of carrying it in every prompt. |
| **Subagents** | `.claude/agents/memo-extractor.md` (tools: Read, Grep, Glob · model: sonnet), `.claude/agents/form-auditor.md` (tools: Read, Grep, Glob, Bash · model: opus) | `memo-extractor` reads a memo and returns, per slot, a value with verbatim quotes and line numbers, or `not_found`. `form-auditor` re-runs `python -m form_guard.cli --verify` on a filled copy and explains every failed check. | Separation of duties in fresh contexts: the extractor cannot write anything; the auditor did not produce the file it judges, so it does not inherit the filler's assumptions. Sonnet is enough for careful extraction; the auditor uses Opus because its job is critical review. |
| **Hook** | `.claude/settings.json` — `PostToolUse` on `Edit\|Write` | After every edit, a one-line `python -c "…"` checks Python syntax and JSON validity and scans the edited file for Anthropic API key patterns, literal API key assignments, local user-profile paths and e-mail addresses outside the reserved example domains. A finding is fed back to Claude (exit code 2). | Hygiene that must never be forgotten is enforced by the harness, not by memory. The command contains no shell-specific characters, so it runs in bash/sh and cmd.exe alike; if Python is missing it simply does not run and never blocks editing. No `bypassPermissions` anywhere. |
| **MCP server** | `.mcp.json` → `python -m form_guard.mcp_server` (`form_guard/mcp_server.py`) | Four tools over stdio JSON-RPC 2.0: `form_guard_scan`, `form_guard_discover_blank_cells`, `form_guard_fill`, `form_guard_verify`, each with a full JSON Schema `inputSchema`. Newline-delimited and Content-Length framed messages; no dependencies (the official `mcp` SDK can be used with `--fastmcp`). | Claude Code (or any MCP client) calls the deterministic engine directly and gets structured results. `form_guard_fill` only writes an **unapproved draft** (`approval_status: UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF`) under `output/mcp_runs/`; reviewer-entered values cannot be sent through MCP, office-only cells cannot be confirmed, and paths must stay inside the project. |

A typical Claude Code session with these assets:

1. *"What does this form need?"* → skill `form-scan` (MCP `form_guard_scan`, or `form_guard_discover_blank_cells` for an
   unmarked form). The **user** confirms which empty cells to fill.
2. *"Fill it from my notes."* → subagent `memo-extractor` reads the memo independently; skill `surgical-fill` runs the
   pipeline (MCP `form_guard_fill` → unapproved draft).
3. *"Check it."* → subagent `form-auditor` re-verifies the draft (MCP `form_guard_verify`) and lists what the human must
   confirm.
4. A named human reviewer signs off in the web app (`python -m form_guard.cli --serve`). Nothing in Claude Code can
   approve a file.

## 2. Multi-model routing inside the application

In live mode (`ANTHROPIC_API_KEY` set), `--model-tier auto` (the default) sends each task to the model it needs
(`form_guard/mapper.py`):

| Task | Model | Why |
|---|---|---|
| Structured fields — `wareki_date`, `date`, `wareki_period`, `digits`, `pattern`, `choice` | `claude-haiku-5-5` | Short, checkable tokens; the deterministic validator re-checks the format and the quote anyway. |
| Blank-cell label classification (live discovery, stage 2) | `claude-haiku-5-5` | Labels are short; the rules always win towards safety (Claude can only make a cell *less* fillable). |
| Standard text, yen amounts, person names | `claude-sonnet-5-5` | Needs reading comprehension but no condensation. |
| Long-text condensation — the memo passage is longer than the printed limit (or the limit is 150+ characters) | `claude-opus-5-5` | Deletion-only condensation that must not drop a condition is the hardest step. |
| Every self-correction pass after a validator rejection | `claude-opus-5-5` | Continues the conversation that proposed the field, with the validator's findings. |

`--model-tier opus|sonnet|haiku` uses one model for every call; `--model` or `FORM_GUARD_MODEL` pins any model id.
Requests are built from a capability table (`model_capabilities()`): adaptive thinking
(`thinking={"type": "adaptive", "display": "summarized"}`) and `output_config.effort` are sent only to models that
accept them (the 5.x models, `claude-opus-4-6`, `claude-sonnet-4-6`); legacy models such as `claude-haiku-4-5` get a
plain structured-output request, so no call fails with a 400. Effort is clamped to a level the model supports, and
the server-side refusal fallback is only requested where it exists. The audit log records which model handled which
field and why, every served model id, per-call stop reasons and tokens, and the thinking summaries the API returned
(labelled "API-returned thinking summary" — a summary written by the API, not the model's raw reasoning). The HTML
report shows the routing table.

## 3. Measured workflow comparison

Measured on 2026-11-02（月） on Windows 11 with Python 3.13, using the offline demo engine (no network; live Claude
calls add API latency that is not included). The left-hand columns count the work a person would do by hand for each
fictional form — they are counted from the files by the same script, **not** a timed study of a person. The
Form-Guard columns are measured. Reproduce with `python docs/measure_workflow.py`.

| Form | Fields to set up by hand | Characters to transcribe | Printed elements to re-check line by line | Form-Guard wall-clock (measured) | Automated checks | Left for human sign-off |
|---|---|---|---|---|---|---|
| form1 `form1_research_plan_change.docx` | 8 (markers / coordinates already designated) | 325 | 492 XML tags · 35 printed text blocks · 19 merged cells/ranges · 9 border definitions · 18 package parts | 175 ms | 21/21 (PASS) | 10 checklist items |
| form2 `form2_municipal_equipment_subsidy.docx` | 11 (markers / coordinates already designated) | 174 | 519 XML tags · 34 printed text blocks · 7 merged cells/ranges · 12 border definitions · 18 package parts | 158 ms | 21/21 (NEEDS_HUMAN_INPUT) | 13 checklist items, 1 field(s) to enter |
| form3 `form3_facility_safety_inspection.xlsx` | 10 (markers / coordinates already designated) | 180 | 323 XML tags · 34 printed text blocks · 40 merged cells/ranges · 350 border definitions · 9 package parts | 180 ms | 21/21 (PASS) | 12 checklist items |
| form4 `form4_unmarked_blank.docx` | 7 (locate 11 empty cells, decide office vs applicant) | 113 | 503 XML tags · 38 printed text blocks · 24 merged cells/ranges · 10 border definitions · 18 package parts | 102 ms | 21/21 (PASS) | 9 checklist items |
| form5 `form5_unmarked_blank.xlsx` | 5 (locate 6 empty cells, decide office vs applicant) | 52 | 282 XML tags · 30 printed text blocks · 34 merged cells/ranges · 269 border definitions · 9 package parts | 145 ms | 21/21 (PASS) | 7 checklist items |

Across the five forms, Form-Guard wrote 40 values (844 characters) into 41 designated fields and compared 2,119 XML
tags, 171 printed text blocks, 124 merged cells/ranges and 650 border definitions, in 0.1–0.2 s per form. What is left
for the person is the 51-item sign-off checklist (each value next to its verbatim source) and the one fact the memo
does not contain (法人番号 in form2), which Form-Guard leaves empty instead of guessing.

## 4. Safety by design

- Discovery never fills anything: the human picks the cells, office-only cells (事務局・受付印・受付番号・審査欄・決裁・検印)
  are refused, and the confirmed slot spec is bound to the SHA-256 of the blank form — a changed form is refused before
  any fill.
- Coordinate-designated cells are checked strictly: a Word cell may gain one run with one `<w:t>` and the blank cell's
  paragraph-mark formatting (no drawing, field, hyperlink or table); an Excel cell may only become an inline string
  with its style id kept, and reverting the designated cells must give back the blank sheet byte for byte.
- The approval in the web app is bound to the revision on screen and to the SHA-256 of the verified bytes; approve and
  download refuse a stale hash or revision and a file that changed on disk.
- All sample forms, memos, names and organisations were written from scratch during the hackathon and are fictional
  and non-medical.

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
| **Skills** | `.claude/skills/form-scan/`・`surgical-fill/`・`structural-audit/`（各 `SKILL.md`） | 様式のスキャン（マーカーなし様式では空欄候補の発見）、メモの原文引用に基づく記入、21 項目＋VIS-01 の検査と人による最終確認の手順。 | 毎回同じ手順（コマンド、報告すること、してはいけないこと）はスキルにまとめ、必要なときだけ読み込ませます。 |
| **サブエージェント** | `.claude/agents/memo-extractor.md`（Read・Grep・Glob／sonnet）、`.claude/agents/form-auditor.md`（Read・Grep・Glob・Bash／opus） | `memo-extractor` はメモを読み、欄ごとに値・原文引用・行番号か `not_found` を返します。`form-auditor` は記入済みファイルを `python -m form_guard.cli --verify` で検査し直し、不合格の理由を説明します。 | 役割の分離です。抽出役は書き込みができず、監査役は自分が作っていないファイルを新しい文脈で検査します。慎重な抽出は Sonnet、批判的な検査は Opus に任せます。 |
| **Hooks** | `.claude/settings.json`（`Edit\|Write` 後の `PostToolUse`） | 編集のたびに 1 行の `python -c "…"` が Python 構文・JSON の妥当性を確認し、API キーらしき文字列、API キーの直書き、ローカルのユーザーフォルダのパス、例示用ドメイン以外のメールアドレスを検出して Claude に返します（終了コード 2）。 | 忘れてはいけない衛生チェックは、記憶ではなく仕組みで強制します。シェル依存の文字を使わないので bash/sh と cmd.exe の両方で動き、Python が無い環境では実行されないだけで編集を妨げません。`bypassPermissions` は使いません。 |
| **MCP サーバー** | `.mcp.json` → `python -m form_guard.mcp_server`（`form_guard/mcp_server.py`） | stdio の JSON-RPC 2.0 で 4 つのツール（`form_guard_scan`・`form_guard_discover_blank_cells`・`form_guard_fill`・`form_guard_verify`）を JSON Schema 付きで提供します。改行区切りと Content-Length 形式の両方に対応し、追加の依存はありません（`--fastmcp` で公式 `mcp` SDK も利用可）。 | Claude Code などの MCP クライアントが決定論的な検証エンジンを直接呼び、構造化された結果を受け取れます。`form_guard_fill` が書くのは**未承認の下書き**（`UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF`）だけで、担当者入力値の送信、事務局欄の確定、プロジェクト外のパスは受け付けません。 |

### 2. アプリ内のモデルの使い分け

ライブモードで `--model-tier auto`（既定）のとき、作業ごとにモデルを選びます。

| 作業 | モデル | 理由 |
|---|---|---|
| 定型の欄（和暦日付・期間・桁数・書式・選択肢） | `claude-haiku-5-5` | 短く検証しやすい値で、形式と引用は検証器が必ず確認します。 |
| 空欄候補のラベル分類（発見の第 2 段階） | `claude-haiku-5-5` | ラベルは短く、判定は常に安全側（Claude は欄を「記入しない」側にしか変えられません）。 |
| 通常の文章・金額・氏名 | `claude-sonnet-5-5` | 読解は必要ですが、要約はいりません。 |
| 字数制限を超える長文の要約 | `claude-opus-5-5` | 条件を落とさずに削除だけで縮める、最も難しい作業です。 |
| 検証器に差し戻された後の自己修正 | `claude-opus-5-5` | その欄を提案した会話を、検証結果を添えて続けます。 |

`--model-tier opus|sonnet|haiku` で 1 つのモデルに固定でき、`--model` や `FORM_GUARD_MODEL` で任意のモデルを指定できます。
adaptive thinking（`display: "summarized"`）と effort は対応モデルにだけ送り、`claude-haiku-4-5` などの旧モデルには
送らないので、パラメータ違いの 400 エラーは起きません。どの欄をどのモデルが担当したか（理由つき）、実際に応答したモデル、
API が返した思考の要約（「API-returned thinking summary」と明記）は監査ログに記録され、HTML レポートにも表示されます。

### 3. 作業量の比較（計測）

2026-11-02（月）に Windows 11・Python 3.13 で、オフラインのデモエンジン（ネットワークなし）を使って計測しました。
ライブモードでは API の応答時間が加わります（表には含みません）。左側の列は、架空の各様式を人が手作業で処理する場合の
作業量を同じスクリプトでファイルから数えたもので、人の作業時間を計ったものではありません。右側の列が Form-Guard の
計測値です。`python docs/measure_workflow.py` で再現できます（表は英語版と同じ）。

5 つの様式で、Form-Guard は 41 の指定欄に 40 件（844 文字）を記入し、XML タグ 2,119 個・定型文 171 か所・結合セル 124・
罫線定義 650 を 1 様式あたり 0.1〜0.2 秒で照合しました。人に残るのは、各値を原文と並べて確認する計 51 項目の最終確認と、
メモに無い事実（様式 2 の法人番号）の入力だけです。Form-Guard はその欄を推測で埋めません。

### 4. 安全のための設計

- 空欄の発見は記入を一切しません。記入する欄は人が選び、事務局欄（事務局・受付印・受付番号・審査欄・決裁・検印）は拒否し、
  確定した欄の定義は白紙様式の SHA-256 に結び付けます。様式が変わっていれば記入前に止めます。
- 座標で指定した欄は厳しく検査します。Word の欄に追加できるのは、元の段落記号の書式を持つ 1 つの run と 1 つの `<w:t>` だけ
  （図・フィールド・ハイパーリンク・表は不可）、Excel の欄は書式番号 `s` を保ったインライン文字列だけで、指定欄を元に戻すと
  白紙のシートとバイト単位で一致しなければなりません。
- Web 画面の承認は、画面上の版（revision）と検査済みファイルの SHA-256 に結び付けます。古いハッシュや版、ディスク上で
  変更されたファイルは、承認もダウンロードも拒否します。
- サンプルの様式・メモ・人名・組織は、すべて本ハッカソン期間中にゼロから自作した架空のもので、医療とは無関係です。
