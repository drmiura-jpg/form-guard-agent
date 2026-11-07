# CLAUDE.md — Form-Guard Agent Project Guide

This file defines the project goal, non-negotiable safety boundaries, repository architecture, and verification commands for Claude Code when working in `form-guard-agent`.
本ファイルは、`form-guard-agent` におけるプロジェクトのゴール、絶対境界条件5原則、同梱の Claude Code 拡張構成、および検証・実行コマンドを定義します。

## 1. Project Goal & Non-Negotiable Boundaries / プロジェクトのゴールと絶対境界条件

- **Goal (ゴール)**: Fill only the designated fields of official Word (`.docx`), Excel (`.xlsx`), and fixed PDF (`.pdf`) forms from free-text memos, verify untouched ZIP entries byte-for-byte and designated-field edits with 21 deterministic structural/semantic checks plus built-in visual comparison (`VIS-01`), and keep a named human reviewer in the loop for final display/print and content sign-off.
  - 指定された欄だけをメモの原文根拠から外科的に差し込み、未変更ZIPエントリの生バイト保持と指定欄以外の構造・罫線・結合セル・定型文を21項目の決定論的検査と内蔵描画による視覚比較（`VIS-01`）で確認し、Word／Excel等での最終表示・印刷確認と内容承認は人間が行う。
- **Non-Negotiable Invariants (絶対に破らない5原則)**:
  1. **Blank templates are immutable (`samples/blank_forms/` 白紙原本の変更禁止)**: Never edit, overwrite, move, or delete `samples/blank_forms/*.docx|*.xlsx|*.pdf` during normal work. Only the deterministic sample generator `python -m form_guard.cli --generate-samples` (the one command the hook exempts) or a human starting Claude Code with `FORM_GUARD_ALLOW_TEMPLATE_WRITE=1` may rebuild them. The `PreToolUse` hook (`scripts/hook_guard.py`) is a best-effort guard on recognizable direct operations (it does not enforce full OS sandboxing and exits 0 on internal hook errors); `--verify`'s byte-level comparison is the backstop.
     - 通常作業では `samples/blank_forms/` 配下の白紙様式（`.docx`・`.xlsx`・`.pdf`）の変更・上書き・移動・削除を禁止する。`PreToolUse` フックは認識可能な直接操作を止める補助ガードであり（サンプル生成コマンド `--generate-samples` と人間による `FORM_GUARD_ALLOW_TEMPLATE_WRITE=1` 起動のみ例外、フック内部エラー時は処理を止めない）、最終的な不変性は `--verify` のバイト照合で裏付ける。
  2. **Zero fabrication / Surface unknowns (`needs_human_input` と警告による推測拒否)**: Never guess facts missing from the memo (*unknown knowns*, e.g., `法人番号` in `form2`) or silently bypass printed form constraints (*unknown unknowns*, e.g., a checkbox option requiring an entry date or a 30-day advance-notice rule). Fields lacking evidence or failing blocking validation remain blank as `needs_human_input`. Advance-notice deadline rules instead retain the memo's stated date and add a warning (`warnings`) for human review (the field and report may remain `filled` and `PASS`).
     - メモに書かれていない事実（未知の既知：例 `form2` の `法人番号`）や様式の印刷ルール（未知の未知：追加入力必須のチェックボックスや30日前届出規定など）を推測で埋めない。根拠不足や解消できない検証エラーは空欄のまま `needs_human_input` とし、30日前届出などの日付間ルールはメモの値を保持して警告（`warnings`）を出し人間の確認に委ねる（警告のみの場合は `filled`／`PASS` のままになり得る）。
  3. **Office-only lock (`事務局専用欄の除外`)**: Never fill or allow designation of office-only cells (`事務局・受付印・受付番号・審査欄・決裁・検印`).
     - 事務局専用欄（`事務局・受付印・受付番号・審査欄・決裁・検印`）への記入や記入対象指定は行わない。
  4. **Human sign-off only (`承認はWeb画面の人間のみ`)**: MCP `form_guard_fill` writes only unapproved drafts (`UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF`) under `output/mcp_runs/`; CLI output under `output/` is a working copy with its report, not an approved deliverable. MCP exposes no approval tool, and agents (and subagents) must never invoke the web app's approval or download endpoints (`POST /api/runs/<run_id>/approve`) or operate the UI on a person's behalf. A human reviewer performs sign-off in the web UI, where the server checks the checklist, `revision`, and `filled_sha256`.
     - MCP の `form_guard_fill` は `output/mcp_runs/` に未承認下書き（`UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF`）のみを出力し、CLI の `output/` 出力は作業用コピーである。MCP に承認ツールは無く、エージェントやサブエージェントが承認・ダウンロード用エンドポイントを直接呼ぶこと（代理承認）は禁止とする。最終承認は Web 画面で人間が行い、サーバーが確認項目・`revision`・`filled_sha256` を照合する。
  5. **Repository hygiene (`秘密情報・個人情報の混入防止`)**: Never write Anthropic API keys, personal user-profile paths, or e-mail addresses outside the reserved example domains (`example.com`, `example.jp`, `example.org`, `users.noreply.github.com`), plus exactly `noreply@anthropic.com` (commit trailer).
     - APIキー、個人のユーザープロファイルパス、および例示用ドメイン（`example.com`・`example.jp`・`example.org`・`users.noreply.github.com`）と `noreply@anthropic.com`（完全一致）以外のメールアドレスをリポジトリ内のファイルに書き込まない。

