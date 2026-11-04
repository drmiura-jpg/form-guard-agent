# Built with Claude: Japan 2026 — 提出フォーム記入文案

作成日：2026-11-03（火）／更新日：2026-11-04（水）
公式提出締切：**2026-11-08（日）20:00 JST**（提出後も 2026-11-08（日）20:00 JST より前ならプラットフォーム上で更新可、20:00 JST 以降は変更不可）
※本ファイルはイベント提出フォーム各欄への記入用テキスト案（文字数別バリエーション）をまとめた補助資料です。プロジェクトの仕様・使い方の正本は [README.md](../README.md) および [BUILT_WITH_CLAUDE_CODE.md](BUILT_WITH_CLAUDE_CODE.md) をご参照ください。

---

## 1. 基本情報（プロジェクト名・テーマ・一言ピッチ・リンク）

### ① プロジェクト名（Project Name）

- **標準表記（短）**：
  `Form-Guard Agent`
- **副題つき表記（欄に余裕がある場合）**：
  `Form-Guard Agent — 元の様式は一切壊さず、指定欄だけをピンポイント記入して21項目で自動検査する書類記入エージェント`
- **英語副題つき表記**：
  `Form-Guard Agent — Pinpoint Designated-Field Form Filler & 21-Check Layout Verifier`

### ② 選択したテーマ（Selected Theme）

- **単一選択の場合の第1推奨**：
  **`テーマ1：あなたにしかない深さを。`**（主選択。テーマ2の観点は選択理由の末尾で補足）
  - **選択理由（フォームに理由欄がある場合）**：
    研究計画書や申請書など、指定様式を1字も崩せない書類を実務で扱ってきた当事者として、生成AIを書類業務に使いにくい最大の壁は「AIの文章が間違うこと」以上に、**「文書全体を作り直す方法では、指定様式の罫線・結合セル・計算式・定型文が変わってしまうことがあり、どこが変わったのか誰にも分からないためそのまま提出できないこと」**にあると痛感してきました。「『そういうものだ』と誰もが諦めて手作業で転記・目視確認してきた日々の不便」を、元の様式は一切触らずに指定欄の文字（バイト範囲）だけをピンポイントで書き換え、指定欄以外が変わっていないことを21項目の自動検査で確かめる仕組みに変えました（あわせて、各値の原文引用表示・事務局欄ロック・人間承認ゲートにより、テーマ2が挙げる専門知識や手続きの壁を越えて、エンジニアでない担当者もAIを盲信せずに使える仕組みにしています）。
- **複数選択可、またはテーマ2を選ぶ場合の文案**：
  **`テーマ2：本来、誰にでも届くべきものを。`**
  - **選択理由**：
    補助金申請や行政・研究手続きの現場では、複雑な指定様式や文字数・和暦・上限額の細かな規定が壁となり、専門スタッフやエンジニアがいない中小事業者・現場担当者の負担になっています。Form-Guard Agent は、AIを盲信しなくても安心して使えるよう、AIの抽出値には元メモの原文引用ハイライトを添え、年の補完は推定フラグを付けて人が確認し、事務局専用欄への誤記入をロックし、メモに無い事実は推測で埋めず人の入力待ち（`needs_human_input`）に残し、人が最終確認するまでダウンロードを解錠しない設計にしました。

### ③ 一言ピッチ / エレベーターピッチ（Short Description / Tagline）

- **日本語（50字以内・48字）**：
  `Word・Excel・固定PDFの元の様式を壊さず指定欄だけを記入し、21項目で自動検査するAI`
- **日本語（100字以内・99字）**：
  `「様式を変えないで」が鉄則の日本の申請業務で、Word・Excel・固定PDFの元の様式は一切触らず指定欄だけを原文根拠つきでピンポイント記入し、欄外の書式不変を21項目検査と視覚比較で確かめるAI`
- **日本語（140字以内・137字）**：
  `研究費や行政申請のWord・Excel・固定PDF様式の指定欄だけをバイト単位で記入。Haiku/Sonnet/Opus 5.5の3階層で原文引用つきの値を作り、21項目の構造検査で欄外の不変を確認。根拠の無い値は推測で埋めず、人の承認までWebダウンロードをロックします。`
