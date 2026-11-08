# Form-Guard Agent — 収録・提出用PC引き継ぎ・収録・提出マスターガイド (`docs/HANDOFF.md`)

- **最終更新**: 2026-11-08（日）
- **対象大会**: Built with Claude: Japan 2026（Anthropic / Cerebral Valley 主催）
- **公式提出締切**: **2026-11-08（日）20:00 JST**（目標提出時刻: **2026-11-08（日）18:00 JST** ／ **2026-11-08（日）17:31 JST 公式フォーム提出・保存確認済み**）

---

## 0. 先に聞かれている3つの質問への即答（結論）

| ご質問 | 回答（結論） |
| :--- | :--- |
| **1. セーフガード / Form-Guard Agent は Web アプリですか？ デプロイ済みの URL はありますか？** | ・**ローカルで起動する FastAPI Web アプリ（＋ CLI ＋ Claude Code MCP サーバー）** です。<br>・**常時稼働のクラウドデプロイ URL はありません**（公式リマインドでも「アプリケーションリンクがあれば」と任意項目になっており、ローカル再現手順で審査可能です）。<br>・初回に `.venv\Scripts\python.exe -m pip install -r requirements.txt`（初回はネット接続が必要）を済ませた後は、`$env:FORM_GUARD_FORCE_DEMO="1"; .\.venv\Scripts\python.exe -m form_guard.cli --serve --port 8000` を実行すると **ローカル Web アプリ（`http://127.0.0.1:8000` で `form1`〜`form6`、`http://127.0.0.1:8000/start` で `form4`〜`form10` の計10様式）が起動** し、**APIキーなし（オフラインデモモード）で同梱10種の架空デモ様式と21項目検査・視覚Diff・人間承認ゲートが動作することを自動テスト277件で確認済み** です。<br>・※外部からブラウザで開く一時URLが必要な場合は、`pwsh`（PowerShell 7）と `cloudflared`（`winget install --id Cloudflare.cloudflared`）を入れ、`.\.venv\Scripts\Activate.ps1` を実行したうえで、`setup_remote_demo.ps1` で起動した8000番サーバーを先に止めるか `pwsh scripts/share_tunnel.ps1 -Port 8001` を指定して一時発行できます。 |
| **2. GitHub リポジトリの URL はわかりますか？** | ・**リポジトリ URL**: `https://github.com/drmiura-jpg/form-guard-agent`<br>・**現在の公開状態（2026-11-08（日）17:31 JST 提出完了時点）**: **Public 公開・`main` ブランチへの push 確認済み（未ログイン照会でも HTTP 200 を確認済み）** です。`.env`・APIキー・個人パス混入 0件も全履歴スキャンで確認済みであり、別PC（クリーン環境）からそのまま `git clone https://github.com/drmiura-jpg/form-guard-agent.git` で取得できます。 |
| **3. 収録・提出用 PC に Git と Node/Python などの実行環境は入っていますか？** | ・**開発用PC側からは収録・提出用PCの環境は未確認**ですので、収録・提出用PCの PowerShell で以下の2行を実行して確認してください：<br>　`git --version`<br>　`python --version`（**Python 3.10 以上**であればOK）<br>・Web UI の実演・収録だけなら Node.js は不要です（Claude Code CLI ターミナルも画面に映す場合のみ `node --version` と `claude --version` を確認してください）。<br>・Git と Python 3.10+ が入っていれば、同梱の `powershell -ExecutionPolicy Bypass -File .\scripts\setup_remote_demo.ps1` を実行することで、`.venv` 作成・`pip install`（初回導入時はネット接続が必要）・Web サーバーのオフラインデモ起動（`FORM_GUARD_FORCE_DEMO=1` 既定・`/api/health` および `/start` の HTTP 応答確認つき）まで完了します（※Claude Desktop の設定ファイル `%APPDATA%\Claude\claude_desktop_config.json` は既定では変更せず、必要な場合のみ `-ConfigureClaudeDesktop` スイッチを付けたときに更新します）。 |

---

## 1. プロダクト概要（誰の・どんな課題を・どう変えるか）

