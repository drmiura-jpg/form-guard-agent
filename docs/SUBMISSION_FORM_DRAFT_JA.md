# Built with Claude: Japan 2026 — 提出フォーム記入文案

作成日：2026-11-03（火）／更新日：2026-11-08（日）
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
- **英語（120 chars / 115 chars）**：
  `Pinpoint Word/Excel/PDF form filler with source quotes, 21 structural checks, built-in layout diff, and human gate.`
- **英語（240 chars / 224 chars）**：
  `Rewrites only designated fields in official Word, Excel, and fixed-PDF forms while checking for zero layout drift outside target cells via 21 deterministic checks, source grounding, 3-tier Claude routing, and human sign-off.`

### ④ リンク欄と提出前チェック

- **公開 GitHub リポジトリ URL**：
  `https://github.com/drmiura-jpg/form-guard-agent`
- **3分間のデモ動画 URL**：
  `＜アップロード後に Loom（Anyone with the link can view）または提出用の YouTube アカウント（既存の個人活動用チャンネルは流用しない）の限定公開（Unlisted）の URL を記入＞`
  - **公開設定・提出前の確認項目**：
    - [ ] GitHub リポジトリを Public に切り替え、シークレットウィンドウ（未ログイン状態）で `README.md` と `LICENSE` が開けることを確認した（全コミット履歴の秘密情報検査済み）。
    - [ ] デモ動画アップロード後、本ファイルの3箇所（§1-④・§5日本語・§5英語）を同一の閲覧URLに置換した。
    - [ ] Loom の場合は「**Anyone with the link can view**」、YouTube の場合は提出用のアカウント（既存の個人活動用チャンネルは流用しない）の「**限定公開（Unlisted）**」に設定した（投稿前にチャンネル名・アイコン・説明欄・投稿者プロフィールの公開表示を確認。「非公開（Private）」は再生不可）。
    - [ ] シークレットウィンドウ（未ログイン状態）で URL を開き、最初から最後まで音声つきで再生できることを確かめた。
    - [ ] 動画の長さが **3分（180秒）以内**（V6 フルHD提出版 `FormGuard_Agent_Demo_175s_1080p.mp4`：実尺 `175.00秒`・`19.08 MiB / 20.01 MB [20,007,852 bytes]`・SHA-256 `192b369383b4b033fb216dafc0416f5616e9d114ad5a609c00c8e48e50b89f78`、または2026-11-08（日）収録の3枚・170秒生声PowerPoint版［台本 `docs/PRESENTATION_SPEECH_170S_JA.md`・スライド `docs/FormGuard_Agent_Pitch_Deck_170s_3Slides_16x9.pptx` から書き出したMP4。提出時は本行および §5 日本語・英語の実尺・バイト数・SHA-256・閲覧URLを書き出し後のMP4の値に更新］）であることを確かめた。
    - [ ] 映像・スライド・まとめ図（`s5_4_autoadapt_showcase.png`）に映る様式はすべて自作の架空様式（`form1`〜`form10`）のみとし、第三者の外部様式画像は提出動画に含めないことを確認した（外部BGM・効果音は不使用）。

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

### B. 日本語・短縮版（2026-11-08（日）公式指定「500字以内＋冒頭『このプロジェクトは、〔誰〕の〔課題〕を〔どう変える〕〔形〕です。』」完全準拠版・そのまま 421字／改行除き 419字・冒頭一文 148字）

このプロジェクトは、大学・自治体・医療現場などの申請業務で「指定様式の罫線や計算式が崩れると差し戻されるためAIを使えない」と困っている担当者・研究者の課題を、元の白紙様式は一切触らず指定欄だけをピンポイントで書き換え、欄外の不変を自動検査して人が最終承認する形に変える書類記入エージェントです。
開発者自身が倫理申請等で何度も差し戻され「もうAIを使うのはやめようか」と思った実体験から開発しました。7区分・21項目の構造検査と内蔵の配置比較（付属様式でずれ0px・実機表示は人が確認）で欄外を検査し、目印のない白紙様式でも申請者欄と事務局専用欄を4段階で自動仕分けして誤記入をロックします。
AI抽出値には元メモの原文引用スパンを紐づけ（年の補完は推定表示）、メモにない事実は推測で埋めず人の入力待ちに残し、人が最終確認するまでダウンロードを解錠しません。Web・CLI・Claude Code MCPで現場の定型様式業務に活用できます。