- **英語（120 chars / 116 chars）**：
  `Pinpoint Word/Excel/PDF form filler with source quotes, 21 structural checks, 0 px non-target drift, and human gate.`
- **英語（240 chars / 224 chars）**：
  `Rewrites only designated fields in official Word, Excel, and fixed-PDF forms while checking for zero layout drift outside target cells via 21 deterministic checks, source grounding, 3-tier Claude routing, and human sign-off.`

### ④ リンク欄と提出前チェック

- **公開 GitHub リポジトリ URL**：
  `https://github.com/drmiura-jpg/form-guard-agent`
- **3分間のデモ動画 URL**：
  `＜収録・アップロード後に YouTube（公開 または 限定公開 Unlisted）または Loom の URL を記入＞`
  - **動画公開設定の確認項目**：
    - [ ] YouTube の場合は「**限定公開（Unlisted）**」または「**公開（Public）**」に設定した（「非公開（Private）」は再生不可）。
    - [ ] Loom の場合は「**Anyone with the link can view**」に設定した。
    - [ ] シークレットウィンドウ（未ログイン状態）で URL を開き、最初から最後まで音声つきで再生できることを確かめた。
    - [ ] 動画の長さが **3分（180秒）以内**（台本設計は 175秒＝2分55秒）であることを確かめた。
    - [x] 合成音声・外部素材のクレジットを `README.md` および `docs/DEMO_VIDEO_SCRIPT_JA.md` に明記した（デモ動画の日本語ナレーション音声は作成者本人の承諾のもと本人の声から ElevenLabs `https://elevenlabs.io/` の `eleven_v4` で生成、外部BGM・効果音は不使用）。

---

## 2. プロジェクト概要：解決する課題と、誰のためのものか
（Project Overview: Problem, Target Users, Solution & Post-Hackathon Use — 審査基準「インパクト 30%」「深さと実行力 20%」対応）

### A. 日本語・標準版（箇条書き・太字・バッククォート・改行除き 1,702字／改行除き 1,784字／そのまま 1,796字）

**【開発者自身の実体験と、誰の・どんな課題か（日本の現場の痛みから出発した課題）】**
- 開発者自身、倫理申請などの書類作成で何度も差し戻しに遭い、非常に困った実体験があります。AIで試してもうまくいかず、「もうAIを使うのはやめようか」とまで思いましたが、「元の様式を壊さずに指定欄だけを埋められる仕組みをうまく作っていけば、申請業務がもっと楽になるはずだ」という思いから本プロダクトを開発しました。
- 日本の大学・研究機関、自治体窓口、施設管理の現場でも、「様式第1号」などの公式な Word（`.docx`）・Excel（`.xlsx`）・固定レイアウト PDF（`.pdf`）が指定され、**「元の書式・罫線・結合セル・計算式・定型文を変えないこと」**が求められます。罫線1本、結合セル1つ、Excel の `SUM` / `COUNTIF` 計算式1つの崩れが差し戻し（押印や決裁の取り直し・再提出の手戻り）の原因になることがあります。
- しかし、文書全体を作り直す方法では、罫線や結合セル、文言が変わってしまうことがあり、「どこが変わったのか」「どの数字がAIの推測なのか」を確かめるだけで手作業以上の時間がかかるため、現場の事務担当者や申請者が安全に使うには壁がありました。