### 冒頭の一文（提出した概要の1文目・146字）
> **このプロジェクトは、行政・大学・施設の指定様式（Word・Excel・固定PDF）を扱う担当者の「AIで下書きすると罫線や結合セル・定型文まで壊れて差し戻される」課題を、指定欄だけを書き換え21項目＋視覚比較で構造不変と原文根拠を確かめてから人間が承認する形に変える書類記入エージェントです。**

### 誰の・どんな課題か（開発者の原体験）
- 開発者自身が倫理申請などの書類作成で何度も差し戻しに遭い、AIで試しても表や罫線が崩れてしまい「もうAIを使うのはやめようか」とまで思った実体験から出発しています（出典：`docs/SUBMISSION_FORM_DRAFT_JA.md` `#4`・`#10`、および `README.md` 日本語版）。
- 日本の大学・研究機関・自治体・企業の現場では、Word（`.docx`）・Excel（`.xlsx`）・固定レイアウト PDF（`.pdf`）の指定様式に対し**「元の書式・罫線・結合セル・計算式・定型文を1箇所も変えないこと」**が求められます。

### どう変えるか（5重のセーフガード）
1. **Guardrail ①（白紙原本の改変阻止フック）**:
   - Claude Code の `PreToolUse` フック（`scripts/hook_guard.py`）が `samples/blank_forms/` の白紙原本に対する直接書き込みを `exit 2`（`deny`）で自動遮断します。
2. **Guardrail ②（目印のない白紙様式からの4段階・空欄発見＆事務局欄ロック）**:
   - `{{マーカー}}` のない白紙様式（`form4`〜`form10`）から空欄を検出し、**「申請者記入欄（緑チェック）」** と **「事務局専用欄（承認印・受付番号・受付日・Excelの `SUM`/`COUNTIF` 数式セルなど：赤ロック）」** を自動仕分けして誤記入をロックします。
3. **Guardrail ③（原文引用スパン照合・推測ゼロ設計・上限自己修正）**:
   - すべての記入値に元メモの**原文引用（行番号・黄色ハイライト）**を紐づけます。
   - メモに書かれていない事実（13桁の法人番号など）は当てずっぽうで埋めず **`needs_human_input`（人の入力待ち）** に残します。
   - ライブAPIモード（`--mode live`）では欄の難易度に応じて **Haiku 4.5**（日付・和暦・番号・空欄分類）、**Sonnet 5**（氏名・金額・自由記述の原文引用抽出）、**Opus 5.5**（200字以内の要約短縮と上限違反等の再提案）を使い分けます（オフラインデモモード `--mode demo` では決定論的ルールエンジン `DemoEngine` が同じ検査・変換ロジックを通信なしで実行し、画面のモデル表示は Live `auto` 時のルーティング計画を示します）。
4. **Guardrail ④（外科的ピンポイント置換＋7区分・21項目の構造検査＆視覚比較 `VIS-01`）**:
   - Word / Excel の対象テキストノードのバイト範囲（固定PDFは末尾への増分追記オーバーレイ）だけを書き換え、無変更の ZIP パーツ（例：`form1` の18パーツ中17パーツ）は再圧縮すら行わず生バイト複製します。
   - 記入直後に **7区分・21項目の決定論的構造検査** と **内蔵の視覚配置比較（`VIS-01`：付属様式の内蔵描画による測定値で指定欄外のずれ `0 px`。最終的な実機の Word / Excel 表示・印刷は人が確認）** を自動実行します。
5. **Guardrail ⑤（MCP承認ツール非搭載＋Web人間承認ゲート）**:
   - MCP には承認ツールを設けず、Web 画面（`/start` または `/`）で人が最終承認する運用です。サーバーの HTTP API は確認者名・必須チェック項目・版（revision）・SHA-256 一致を検証してダウンロードを解錠します（※HTTP リクエストの操作主体が人間か AI かを技術的に識別するものではありません）。

---

## 2. 技術構成と起動手順（必要なツール・環境変数の「名前」のみ）

