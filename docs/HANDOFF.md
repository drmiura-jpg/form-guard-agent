# Form-Guard Agent — 収録・提出用PC引き継ぎ・収録・提出マスターガイド (`docs/HANDOFF.md`)

- **最終更新**: 2026-11-08（日）
- **対象大会**: Built with Claude: Japan 2026（Anthropic / Cerebral Valley 主催）
- **公式提出締切**: **2026-11-08（日）20:00 JST**（目標提出時刻: **2026-11-08（日）18:00 JST**）

---

## 0. 先に聞かれている3つの質問への即答（結論）

| ご質問 | 回答（結論） |
| :--- | :--- |
| **1. セーフガード / Form-Guard Agent は Web アプリですか？ デプロイ済みの URL はありますか？** | ・**ローカルで起動する FastAPI Web アプリ（＋ CLI ＋ Claude Code MCP サーバー）** です。<br>・**常時稼働のクラウドデプロイ URL はありません**（公式リマインドでも「アプリケーションリンクがあれば」と任意項目になっており、ローカル再現手順で審査可能です）。<br>・初回に `.venv\Scripts\python.exe -m pip install -r requirements.txt`（ネット接続が必要）を済ませた後は、`.venv\Scripts\python.exe -m form_guard.cli --serve --port 8000` を実行すると **ローカル Web アプリ（`http://127.0.0.1:8000` および `http://127.0.0.1:8000/start`）が起動** し、**APIキーなし（オフラインデモモード）で同梱10種の架空デモ様式と21項目検査・視覚Diff・人間承認ゲートが動作することを開発用PCで確認済み（自動テスト `pytest` 277 passed）** です。<br>・※外部からブラウザで開く一時URLが必要な場合は、同梱の `pwsh scripts/share_tunnel.ps1`（Cloudflare Quick Tunnel・合言葉保護つき）で一時発行できます。 |
| **2. GitHub リポジトリの URL はわかりますか？** | ・**予定 URL**: `https://github.com/drmiura-jpg/form-guard-agent`<br>・**現在の公開状態（2026-11-08（日）時点）**: 開発用PCのローカル Git（`C:\hackathon\form-guard-agent`、`main` ブランチ）には全コミット（`.env`・APIキー・個人パス混入 0件を全履歴スキャンで確認済み）が揃っていますが、**GitHub 上のリモートリポジトリ `drmiura-jpg/form-guard-agent` はまだ作成・push されておらず（未 push のため現時点では `git clone` 不可）、本人の OK 後に Public リポジトリ作成 ＆ `git push -u origin main` を実行する状態**です。<br>・※GitHub へ push する前に別PCへ渡す場合は、生成済みの ZIP パッケージ（軽量版 `form-guard-agent-slim-code-only.zip` または完全版 `form-guard-agent-remote-demo-pack.zip`）を展開してすぐ起動できます。 |
| **3. 収録・提出用 PC に Git と Node/Python などの実行環境は入っていますか？** | ・**開発用PC側からは収録・提出用PCの環境は未確認**ですので、収録・提出用PCの PowerShell で以下の2行を実行して確認してください：<br>　`git --version`<br>　`python --version`（**Python 3.10 以上**であればOK）<br>・Web UI の実演・収録だけなら Node.js は不要です（Claude Code CLI ターミナルも画面に映す場合のみ `node --version` と `claude --version` を確認してください）。<br>・Git と Python 3.10+ が入っていれば、同梱の `powershell -ExecutionPolicy Bypass -File .\scripts\setup_remote_demo.ps1` を実行することで、`.venv` 作成・`pip install`（初回のみネット接続が必要）・Claude Desktop の MCP 設定登録・Web サーバー起動（`/api/health` 応答確認つき）まで自動で完了します。 |

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
   - ライブAPIモード（`--mode live`）では欄の難易度に応じて **Haiku 4.5**（日付・和暦・番号・空欄分類）、**Sonnet 5**（氏名・金額・自由記述の原文引用抽出）、**Opus 5.5**（200字以内の要約短縮と上限違反等の再提案）を使い分けます（オフラインデモモード `--mode demo` では決定論的ルールエンジン `DemoEngine` が同じ検査・変換ロジックを通信なしで実行します）。