---

### C. 英語版（English Overview — 箇条書き・太字・斜体・バッククォート・改行除き 2,408 chars／改行除き 2,478 chars／そのまま 2,487 chars）

**Problem & Target Users (Born from Firsthand Experience):**
- I repeatedly experienced frustrating administrative rejections (差し戻し) on ethics review and institutional forms; when I tried using AI to help, it did not work as I had hoped, to the point where I almost gave up on using AI for paperwork altogether. Form-Guard Agent was born from that firsthand frustration and the conviction that preserving the original template while filling only designated fields can genuinely make application workflows much easier.
- In Japanese universities, municipal offices, and facility management, official Word (`.docx`), Excel (`.xlsx`), and fixed-layout PDF (`.pdf`) forms come with a strict rule: *do not alter the original layout, borders, merged cells, formulas, or printed clauses*.
- One broken border or altered clause can trigger administrative rejection (差し戻し), requiring internal seals/approvals to be re-obtained and the form to be re-submitted. Regenerating a complete form can alter borders, merged cells, or formulas and introduce unstated numbers, making it unsafe for non-engineer staff. (The comparison file in the demo uses scripted simulated regeneration failures to illustrate these structural checks.)

**Solution:**
- Form-Guard Agent performs **byte-range pinpoint replacement** only on designated fields (and byte-preserving incremental overlay on supported fixed PDFs that provide a Unicode-encoded CID font in the page resources; see `README.md` Limitations), backed by **21 deterministic structural checks + `VIS-01` visual verification**:
- **1. Visual & Structural Checks:** Visual checks compare built-in layout geometry for Word/Excel and pixels outside designated boxes for supported PDFs (`0 px` drift in the built-in checks; native display and printing require final human review, and production adoption requires per-template field and display verification).
- **2. 4-Stage Blank-Cell Discovery** on unmarked forms (`form4` Word, `form5` Excel, `form6` PDF), separating `applicant_fillable` cells from locked `office_only` cells and Excel formulas.
- **3. Verbatim Source Grounding (no guessed facts) & Human Gate:** AI-extracted values carry source quotes; inferred years are flagged for review (`year_inferred`), unstated facts stay `needs_human_input`, and reviewer-entered values are recorded as human attestations. The application holds unsafe deletions during its own length repair, while pre-condensed model output is reviewed against the full source.

---

## 3. Claude および Claude Code の活用に関する質問
（How You Used Claude & Claude Code — 審査基準「Claudeの活用 25%」対応）

### A. 日本語・標準版（箇条書き・太字・バッククォート・改行除き 2,488字／改行除き 2,682字／そのまま 2,702字）

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

**3. なぜ Claude Code 無しでは実現できなかったか（確率的AIと決定論的検査の役割分担）**
- ソフトウェアエンジニアではない現場の実務者が、7日間の会期中に OOXML のバイト範囲置換、標準ライブラリによる固定PDF増分更新、7区分21項目の構造検査器、全277件の自動テストを一貫して構築できたのは Claude Code があってこそです。同時に「自由文メモからの原文引用つき提案は Claude に任せ、様式の不変検査と合否判定は LLM を使わない決定論的コードで行う」という境界を守っています。

---

### B. 日本語・短縮版（文字数制限が厳しい欄向け・箇条書き・バッククォート・改行除き 540字／改行除き 570字／そのまま 571字）

- 欄ごとに求められる能力が異なるため、`--model-tier auto` で Claude 3階層を自動振り分けしています。日付・番号・選択肢や空欄分類は `claude-haiku-4-5-20251001`、原文引用スパン（`quote`）つき文章・金額抽出は `claude-sonnet-5`、200字以内の要約案と解消できない欄の再提案は `claude-opus-5-5`（adaptive thinking）が担当します（上限超過等はまず原文候補から決定論的に再抽出。ライブAPI実機疎通で `form1` への3モデル初回提案と `21/21 PASS` を確認済み）。
- 開発の中心に Claude Code を据え、①白紙様式の改変やAPIキー・個人パス混入を書き込み前に `exit 2`＋`deny` で遮断し書き込み後に構文・import・JSONを検査する2段階フック、②ファイル編集ツール（`Edit`/`Write`）を持たない4種のサブエージェント、③6種の Skills、④追加のMCP SDK不要の5ツールMCPサーバー（承認ツールを設けず未承認下書きと `review_url` を返し同一SHA-256のままWeb画面で人が最終承認）と模擬公募ポータル（`/portal`）を実装しています。