### 技術スタック
- **言語・ランタイム**: Python 3.10 以上（開発環境の `.venv` は 3.13.9 で `pytest` 277件合格を確認）、Git
- **Web フレームワーク**: FastAPI + Uvicorn（`form_guard/server.py`）
- **文書解析・ピンポイント置換エンジン**: `zipfile` / `lxml`（OOXML の生バイト保持置換）、`python-docx` / `openpyxl`（独立した読み込み確認・描画）、固定PDFは追加PDFライブラリなしの自前実装（`form_guard/pdf_core.py`・`pdf_support.py`：増分更新の重ね書き）、`Pillow`（PNG プレビューと固定PDFの画素比較・内蔵レイアウト比較 `VIS-01`）、`pydantic`。依存の正本は `requirements.txt`
- **AI モデル連携**: Anthropic Python SDK（`anthropic`）による3階層ルーティング（`form_guard/mapper.py`）※APIキー未設定時または `FORM_GUARD_FORCE_DEMO=1` 時は自動的に決定論的ルールエンジン `DemoEngine` で動作
- **Claude Code 拡張機能（リポジトリ同梱）**:
   - **2段フック**: `.claude/settings.json` ＋ `scripts/hook_guard.py`（`PreToolUse` 白紙保護・秘密情報遮断 ／ `PostToolUse` 構文・import・JSON検証）
   - **4サブエージェント（`Edit`/`Write` ツール非付与）**: `.claude/agents/`（`memo-extractor`, `field-mapper`, `structure-auditor`, `form-auditor`）
   - **6 Skills ＆ 1 Slash Command**: `.claude/skills/`（`form-scan`, `surgical-fill`, `structural-audit`, `form-fill-verify`, `visual-diff-audit`, `portal-form-fill`）＋ `.claude/commands/fill-form.md`（`/fill-form`）
   - **5ツール MCP サーバー（追加SDK不要・標準入出力 JSON-RPC 2.0）**: `.mcp.json` ＋ `form_guard/mcp_server.py`（`form_guard_list_demos`, `form_guard_scan`, `form_guard_discover_blank_cells`, `form_guard_fill`, `form_guard_verify`）

### 環境変数の「名前」一覧（値は記載しません。必要時のみ別PCの `.env` またはシェルに設定）

| 環境変数名 | 必須/任意 | 用途（オフラインデモ収録時はすべて未設定または `FORM_GUARD_FORCE_DEMO=1` で動作します） |
| :--- | :---: | :--- |
| `FORM_GUARD_API_KEY` | 任意（Live時のみ） | `--mode live` 等で実際の Claude API を呼ぶ場合の API キー（推奨：`.env` に記述） |
| `ANTHROPIC_API_KEY` | 任意（Live時のみ） | `FORM_GUARD_API_KEY` 未設定時のフォールバック用 API キー |
| `FORM_GUARD_FORCE_DEMO` | 任意（Web単体用） | `1` に設定すると、PC内や `.env` に API キーがあっても通信を行わず Web 画面をオフラインデモモードに固定します（`setup_remote_demo.ps1` の既定値）。<br>**⚠ 注意**: `FORM_GUARD_FORCE_DEMO=1` のときは公開トンネル保護のため MCP の下書き（`output/mcp_runs/` の `review_url`）を取り込めないので、Claude Code / MCP 連携を見せる回では外す（`setup_remote_demo.ps1 -AllowLive` または変数解除＋MCP ツール引数 `mode="demo"`）ようにしてください。 |
| `FORM_GUARD_MCP_ROOT` | 任意（MCP時） | MCP サーバー（`form_guard/mcp_server.py`）が読み書きを許可する基準フォルダ（リポジトリの絶対パス） |
| `FORM_GUARD_ACCESS_KEY` | 任意（Tunnel時） | `scripts/share_tunnel.ps1` で一時公開トンネルを開く際の合言葉（スクリプトが自動設定） |
| `FORM_GUARD_ALLOWED_HOSTS` | 任意（Tunnel時） | 一時公開時に許可するホスト名（通常は `127.0.0.1` / `localhost` のみ） |
| `FORM_GUARD_HOOK_PYTHON` | 任意 | Claude Code フックが使う Python パスの明示指定（未設定時は `.venv` を自動優先） |

- ※実行モードは環境変数ではなく CLI の `--mode auto / demo / live` で指定します（Web 画面をオフラインに固定するときは `FORM_GUARD_FORCE_DEMO=1`）。

