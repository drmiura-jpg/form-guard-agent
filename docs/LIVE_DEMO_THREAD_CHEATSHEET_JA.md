# 「デモ＞プレゼン」実演スレッド作成＆画面収録カンペ（2026-11-08（日）更新）

運営からの12時間前リマインド（**「デモ＞プレゼン。スライドではなく、実際に使っている姿を見たい。」**）に直結する、**Claude Code で実際にデモ用スレッドを動かして申請書を完成させる実演手順**です。

---

## 💡 最初に結論：クリーン環境（方法B）と開発PC分離環境（方法A）の使い分け

| 比較項目 | 方法A：開発PCの隔離環境（追加導入不要・すぐ起動） | 方法B：別PC（クリーン環境）に入れて撮る（導入の所要時間は未計測） |
| :--- | :--- | :--- |
| **アカウント** | 分離環境（`.claude-hackathon`）で起動したときだけ普段の設定と分かれる（録画前に `/status` で確認） | 別PCでハッカソン用アカウントに新規ログイン（録画前に `/status` で確認） |
| **個人パス** | スクリプト相対パスへ移動（起動画面のメール表示と `/status` は録画外で確認） | `C:\hackathon\form-guard-agent` に置けば非表示（メール表示は録画外で確認） |
| **MCP・Web** | `.mcp.json`・Web確認画面・Word/Excel が連動 | 別PCで `setup_remote_demo.ps1` を実行して `.venv` と Web サーバーを準備 |
| **おすすめの役割** | **予備の録画・事前リハーサル** | **本番の実演録画と提出・審査員と同じ `git clone` で動くかの再現確認** |

- 別PCへの環境導入時間は通信・Node.js/Python導入状況・認証により変動するため未計測です。
- 実演録画では、Claude Code に渡す `FORM_GUARD_FORCE_DEMO=1` と台本の `mode="demo"` で記入をオフライン（固定ルール）にします。Web サーバーには `FORM_GUARD_FORCE_DEMO=1` を付けません（付けると `start_url` の下書き取り込みが無効になるため）。
- このデモ映像を Haiku・Sonnet・Opus の3モデルAPI通信の実行証拠としては説明しません。

---

## 🚀 方法A：開発PC（または設定済み環境）でデモ用スレッドを立ち上げる手順

- **手順1：専用ランチャーまたは `scripts\launch_clean_demo.cmd` を実行**します。
  - バッチ内部でスクリプト相対パス（`%~dp0..`）へ移動し、まず `FORM_GUARD_FORCE_DEMO=1` を付けないオフラインデモ設定（`live_available=false`・`default_mode=demo`）で `http://127.0.0.1:8000/api/health` と `http://127.0.0.1:8000/start` の HTTP 応答および 8000 番の待受プロセス（このフォルダの `.venv\Scripts\python.exe`）を確認し（失敗時はエラーを表示して停止）、その後に Claude Code / MCP 側へ `FORM_GUARD_FORCE_DEMO=1` を設定して起動します。
  - 開発PCでは分離環境（`%USERPROFILE%\.claude-hackathon\claude-hackathon.cmd`）で起動し、分離ラッパーが無いPCでは `CLAUDE_CONFIG_DIR` 設定または `scripts\launch_clean_demo.cmd --remote` 指定時のみ起動します。
  - **録画を始める前に**、画面に映さずに `/status` でハッカソン用アカウントか確かめ、`/mcp` で `form-guard` の接続を確認してから `cls` で画面を消します。
- **手順2：「🎬 3ターン実演コピペ台本」を順番に貼り付けて実行**します。
  - 録画後に思考の待ち時間だけを切り、設計書 第5節の116秒の枠および運営指定の180秒以内に収まるかをリハーサルで測ります。

---

## 💻 方法B：別PC（クリーン環境）にダウンロードして本番収録する3ステップ

### ステップ1：リポジトリ一式の配置と Python・Webサーバー準備
- 公開設定を確認した GitHub リポジトリからの `git clone` を基本とします（ZIP時は公開前確認済みのファイルだけで作成し、`.git`・`.venv`・`.env`・`output`・ローカル認証設定を含めません）。
- 別PCの PowerShell で以下の2行を実行します：
```powershell
cd C:\hackathon\form-guard-agent
powershell -ExecutionPolicy Bypass -File .\scripts\setup_remote_demo.ps1 -AllowLive
```
- `-AllowLive` は Web サーバーを `FORM_GUARD_FORCE_DEMO=1` に固定しない指定です（付けないとターン3で `start_url` の下書きが開けません）。別PCに `.env` や API キー（環境変数を含む）を置かなければ、Web 画面もお試しモードのままで API は呼ばれません。先に引数なしで起動してしまったときは、`docs/HANDOFF.md` 2節のコマンドで Form-Guard のサーバーだけ止めてから実行し直します。
- `.venv` 作成・`pip install`・Web サーバー起動と `/api/health`・`/start` の応答確認まで自動で行われます（Claude Desktop の MCP 設定は `-ConfigureClaudeDesktop` を付けたときだけ更新します。Claude Code CLI で実演するなら不要で、`.mcp.json` が使われます）。完了後に `http://127.0.0.1:8000/api/health` の `live_available` が `false`（`default_mode` が `demo`）であり、`http://127.0.0.1:8000/start` が開くか確かめます。
- 手動で打つ場合は `Activate.ps1` を使わず、`python -V`（3.10以上）を確認して `python -m venv .venv` を作り、`.\.venv\Scripts\python.exe -m pip install -r requirements.txt` と `.\.venv\Scripts\python.exe -m pip install -e .` を実行し（作成と各インストールが非ゼロ終了した場合は停止）、`-WorkingDirectory (Get-Location).Path` 付きでサーバーを起動します。

