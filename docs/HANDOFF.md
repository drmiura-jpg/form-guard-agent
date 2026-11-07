# Form-Guard Agent — 別PC（新宿PC）引き継ぎ・収録・提出マスターガイド (`docs/HANDOFF.md`)

- **最終更新**: 2026-11-08（日）
- **対象大会**: Built with Claude: Japan 2026（Anthropic / Cerebral Valley 主催）
- **公式提出締切**: **2026-11-08（日）20:00 JST**（目標提出時刻: **2026-11-08（日）18:00 JST**）

---

## 0. 先に聞かれている3つの質問への即答（結論）

| ご質問 | 回答（結論） |
| :--- | :--- |
| **1. セーフガード / Form-Guard Agent は Web アプリですか？ デプロイ済みの URL はありますか？** | ・**ローカルで起動する FastAPI Web アプリ（＋ CLI ＋ Claude Code MCP サーバー）** です。<br>・**常時稼働のクラウドデプロイ URL はありません**（公式リマインドでも「アプリケーションリンクがあれば」と任意項目になっており、ローカル再現手順で審査可能です）。<br>・初回に `pip install -r requirements.txt`（ネット接続が必要）を済ませた後は、`python -m form_guard.cli --serve` を実行すると **数秒でローカル Web アプリ（`http://127.0.0.1:8000` および `http://127.0.0.1:8000/start`）が起動** し、**APIキーなし（オフラインデモモード）で同梱10種の架空デモ様式と21項目検査・視覚Diff・人間承認ゲートが動作することを鴨川PCで確認済み（`pytest` 277 passed）** です。<br>・※外部からブラウザで開く一時URLが必要な場合は、同梱の `pwsh scripts/share_tunnel.ps1`（Cloudflare Quick Tunnel・合言葉保護つき）で一時発行できます。 |
| **2. GitHub リポジトリの URL はわかりますか？** | ・**公開 URL**: `https://github.com/drmiura-jpg/form-guard-agent`（**Public 公開・push 済み**）<br>・**現在の公開状態**: 鴨川PCのローカル Git（`C:\hackathon\form-guard-agent`、`main` ブランチ、`.env`・APIキー・個人パス混入 0件を全履歴スキャンで確認済み）から **Public リポジトリとして作成・push 完了済み** です。新宿PCからそのまま `git clone https://github.com/drmiura-jpg/form-guard-agent.git` でクローンできます。 |
| **3. 新宿の PC に Git と Node/Python などの実行環境は入っていますか？** | ・**鴨川PC側からは新宿PCの環境は未確認**ですので、新宿PCの PowerShell で以下の2行を実行して確認してください：<br>　`git --version`<br>　`python --version`（**Python 3.10 以上**であればOK）<br>・Web UI の実演・収録だけなら Node.js は不要です（Claude Code CLI ターミナルも画面に映す場合のみ `node --version` と `claude --version` を確認してください）。<br>・Git と Python 3.10+ が入っていれば、同梱の `powershell -ExecutionPolicy Bypass -File .\scripts\setup_remote_demo.ps1` を実行するだけで、`.venv` 作成・`pip install`（初回のみネット接続が必要）・Claude Desktop の MCP 設定登録・Web サーバー起動まで自動で完了します。 |

---

## 1. プロダクト概要（誰の・どんな課題を・どう変えるか）

### 冒頭の一文（公式指定フォーマット・148字）
> **このプロジェクトは、大学・自治体・医療現場などの申請業務で「指定様式の罫線や計算式が崩れると差し戻されるためAIを使えない」と困っている担当者・研究者の課題を、元の白紙様式は一切触らず指定欄だけをピンポイントで書き換え、欄外の不変を自動検査して人が最終承認する形に変える書類記入エージェントです。**

### 誰の・どんな課題か（開発者の原体験）
- 開発者自身が臨床研究・倫理審査や各種申請書の作成で、人の目では確認して提出したのに「原本から罫線や枠が微妙にずれている」と何度も差し戻された実体験から出発しています。
- 生成AIで書類を作り直すと結合セルや計算式が壊れ、「結局1欄ずつ手でコピペするしかないのか」と心がくじけました。
- 日本の大学・研究機関・自治体・企業の現場では、Word（`.docx`）・Excel（`.xlsx`）・固定レイアウト PDF（`.pdf`）の指定様式に対し**「元の書式・罫線・結合セル・計算式・定型文を1箇所も変えないこと」**が求められます。

