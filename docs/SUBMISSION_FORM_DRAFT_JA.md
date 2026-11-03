# Built with Claude: Japan 2026 — 提出フォーム記入文案（コピペ用下書き）

作成・更新日：2026-11-03（火）
提出締切：**2026-11-08（日）20:00 JST**（内部目標：2026-11-08（日）18:00 JST までに提出完了。20:00 JST より前ならプラットフォーム上で更新可、20:00 JST 以降は変更不可）

---

## 1. 基本情報（プロジェクト名・テーマ・一言ピッチ・リンク）

### ① プロジェクト名（Project Name）

- **標準表記（短）**：
  `Form-Guard Agent`
- **副題つき表記（欄に余裕がある場合）**：
  `Form-Guard Agent — 指定様式を1バイトも壊さない外科的書類記入＆21項目構造証明エージェント`
- **英語副題つき表記**：
  `Form-Guard Agent — Zero-Layout-Breakage Surgical Form Filler & 21-Check Structural Verifier`

### ② 選択したテーマ（Selected Theme）

- **単一選択の場合の第1推奨**：
  **`テーマ1：あなたにしかない深さを。`**
  - **選択理由（フォームに理由欄がある場合）**：
    日本の大学・自治体・施設の現場で毎月「様式第○号」の Word / Excel / 固定PDF 書類を作成してきた実務当事者として、生成AIを書類業務に使えない最大の壁は「AIの文章が間違うこと」以上に、**「AIが指定様式の罫線・結合セル・計算式・定型文を黙って作り変えてしまい、どこが変わったのか誰にも分からないため怖くて提出できないこと」**にあると痛感してきました。「『そういうものだ』と誰もが諦めて手作業で転記・目視確認してきた日々の不便」を、指定欄のバイト範囲だけを外科的に差し替え、指定欄以外が1バイトも変わっていないことを21項目の決定論的検査で証明する仕組みに変えました。
- **複数選択可、またはテーマ2を選ぶ場合の文案**：
  **`テーマ2：本来、誰にでも届くべきものを。`**
  - **選択理由**：
    補助金申請や行政・研究手続きの現場では、複雑な指定様式や文字数・和暦・上限額の細かな規定が壁となり、専門スタッフやエンジニアがいない中小事業者・現場担当者の負担になっています。Form-Guard Agent は、AIを盲信しなくても誰でも安心して使えるよう、すべての記入値に元メモの一字一句の引用ハイライトを添え、事務局専用欄への誤記入をロックし、メモに無い事実は推測せず人の入力待ち（`needs_human_input`）に残し、人が最終承認するまでダウンロードを解錠しない設計にしました。

### ③ 一言ピッチ / エレベーターピッチ（Short Description / Tagline）

- **日本語（50字以内・49字）**：
  `Word・Excel・固定PDFの指定欄だけを外科的に記入し、書式不変を21項目検査で証明するAI`
- **日本語（100字以内・96字）**：
  `「様式を変えないで」が鉄則の日本の申請業務で、Word・Excel・固定PDFの指定欄だけを原文根拠つきで外科的に記入し、欄外の書式・計算式不変を21項目検査と視覚Diffで証明するエージェント`
- **日本語（140字以内・137字）**：
  `研究費や行政申請のWord・Excel・固定PDF様式に対し、指定欄だけをバイト単位で外科的に記入。Haiku/Sonnet/Opus 5.5の3階層連携で原文引用と上限・字数制約を守り、21項目の構造検査と人間承認ロックで「書式崩壊ゼロ・推測ゼロ」を証明します。`
- **英語（120 chars / 116 chars）**：
  `Surgical Word/Excel/PDF form filler with source quotes, 21 structural checks, 0 px non-target drift, and human gate.`
- **英語（240 chars / 228 chars）**：
  `Rewrites only designated fields in official Word, Excel, and fixed-PDF forms while proving zero layout drift outside target cells via 21 deterministic checks, verbatim source grounding, 3-tier Claude routing, and human sign-off.`