**【どう解決するか（Form-Guard Agent のアプローチ）】**
- Form-Guard Agent は「AIに書類全体を作らせる」のではなく、**「元の白紙様式は一切触らずに指定欄の文字（バイト範囲）だけをピンポイントで書き換え、それ以外が変わっていないことを決められたルールで自動検査する」**エージェントです。
- **1. 指定欄だけのピンポイント書き換えと21項目＋視覚Diff検査（`VIS-01`）**：対象テキストノードのバイト範囲（固定PDFは増分更新オーバーレイ）だけを書き換え、他のZIPエントリは再圧縮すら行わず生バイト複製します。記入後に7区分・21項目の構造検査と視覚検査（`VIS-01`：Word／Excelは内蔵描画エンジンによる配置比較、対応する固定PDFは指定枠外の画素比較。`0 px` は各比較での測定値であり、任意文書の実機表示・印刷の完全一致保証ではなく最終確認は人が行います）を実行し、付属5様式については実機 Microsoft Word 16.0・Excel 16.0 でも警告ゼロ・ページ数不変・計算式一致を確認済みです。
- **2. マーカーなし白紙様式の4段階・空欄自動発見**：`{{マーカー}}` が無い白紙様式（デモ4 Word・デモ5 Excel・デモ6 固定PDF）でも、申請者記入欄（`applicant_fillable`）と事務局専用欄（`office_only`：受付番号・審査欄等）を自動仕分けし、事務局欄や計算式セルへの誤記入をロックします。
- **3. 根拠付き抽出・不明点の明示と人間承認ロック**：AIの抽出値には元メモの原文引用スパンを紐づけ、年の補完は推定フラグ（`year_inferred`）を付けて人が確認し、メモに無い事実（デモ2の法人番号）は推測で埋めず `needs_human_input`（人の追加入力は本人確認記録 `human attestation`）として残します。アプリ側の字数超過修復処理では、検知した条件語の削除を自動保留（`unsafe_condensation`）して削除文を全文表示し、モデルが最初から字数内で要約した文章も含め、省略の妥当性は人が原文全体と照合します。確認者が全項目を確認して承認するまで、ダウンロードはサーバー側でロックされます。

**【ハッカソン後の継続利用と導入条件】**
- Web UI・CLI・Claude Code MCP サーバー（`.mcp.json`）の3形態で動作し、APIキーがない端末でもルール式のオフラインデモで再現できます。実務導入時は様式ごとの対象欄設定と実機表示確認を行い、固定PDFはページ内に Unicode の CID フォントを持つ PDF を対象とします（Identity-H やスキャン画像PDF等は安全に拒否。詳細は `README.md` の Limitations 参照）。

---

### B. 日本語・短縮版（文字数制限が厳しい欄向け・箇条書き・バッククォート・改行除き 524字／改行除き 532字／そのまま 534字）

- 開発者自身も倫理申請などで何度も差し戻されて困り、AIで試してもうまくいかず「もうAIを使うのはやめようか」と思った実体験から開発しました。日本の大学・自治体・施設の申請業務では、指定された Word・Excel・固定PDF 様式の罫線や結合セル、計算式が崩れると差し戻し（押印・決裁の取り直しや再提出の手戻り）の原因になり、文書全体を作り直す方法ではどこが変わったか分からず安全に使えませんでした。
- Form-Guard Agent は、元の様式は一切触らずに指定欄だけをピンポイントで書き換え（対応する固定PDFは元バイト不変の増分更新）、7区分・21項目の構造検査と視覚検査（Word/Excelの内蔵配置比較・固定PDFの欄外画素比較でずれ `0 px`、最終的な実機表示は人が確認）で欄外の不変を確かめます。
- マーカーの無い白紙様式でも申請者欄と事務局専用欄を4段階で自動仕分けして誤記入をロックし、AI抽出値に原文引用ハイライトを付与（年の補完は推定表示）。メモに無い事実は推測で埋めず人の入力待ちに残し、人が最終確認するまでダウンロードを解錠しません。Web UI・CLI・Claude Code MCP で現場の定型様式業務に活用できます。

---

### C. 英語版（English Overview — 箇条書き・太字・斜体・バッククォート・改行除き 2,378 chars／改行除き 2,448 chars／そのまま 2,457 chars）

**Problem & Target Users (Born from Firsthand Experience):**
- I repeatedly experienced frustrating administrative rejections (差し戻し) on ethics review and institutional forms; when I tried using AI to help, it did not work as I had hoped, to the point where I almost gave up on using AI for paperwork altogether. Form-Guard Agent was born from that firsthand frustration and the conviction that preserving the original template while filling only designated fields can genuinely make application workflows much easier.
- In Japanese universities, municipal offices, and facility management, official Word (`.docx`), Excel (`.xlsx`), and fixed-layout PDF (`.pdf`) forms come with a strict rule: *do not alter the original layout, borders, merged cells, formulas, or printed clauses*.
- One broken border or altered clause can trigger administrative rejection (差し戻し), requiring internal seals/approvals to be re-obtained and the form to be re-submitted. Regenerating a complete form can alter borders, merged cells, or formulas and introduce unstated numbers, making it unsafe for non-engineer staff. (The comparison file in the demo uses scripted simulated regeneration failures to illustrate these structural checks.)