## 2. Claude Code Extensibility Suite / 同梱の Claude Code 拡張構成

- **Skills & Slash Command (`.claude/skills/*/SKILL.md` — 6 skills & `.claude/commands/fill-form.md` / スキル6種＆スラッシュコマンド)**:
  - `form-scan`, `surgical-fill`, `structural-audit`, `form-fill-verify`, `visual-diff-audit`, `portal-form-fill`, plus `/fill-form <form> <memo>` (`.claude/commands/fill-form.md`)
  - 様式スキャン・空欄発見、原文引用に基づく外科的差込、21項目＋`VIS-01`構造監査、一括記入・検証、視覚差分監査、公募ポータル（`/portal`）連携の定型手順と、1行で一気通貫実行する `/fill-form` コマンドを提供する。
- **Subagents (`.claude/agents/*.md` — 4 subagents without `Edit`/`Write` tools; `memo-extractor` has only `Read, Grep, Glob`, the other three also have `Bash` for verification commands and are not technically read-only — their `Bash` is guided by prompt instructions and checked by the `PreToolUse` hook, which stops a shell write to `samples/blank_forms/`)**:
  - `memo-extractor` (`sonnet` · `Read, Grep, Glob`): Extracts verbatim memo quotes blind before seeing form slots.（欄を見る前にメモの事実を原文引用で抽出・`Read, Grep, Glob` のみ）
  - `field-mapper` (`claude-opus-5-5` · `Read, Glob, Grep, Bash`): Proposes slot values under printed form rules.（印刷ルールに沿って欄の値を提案）
  - `structure-auditor` (`sonnet` · `Read, Glob, Grep, Bash`): Runs `--verify`, ZIP entry hash checks, and `audit_log.json` cross-checks.（`--verify`・ZIPハッシュ照合・監査ログ突合を実行）
  - `form-auditor` (`opus` · `Read, Grep, Glob, Bash`): Performs adversarial review against the memo and lists human confirmation items.（敵対的再検証と人間確認項目の洗い出し）
  - ※4役とも `Edit`/`Write` ツールを持たない。`memo-extractor` 以外の3役が持つ `Bash` は読み取り専用の検証コマンド用という運用指示であり、任意のシェル書き込みを技術的に全面禁止する権限ではない（`samples/blank_forms/` への直接書き込みは `PreToolUse` フックが阻止する）。
- **Hooks (`.claude/settings.json` → `scripts/hook_guard.py` / 2段階フック)**:
  - `PreToolUse` (`Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell`): Blocks recognizable blank-template mutations; scans `Write|Edit|MultiEdit|NotebookEdit` payloads for API key patterns, personal paths, and non-example email addresses, and scans `Bash|PowerShell` commands for API key patterns only (`secrets_only=True`) (exit code 2 + `permissionDecision: "deny"`).
  - `PostToolUse` (`Write|Edit|MultiEdit`): Runs `py_compile`, module import smoke test, JSON validation, and hygiene re-scan on written files (`decision: "block"` on findings).
  - 実行前（`PreToolUse`）は白紙様式の変更阻止に加え、ファイル編集ツールでは APIキー・個人パス・非例示メールを検査し、シェルコマンドでは APIキーパターンのみを検査する。書き込み後（`PostToolUse`、対象は `Write|Edit|MultiEdit`）は Python 構文・import・JSON・衛生再検査を行う。
- **MCP Server (`.mcp.json` → `python -m form_guard.mcp_server` / MCPサーバー)**:
  - 5 stdio JSON-RPC 2.0 tools: `form_guard_list_demos`, `form_guard_scan`, `form_guard_discover_blank_cells`, `form_guard_fill`, `form_guard_verify`.
  - 標準入出力 JSON-RPC 2.0 で上記5つのツールを提供する（既定は `--mode demo`）。