### 別PC（クリーン環境）での初回セットアップ手順（コピペ用・所要時間未計測・初回は `pip install` にネット接続が必要）

> **収録時の `.env` に関する注意**:
> - 収録PCでは `.env` を作らないことを推奨します。
> - もし `.env` や環境変数に API キーを入れる場合は、サーバーが `.env` を読んで画面の既定がライブAPIになるため、画面だけの収録の前に `$env:FORM_GUARD_FORCE_DEMO = "1"` を設定してから起動してください（`setup_remote_demo.ps1` は既定で `FORM_GUARD_FORCE_DEMO=1` を子プロセスに設定します）。
> - ただし `1` のときは MCP の下書き（`review_url`）を取り込めないので、Claude Code 連携を見せる回では外してください（`-AllowLive` を指定）。

```powershell
# 1. クローンしてフォルダへ移動
git clone https://github.com/drmiura-jpg/form-guard-agent.git C:\hackathon\form-guard-agent
cd C:\hackathon\form-guard-agent

# 2. 初回セットアップ＆Webサーバー起動（既定はオフラインデモ固定・Claude Desktop設定は変更しない）
powershell -ExecutionPolicy Bypass -File .\scripts\setup_remote_demo.ps1

# ※Claude Desktop（Claude のデスクトップアプリ）にも MCP を登録したい場合のみ：
# powershell -ExecutionPolicy Bypass -File .\scripts\setup_remote_demo.ps1 -ConfigureClaudeDesktop
```

手動で1行ずつ起動する場合（PowerShell の実行ポリシーに左右されない `.venv\Scripts\python.exe` 直接指定）：
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Web サーバー起動（オフラインデモ固定・APIキー不要）
$env:FORM_GUARD_FORCE_DEMO = "1"
.\.venv\Scripts\python.exe -m form_guard.cli --serve --port 8000
```

初回導入後の2回目以降の再起動（ネット接続不要・仮想環境の Python で直接起動）：
```powershell
cd C:\hackathon\form-guard-agent
$env:FORM_GUARD_FORCE_DEMO = "1"
.\.venv\Scripts\python.exe -m form_guard.cli --serve --port 8000
```

バックグラウンド起動した 8000 番ポートのこのフォルダの Form-Guard Web サーバーだけを止める場合（起動元コンソールの場合は `Ctrl+C` を基本とし、バックグラウンド起動時は 8000 番の待受 PID とこのフォルダの `.venv\Scripts\python.exe` パスが一致するときだけその PID を停止）：
```powershell
$expPy = [IO.Path]::GetFullPath('.\.venv\Scripts\python.exe'); Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction Ignore | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { $w = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $_) -ErrorAction Ignore; if ($w -and $w.CommandLine -and ($w.CommandLine.IndexOf($expPy, [StringComparison]::OrdinalIgnoreCase) -ge 0)) { Stop-Process -Id $_ -Force } }
```

- **かんたん5ステップ画面（`form4`〜`form10` の7様式）**: `http://127.0.0.1:8000/start`
- **詳細検証＆審査員向け90秒ガイドツアー画面（`form1`〜`form6` の6様式）**: `http://127.0.0.1:8000/#tour=1`
- **全277件の自動テスト実行**: `.\.venv\Scripts\python.exe -m pytest -q`（自動テスト277件で確認済み）

---

## 3. デプロイ済み URL の有無・審査員用のテストアカウントの有無

- **デプロイ済み URL**: **なし（ローカル起動型）**
  - 申請書類という機密性の高い文書を扱う性質上、Form-Guard のオフラインデモモード（`--mode demo` または `FORM_GUARD_FORCE_DEMO=1`）では外部へファイルを送信せず手元（`127.0.0.1`）で完結します（※`--mode live` のライブAPI利用時のみメモ全文と対象欄定義を Anthropic API に送信し、Claude Desktop / Claude Code チャットに入力した文面は Claude サービス側で処理されます）。