### どう変えるか（5重のセーフガード）
1. **Guardrail ①（白紙原本の改変阻止フック）**:
   - Claude Code の `PreToolUse` フック（`scripts/hook_guard.py`）が `samples/blank_forms/` の白紙原本に対する直接書き込みを `exit 2`（`deny`）で自動遮断します。
2. **Guardrail ②（目印のない白紙様式からの4段階・空欄発見＆事務局欄ロック）**:
   - `{{マーカー}}` のない白紙様式（`form4`〜`form10`）から空欄を検出し、**「申請者記入欄（緑チェック）」** と **「事務局専用欄（承認印・受付番号・受付日・Excelの `SUM`/`COUNTIF` 数式セルなど：赤ロック）」** を自動仕分けして誤記入をロックします。
3. **Guardrail ③（原文引用スパン照合・推測ゼロ設計・上限自己修正）**:
   - すべての記入値に元メモの**原文引用（行番号・黄色ハイライト）**を紐づけます。
   - メモに書かれていない事実（13桁の法人番号など）は当てずっぽうで埋めず **`needs_human_input`（人の入力待ち）** に残します。
   - 欄の難易度に応じて **Haiku 4.5**（日付・和暦・番号・空欄分類）、**Sonnet 5**（氏名・金額・自由記述の原文引用抽出）、**Opus 5.5**（200字以内の要約短縮と上限違反等の再提案）を使い分けます。
4. **Guardrail ④（外科的ピンポイント置換＋7区分・21項目の構造検査＆視覚比較 `VIS-01`）**:
   - Word / Excel の対象テキストノードのバイト範囲（固定PDFは末尾への増分追記オーバーレイ）だけを書き換え、無変更の ZIP パーツ（例：`form1` の18パーツ中17パーツ）は再圧縮すら行わず生バイト複製します。
   - 記入直後に **7区分・21項目の決定論的構造検査** と **内蔵の視覚配置比較（`VIS-01`：付属デモで欄外のずれ `0 px`）** を自動実行します。
5. **Guardrail ⑤（AIの自己承認禁止＋人間承認ゲート）**:
   - MCP にはあえて「承認ツール」を置かず、**人間が Web 画面で左右比較・全項目チェックを行い、確認者名を入力して承認ボタンを押すまでダウンロードをサーバー側でロック** します。

---

## 2. 技術構成と起動手順（必要なツール・環境変数の「名前」のみ）

### 技術スタック
- **言語・ランタイム**: Python 3.10+（推奨 3.11 / 3.12）、Git
- **Web フレームワーク**: FastAPI + Uvicorn（`form_guard/server.py`）
- **文書解析・ピンポイント置換エンジン**: `lxml` / `zipfile`（OOXML 生バイト保持置換）、`pypdf` / `reportlab` / `pymupdf`（固定PDF 増分オーバーレイ＆画素比較）、`Pillow`（内蔵レイアウト描画比較 `VIS-01`）
- **AI モデル連携**: Anthropic Python SDK（`anthropic`）による3階層ルーティング（`form_guard/mapper.py`）※APIキー未設定時は自動的に決定論的ルールエンジン `DemoEngine` で動作
- **Claude Code 拡張機能（リポジトリ同梱）**:
  - **2段フック**: `.claude/settings.json` ＋ `scripts/hook_guard.py`（`PreToolUse` 白紙保護・秘密情報遮断 ／ `PostToolUse` 構文・import・JSON検証）
  - **4サブエージェント（`Edit`/`Write` ツール非付与）**: `.claude/agents/`（`memo-extractor`, `field-mapper`, `structure-auditor`, `form-auditor`）
  - **6 Skills ＆ 1 Slash Command**: `.claude/skills/`（`form-scan`, `surgical-fill`, `structural-audit`, `form-fill-verify`, `visual-diff-audit`, `portal-form-fill`）＋ `.claude/commands/fill-form.md`（`/fill-form`）
  - **5ツール MCP サーバー（追加SDK不要・標準入出力 JSON-RPC 2.0）**: `.mcp.json` ＋ `form_guard/mcp_server.py`（`form_guard_list_demos`, `form_guard_scan`, `form_guard_discover_blank_cells`, `form_guard_fill`, `form_guard_verify`）