## 3. Architecture & Model Orchestration / アーキテクチャとモデル制御

- **3-tier model routing (`form_guard/mapper.py` / 3階層モデル振り分け)**:
  - `claude-haiku-4-5-20251001` (structured fields & blank-cell label classification) → `claude-sonnet-5` (standard text, amounts, names) → `claude-opus-5-5` (long-text deletion-only condensation when the memo passage exceeds the limit or no matching passage is located and the limit is at least 150 chars, plus validator-rejected self-correction), with automatic `404` fallback along `MODEL_FALLBACK_CHAIN`.
  - 定型欄・空欄ラベル分類は `claude-haiku-4-5-20251001`、通常文・金額・氏名は `claude-sonnet-5`、長文要約（対応メモが字数上限を超えるか、対応箇所が特定できず上限150字以上の場合）および検証差し戻し後の自己修正は `claude-opus-5-5` へ振り分ける。
- **Why local deterministic file surgery + Messages API + MCP (vs. remote cloud container execution)**:
  - `--mode demo` makes zero API calls from the Form-Guard engine; it is the MCP server's default (this describes Form-Guard's own engine and does not restrict Claude Code's own connection). The CLI default `--mode auto` and the web UI call the Messages API only when an Anthropic API key is available (`ANTHROPIC_API_KEY` or the git-ignored `.env`); otherwise they run the same offline engine — pass `--mode demo` to guarantee zero Form-Guard API calls. In `live` mode, Form-Guard sends text payloads (the full memo text, form name, target slot definitions / printed rules, and blank-cell classification candidates) to the Messages API; the binary form files (`.docx` / `.xlsx` / `.pdf`) stay on the local disk (the web UI listens only on `127.0.0.1`), where the surgical writer (`form_guard/writer.py`) copies untouched ZIP entries byte-for-byte, the verifier (`form_guard/verifier.py`) runs the 21 structural/semantic checks (plus `VIS-01`: layout drift or overflow is appended as a failed check), and the human reviewer signs off against the exact local `filled_sha256` and `revision`.
  - `--mode demo`（MCPサーバーの既定）では Form-Guard エンジンからの外部API通信は行わない（Claude Code 本体の通信を制御するものではない）。CLI の既定 `--mode auto` と Web UI は APIキー（`ANTHROPIC_API_KEY` または `.env`）があるときのみ Claude API を呼び、無ければオフラインエンジンで動くため、通信ゼロを強制したい場合は `--mode demo` を指定する。`live` モードではメモ全文・様式名・欄定義・空欄分類候補などのテキストを Messages API に送る一方、様式のバイナリファイル（`.docx`・`.xlsx`・`.pdf`）自体はローカルディスク上に留まり（Web UI は `127.0.0.1` のみで待受）、未変更ZIPエントリの生バイト保持・21項目検査（＋レイアウトずれ・はみ出し検出時の `VIS-01`）・`filled_sha256` と `revision` に紐づく人間承認をローカルで完結させる。

## 4. Verification & Development Commands / 検証・実行コマンド

```bash
# Run the full automated test suite (works offline without an API key)
# 自動テスト全277件を実行（APIキー不要・オフラインで動作）
python -m pytest -q

# Run the 3 marked fictional demos (form1–form3) offline -> output/index.html
# マーカーあり架空デモ3件（form1〜form3）をオフライン実行（output/form1/ 等を生成）
python -m form_guard.cli --all-demos --mode demo

# Verify a filled copy against its blank template (ORIGINAL and FILLED are positional; run --all-demos first)
# 白紙原本と記入済みファイルを検証（ORIGINAL と FILLED は位置引数2つ・先に --all-demos を実行）
python -m form_guard.cli --verify samples/blank_forms/form1_research_plan_change.docx output/form1/filled_form1_research_plan_change.docx --memo samples/memos/memo1_research_change.txt

# Launch the local web UI (http://127.0.0.1:8000 — all 6 demos + /portal)
# ローカル Web UI を起動（全6デモ＋公募ポータル /portal）
python -m form_guard.cli --serve
```

See [README.md](README.md) and [docs/BUILT_WITH_CLAUDE_CODE.md](docs/BUILT_WITH_CLAUDE_CODE.md) for full details.
詳細は [README.md](README.md) および [docs/BUILT_WITH_CLAUDE_CODE.md](docs/BUILT_WITH_CLAUDE_CODE.md) を参照してください。