- **審査員用のテストアカウント・APIキー**: **一切不要**
  - 審査員が `git clone` して `.venv` 作成と `pip install -r requirements.txt` のあと `.venv\Scripts\python.exe -m form_guard.cli --serve` で、APIキーもログインも無しに、Web 画面（`/` で `form1`〜`form6`、`/start` で `form4`〜`form10`、重複を除き計10様式）で全10種・21項目検査・視覚比較・人間承認ゲートを試せます（CLI の `--all-demos --mode demo` は印のある3様式 `form1`〜`form3` の一括実行であり、承認・ダウンロードは Web 画面で確認します）。

---

## 4. 現在動く機能 ／ 動かない機能・既知の制限事項（自動テスト277件で確認済み）

### ✅ 現在動く機能
- **Web UI（`/start` かんたん5ステップ画面 ＆ `/` 詳細検証画面 ＆ `/portal` 模擬公募ポータル）**: 自動テスト277件で確認済み。
- **Web UI での同梱10様式すべてのピンポイント記入・21項目検査・視覚比較（`VIS-01`）・人間承認ゲート・検査済みファイル出力**:
  - `form1`（共同研究計画変更届出書 `.docx`：8/8欄記入・無変更17パーツ生バイト複製・21/21合格・内蔵比較の欄外ずれ `0 px`）
  - `form2`（省エネ設備導入補助金交付申請書 `.docx`：上限100万円超過の自動差し戻し修復＋メモに無い法人番号を `needs_human_input` に安全保持）
  - `form3`（月次設備安全点検報告書 `.xlsx`：`COUNTIF`/`SUM` 数式セル不変・結合セル保護）
  - `form4`〜`form10`（マーカーなし白紙様式 `.docx` / `.xlsx` / 固定 `.pdf`：4段階空欄自動発見・事務局専用欄の赤ロック・模擬Gmail添付取込デモを含む）
- **CLI 一括実行（`.venv\Scripts\python.exe -m form_guard.cli --all-demos --mode demo`）**: 印のある3様式（`form1`〜`form3`）の一括記入・21項目検査・レポート出力が動作します（自動テスト277件で確認済み。※全10様式と人間承認ゲート・ダウンロード解錠は Web 画面で確認します）。
- **Claude Code MCP 5ツール連携（`.mcp.json`）＆ 2段階フック（`scripts/hook_guard.py`）**: 自動テスト277件で確認済み。

### ⚠️ 既知の制限事項（バグではなく仕様として `README.md` に明記済みの範囲）
1. **MCP からの直接承認は「仕様として」不可**:
   - MCP には承認ツールを設けず、`form_guard_fill` 実行時点では常に `UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF`（未承認）になります。最終承認は人が Web 画面（`/start` または `/`）で行う運用であり、サーバーの HTTP API は確認者名・必須チェック項目・版（revision）・SHA-256 一致を検証してダウンロードを解錠します（※HTTP API 自体は人と AI を技術的に識別しません）。
2. **固定 PDF の対応範囲**:
   - ページ内に Unicode エンコードの CID フォントを持つ固定レイアウト PDF（`form6` 等）が対象です。スキャン画像だけの PDF や暗号化 PDF、旧バイナリ形式（`.doc` / `.xls`）は壊さずに安全に拒否（`unsupported`）します。
3. **`pytest` 実行時の注意**:
   - グローバルの Python ではなく、必ずリポジトリ直下で **`.venv\Scripts\python.exe -m pytest -q`** を使って実行してください。

---

## 5. デモで見せるべき一番の「決め所」シナリオ（3分＝180秒タイムラインの当初案・Web 画面ツアー版）

> **注記**: この表は Web 画面のガイドツアー（`#tour=1`〜）で見せる当初案（180秒）です。提出した2分44秒の動画（`4MvCEiBFhgU`）は、下の「提出済みデモ動画」のとおり Claude Code → `/start` → Word の実演で、この表の構成ではありません。