### 環境変数の「名前」一覧（値は記載しません。必要時のみ別PCの `.env` またはシェルに設定）

| 環境変数名 | 必須/任意 | 用途（オフラインデモ収録時はすべて未設定で動作します） |
| :--- | :---: | :--- |
| `FORM_GUARD_API_KEY` | 任意（Live時のみ） | `--mode live` 等で実際の Claude API を呼ぶ場合の API キー（推奨：`.env` に記述） |
| `ANTHROPIC_API_KEY` | 任意（Live時のみ） | `FORM_GUARD_API_KEY` 未設定時のフォールバック用 API キー |
| `FORM_GUARD_FORCE_DEMO` | 任意 | `1` に設定すると、PC内に API キーがあっても通信を行わず高速なオフラインデモモードに固定（収録時のトラブル防止に推奨） |
| `FORM_GUARD_MODE` | 任意 | 既定の実行モード指定（`auto` / `demo` / `live`） |
| `FORM_GUARD_ACCESS_KEY` | 任意（Tunnel時） | `scripts/share_tunnel.ps1` で一時公開トンネルを開く際の合言葉（スクリプトが自動設定） |
| `FORM_GUARD_ALLOWED_HOSTS` | 任意（Tunnel時） | 一時公開時に許可するホスト名（通常は `127.0.0.1` / `localhost` のみ） |
| `FORM_GUARD_HOOK_PYTHON` | 任意 | Claude Code フックが使う Python パスの明示指定（未設定時は `.venv` を自動優先） |

### 新宿 PC での30秒起動手順（コピペ用）

```powershell
# 1. クローンしてフォルダへ移動
git clone https://github.com/drmiura-jpg/form-guard-agent.git C:\hackathon\form-guard-agent
cd C:\hackathon\form-guard-agent

# 2. ワンクリック環境構築＆Webサーバー起動（.venv作成・pip install・http://127.0.0.1:8000 起動まで自動）
powershell -ExecutionPolicy Bypass -File .\scripts\setup_remote_demo.ps1
```

手動で1行ずつ起動する場合：
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Web サーバー起動（APIキー不要・オフラインでも即動作）
python -m form_guard.cli --serve --port 8000
```
- **かんたん5ステップ画面（一番わかりやすいメイン画面）**: `http://127.0.0.1:8000/start`
- **詳細検証＆審査員向け90秒ガイドツアー画面**: `http://127.0.0.1:8000/#tour=1`
- **全277件の自動テスト実行**: `.\.venv\Scripts\python.exe -m pytest -q`（`277 passed`）

---

## 3. デプロイ済み URL の有無・審査員用のテストアカウントの有無

- **デプロイ済み URL**: **なし（ローカル即時起動型）**
  - 申請書類という機密性の高い文書を扱う性質上、外部クラウドへファイルを送信せず手元（`127.0.0.1`）で完結する設計を基本としています。
- **審査員用のテストアカウント・APIキー**: **一切不要**
  - 審査員が `git clone` して `python -m form_guard.cli --serve`（または `--all-demos --mode demo`）を実行するだけで、**有料アカウントも API キーもログインも一切なしで**、全10種の架空様式デモ・21項目検査・視覚比較・人間承認ゲートを追試できます。

---

## 4. 現在動く機能 ／ 動かない機能・既知の制限事項（`277 passed, 0 failed` 確認済み）

### ✅ 現在動く機能
- **Web UI（`/start` かんたん5ステップ画面 ＆ `/` 詳細検証画面 ＆ `/portal` 模擬公募ポータル）**: 完全動作。
- **同梱10様式すべてのピンポイント記入・21項目検査・視覚比較（`VIS-01`）・人間承認ゲート・検査済みファイル出力**:
  - `form1`（共同研究計画変更届出書 `.docx`：8/8欄記入・無変更17パーツ生バイト複製・21/21合格・ずれ `0 px`）
  - `form2`（省エネ設備導入補助金交付申請書 `.docx`：上限100万円超過の自動差し戻し修復＋メモに無い法人番号を `needs_human_input` に安全保持）
  - `form3`（月次設備安全点検報告書 `.xlsx`：`COUNTIF`/`SUM` 数式セル不変・結合セル保護）
  - `form4`〜`form10`（マーカーなし白紙様式 `.docx` / `.xlsx` / 固定 `.pdf`：4段階空欄自動発見・事務局専用欄の赤ロック・模擬Gmail添付取込デモを含む）