### ④ リンク欄と提出前チェック

- **公開 GitHub リポジトリ URL**：
  `https://github.com/drmiura-jpg/form-guard-agent`
  （※ご本人の公開OK後に Public リポジトリとして push し、シークレットウィンドウで未ログイン閲覧できることを確認してから提出）
- **3分間のデモ動画 URL**：
  `＜収録・アップロード後に YouTube（公開 または 限定公開 Unlisted）または Loom の URL を記入＞`
  - **動画公開設定の必須確認**：
    - [ ] YouTube の場合は「**限定公開（Unlisted）**」または「**公開（Public）**」に設定した（「非公開（Private）」は審査員が再生できないため不可）。
    - [ ] Loom の場合は「**Anyone with the link can view**」に設定した。
    - [ ] シークレットウィンドウ（未ログイン状態）で URL を開き、最初から最後まで音声つきで再生できることを確かめた。
    - [ ] 動画の長さが **3分（180秒）以内**（台本設計は 175秒＝2分55秒）であることを確かめた。

---

## 2. プロジェクト概要：解決する課題と、誰のためのものか
（Project Overview: Problem, Target Users, Solution & Post-Hackathon Use — 審査基準「インパクト 30%」「深さと実行力 20%」対応）

### A. 日本語・標準版（約820字・そのまま貼り付け可）

**【誰の・どんな課題か（日本の現場から出発した課題）】**
日本の大学・研究機関、自治体窓口、施設管理の現場では、「様式第1号」などの公式な Word（`.docx`）・Excel（`.xlsx`）・固定レイアウト PDF（`.pdf`）が指定され、**「元の書式・罫線・結合セル・計算式・定型文を一切変えないこと」**が求められます。罫線1本、結合セル1つ、Excel の `SUM` / `COUNTIF` 計算式1つの崩れが差し戻しや再提出の原因になります。
しかし、既存の生成AIに書類作成を頼むと様式ごと作り直してしまい、見た目や構造が微妙に変わるうえ、「どこが勝手に変わったのか」「どの数字がAIの推測なのか」を確かめるだけで手作業以上の時間がかかるため、現場の事務担当者や申請者はAIを使えませんでした。

**【どう解決するか（Form-Guard Agent のアプローチ）】**
Form-Guard Agent は「AIに書類を作らせる」のではなく、**「白紙様式の指定欄だけをバイト単位で外科的に差し替え、それ以外が1バイトも変わっていないことを決定論的に証明する」**エージェントです。
1. **外科的差込と21項目＋視覚Diff検査（`VIS-01`）**：対象テキストノードのバイト範囲（固定PDFは増分更新オーバーレイ）だけを書き換え、他のZIPエントリは再圧縮すら行わず生バイト複製します。記入後に7区分・21項目の構造検査とピクセル比較（指定欄以外のずれ `0 px`）を実行し、実機 Microsoft Word 16.0・Excel 16.0 でも警告ゼロ・ページ数不変・計算式一致を確認済みです。
2. **マーカーなし白紙様式の4段階・空欄自動発見**：`{{マーカー}}` が無い白紙様式（デモ4 Word・デモ5 Excel・デモ6 固定PDF）でも、申請者記入欄（`applicant_fillable`）と事務局専用欄（`office_only`：受付番号・審査欄等）を自動仕分けし、事務局欄や計算式セルへの誤記入をロックします。
3. **推測ゼロの原文根拠ハイライトと人間承認ロック**：すべての値に元メモの一字一句の引用スパンを紐づけ、メモに無い事実（デモ2の法人番号）は推測せず `needs_human_input` として残します。200字制限を超える場合は削除のみで要約し、条件語の削除を検知した要約は保留します。確認者が全項目を確認して承認するまで、ダウンロードはサーバー側でロックされます。