**Solution:**
- Form-Guard Agent performs **byte-range pinpoint replacement** only on designated fields (and byte-preserving incremental overlay on supported fixed PDFs with embedded Unicode CID fonts; see `README.md` Limitations), backed by **21 deterministic structural checks + `VIS-01` visual verification**:
- **1. Visual & Structural Checks:** Visual checks compare built-in layout geometry for Word/Excel and pixels outside designated boxes for supported PDFs (`0 px` drift in the built-in checks; native display and printing require final human review, and production adoption requires per-template field and display verification).
- **2. 4-Stage Blank-Cell Discovery** on unmarked forms (`form4` Word, `form5` Excel, `form6` PDF), separating `applicant_fillable` cells from locked `office_only` cells and Excel formulas.
- **3. Verbatim Source Grounding (no guessed facts) & Human Gate:** AI-extracted values carry source quotes; inferred years are flagged for review (`year_inferred`), unstated facts stay `needs_human_input`, and reviewer-entered values are recorded as human attestations. The application holds unsafe deletions during its own length repair, while pre-condensed model output is reviewed against the full source.

---

## 3. Claude および Claude Code の活用に関する質問
（How You Used Claude & Claude Code — 審査基準「Claudeの活用 25%」対応）

### A. 日本語・標準版（箇条書き・太字・バッククォート・改行除き 2,232字／改行除き 2,420字／そのまま 2,437字）

**1. 欄の性質に応じた Claude 3モデルの目的別ルーティングと自己修正設計（アプリ中核）**
1つの様式の中でも欄ごとに求められる能力が異なるため、`--model-tier auto` で3階層モデルを自動振り分けしています：
- **`claude-haiku-4-5-20251001`**：日付（和暦変換・曜日整合）・識別番号・定型選択肢の分類と、マーカーなし白紙様式の空欄候補分類。
- **`claude-sonnet-5`**：自由文メモからの原文引用スパン（`quote`）つき値抽出・金額の正規化。
- **`claude-opus-5-5`（adaptive thinking `display: "summarized"`）**：200字以内の要約案の作成と、検証器で解消できない欄の再提案。印刷された上限違反（例：デモ2で上限100万円を超過した申請額 `1,612,000円` → `1,000,000円`）はまず原文候補の決定論的再抽出で修正し、解消できない場合にモデルへ再提案を求めます（オフラインデモとモックAPIテストで確認済み。提示のライブAPI実機疎通 `run_id: 20261103-164113-f83bad` では、`form1` に対する3モデルの初回提案ルーティングと `21/21 PASS`・差し戻し0件を確認しました）。
さらに、API環境ごとのモデル利用可否に対応する `MODEL_FALLBACK_CHAIN` と、プロンプトキャッシュ（`cache_creation_input_tokens` / `cache_read_input_tokens`）および思考要約（`thinking_summaries`）の監査ログ・Web画面表示を実装しています。

**2. Claude Code を開発の中心に据えた4つの拡張資産（`.claude/` ＆ `.mcp.json`）**
「白紙様式を壊さない」という安全設計を、開発プロセス自体にも Claude Code の機能で組み込みました：
- **① 2段階フック（`scripts/hook_guard.py`・`.claude/settings.json`）**：
  - `PreToolUse`（書き込み前検査）：設定された対象ツール呼び出しを検査し、`samples/blank_forms/` の白紙様式（`.docx`/`.xlsx`/`.pdf`）への検知可能な書き込み・削除・上書きや、ファイル編集ペイロードへのAPIキー・個人パス・例示用以外のメールアドレス混入（シェルコマンドはAPIキーパターンを検査）を、実行前に終了コード `2`＋`permissionDecision: "deny"` で遮断。
  - `PostToolUse`（書き込み後検査）：`Write`/`Edit`/`MultiEdit` によるファイル編集直後に `py_compile`・`.venv` での import スモークテスト（1.8秒予算内）・JSON 構文＆BOM 検査・衛生検査を自動実行し、違反時は `decision: "block"` で修正を求めます（フック内部の予期しない例外時は作業を止めない fail-open 設計）。