- **CLI 一括実行（`python -m form_guard.cli --all-demos --mode demo`）**: 完全動作。
- **Claude Code MCP 5ツール連携（`.mcp.json`）＆ 2段階フック（`scripts/hook_guard.py`）**: 完全動作。

### ⚠️ 既知の制限事項（バグではなく仕様として `README.md` に明記済みの範囲）
1. **AI（MCP）からの直接承認は「仕様として」不可**:
   - MCP の `form_guard_fill` 実行時点では常に `UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF`（未承認）になります。必ずブラウザで人間が確認者名を入れて承認ボタンを押すことでダウンロードが解錠されます（これが安全設計の核心です）。
2. **固定 PDF の対応範囲**:
   - ページ内に Unicode エンコードの CID フォントを持つ固定レイアウト PDF（`form6` 等）が対象です。スキャン画像だけの PDF や暗号化 PDF、旧バイナリ形式（`.doc` / `.xls`）は壊さずに安全に拒否（`unsupported`）します。
3. **`pytest` 実行時の注意**:
   - グローバルの Python ではなく、必ずリポジトリ直下で **`.venv\Scripts\python.exe -m pytest -q`** を使って実行してください。

---

## 5. デモで見せるべき一番の「決め所」シナリオ（3分＝180秒タイムライン準拠）

ご提示いただいた「画面操作中心の3分構成（`0:00–0:20` 課題 → `0:20–2:20` 実演3パターン → `2:20–2:50` 仕組み → `2:50–3:00` 冒頭の一文で締め）」に沿って、ブラウザ操作だけで失敗なく撮れる手順です。

| 時間帯 | パート | 画面で見せる操作（`http://127.0.0.1:8000`） | 審査員に見せる「セーフガード作動」の決め所 |
| :---: | :--- | :--- | :--- |
| **0:00–0:20**<br>*(20秒)* | **① 課題提示**<br>（ガードがない時の危険） | `http://127.0.0.1:8000/#tour=1`（または `/start` の STEP 2 比較）を開き、左側の赤い失敗再現ファイル（`✗ 7項目不合格`）を見せる。 | 生成AIに書類を丸ごと作り直させると、パッと見は似ていても**結合セル19個や罫線定義が消え、用紙がA4→レターに変わって差し戻される危険**を1シーンで見せる。 |
| **0:20–1:00**<br>*(40秒)* | **② 実演パターン1**<br>（白紙空欄発見＆事務局欄ロック） | `http://127.0.0.1:8000/start` で「📧 Gmail受信箱から選ぶ」→「国内出張申請書（`form7`）」（または `#tour=2` の `form4`）を選択し、空欄自動発見を実行。 | **セーフガード①作動**: 目印のない白紙から空欄を検出し、**申請者が書く欄（緑チェック）** と **事務局専用欄（承認印・旅費精算番号・受付番号など：赤ロック）** を自動仕分けして誤記入を防止！ |
| **1:00–1:40**<br>*(40秒)* | **③ 実演パターン2**<br>（原文引用・上限修正・推測拒否） | `#tour=3`（「記入値と根拠」タブ）をクリックし、各欄の黄色い引用タグと `form2`（補助金申請書）の金額・法人番号欄を表示。 | **セーフガード②作動**: すべての記入値に**元メモの行番号と黄色ハイライト引用**が付く。上限100万円を超える金額（161.2万円）は100万円へ自動修正され、メモに無い**13桁の法人番号はAIが創作せず `人の入力待ち` で安全停止**！ |
| **1:40–2:20**<br>*(40秒)* | **④ 実演パターン3**<br>（左右比較・21項目検査・人間承認解錠） | `/start` の STEP 5（または `#tour=5`）で白紙原本（左）と記入後（右）を比較 → 21項目検査（`21/21 PASS`）を表示 → 確認者名（`山田 太郎`）を入れて「承認してダウンロード」をクリック。 | **セーフガード③作動**: 指定欄外の描画ずれ **`0 px`**・無変更ZIPパーツSHA-256完全一致を21項目で証明。AIは自己承認できず、**人が名前を入れて承認した瞬間だけ緑のダウンロードボタンが解錠**！ |
| **2:20–2:50**<br>*(30秒)* | **⑤ 仕組みと検証規模** | `#tour=4`（「Claude Code 連携」タブ）を開き、3階層モデル・前後フック・4サブエージェント・5ツールMCP・全10様式を見せる。 | 欄の難易度に応じた **Haiku 4.5 / Sonnet 5 / Opus 5.5** の使い分け、白紙改変を防ぐ `PreToolUse` フック、**自動テスト277件合格＆公開190様式試験で構造改変0件** を一言で紹介。 |
| **2:50–3:00**<br>*(10秒)* | **⑥ 結び**<br>（公式指定の冒頭一文） | 承認済みの画面（またはダウンロードして開いた Word 申請書）を映して締める。 | **「このプロジェクトは、大学・自治体・医療現場などの申請業務で『指定様式の罫線や計算式が崩れると差し戻されるためAIを使えない』と困っている担当者・研究者の課題を、元の白紙様式は一切触らず指定欄だけをピンポイントで書き換え、欄外の不変を自動検査して人が最終承認する形に変える書類記入エージェントです。」** |