---

### C. 英語版（How You Used Claude & Claude Code — 箇条書き・太字・バッククォート・改行除き 2,263 chars／改行除き 2,397 chars／そのまま 2,406 chars）

**1. Purpose-Driven 3-Tier Claude Routing & Self-Correction (`--model-tier auto`):**
- **`claude-haiku-4-5-20251001`** handles date/era normalization, IDs, enum selection, and blank-cell classification.
- **`claude-sonnet-5`** extracts prose, currency amounts, and verbatim source quotes (`quote` spans).
- **`claude-opus-5-5` (with adaptive thinking)** drafts `≤200`-char summaries and handles model re-proposals when needed. Printed-limit violations (e.g., `1,612,000円` → `1,000,000円` in `form2`) first trigger deterministic re-extraction from candidate spans, falling back to model re-proposal if unresolved (verified in offline demos and mock-API tests; the cited live API run `20261103-164113-f83bad` on `form1` verifies 3-model proposal routing with `21/21 PASS`).

**2. Built with Claude Code at the Core (`.claude/` & `.mcp.json`):**
- **2-Stage Guard Hooks (`scripts/hook_guard.py`)**: `PreToolUse` inspects configured tool calls before disk write (`exit 2` + `deny`) for recognizable `samples/blank_forms/` mutations and scans file-edit payloads for API keys, personal paths, and non-example e-mails (and shell commands for key patterns); `PostToolUse` runs on `Write`/`Edit`/`MultiEdit` to check `py_compile`, import smoke tests, and JSON/BOM validation (failing open on internal hook errors).
- **4 Subagents without `Edit`/`Write` tools**: `memo-extractor` (Sonnet, `Read/Grep/Glob` only: blind span extraction), `field-mapper` (`claude-opus-5-5`: rule-aware mapping), `structure-auditor` (Sonnet: structural audit), and `form-auditor` (Opus: independent verification & sign-off checklist); the latter three use hook-guarded `Bash`.
- **6 Skills (`form-scan`, `surgical-fill`, `structural-audit`, `form-fill-verify`, `visual-diff-audit`, `portal-form-fill`) & 5-Tool stdio MCP Server with no additional MCP SDK dependency (application dependencies required; `form_guard/mcp_server.py`)**: MCP omits any approval tool and returns `review_url` (`/?run_id=...`) so a reviewer signs off in the Web UI against the revision and SHA-256 hash.
- **What Claude Code Made Possible:** Built in 7 days by a non-engineer domain practitioner using Claude Code to implement byte-range OOXML surgery, pure-Python PDF incremental update, 21 deterministic checks, and 277 tests — while keeping a strict boundary where Claude proposes grounded values and deterministic LLM-free code decides structural pass/fail.

---

## 4. 審査基準 4区分・14項目 × Form-Guard Agent の定量エビデンス対照表