**【ハッカソン後の継続利用】**
Web UI・CLI・Claude Code MCP サーバー（`.mcp.json`）の3形態で動作し、APIキーがない端末でもオフライン決定論エンジンで即座に再現できます。ハッカソン後も研究事務・自治体申請・施設点検の毎月・四半期の定型様式記入にそのまま使い続けられます。

---

### B. 日本語・短縮版（文字数制限が厳しい欄向け・約380字）

日本の大学・自治体・施設の申請業務では、指定された Word・Excel・固定PDF 様式の罫線や結合セル、計算式が1つ崩れるだけで差し戻しの原因になります。従来の生成AIは様式ごと作り直すため書式が崩れ、どこが変わったか分からず現場で使えませんでした。
Form-Guard Agent は、指定欄だけをバイト単位で外科的に書き換え（固定PDFは元バイト不変の増分更新）、7区分・21項目の構造検査と視覚Diff（欄外のずれ `0 px`）で「書式が壊れていないこと」を証明します。マーカーの無い白紙様式でも申請者欄と事務局専用欄を4段階で自動仕分けして誤記入をロックし、全項目に原文引用ハイライトを付与。メモに無い事実は推測せず人の入力待ちに残し、人が最終確認するまでダウンロードを解錠しません。Web UI・CLI・Claude Code MCP で明日から現場で使い続けられます。

---

### C. 英語版（English Overview — 併記用）

**Problem & Target Users:**
In Japanese universities, municipal offices, and facility management, official Word (`.docx`), Excel (`.xlsx`), and fixed-layout PDF (`.pdf`) forms come with a strict rule: *never alter the original layout, borders, merged cells, formulas, or printed clauses*. Even a single broken border or overwritten `SUM` formula can trigger rejection. Standard generative AI regenerates the whole document, silently breaking layouts and hallucinating unstated facts, making it unusable for non-engineer staff.

**Solution:**
Form-Guard Agent performs **byte-range surgical replacement** only on designated fields (and byte-preserving incremental overlay on fixed PDFs), backed by **21 deterministic structural checks + `VIS-01` visual diff (`0 px` drift outside target fields)**:
1. **4-Stage Blank-Cell Discovery** on unmarked forms (`form4` Word, `form5` Excel, `form6` PDF), automatically separating `applicant_fillable` cells from locked `office_only` cells and Excel formulas.
2. **Zero-Hallucination Verbatim Grounding:** Every value is linked to an exact character span in the source memo. Unstated facts (e.g., Corporate Number in `form2`) are never guessed and remain `needs_human_input`. Over-length drafts are condensed extractively while holding back any summary that drops conditional clauses.
3. **Server-Enforced Human-in-the-Loop Gate:** Downloads remain locked on the server until a named reviewer checks all items against the revision and SHA-256 hash.

---

## 3. Claude および Claude Code の活用に関する質問
（How You Used Claude & Claude Code — 審査基準「Claudeの活用 25%」対応）

### A. 日本語・標準版（約920字・そのまま貼り付け可）

**1. 欄の性質に応じた Claude 3モデルの目的別ルーティングと自己修正ループ（アプリ中核）**
1つの様式の中でも欄ごとに求められる能力が異なるため、`--model-tier auto` で3階層モデルを自動振り分けしています（ライブAPI実機疎通 `run_id: 20261103-164113-f83bad` で3モデルすべての応答と `21/21 PASS` を確認済み）：
- **`claude-haiku-4-5-20251001`**：日付（和暦変換・曜日整合）・識別番号・定型選択肢の高速分類と、マーカーなし白紙様式の空欄候補分類。
- **`claude-sonnet-5`**：自由文メモからの原文引用スパン（`quote`）つき値抽出・金額の正規化。
- **`claude-opus-5-5`（adaptive thinking `display: "summarized"`）**：但し書きや数値条件を落とさない200字以内要約と、決定論的検証器に差し戻された欄（例：デモ2で印刷された上限100万円を超過した申請額 `1,612,000円` → `1,000,000円`）の自己修正（エスカレーション再抽出）。
さらに、API環境ごとのモデル利用可否に対応する `MODEL_FALLBACK_CHAIN` と、プロンプトキャッシュ（`cache_creation_input_tokens` / `cache_read_input_tokens`）および思考要約（`thinking_summaries`）の監査ログ・Web画面表示を実装しています。