### （補足）スライド形式で手元クリック進行・ワイプ収録したい場合の同梱ファイル
- **14枚・60段階クリック表示デッキ**: `C:\hackathon\demo_video_output\FormGuard_Agent_ClickStep_Pitch_Deck_16x9.pptx`（および `175.00秒` ミニシアターワイプ動画 `FormGuard_Agent_Presenter_Wipe_Demo_175s.mp4`、台本 `docs/PRESENTER_WIPE_FULL_SCRIPT_JA.md`）
- **13枚パラパラ切り替え＆右下ワイプ対応デッキ**: `docs/FormGuard_Agent_Pitch_Deck_13Slides_Flip_Wipe_16x9.pptx`（台本 `docs/PRESENTATION_SPEECH_13SLIDES_FLIP_JA.md`）

---

## 6. 提出フォーム用テキスト（概要500字以内・全文は `docs/SUBMISSION_FORM_DRAFT_JA.md` に収録済み）

### 概要 500字以内（公式指定「冒頭の一文」完全準拠・421字）
```text
このプロジェクトは、大学・自治体・医療現場などの申請業務で「指定様式の罫線や計算式が崩れると差し戻されるためAIを使えない」と困っている担当者・研究者の課題を、元の白紙様式は一切触らず指定欄だけをピンポイントで書き換え、欄外の不変を自動検査して人が最終承認する形に変える書類記入エージェントです。
開発者自身が倫理申請等で何度も差し戻され「もうAIを使うのはやめようか」と思った実体験から開発しました。7区分・21項目の構造検査と内蔵の配置比較（付属様式でずれ0px・実機表示は人が確認）で欄外を検査し、目印のない白紙様式でも申請者欄と事務局専用欄を4段階で自動仕分けして誤記入をロックします。
AI抽出値には元メモの原文引用スパンを紐づけ（年の補完は推定表示）、メモにない事実は推測で埋めず人の入力待ちに残し、人が最終確認するまでダウンロードを解錠しません。Web・CLI・Claude Code MCPで現場の定型様式業務に活用できます。
```

---

## 7. GitHub リポジトリの URL と公開設定チェックリスト

- **リポジトリ URL**: `https://github.com/drmiura-jpg/form-guard-agent`
- **公開（Public）確認手順**:
  1. 鴨川側で公開リポジトリ作成 ＆ `git push -u origin main` を実行後、新宿の PC で `git clone https://github.com/drmiura-jpg/form-guard-agent.git` が通ることを確認します。
  2. 18:00 の提出時および 18:00–20:00 の最終確認時に、ブラウザの**シークレットウィンドウ（未ログイン状態）**で以下2つが開けることを必ず確認します：
     - GitHub リポジトリ（`https://github.com/drmiura-jpg/form-guard-agent`）が `Public` で表示されること
     - デモ動画 URL（YouTube **限定公開** または Loom `Anyone with the link can view`）がログインなしで再生できること