### ステップ2：Claude Code のインストールとハッカソンアカウントログイン
- 別PCで Claude Code を導入してログインします（Node.js がある場合。Node.js が無い場合は、公式ドキュメントの Setup ページにある Windows 向けの入れ方で入れます）：
```powershell
npm install -g @anthropic-ai/claude-code
cd C:\hackathon\form-guard-agent
claude auth login
```
- ブラウザが開いたら **ハッカソンの Max 5x コードを適用したアカウント** でログインし、録画外で `/status` を確認したあと `cls` でクリアしてから録画を開始します。

### ステップ3：デモ用スレッドの開始
- `C:\hackathon\form-guard-agent` の中で `scripts\launch_clean_demo.cmd --remote`（または `claude`）を起動すると、`.mcp.json` と `.claude/` が読み込まれます。
- **初めて開くフォルダでは**確認が出ることがあるので、**録画前に1回起動して済ませ、`/mcp` で `form-guard` がつながっていることを確かめます**（`/status` はメールアドレスが出るため画面に映しません）。

---

## 🎬 3ターン実演コピペ台本（台本の MCP 操作は白紙原本に書き込みません。記入は `output/mcp_runs/` の写しに作られます）

- 原本保持は収録前後の白紙原本 SHA-256 ハッシュ完全一致で検証します。
- 実演では Claude Code から MCP を呼び、MCP 内部の分類・記入は `mode="demo"`（オフライン決定論規則処理）で実演します。
- 出張先・日付・金額は同梱メモ（`samples/memos/memo7_business_trip.txt`）を正本とし、相違があれば記入前に確認します。

### 【ターン1：白紙の出張申請書とメモを渡し、空欄自動発見＆文章整理を頼む】
```text
samples/blank_forms/form7_business_trip.docx と @samples/memos/memo7_business_trip.txt を見てください。
架空県架空市の架空産業展示会への出張（2026年11月24日（火）〜11月25日（水）、新製品の説明と取引先3社との打合せ、概算旅費58,400円、申請日2026年11月12日（木））で、メモのとおりに申請書を作りたいです。
まず Form-Guard の MCP（form_guard_discover_blank_cells, mode="demo"）で白紙様式の空欄を調べて、「申請者が書く欄」と「総務部用の欄（ロック対象）」を表で見せてください。
```

### 【ターン2：推奨7欄だけにピンポイント記入＆21項目検査を実行し、確認用 `start_url` を出させる】
```text
ありがとうございます。同梱メモ（memo_path="samples/memos/memo7_business_trip.txt"）を正として、申請者が書く c01〜c07 の7欄だけに、用務内容を100字以内でわかりやすく整えて Form-Guard の MCP（form_guard_fill, mode="demo"）でピンポイント記入し、21項目の検査結果とブラウザ確認用の start_url（http://127.0.0.1:8000/start?run_id=...）を出してください。総務部欄（c08, c09）は空欄のまま守ってください。
```

### 【ターン3：出てきた `start_url`（`http://127.0.0.1:8000/start?run_id=...`）をブラウザで開いて見せる】
- **確認1**：ターミナルに出た `start_url`（`http://127.0.0.1:8000/start?run_id=...`）をクリックしてブラウザで開きます（`review_url` の `http://127.0.0.1:8000/?run_id=...` は詳細検証画面であり、5ステップ画面は `start_url` です）。
- **確認2**：「🔍 STEP 5 詳細レポートでチェック」で、上の判定（✅）と「① 機械がチェックした8つの安全項目」を見せ、「② 書類の見た目を比べる（白紙 vs AI記入後）」の「左右に並べて比較」で、色のついた枠の7欄だけに文字が入り、指定欄以外の配置差分と検査結果を確認します（入力待ちの欄があるときは STEP 4 が開きます）。
- **確認3**：原文・記入値・指定欄・表示を確認し、「③ あなたが目で確かめること」の確認項目をすべてチェックします（未確認・入力待ち・不合格が残る場合は承認せず、解消後に進めます）。
- **確認4**：確認者名（例：`山田 太郎`）を入れて「✅ 承認してダウンロード」を押し、ダウンロードされた `.docx` を実機の Word で開いて改ページ・罫線・文字切れに問題がないことを確認して見せます（問題があれば完成扱いにしません）。

---

## 🛡️ 提出・GitHub 反映前の最終安全確認
- GitHub へ push する前には必ず `git status --short` と `git diff --cached` を確認し、`.env`・`.venv`・`output/`・個人ユーザー名パスが含まれていないことを確かめます。
- **提出目標**：**2026-11-08（日）18:00 JST**（**公式締切：2026-11-08（日）20:00 JST**）