**2. Claude Code を開発の中心に据えた4つの拡張資産（`.claude/` ＆ `.mcp.json`）**
「白紙様式を絶対に壊さない」というプロダクトの安全哲学を、開発プロセス自体にも Claude Code の機能で組み込みました：
- **① 2段階フック（`scripts/hook_guard.py`・`.claude/settings.json`）**：
  - `PreToolUse`（書き込み前阻止）：`samples/blank_forms/` の白紙様式（`.docx`/`.xlsx`/`.pdf`）への書き込み・削除・上書きコマンドや、APIキー・個人パス・非サンプルメールの混入を、実行前に終了コード `2`＋`permissionDecision: "deny"` で遮断。
  - `PostToolUse`（書き込み後検査）：編集直後に `py_compile`・`.venv` での import スモークテスト（1.8秒予算内）・JSON 構文＆BOM 検査・衛生検査を自動実行し、違反時は `decision: "block"` で即座に自己修正させます。
- **② 編集権限（`Edit`/`Write`）を持たない4種のサブエージェント（`.claude/agents/`）**：
  様式スキーマを見る前にメモから原文スパンだけを抜く `memo-extractor`（Sonnet）、差込結果を独立検証する `form-auditor`（Opus）、マーカーなし様式の空欄を分析する `blank-cell-discoverer`（Sonnet）、安全規則を点検する `Constraint Reviewer` の4役に分担し、バイアスと誤改変を防ぎました。
- **③ 6種の Skills（`.claude/skills/`）**：
  `form-scan`・`surgical-fill`・`structural-audit`・`blank-cell-discovery`・`pdf-overlay-fill`・`portal-form-fill` として定型ワークフローをモジュール化。
- **④ 外部依存ゼロの5ツール MCP サーバー（`form_guard/mcp_server.py`・`.mcp.json`）とブラウザ引き継ぎ**：
  `form_guard_list_demos`・`form_guard_scan`・`form_guard_discover_blank_cells`・`form_guard_fill`・`form_guard_verify` を標準入出力 JSON-RPC 2.0 で公開。Claude Code や **Claude in Chrome**（模擬公募ポータル `/portal` からの読み取り）で生成した下書きは、AIが勝手に確定させることはできず、必ず `UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF` と `review_url`（`http://127.0.0.1:8000/?run_id=...`）を返し、同じ SHA-256 のまま Web 画面で人が最終承認して初めてダウンロードできる一気通貫の協調フローを実現しました。

---

### B. 英語版（How You Used Claude & Claude Code — 併記用）

**1. Purpose-Driven 3-Tier Claude Routing & Self-Correction (`--model-tier auto`):**
- **`claude-haiku-4-5-20251001`** handles date/era normalization, IDs, enum selection, and blank-cell classification.
- **`claude-sonnet-5`** extracts prose, currency amounts, and verbatim source quotes (`quote` spans).
- **`claude-opus-5-5` (with adaptive thinking)** handles condition-preserving condensation (`≤200` chars) and self-correction when the deterministic verifier rejects a draft (e.g., escalating an over-cap subsidy request from `1,612,000円` to the printed cap `1,000,000円`). Verified end-to-end on live API (`run_id: 20261103-164113-f83bad`, `21/21 PASS`).