| 時間帯 | パート | 画面で見せる操作と画面切替手順（`http://127.0.0.1:8000`） | 審査員に見せる「セーフガード作動」の決め所 |
| :---: | :--- | :--- | :--- |
| **0:00–0:20**<br>*(20秒)* | **① 課題提示**<br>（ガードがない時の危険） | 詳細画面 `http://127.0.0.1:8000/#tour=1`（または `/start` の STEP 2 比較）を開き、左側の赤い失敗再現ファイル（`✗ 7項目不合格`）を見せる。 | 生成AIに書類を丸ごと作り直させると、パッと見は似ていても**結合セル19個や罫線定義が消え、用紙がA4→レターに変わって差し戻される危険**を1シーンで見せる。 |
| **0:20–1:00**<br>*(40秒)* | **② 実演パターン1**<br>（白紙空欄発見＆事務局欄ロック） | 詳細画面 `http://127.0.0.1:8000/#tour=2`（デモ4 `form4`：施設・備品一時使用許可申請書 `.docx`、または別タブで開いた `/start` の `form7` 出張申請書）に切り替え、目印のない白紙様式に対する空欄自動発見と色分けプレビューを表示する。 | **セーフガード（Guardrail ②：空欄発見・事務局欄ロック）**: 目印のない白紙から空欄を検出し、**申請者が書く欄（緑チェック）** と **事務局専用欄（受付番号・受付日・審査欄・事務局受付欄など：赤ロック）** を自動仕分けして誤記入を防止！ |
| **1:00–1:40**<br>*(40秒)* | **③ 実演パターン2**<br>（原文引用・上限修正・推測拒否） | 詳細画面のデモ選択で **デモ2（`form2`：省エネ補助金申請書）** を選び（または `#tour=3` の「記入値と根拠」タブを開き）、各欄の黄色い引用タグ、補助金申請額欄（161.2万円→上限100万円）、および法人番号欄（`人の入力待ち`）を表示する（※`form2` は法人番号が未入力のため承認ロック中であることを確認）。 | **セーフガード（Guardrail ③：原文引用・上限修正・推測拒否）**: すべての記入値に**元メモの行番号と黄色ハイライト引用**が付く。上限100万円を超える金額（161.2万円）は100万円へ自動修正され、メモに無い**13桁の法人番号はAIが創作せず `人の入力待ち` で安全停止**！ |
| **1:40–2:20**<br>*(40秒)* | **④ 実演パターン3**<br>（左右比較・21項目検査・人間承認解錠） | 承認可能な未入力ゼロの様式へ切り替える：詳細画面で **デモ1（`form1`：共同研究計画変更届出書、未入力 0件・`21/21 PASS`）** を選ぶ（または別タブ `/start` の `form7` STEP 5 を開く）→ 白紙原本（左）と記入後（右）を比較 → 21項目検査（`21/21 PASS`）を表示 → **人間承認欄の必須チェックボックスをすべてチェック** → 確認者名（`山田 太郎`）を入力 → **「✅ 承認してダウンロード」** をクリックして `.docx` を取得する。 | **セーフガード（Guardrail ④・⑤：21項目検査・人間承認ロック解錠）**: 構造検査21項目に合格し、ずれ `0 px` は付属様式の内蔵描画比較（`VIS-01`）による測定値で最終的な実機表示・印刷は人が確認。MCPには承認ツールを設けず人がWeb画面で最終承認する運用であり（サーバーAPIは確認者名・必須チェック・版・SHA-256を検査し人とAI自体は識別しない）、**未入力ゼロ・21/21合格・全項目チェック・確認者名入力が揃った瞬間だけダウンロードが解錠**！ |
| **2:20–2:50**<br>*(30秒)* | **⑤ 仕組みと検証規模** | `#tour=4`（「Claude Code 連携」タブ）を開き、前後フック・4サブエージェント・5ツールMCP・全10様式を見せる。 | 白紙改変を防ぐ `PreToolUse` フック（**Guardrail ①**）、3階層モデル表示（オフライン実行時のモデル表示は Live `auto` 時のルーティング計画：Haiku 4.5 / Sonnet 5 / Opus 5.5）、**自動テスト277件合格＆公開190様式のオフライン合成メモ構造試験（安全拒否・入力待ちを含め構造改変0件）** を一言で紹介。 |
| **2:50–3:00**<br>*(10秒)* | **⑥ 結び**<br>（冒頭の一文で締める） | 承認済みの画面とテロップを映し、10秒で無理なく読める動画ナレーション用短縮文（62字）を読み上げて締める。 | **【動画ナレーション用短縮文（62字・10秒用）】**<br>「このプロジェクトは、申請担当者の様式崩れの悩みを、指定欄だけの記入と21項目検査・人の承認に変える書類記入エージェントです。」<br>**【提出した概要欄の1文目（146字・参照用）】**<br>「このプロジェクトは、行政・大学・施設の指定様式（Word・Excel・固定PDF）を扱う担当者の「AIで下書きすると罫線や結合セル・定型文まで壊れて差し戻される」課題を、指定欄だけを書き換え21項目＋視覚比較で構造不変と原文根拠を確かめてから人間が承認する形に変える書類記入エージェントです。」 |