| 審査基準（配点） | 公式チェック項目（要約） | Form-Guard Agent の実装と定量エビデンス |
| :--- | :--- | :--- |
| **1. インパクト (30%)** | ・日本の具体的な現場から出発しているか<br>・テーマ1の場合：その世界に身を置いてきた人だからこそ見えた「足りないもの」を捉えているか<br>・テーマ2の場合：届いていなかった具体的な人を阻んでいた壁を取り除いているか<br>・誰がどんな場面でどれくらい困っているか具体的か<br>・ハッカソン後も使い続けられるか | ・研究費変更届（Word）・自治体補助金申請（Word）・月次設備点検（Excel）・白紙2様式・固定PDF助成金申請の計6様式で実証<br>・付属Word/Excel 5様式（41欄中40欄記入・848字）をオフラインで計測：XMLタグ 2,119・定型文 171・結合セル 124・罫線 650 を、計測スクリプトの表で 120〜246 ms・Web保存測定で 119〜379 ms（0.4秒未満）、デモ動画収録時の画面実測（`seg18`）で 361〜1,234 ms（約0.4〜1.2秒）で照合（固定PDF・API応答・人の確認時間は含まない。`form1` ライブAPI記録は各段階合計約29.63秒）<br>・Web UI / CLI / `.mcp.json` で継続利用可能（実務導入時は様式ごとの欄指定と表示確認が必要。固定PDFはページ内にUnicode CIDフォントを持つPDFが対象） |
| **2. デモ (25%)** | ・冒頭で「誰の、どんな課題を、どう解決するか」が明確か<br>・前提知識のない人でも実際の操作で価値が分かるか<br>・最後まで見たくなる動画か | ・175秒・全19カット構成（Step 1: 0:00–0:35 原体験と課題解決／Step 2: 0:35–1:10 原文引用と推測ゼロ／Step 3: 1:10–1:45 白紙空欄4段階発見／Step 4: 1:45–2:19 Claude 3階層＆Claude Code拡張／Step 5: 2:19–2:55 21項目検査・人間承認・公開190件検証）において、様式ごと作り直した場合の失敗を再現した比較用の模擬ファイル（結合セル19→0・罫線定義9→0・A4→レター・7項目不合格）と Form-Guard 版（21/21合格・付属デモの内蔵配置比較でずれ `0 px`）を対比<br>・自作架空様式のみを使用し、日本語ナレーションと操作画面の同期で非エンジニアにも直感的に伝達 |
| **3. Claudeの活用 (25%)** | ・基本的な使い方を超えた創造的活用か<br>・Claude Code を開発の中心に据えているか<br>・モデルや機能（Skills、サブエージェント、MCP、フック等）を目的に応じて使い分けているか | ・欄別3モデル自動振り分け（Haiku 4.5 / Sonnet 5 / Opus 5.5：ライブAPI `form1` で3モデルの初回提案応答と `21/21 PASS` を確認）＋上限超過等の決定論的再抽出と Opus 自己修正設計（オフラインデモとモックAPIテストで確認）<br>・2段階フック（`PreToolUse` 白紙保護＋`PostToolUse` 構文・import検査）、`Edit`/`Write` 無しの4サブエージェント（うち3種はフック付き `Bash`）、6 Skills、5ツール MCP＋Web承認引き継ぎ |
| **4. 深さと実行力 (20%)** | ・試行錯誤を重ねて磨き上げ、課題にじっくり向き合った跡が見えるか<br>・設計と実装が丁寧か<br>・困っている人が安心してそのまま使える形か | ・`.venv` 環境で **全277件の `pytest` 合格（`277 passed, 0 skipped`・2026-11-07（土）検証ログ `output/pytest_run_20261107_2205.log` 保存済み）**、オフラインプログラム **45試行（`45/45 ok`・2026-11-04（水）にコミット `282fb0c` で再検証済み）**、ヘッドレス Edge 実ブラウザ **16試行（`16/16 ok`・コンソールエラー0・2026-11-04（水）にコミット `282fb0c` で再検証済み）**<br>・付属Word/Excel 5様式について実機 Microsoft Word 16.0 / Excel 16.0 での開閉・ページ数・計算式再計算（`COUNTIF` / `SUM`）確認済み<br>・マーカーの無い白紙様式（表や対応段落の空欄を持つ `.docx` / `.xlsx`。固定PDFでの自動適応は未検証）に対応するゼロ設定の自動適応モード（`--auto-adapt`：空欄自動発見＋`VIS-01` セルはみ出し `drift_outside_slots` / `grown_slots` 検出時の `max_chars` 自動縮小［比率 `0.60〜0.65`・最小 `1`・最大3ラウンド］＋適応時の `<form>.adapted.slots.json` 保存）を実装し、公開実ファイル計190件（3段階間のURL・SHA-256重複0件）の3段階限定試験（オフライン・合成ダミーメモによる記入試験：①公開100ファイルで完走63件中58件が21項目合格［隔離環境での修正後63/63合格・空欄0件16・旧バイナリ等21件安全拒否］、②融資・信用保証・登記40様式で39/40件が21項目＋`VIS-01` 合格［図形のみの1件は空欄候補0件］、③①②と重複しない別ジャンルの新規公開50様式の `--auto-adapt` 検証で50/50件が21項目＋`VIS-01` 検査通過［191欄記入・69欄安全保留・`PASS` 4様式・入力待ち46様式］）でも構造改変 0件・`GRD-01` 違反 0件を検証済み（2026-11-04（水）実施・全190件のURLとSHA-256および検査結果は `docs/PUBLIC_BENCHMARK_190_MANIFEST.json`、`docs/BUILT_WITH_CLAUDE_CODE.md` 第5節-7、`docs/DEMO_VIDEO_SCRIPT_JA.md` 第3節に明記） |