- **② ファイル編集ツール（`Edit`/`Write`）を持たない4種のサブエージェント（`.claude/agents/`）**：
  様式スキーマを見る前にメモから原文スパンを抜く `memo-extractor`（Sonnet・`Read, Grep, Glob` のみ）、規則を踏まえて値を提案する `field-mapper`（`claude-opus-5-5`）、構造検査を担う `structure-auditor`（Sonnet）、独立再検証と最終確認一覧を担う `form-auditor`（Opus）の4役に分担しました（後ろの3種は検証用にフック付き `Bash` を使い、白紙様式への書き込みは `PreToolUse` フックが阻止します）。
- **③ 6種の Skills（`.claude/skills/`）**：
  `form-scan`・`surgical-fill`・`structural-audit`・`form-fill-verify`・`visual-diff-audit`・`portal-form-fill` として定型の手順をまとめました。
- **④ 追加のMCP SDK不要の5ツール MCP サーバー（アプリ本体の依存パッケージは必要；`form_guard/mcp_server.py`・`.mcp.json`）とブラウザ引き継ぎ**：
  `form_guard_list_demos`・`form_guard_scan`・`form_guard_discover_blank_cells`・`form_guard_fill`・`form_guard_verify` を標準入出力 JSON-RPC 2.0 で公開し、**Claude in Chrome** や Claude Code（`portal-form-fill` スキル）から読み取れる模擬公募ポータル（`/portal`）も用意しました。MCPには承認ツールを設けず未承認下書き（`UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF`）と `review_url`（`http://127.0.0.1:8000/?run_id=...`）のみを返し、同じ SHA-256 のまま Web 画面で人が承認する運用です（HTTP API は確認者名・必須チェック項目・版・ハッシュを検査しますが、操作主体が人間かは判定しません）。

---

### B. 英語版（How You Used Claude & Claude Code — 箇条書き・太字・バッククォート・改行除き 1,912 chars／改行除き 2,040 chars／そのまま 2,048 chars）

**1. Purpose-Driven 3-Tier Claude Routing & Self-Correction (`--model-tier auto`):**
- **`claude-haiku-4-5-20251001`** handles date/era normalization, IDs, enum selection, and blank-cell classification.
- **`claude-sonnet-5`** extracts prose, currency amounts, and verbatim source quotes (`quote` spans).
- **`claude-opus-5-5` (with adaptive thinking)** drafts `≤200`-char summaries and handles model re-proposals when needed. Printed-limit violations (e.g., `1,612,000円` → `1,000,000円` in `form2`) first trigger deterministic re-extraction from candidate spans, falling back to model re-proposal if unresolved (verified in offline demos and mock-API tests; the cited live API run `20261103-164113-f83bad` on `form1` verifies 3-model proposal routing with `21/21 PASS`).

**2. Built with Claude Code at the Core (`.claude/` & `.mcp.json`):**
- **2-Stage Guard Hooks (`scripts/hook_guard.py`)**: `PreToolUse` inspects configured tool calls before disk write (`exit 2` + `deny`) for recognizable `samples/blank_forms/` mutations and scans file-edit payloads for API keys, personal paths, and non-example e-mails (and shell commands for key patterns); `PostToolUse` runs on `Write`/`Edit`/`MultiEdit` to check `py_compile`, import smoke tests, and JSON/BOM validation (failing open on internal hook errors).
- **4 Subagents without `Edit`/`Write` tools**: `memo-extractor` (Sonnet, `Read/Grep/Glob` only: blind span extraction), `field-mapper` (`claude-opus-5-5`: rule-aware mapping), `structure-auditor` (Sonnet: structural audit), and `form-auditor` (Opus: independent verification & sign-off checklist); the latter three use hook-guarded `Bash`.
- **6 Skills (`form-scan`, `surgical-fill`, `structural-audit`, `form-fill-verify`, `visual-diff-audit`, `portal-form-fill`) & 5-Tool stdio MCP Server with no additional MCP SDK dependency (application dependencies required; `form_guard/mcp_server.py`)**: MCP omits any approval tool and returns `review_url` (`/?run_id=...`) so a reviewer signs off in the Web UI against the revision and SHA-256 hash.