4. **Guardrail ④（外科的ピンポイント置換＋7区分・21項目の構造検査＆視覚比較 `VIS-01`）**:
   - Word / Excel の対象テキストノードのバイト範囲（固定PDFは末尾への増分追記オーバーレイ）だけを書き換え、無変更の ZIP パーツ（例：`form1` の18パーツ中17パーツ）は再圧縮すら行わず生バイト複製します。
   - 記入直後に **7区分・21項目の決定論的構造検査** と **内蔵の視覚配置比較（`VIS-01`：付属デモで指定欄外のずれ `0 px`。実機の Word / Excel 表示・印刷は人が最終確認）** を自動実行します。
5. **Guardrail ⑤（MCP承認ツール非搭載＋Web人間承認ゲート）**:
   - MCP には承認ツールを設けず、運用上の最終承認は人が Web 画面（`/start` または `/`）で行います。HTTP API は必須確認項目の全チェック・版（revision）・SHA-256 一致・确认者名入力を検査してダウンロードを解錠します（※HTTP API 自体は生体認証等で操作主体が人間かを技術的に証明するものではありません）。

---

## 2. 技術構成と起動手順（必要なツール・環境変数の「名前」のみ）

### 技術スタック
- **言語・ランタイム**: Python 3.10+（推奨 3.11 / 3.12）、Git
- **Web フレームワーク**: FastAPI + Uvicorn（`form_guard/server.py`）
- **文書解析・ピンポイント置換エンジン**: `lxml` / `zipfile`（OOXML 生バイト保持置換）、固定PDFは標準ライブラリだけの自作実装（`form_guard/pdf_core.py`・`pdf_support.py`：増分更新オーバーレイ）、`Pillow`（PNG描画・PDF指定枠外の画素比較・内蔵レイアウト比較 `VIS-01`）
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
| `FORM_GUARD_FORCE_DEMO` | 任意（Web単体用） | `1` に設定すると、PC内に API キーがあっても通信を行わずオフラインデモモードに固定します。<br>**⚠ 注意**: `FORM_GUARD_FORCE_DEMO=1` は公開トンネル保護のため MCP 下書きフォルダ（`output/mcp_runs/`）のブラウザ取り込みを無効化します。**Web 画面単体で収録するときは `1` で可ですが、Claude Code / MCP で作った下書きを `start_url` でブラウザに引き継ぐ実演では `FORM_GUARD_FORCE_DEMO` を設定せず（または `0` にし）、MCP ツール呼び出し側の引数に `mode="demo"` を指定してください。** |
| `FORM_GUARD_MCP_ROOT` | 任意（MCP時） | MCP サーバー（`form_guard/mcp_server.py`）が読み書きを許可する基準フォルダ（リポジトリの絶対パス） |
| `FORM_GUARD_ACCESS_KEY` | 任意（Tunnel時） | `scripts/share_tunnel.ps1` で一時公開トンネルを開く際の合言葉（スクリプトが自動設定） |
| `FORM_GUARD_ALLOWED_HOSTS` | 任意（Tunnel時） | 一時公開時に許可するホスト名（通常は `127.0.0.1` / `localhost` のみ） |
| `FORM_GUARD_HOOK_PYTHON` | 任意 | Claude Code フックが使う Python パスの明示指定（未設定時は `.venv` を自動優先） |

- ※実行モード（`demo` / `live` / `auto`）は環境変数ではなく CLI の `--mode demo` / `--mode live` / `--mode auto`（既定 `auto`）または MCP ツール引数 `mode="demo"` で指定します。

### 収録・提出用 PC での初回セットアップ＆起動手順（コピペ用）

> **前提**: 以下の `git clone` は、開発用PCで GitHub Public リポジトリ作成 ＆ `git push -u origin main` を完了した後に実行してください。push 前に ZIP パッケージ（`form-guard-agent-slim-code-only.zip` 等）で受け取った場合は、`C:\hackathon\form-guard-agent` に解凍して `cd C:\hackathon\form-guard-agent` から始めてください。

```powershell
# 1. クローンしてフォルダへ移動（ZIP解凍済みの場合は cd のみ）
git clone https://github.com/drmiura-jpg/form-guard-agent.git C:\hackathon\form-guard-agent
cd C:\hackathon\form-guard-agent

# 2. ワンクリック環境構築＆Webサーバー起動（.venv作成・pip install・http://127.0.0.1:8000/api/health 確認まで自動）
powershell -ExecutionPolicy Bypass -File .\scripts\setup_remote_demo.ps1
```