## 5. 審査用アクセス情報 (Judging & Review Access)

- **公開 GitHub リポジトリ**: `https://github.com/drmiura-jpg/form-guard-agent`
- **デモ動画 (YouTube / Loom / MP4)**: 3分以内（`FormGuard_Agent_Demo_175s_1080p.mp4`：実尺 `175.00秒`・`19.08 MiB / 20.01 MB [20,007,852 bytes]`・SHA-256 `192b369383b4b033fb216dafc0416f5616e9d114ad5a609c00c8e48e50b89f78`、自作架空様式のみ使用。台本と絵コンテは `docs/DEMO_VIDEO_SCRIPT_JA.md`。※2026-11-08（日）作成の1クリック進行14枚版［台本 `docs/PRESENTER_WIPE_FULL_SCRIPT_JA.md`・スライド `FormGuard_Agent_ClickStep_Pitch_Deck_16x9.pptx`、全14枚・60段階クリック表示収録］で提出する場合は、カメラ合成後の `FormGuard_Agent_FINAL_Presenter_Demo_175s.mp4` または生成済みの `FormGuard_Agent_Presenter_Wipe_Demo_175s.mp4`［実尺 `175.00秒`・`8,736,316 bytes`・SHA-256 `5b7151d7cfcbcb5dc5f1afc5a88916bcd66ad83ac28cac0db490ffa887cbd840`］／`FormGuard_Agent_Clean_Slide_Deck_175s.mp4`［実尺 `175.00秒`・`8,201,191 bytes`・SHA-256 `4f1246ea417464b8bd72f8e89ca689cee74a530ea2961b2a1438e5b91285ba8c`］の実測値・閲覧URLに更新して提出する）
  - **デモ動画 閲覧 URL**: `＜アップロード後に Loom（Anyone with the link can view）または提出用 YouTube アカウントの限定公開（Unlisted）URL を記入＞`
- **ローカル即時検証（APIキー不要・オフライン再現手順）**: Python 3.10 以上の環境で `python -m venv .venv` → `.venv\Scripts\activate`（macOS/Linux は `source .venv/bin/activate`）→ `pip install -r requirements.txt` を実行後、以下のコマンドで即座に再現できます：
  1. `python -m form_guard.cli --all-demos --mode demo`：マーカーあり架空デモ3様式（`form1`〜`form3`）のオフラインピンポイント記入と21項目検査（`output/index.html` 生成）
  2. `python -m form_guard.cli --serve`（環境にAPIキーがある場合も完全オフラインに固定するときは `FORM_GUARD_FORCE_DEMO=1` を設定）：ローカル Web UI（`http://127.0.0.1:8000` で全6デモ＋公募ポータル `/portal`、かんたん5ステップ画面 `http://127.0.0.1:8000/start` でマーカーなし白紙7様式 `form4`〜`form10`）のオフライン試行
  3. `pytest -v`：全277件の自動テスト（`277 passed, 0 skipped`）の実行