**2. Built with Claude Code at the Core (`.claude/` & `.mcp.json`):**
- **2-Stage Guard Hooks (`scripts/hook_guard.py`)**: `PreToolUse` blocks any modification to `samples/blank_forms/` or secret/path leaks before disk write (`exit 2` + `deny`); `PostToolUse` runs `py_compile`, import smoke tests, and JSON/BOM validation immediately after every edit.
- **4 Specialized Subagents without `Edit`/`Write` tools**: `memo-extractor` (blind span extraction), `form-auditor` (independent verification), `blank-cell-discoverer`, and constraint review.
- **6 Skills & Zero-Dependency 5-Tool stdio MCP Server (`form_guard/mcp_server.py`)**: Drafts created via Claude Code MCP or **Claude in Chrome** on the mock grant portal (`/portal`) are never auto-finalized; they return a `review_url` (`/?run_id=...`) with identical SHA-256 for human sign-off in the browser.

---

## 4. 審査基準 4区分・14項目 × Form-Guard Agent の定量エビデンス対照表
（フォームの自由記述欄や補足欄に貼れるコンパクト整理）

| 審査基準（配点） | 公式チェック項目 | Form-Guard Agent の実装と定量エビデンス |
| :--- | :--- | :--- |
| **1. インパクト (30%)** | ・日本の具体的な現場から出発しているか<br>・テーマ1「足りないもの」／テーマ2「阻んでいた壁」を捉えているか<br>・誰がどんな場面でどれくらい困っているか具体的か<br>・ハッカソン後も使い続けられるか | ・研究費変更届（Word）・自治体補助金申請（Word）・月次設備点検（Excel）・白紙2様式・固定PDF助成金申請の計6様式で実証<br>・5様式41欄中40欄を自動記入（848字）、XMLタグ 2,119・定型文 171・結合セル 124・罫線 650 を1様式 0.4秒未満（CLI表 120〜246 ms・Web計測 119〜379 ms）で照合<br>・Web UI / CLI / `.mcp.json` でハッカソン後も毎月の定型書類業務に即利用可能 |
| **2. デモ (25%)** | ・冒頭で「誰の、どんな課題を、どう解決するか」が明確か<br>・前提知識のない人でも実際の操作で価値が分かるか<br>・最後まで見たくなる動画か | ・冒頭28秒（`#tour=1`）で「素のAIで作り直して結合セル19→0・罫線9→0に壊れた例」と「Form-Guard（21/21合格・ずれ `0 px`）」をスライダー比較<br>・Web画面上部の5ステップ審査員ガイドツアー（`#tour=1`〜`#tour=5`）と専門用語噛み砕きテロップで非エンジニアにも直感的に伝達 |
| **3. Claudeの活用 (25%)** | ・基本的な使い方を超えた創造的活用か<br>・Claude Code を開発の中心に据えているか<br>・モデルや機能（Skills、サブエージェント、MCP、フック等）を目的に応じて使い分けているか | ・欄別3モデル自動振り分け（Haiku 4.5 / Sonnet 5 / Opus 5.5）＋検証器差し戻し時の Opus 自己修正ループ（ライブAPI実機疎通 `21/21 PASS`）<br>・2段階フック（`PreToolUse` 白紙保護＋`PostToolUse` 構文・import検査）、`Edit`/`Write` 無しの4サブエージェント、6 Skills、5ツール MCP＋Web承認引き継ぎ |
| **4. 深さと実行力 (20%)** | ・試行錯誤を重ねて磨き上げ、課題にじっくり向き合った跡が見えるか<br>・設計と実装が丁寧か<br>・困っている人が安心してそのまま使える形か | ・展開後 **全272件の `pytest` 合格**、オフラインプログラム **45試行（`45/45 ok`）**、ヘッドレス Edge 実ブラウザ **16試行（`16/16 ok`・コンソールエラー0）**、1,000回ファジングテスト違反0件<br>・実機 Microsoft Word 16.0 / Excel 16.0 での開閉・ページ数・計算式再計算（`COUNTIF` / `SUM`）確認済み |
