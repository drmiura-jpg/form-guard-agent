# 「デモ＞プレゼン」実演スレッド作成＆画面収録カンペ（2026-11-08（日）更新）

運営からの12時間前リマインド（**「デモ＞プレゼン。スライドではなく、実際に使っている姿を見たい。」**）に直結する、**Claude Code で実際にデモ用スレッドを動かして申請書を完成させる実演手順**です。

---

## 💡 最初に結論：手元PC（このPC）とリモートPC、どちらで撮るのが早いか？

| 比較項目 | 方法A：このPCの隔離環境（追加導入不要・すぐ起動） | 方法B：リモートPCに入れて撮る（移行 約15〜20分） |
| :--- | :--- | :--- |
| **アカウント** | ハッカソン専用 Max 5x でログイン済・個人設定と完全分離 | リモートPCでハッカソン用アカウントに新規ログイン |
| **個人パス** | `C:\hackathon\form-guard-agent>` 表示（起動画面のメール表示と `/status` に注意） | `C:\hackathon\form-guard-agent` に置けば非表示（起動画面のメール表示に注意） |
| **MCP・Web** | `.mcp.json`・Web確認画面・Word/Excel がすぐ連動 | リモートPC側にも本体一式と `.venv` の配置が必要 |
| **おすすめの役割** | **本番の「Claude Code → MCP → Web確認 → Word」実演録画** | **GitHub公開後に審査員と同じ手順で `git clone` して動くかの再現テスト（またはチャット画面の単独収録）** |

---

## 🚀 方法A：このPCでクリーンなデモ用スレッドを立ち上げる手順（追加インストール不要）

1. **`C:\hackathon\form-guard-agent\scripts\launch_clean_demo.cmd` をダブルクリック**（またはターミナルで実行）します。
   - 裏で自動的に `http://127.0.0.1:8000`（Form-Guard Webサーバー）が起動し、画面をクリアしたうえでハッカソン専用 Claude Code が立ち上がります。
2. 下の **「🎬 3ターン実演コピペ台本」** を順番に貼り付けて実行します（録画後に思考待ち時間だけをカット編集すれば、60〜90秒で超濃密な実演パートになります）。

---

## 💻 方法B：リモートPCにダウンロードしてデモ用スレッドを作る場合の3ステップ

リモートPCで「Claude Code から Form-Guard MCP を呼んで Web 画面で確認・承認する姿」まで動かす場合は、以下の3ステップで行えます。

1. **ステップ1（リポジトリ一式の配置と Python 準備）**：
   - このPCの `C:\hackathon\form-guard-agent` フォルダ（`.git` や `output` を除いた本体、または GitHub push 後のリポジトリ）をリモートPCの `C:\hackathon\form-guard-agent` に置きます。
   - リモートPCの PowerShell で以下を実行します：
     ```powershell
     cd C:\hackathon\form-guard-agent
     python -m venv .venv
     .\.venv\Scripts\activate
     pip install -r requirements.txt
     Start-Process .\.venv\Scripts\python.exe -ArgumentList "-m form_guard.cli --serve --port 8000" -WindowStyle Hidden
     ```
2. **ステップ2（Claude Code のインストールとハッカソンアカウントログイン）**：
   - リモートPCで Claude Code をインストールしてログインします（Node.js がある場合）：
     ```powershell
     npm install -g @anthropic-ai/claude-code
     cd C:\hackathon\form-guard-agent
     claude login
     ```
   - ※ブラウザが開いたら、**ハッカソンの Max 5x コードを適用したアカウント** でログインします（録画中にメールアドレスが映らないよう、ログイン完了後に画面を `cls` でクリアしてから録画開始します）。
3. **ステップ3（デモ用スレッドの開始）**：
   - `C:\hackathon\form-guard-agent` の中で `claude` を起動すると、同フォルダの `.mcp.json`（`form-guard` 5ツール）と `.claude/`（Skills・サブエージェント・2段フック）が自動で読み込まれます。

---

## 🎬 3ターン実演コピペ台本（どれを貼っても白紙原本は1バイトも傷つきません）

### 【ターン1：白紙の出張申請書とメモを渡し、空欄自動発見＆文章整理を頼む】
```text
@samples/blank_forms/form7_business_trip.docx と @samples/memos/memo7_business_trip.txt を見てください。
12月3日（木）〜12月4日（金）の大阪出張（新製品の展示と取引先3社への説明、帰社後に導入時期と必要数量を提案書へ反映、概算旅費62,800円、申請日2026年11月28日（土））で申請書を作りたいです。
まず Form-Guard の MCP（form_guard_discover_blank_cells, mode="demo"）で白紙様式の空欄を調べて、「申請者が書く欄」と「総務部用の欄（ロック対象）」を表で見せてください。
```

### 【ターン2：推奨7欄だけにピンポイント記入＆21項目検査を実行し、確認URLを出させる】
```text
ありがとうございます。申請者が書く c01〜c07 の7欄だけに、用務内容を100字以内でわかりやすく整えて Form-Guard の MCP（form_guard_fill, mode="demo"）でピンポイント記入し、21項目の検査結果とブラウザ確認用の review_url を出してください。総務部欄（c08, c09）は空欄のまま守ってください。
```

### 【ターン3：出てきた `review_url`（`http://127.0.0.1:8000/start?run_id=...`）をブラウザで開いて見せる】
1. ターミナルに出た `http://127.0.0.1:8000/start?run_id=...`（または `http://127.0.0.1:8000/?run_id=...`）をクリックしてブラウザで開きます。
2. **「STEP 5：白紙の原本と記入後を並べて確認」** で、指定した7欄（緑枠）だけが入っており、総務部欄（赤ロック）や罫線が1ミリも崩れていないこと、21項目すべて PASS していることを見せます。
3. 確認者名（例：`山田 太郎`）を入れて **「内容を確認して承認する（ロック解除）」** を押し、解錠された `.docx` をダウンロードして実機の Word で開いて見せます。