- **審査用アカウント / 認証条件**: 審査用のオフラインデモ（`--mode demo` または APIキー未設定時の `--mode auto`）は外部の資格情報や有料アカウント不要でローカル（`127.0.0.1`）で動作し、ルール式の仮記入・21項目検査・人間承認手順を一通り確認できます。ライブAPIモード（`--mode live`）の実行には `ANTHROPIC_API_KEY`（または `FORM_GUARD_API_KEY`）、Claude Code 連携（`.mcp.json`・2段階フック・サブエージェント・Skills）の利用には Claude Code 実行環境と設定の有効化が必要です（料金・認証条件は各利用契約に従います）。
- **English Judging Access Summary**:
  - **Repository**: `https://github.com/drmiura-jpg/form-guard-agent`
  - **Demo Video**: Under the 3-minute limit (`FormGuard_Agent_Demo_175s_1080p.mp4`: `175.00 s`, `19.08 MiB / 20.01 MB [20,007,852 bytes]`, SHA-256 `192b369383b4b033fb216dafc0416f5616e9d114ad5a609c00c8e48e50b89f78`, using only self-authored fictional templates; script in `docs/DEMO_VIDEO_SCRIPT_JA.md`; if submitting the 14-slide 60-click-step edition [`docs/PRESENTER_WIPE_FULL_SCRIPT_JA.md`], update duration, byte size, SHA-256, and viewing URL to the merged `FormGuard_Agent_FINAL_Presenter_Demo_175s.mp4` or `FormGuard_Agent_Presenter_Wipe_Demo_175s.mp4` [`175.00 s`, `8,736,316 bytes`, SHA-256 `5b7151d7cfcbcb5dc5f1afc5a88916bcd66ad83ac28cac0db490ffa887cbd840`] / `FormGuard_Agent_Clean_Slide_Deck_175s.mp4` [`175.00 s`, `8,201,191 bytes`, SHA-256 `4f1246ea417464b8bd72f8e89ca689cee74a530ea2961b2a1438e5b91285ba8c`]). Viewing URL: `<Unlisted YouTube or Loom URL to be filled after upload>`
  - **Offline Reproduction (No API Key Required)**: Run `python -m venv .venv`, activate it, run `pip install -r requirements.txt`, then run `python -m form_guard.cli --all-demos --mode demo` (3 marked demos offline), `python -m form_guard.cli --serve` (set `FORM_GUARD_FORCE_DEMO=1` to force offline even if an API key is present in the environment; opens `http://127.0.0.1:8000` for 6 demos + `/portal`, and `/start` for 7 unmarked forms `form4`–`form10`), and `pytest -v` (`277 passed, 0 skipped`). Live API calls require `ANTHROPIC_API_KEY` / `FORM_GUARD_API_KEY`, and Claude Code integration requires an active Claude Code environment.

---

## 6. Claude Code を使った開発の感想（必須項目⑥・非公開アンケート欄および自由記入欄の下書き）

### ① Claude Code を使って開発してみた感想（運営メール必須項目⑥）
アプリのコード・テスト・サンプル生成・Claude Code 拡張資産の構築に Claude Code（Opus 5.5 / Sonnet 5 / Haiku 4.5）を中心として活用しました。特に印象深かったのは、単なるコード生成にとどまらず、Skills（6種）・ファイル編集ツールを持たないサブエージェント（4種）・前後2段階フック（`PreToolUse` / `PostToolUse`）・5ツール MCP サーバーといった拡張機能を組み合わせることで、全277件の自動テストや7区分・21項目の構造検査器を短期間で一貫して構築できた点です。書き込み前フック（`PreToolUse`）で `samples/blank_forms/` の白紙様式への認識可能な変更操作や秘密情報の混入を遮断し、書き込み後フック（`PostToolUse`）で構文・import・JSON 検査を自動実行する仕組みを整えたことで、開発作業自体も安全に進められました。OOXML のバイト範囲置換や結合セル・計算式の保護といった細かい制約にじっくり向き合い、検証器の指摘から自己修正（Adaptive Thinking）を回す設計を形にできたのは Claude Code ならではだと感じています。

### ② 自由記入欄 / 審査員への補足メッセージ（欄がある場合）
日本の研究機関・自治体・施設管理の現場では、毎月多くの「指定様式書類」の作成に追われ、罫線や結合セル、計算式の崩れによる差し戻しが手戻りを生んでいます。Form-Guard Agent は、私自身が倫理申請などの書類作成で何度も差し戻されて困り「もうAIを使うのはやめようか」と思った実体験から、「元の白紙様式は一切触らずに指定欄だけをピンポイントで書き換え、欄外が変わっていないことを21項目で検査し、最終判断は人が行う」という現実的な解として開発しました。オフラインデモ（APIキー不要）で手元の環境でもすぐにお試しいただけます。