### （補足）提出済みデモ動画（YouTube 限定公開）とリポジトリ同梱の参考素材
- **提出済みデモ動画（2分44秒［`163.967秒`］・1080p・YouTube 限定公開）**:
  - **URL**: `https://youtu.be/4MvCEiBFhgU`（標準URL: `https://www.youtube.com/watch?v=4MvCEiBFhgU`）。中身は Claude Code の `/fill-form` → `/start` での確認と承認 → 承認した `.docx` を Microsoft Word で開く、までの実際の操作の録画です（記入はお試しモード）。175秒版の前の台本は `docs/DEMO_VIDEO_SCRIPT_JA.md`。
- **リポジトリに入っている参考素材（`git clone` で取得可能）**:
  - **13枚デッキ**: `docs/FormGuard_Agent_Pitch_Deck_13Slides_Flip_Wipe_16x9.pptx`（台本 `docs/PRESENTATION_SPEECH_13SLIDES_FLIP_JA.md`）
  - **3枚・170秒構成デッキ**: `docs/FormGuard_Agent_Pitch_Deck_170s_3Slides_16x9.pptx`（台本 `docs/PRESENTATION_SPEECH_170S_JA.md`）

---

## 6. 提出フォーム用テキスト（概要500字以内・全文は `docs/SUBMISSION_FORM_DRAFT_JA.md` に収録済み）

### 概要 500字以内（公式指定「冒頭の一文」＋具体的な利用場面完全準拠・改行込み461字／改行除き457字）
```text
このプロジェクトは、行政・大学・施設の指定様式（Word・Excel・固定PDF）を扱う担当者の「AIで下書きすると罫線や結合セル・定型文まで壊れて差し戻される」課題を、指定欄だけを書き換え21項目＋視覚比較で構造不変と原文根拠を確かめてから人間が承認する形に変える書類記入エージェントです。

開発者自身も倫理申請などで差し戻しを重ねた経験から作りました。例えば大学の研究事務担当者が研究計画変更届の白紙Wordと打ち合わせメモを渡すと、指定欄だけが埋まり、各値に原文の1行引用が付きます。Web画面では担当者が引用を確かめて承認するまでダウンロードできず、補助金申請ではメモに無い法人番号を推測で埋めず人に回します。

Form-Guard Agentは①マーカーなし白紙の空欄自動発見、②欄ごとのHaiku 4.5／Sonnet 5／Opus 5.5振り分け、③未変更部分の生バイト保持とPDF増分更新、④21項目＋VIS-01検査とSHA-256連動の承認ロックを、Web画面とClaude Codeで提供します。
```

---

## 7. GitHub リポジトリの URL と公開設定チェックリスト

- **リポジトリ URL**: `https://github.com/drmiura-jpg/form-guard-agent`
- **現在の状態（2026-11-08（日）17:31 JST 提出完了時点）**:
  - **Public（公開）リポジトリ作成 ＆ `main` ブランチへの push 確認済み（HTTP 200）** です。
- **公開（Public）確認手順**:
  1. 別PC（クリーン環境）で `git clone https://github.com/drmiura-jpg/form-guard-agent.git`（既に clone 済みの場合は `git pull origin main`）を実行し、最新の `docs/HANDOFF.md` と `scripts/setup_remote_demo.ps1` が揃っていることを確認します。
  2. 提出時および最終確認時に、ブラウザの**シークレットウィンドウ（未ログイン状態）**で以下2つが開けることを必ず確認します：
     - GitHub リポジトリ（`https://github.com/drmiura-jpg/form-guard-agent`）が `Public` で表示されること
     - デモ動画 URL（`https://youtu.be/4MvCEiBFhgU`・YouTube **限定公開**・1080p）がログインなしで再生できること