手動で1行ずつ起動する場合（PowerShell の実行ポリシーに左右されない `.venv\Scripts\python.exe` 直接呼び出し）：
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Web サーバー起動（APIキー不要・オフラインでも即動作）
.\.venv\Scripts\python.exe -m form_guard.cli --serve --port 8000
```
- **かんたん5ステップ画面（一番わかりやすいメイン画面）**: `http://127.0.0.1:8000/start`
- **詳細検証＆審査員向け90秒ガイドツアー画面**: `http://127.0.0.1:8000/#tour=1`
- **全277件の自動テスト実行**: `.\.venv\Scripts\python.exe -m pytest -q`（`277 passed`）

---

## 3. デプロイ済み URL の有無・審査員用のテストアカウントの有無

- **デプロイ済み URL**: **なし（ローカル即時起動型）**
  - 申請書類という機密性の高い文書を扱う性質上、Form-Guard 内部のオフラインデモモード（既定）では外部クラウドへファイルを送信せず手元（`127.0.0.1`）で完結する設計を基本としています（※`--mode live` のライブAPI利用時のみメモと欄定義を Anthropic API に送信し、Claude Desktop / Claude Code チャットに入力した文面は Claude サービス側で処理されます）。
- **審査員用のテストアカウント・APIキー**: **一切不要**
  - 審査員が `git clone` して `.venv` 作成と `pip install -r requirements.txt` のあとに `.venv\Scripts\python.exe -m form_guard.cli --serve` を実行するだけで、**有料アカウントも API キーもログインも一切なしで**、Web 画面（`/` および `/start`）から全10種の架空様式デモ・21項目検査・視覚比較・人間承認ゲートを追試できます（CLI の `--all-demos --mode demo` ではマーカー付き3様式 `form1`〜`form3` の一括記入・21項目検査をコマンド1行で追試できます）。

---

## 4. 現在動く機能 ／ 動かない機能・既知の制限事項（`277 passed, 0 failed` 確認済み）

### ✅ 現在動く機能
- **Web UI（`/start` かんたん5ステップ画面 ＆ `/` 詳細検証画面 ＆ `/portal` 模擬公募ポータル）**: 動作します（自動テスト277件合格）。
- **Web UI での同梱10様式すべてのピンポイント記入・21項目検査・視覚比較（`VIS-01`）・人間承認ゲート・検査済みファイル出力**:
  - `form1`（共同研究計画変更届出書 `.docx`：8/8欄記入・無変更17パーツ生バイト複製・21/21合格・内蔵比較の欄外ずれ `0 px`）
  - `form2`（省エネ設備導入補助金交付申請書 `.docx`：上限100万円超過の自動差し戻し修復＋メモに無い法人番号を `needs_human_input` に安全保持）
  - `form3`（月次設備安全点検報告書 `.xlsx`：`COUNTIF`/`SUM` 数式セル不変・結合セル保護）
  - `form4`〜`form10`（マーカーなし白紙様式 `.docx` / `.xlsx` / 固定 `.pdf`：4段階空欄自動発見・事務局専用欄の赤ロック・模擬Gmail添付取込デモを含む）
- **CLI 一括実行（`.venv\Scripts\python.exe -m form_guard.cli --all-demos --mode demo`）**: マーカー付き3様式（`form1`〜`form3`）のピンポイント記入・21項目検査・レポート出力が動作します（※人間承認ゲートとダウンロードロックの確認は Web UI で行います）。
- **Claude Code MCP 5ツール連携（`.mcp.json`）＆ 2段階フック（`scripts/hook_guard.py`）**: 動作します。

### ⚠️ 既知の制限事項（バグではなく仕様として `README.md` に明記済みの範囲）
1. **MCP からの直接承認は「仕様として」不可**:
   - MCP には承認ツールを設けず、`form_guard_fill` 実行時点では常に `UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF`（未承認）になります。ブラウザ（`/start` または `/`）で必須の確認項目をすべてチェックし、確認者名を入れて「✅ 承認してダウンロード」を押すことでダウンロードが解錠されます。
2. **固定 PDF の対応範囲**:
   - ページ内に Unicode エンコードの CID フォントを持つ固定レイアウト PDF（`form6` 等）が対象です。スキャン画像だけの PDF や暗号化 PDF、旧バイナリ形式（`.doc` / `.xls`）は壊さずに安全に拒否（`unsupported`）します。