---

## 4. 審査基準 4区分・14項目 × Form-Guard Agent の定量エビデンス対照表

| 審査基準（配点） | 公式チェック項目（要約） | Form-Guard Agent の実装と定量エビデンス |
| :--- | :--- | :--- |
| **1. インパクト (30%)** | ・日本の具体的な現場から出発しているか<br>・テーマ1「足りないもの」／テーマ2「阻んでいた壁」を捉えているか<br>・誰がどんな場面でどれくらい困っているか具体的か<br>・ハッカソン後も使い続けられるか | ・研究費変更届（Word）・自治体補助金申請（Word）・月次設備点検（Excel）・白紙2様式・固定PDF助成金申請の計6様式で実証<br>・付属Word/Excel 5様式（41欄中40欄記入・848字）をオフラインで計測：XMLタグ 2,119・定型文 171・結合セル 124・罫線 650 を、計測スクリプトの表で 120〜246 ms・Web保存測定で 119〜379 ms（0.4秒未満）、デモ動画収録時の画面実測（`seg18`）で 361〜1,234 ms（約0.4〜1.2秒）で照合（固定PDF・API応答・人の確認時間は含まない。`form1` ライブAPI記録は各段階合計約29.63秒）<br>・Web UI / CLI / `.mcp.json` で継続利用可能（実務導入時は様式ごとの欄指定と表示確認が必要。固定PDFはページ内にUnicode CIDフォントを持つPDFが対象） |
| **2. デモ (25%)** | ・冒頭で「誰の、どんな課題を、どう解決するか」が明確か<br>・前提知識のない人でも実際の操作で価値が分かるか<br>・最後まで見たくなる動画か | ・冒頭35秒（0〜25秒で開発者の実体験・社会的意義と現場課題、25〜35秒の `#tour=1` で比較）において、様式ごと作り直した場合の失敗を再現した比較用の模擬ファイル（結合セル19→0・罫線定義9→0・A4→レター・7項目不合格）と Form-Guard 版（21/21合格・内蔵配置比較のずれ `0 px`）を Before/After スライダーで比較<br>・Web画面上部の5ステップ審査員ガイドツアー（`#tour=1`〜`#tour=5`）と専門用語噛み砕きテロップで非エンジニアにも直感的に伝達 |
| **3. Claudeの活用 (25%)** | ・基本的な使い方を超えた創造的活用か<br>・Claude Code を開発の中心に据えているか<br>・モデルや機能（Skills、サブエージェント、MCP、フック等）を目的に応じて使い分けているか | ・欄別3モデル自動振り分け（Haiku 4.5 / Sonnet 5 / Opus 5.5：ライブAPI `form1` で3モデルの初回提案応答と `21/21 PASS` を確認）＋上限超過等の決定論的再抽出と Opus 自己修正設計（オフラインデモとモックAPIテストで確認）<br>・2段階フック（`PreToolUse` 白紙保護＋`PostToolUse` 構文・import検査）、`Edit`/`Write` 無しの4サブエージェント（うち3種はフック付き `Bash`）、6 Skills、5ツール MCP＋Web承認引き継ぎ |
| **4. 深さと実行力 (20%)** | ・試行錯誤を重ねて磨き上げ、課題にじっくり向き合った跡が見えるか<br>・設計と実装が丁寧か<br>・困っている人が安心してそのまま使える形か | ・展開後 **全272件の `pytest` 合格**、オフラインプログラム **45試行（`45/45 ok`・2026-11-04（水）にコミット `282fb0c` で再検証済み）**、ヘッドレス Edge 実ブラウザ **16試行（`16/16 ok`・コンソールエラー0・2026-11-04（水）にコミット `282fb0c` で再検証済み）**<br>・付属Word/Excel 5様式について実機 Microsoft Word 16.0 / Excel 16.0 での開閉・ページ数・計算式再計算（`COUNTIF` / `SUM`）確認済み |