3. **`pytest` 実行時の注意**:
   - グローバルの Python ではなく、必ずリポジトリ直下で **`.venv\Scripts\python.exe -m pytest -q`** を使って実行してください。

---

## 5. デモで見せるべき一番の「決め所」シナリオ（3分＝180秒タイムライン準拠・本番採用版）

> **方針の統一**: 本番収録はこの第5節の「画面操作中心の4区切り構成（`0:00–0:20` 課題 → `0:20–2:20` 実演3パターン → `2:20–2:35` 仕組み → `2:35–2:58` 冒頭の一文で締め、`2:58–3:00` 余白）」を**正本（メイン台本）**とします。ブラウザ単体でも、左画面に Claude Code（MCP）を並べても同じ時間割で進行できます。

| 時間帯 | パート | 画面で見せる操作（`http://127.0.0.1:8000`） | 審査員に見せる「セーフガード作動」の決め所 |
| :---: | :--- | :--- | :--- |
| **0:00–0:20**<br>*(20秒)* | **① 課題提示**<br>（ガードがない時の危険） | `http://127.0.0.1:8000/#tour=1`（または `/start` の STEP 2 比較）を開き、左側の赤い失敗再現ファイル（`✗ 7項目不合格`）を見せる。 | 生成AIに書類を丸ごと作り直させると、パッと見は似ていても**結合セル19個や罫線定義が消え、用紙がA4→レターに変わって差し戻される危険**を1シーンで見せる。 |
| **0:20–1:00**<br>*(40秒)* | **② 実演パターン1**<br>（白紙空欄発見＆事務局欄ロック） | `http://127.0.0.1:8000/start` で「📧 Gmail受信箱から選ぶ」→「国内出張申請書（`form7`）」（または `#tour=2` の `form4`）を選択し、空欄自動発見を実行。 | **セーフガード②作動**: 目印のない白紙から空欄を検出し、**申請者が書く欄（緑チェック）** と **事務局専用欄（承認印・旅費精算番号・受付番号など：赤ロック）** を自動仕分けして誤記入を防止！ |
| **1:00–1:40**<br>*(40秒)* | **③ 実演パターン2**<br>（原文引用・上限修正・推測拒否） | `#tour=3`（「記入値と根拠」タブ）をクリックし、各欄の黄色い引用タグと `form2`（補助金申請書：別様式の機能例）の金額・法人番号欄を表示。 | **セーフガード③作動**: すべての記入値に**元メモの行番号と黄色ハイライト引用**が付く。上限100万円を超える金額（161.2万円）は100万円へ自動修正され、メモに無い**13桁の法人番号はAIが創作せず `人の入力待ち` で安全停止**！ |
| **1:40–2:20**<br>*(40秒)* | **④ 実演パターン3**<br>（左右比較・21項目検査・人間承認解錠） | `/start` の STEP 5（`form7`）に戻り、白紙原本（左）と記入後（右）を比較 → 21項目検査（`21/21 PASS`）を表示 → **「③ あなたが目で確かめること」の確認項目をすべてチェック** → 確認者名（`山田 太郎`）を入力 → **「✅ 承認してダウンロード」** をクリック。 | **セーフガード④・⑤作動**: 構造検査21項目に合格し、内蔵レイアウト比較（`VIS-01`）では指定欄外のずれ **`0 px`**（実機の Word / Excel 表示・印刷は人が別途確認）。MCPには承認ツールがなく、**人が全確認項目をチェックし名前を入れて承認した瞬間だけダウンロードが解錠**！ |
| **2:20–2:35**<br>*(15秒)* | **⑤ 仕組みと検証規模** | `#tour=4`（「Claude Code 連携」タブ）を開き、前後フック・4サブエージェント・5ツールMCP・全10様式を見せる。 | 白紙改変を防ぐ `PreToolUse` フック（**セーフガード①**）、ライブAPI時の3階層モデル（Haiku 4.5 / Sonnet 5 / Opus 5.5）、**自動テスト277件合格＆公開190様式のオフライン合成メモ構造試験（安全拒否・入力待ちを含め構造改変0件）** を一言で紹介。 |
| **2:35–2:58**<br>*(23秒＋余白2秒)* | **⑥ 結び**<br>（公式指定の冒頭一文） | 承認済みの画面（またはダウンロードして開いた Word 申請書）と148字の全文テロップを映して締める（2:58–3:00は2秒の安全余白）。 | **【画面テロップ（148字全文）】**<br>「このプロジェクトは、大学・自治体・医療現場などの申請業務で『指定様式の罫線や計算式が崩れると差し戻されるためAIを使えない』と困っている担当者・研究者の課題を、元の白紙様式は一切触らず指定欄だけをピンポイントで書き換え、欄外の不変を自動検査して人が最終承認する形に変える書類記入エージェントです。」<br>**【読み上げ用短縮版（87字・約17秒で余裕をもって読める文面）】**<br>「このプロジェクトは、様式が崩れると差し戻されるためAIを使えない申請担当者の課題を、元の様式に触れず指定欄だけを書き換え、人が最終承認する形に変える書類記入エージェントです。」 |

### （予備・今回は使わない）スライド形式で手元クリック進行・ワイプ収録したい場合の同梱ファイル
- ※本番は「スライドではなく実際に使っている姿」の方針に従い、上のブラウザ／Claude Code 画面操作を使います。予備としてリポジトリ内に同梱しているスライドは以下のとおりです：
  - **13枚パラパラ切り替え＆右下ワイプ対応デッキ（リポジトリ同梱）**: `docs/FormGuard_Agent_Pitch_Deck_13Slides_Flip_Wipe_16x9.pptx`（台本 `docs/PRESENTATION_SPEECH_13SLIDES_FLIP_JA.md`）
  - **3枚・170秒構成デッキ（リポジトリ同梱）**: `docs/FormGuard_Agent_Pitch_Deck_170s_3Slides_16x9.pptx`（台本 `docs/PRESENTATION_SPEECH_170S_JA.md`）
  - ※14枚クリック進行版（`C:\hackathon\demo_video_output\...`）はリポジトリ外のローカル出力フォルダにあるため `git clone` には含まれません。

---

## 6. 提出フォーム用テキスト（概要500字以内・全文は `docs/SUBMISSION_FORM_DRAFT_JA.md` に収録済み）

### 概要 500字以内（公式指定「冒頭の一文」完全準拠・改行込み421字／改行除き419字）
```text
このプロジェクトは、大学・自治体・医療現場などの申請業務で「指定様式の罫線や計算式が崩れると差し戻されるためAIを使えない」と困っている担当者・研究者の課題を、元の白紙様式は一切触らず指定欄だけをピンポイントで書き換え、欄外の不変を自動検査して人が最終承認する形に変える書類記入エージェントです。
開発者自身が倫理申請等で何度も差し戻され「もうAIを使うのはやめようか」と思った実体験から開発しました。7区分・21項目の構造検査と内蔵の配置比較（付属様式でずれ0px・実機表示は人が確認）で欄外を検査し、目印のない白紙様式でも申請者欄と事務局専用欄を4段階で自動仕分けして誤記入をロックします。
AI抽出値には元メモの原文引用スパンを紐づけ（年の補完は推定表示）、メモにない事実は推測で埋めず人の入力待ちに残し、人が最終確認するまでダウンロードを解錠しません。Web・CLI・Claude Code MCPで現場の定型様式業務に活用できます。
```

---

## 7. GitHub リポジトリの URL と公開設定チェックリスト

- **リポジトリ URL**: `https://github.com/drmiura-jpg/form-guard-agent`
- **現在の状態（2026-11-08（日）時点）**:
  - 開発用PCのローカル `main` ブランチにコミット済みです。GitHub 上のリモートリポジトリはまだ作成されておらず（未 push）、本人の OK の後に Public で作成し `git push -u origin main` を実行します。
- **公開（Public）確認手順**:
  1. 開発用PCで公開リポジトリ作成 ＆ `git push -u origin main` を実行後、収録・提出用 PC で `git clone https://github.com/drmiura-jpg/form-guard-agent.git` が通ることを確認します。
  2. 18:00 の提出時および 18:00–20:00 の最終確認時に、ブラウザの**シークレットウィンドウ（未ログイン状態）**で以下2つが開けることを必ず確認します：
     - GitHub リポジトリ（`https://github.com/drmiura-jpg/form-guard-agent`）が `Public` で表示されること
     - デモ動画 URL（YouTube **限定公開** または Loom `Anyone with the link can view`）がログインなしで再生できること
