# リモートPCの Claude デスクトップアプリ（Claude Desktop）でのデモ画面作成・実演向けおすすめ指示・情報ガイド

- **作成日**: 2026-11-08（日）
- **対象**: リモートPC（収録・提出用PC）の Claude Desktop でデモ画面作成・実演を行う際の手順・指示文・注意点
- **関連正本**: `C:/hackathon/form-guard-agent/docs/HANDOFF.md`、`C:/hackathon/form-guard-agent/docs/REMOTE_CLAUDE_LIVE_DEMO_MASTER_JA.md`、`C:/hackathon/form-guard-agent/scripts/setup_remote_demo.ps1`

---

## 1. 最重要：Claude Desktop からブラウザへ引き継ぐときの起動スイッチ

`scripts/setup_remote_demo.ps1` は、安全のため既定（スイッチなし）では以下の動作をします：
1. `%APPDATA%\Claude\claude_desktop_config.json`（Claude Desktop の全体設定）を変更しない。
2. `$env:FORM_GUARD_FORCE_DEMO = "1"` を設定して Web サーバーを起動する（この状態では公開トンネル保護のため、`output/mcp_runs/` の MCP 下書きを `start_url` でブラウザに取り込む機能が無効化される：`form_guard/server.py` 443行目）。

そのため、**リモートPCの Claude Desktop で MCP ツールを動かして下書きを作り、発行された `start_url`（`http://127.0.0.1:8000/start?run_id=...`）をブラウザで開いて人間承認まで繋げる場合**は、リモートPCの PowerShell で以下の手順を実行します：

```powershell
cd C:\hackathon\form-guard-agent
git pull origin main

# 1. -AllowLive 対応の最新スクリプトが入っているか確認（1行出力されればOK。何も出なければ git pull を確認）
Select-String -Path .\scripts\setup_remote_demo.ps1 -Pattern AllowLive -List

# 2. 同じ窓に過去の FORM_GUARD_FORCE_DEMO=1 や API キー環境変数が残っていると影響するためクリアする
Remove-Item Env:FORM_GUARD_FORCE_DEMO, Env:FORM_GUARD_API_KEY, Env:ANTHROPIC_API_KEY, Env:ANTHROPIC_AUTH_TOKEN -ErrorAction SilentlyContinue

# 3. 既に起動済みの Form-Guard サーバーがある場合は停止する
# （起動コンソールが開いている場合はそこで Ctrl+C。バックグラウンド起動の場合は、実行引数で Form-Guard と確認できたプロセスだけを停止し、別アプリが8000番を使っている場合は止めない）
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'form_guard\.cli.*--serve' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

# 4. Claude Desktop への MCP 登録 (-ConfigureClaudeDesktop) ＋ MCP下書きのブラウザ引き継ぎ有効 (-AllowLive) で起動
powershell -ExecutionPolicy Bypass -File .\scripts\setup_remote_demo.ps1 -ConfigureClaudeDesktop -AllowLive
```

> **💡 補足**: すでに Claude Desktop のツール一覧に `form-guard` の 5 ツールが出ているなら、`-ConfigureClaudeDesktop` を外して `powershell -ExecutionPolicy Bypass -File .\scripts\setup_remote_demo.ps1 -AllowLive` だけで起動すれば、Claude Desktop の再起動は要りません。

- **注意点①（オフライン実行の維持と Web 画面のモード確認）**:
  - 外部 AI API を使わないのは、`mode="demo"` を指定した Form-Guard の処理です（Claude Desktop のチャットに入力した文面自体は Claude サービスで処理されます）。
  - `-AllowLive` を付けると Web サーバーの強制オフライン（`FORM_GUARD_FORCE_DEMO=1`）が外れます。MCP ツールは下の指示文のとおり `mode="demo"` を付ければ API キーがあってもオフラインの `DemoEngine` で動きますが、ブラウザの詳細画面（`/#tour=1`・`/#tour=3`）は既定の「自動（`auto`）」で動くため、PC に `.env` や環境変数（`FORM_GUARD_API_KEY`・`ANTHROPIC_API_KEY`・`ANTHROPIC_AUTH_TOKEN`）があると本物の Claude API を呼び得ます。
  - そのため、録画する PC では `.env` を作らず、上の起動コマンドの前に `Remove-Item Env:FORM_GUARD_API_KEY, Env:ANTHROPIC_API_KEY, Env:ANTHROPIC_AUTH_TOKEN` を実行し、収録前に `http://127.0.0.1:8000/api/health` で `live_available=false`・`default_mode="demo"`（詳細画面のエンジン表示が **「オフライン（API キーなし）」**）になっていることを確かめてから撮ります（確認できない場合は `form2` の補足実演を始めないでください）。
- **注意点②（Claude Desktop の完全再起動）**:
  - `-ConfigureClaudeDesktop` を実行したあとは、ウィンドウ右上の「×」だけでなく、**画面右下タスクトレイの Claude アイコンを右クリック →「Quit（終了）」してから再起動** すると `form-guard` の 5 つの MCP ツール（`form_guard_list_demos`, `form_guard_scan`, `form_guard_discover_blank_cells`, `form_guard_fill`, `form_guard_verify`）が読み込まれます。
- **注意点③（3ツール一巡のリハーサルと事前許可で録画中のポップアップを防ぐ）**:
  - 録画の前に、下のターン1・ターン2を一度リハーサルとして通しで実行し、本番で使う 3 つのツール（`form_guard_discover_blank_cells`・`form_guard_fill`・`form_guard_verify`）それぞれで出る確認ダイアログの挙動を確認して許可設定（`Always allow` 等が表示される場合は選択）を済ませ、`start_url` のブラウザ取り込みまで確かめます。
  - **本番の録画は新しいチャットを開き、新しい未承認下書きを生成して撮ります**（ツールが見当たらないときは入力欄の「＋」かツールアイコンで 5 ツールが出ているか確かめ、出ていなければ `%APPDATA%\Claude\logs\mcp*.log` を確認します）。

---

## 2. Claude Desktop にそのまま貼る「おすすめ指示文（2ターン完結）」

画面（1920×1080）を **左半分：Claude Desktop（`Ctrl` + `+` で文字を少し拡大）**、**右半分：ブラウザ（`http://127.0.0.1:8000/#tour=1` または `/start`）** に並べ、以下の 2 ターンを順に貼る構成です（もと：`docs/REMOTE_CLAUDE_LIVE_DEMO_MASTER_JA.md` 第5節の台本を一部調整）。

### 🎬 ターン1の指示文（Guardrail ②：白紙の出張申請書から空欄9個を発見し、申請者7欄と総務部2欄［ロック］を自動仕分けさせる）

```text
出張申請書（samples/blank_forms/form7_business_trip.docx）を作りたいです。
今回の申請メモは以下のとおりです（すべて架空のデモ設定です）：

申請日：2026年11月28日（土）
申請者氏名：見本 七海
所属部署：営業部 第二営業課
出張先：架空県架空市（架空産業展示会 会場）
出張期間：2026年12月3日（木）から2026年12月4日（金）まで
用務内容：新製品の展示と取引先3社への説明のため出張する。帰社後は、展示会でヒアリングした導入時期と必要数量を提案書へ反映する。
概算旅費：62,800円（新幹線往復と宿泊1泊を含む）

まず Form-Guard の MCP（form_guard_discover_blank_cells, form_path="samples/blank_forms/form7_business_trip.docx", mode="demo"）で白紙様式の空欄を調べて、「申請者が記入する欄（✓）」と「総務部専用の欄（[LOCK] ロック対象）」を見やすい表で出してください。まだ記入はしないでください。
```

### 🎬 ターン2の指示文（Guardrail ③・④：7欄だけピンポイント記入＋和暦変換＋21項目検査＋未承認下書きURL発行）

```text
ありがとうございます。申請者が書く推奨欄 c01〜c07 の7欄で確定します（c08 承認印・c09 旅費精算番号は空欄のまま維持）。
1. 記入：form_path="samples/blank_forms/form7_business_trip.docx"、memo_text（上の申請メモ全文）、discovery（手順1の結果）、confirmed_ids=["c01","c02","c03","c04","c05","c06","c07"]、mode="demo" を form_guard_fill に渡してピンポイント記入してください。
2. 検査：original_path="samples/blank_forms/form7_business_trip.docx"、filled_path に直前の form_guard_fill が返した draft_path、slot_spec_path に同結果の slot_spec_path、memo_text に申請メモ全文を渡して form_guard_verify で検査し、ツールが返した実結果（overall_status, passed, failed, pending_slots, visual.checked, max_shift_px, approval_status）のまま、記入値・21項目検査の合否と件数・VIS-01 の欄外のずれ・承認状態・form_guard_fill が返したブラウザ確認用の start_url を短くまとめて出してください。
```
- ※**リハーサルで確かめる期待値（指示文の中には期待値を書かず、ツールの実返却値をそのまま表示させます）**: `overall_status="PASS"`、`passed=21`、`failed=0`、`pending_slots=0`、`visual.checked=true`、`max_shift_px=0`（付属様式の内蔵描画比較 `VIS-01` による測定値）、`approval_status="UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF"`。万一失敗・未検証・入力待ちが出た場合は承認へ進みません。

### 🎬 ターン3（右半分のブラウザ操作：Guardrail ⑤ 人間承認ゲート）
1. Claude Desktop が出力した **`http://127.0.0.1:8000/start?run_id=...`** を右側のブラウザで開きます。
2. 「② 書類の見た目を比べる（白紙 vs AI記入後）」の **「📄 記入前（白紙）」（左）と「✅ 記入後（AI が書いたもの）」（右）** を映し、緑の枠の 7 欄だけに文字が入り、枠の外の罫線・注意書きと総務部の 2 欄（承認印・旅費精算番号：枠のない空欄のまま）が白紙と同じであることを見せます（※この画面には総務部欄のロック枠は出ません。ロック仕分けはターン1で Claude が出す表の `[LOCK]` で見せます）。
3. 下へスクロールして **「③ あなたが目で確かめること」の必須チェックボックスをすべてチェック** → 確認者名に **`山田 太郎`** と入力 → **「✅ 承認してダウンロード」** をクリックして `.docx` が解錠される瞬間を見せます（最終的な実機 Word 表示・印刷は人が確認する運用です）。
4. **補足（Guardrail ③ の上限修正・推測拒否も見せる場合）**: ブラウザの別タブで開いておいた **`http://127.0.0.1:8000/#tour=3`（デモ2 `form2`：省エネ設備導入補助金交付申請書）** に切り替えると「記入値と根拠」タブが開きます。上の説明帯にあるリンク **「補助金申請額（上限で再抽出）→」** をクリックして `1,612,000円 → 1,000,000円`、続けて **「法人番号（人の入力待ち）→」** をクリックしてメモにない法人番号が `人の入力待ち`（未入力のため承認ロック中）であることを、2 クリックで順に見せます。

---

## 3. Claude Desktop で「デモ用ビジュアル画面（Artifacts）」を作らせている場合の指示文

もし Claude Desktop の **Artifacts（HTML/React プレビュー画面）** で冒頭の課題提示（`0:00–0:20`）や結び（`2:50–3:00`）の補助画面を作らせている場合は、以下の指示を追加すると録画時の視認性と事実整合が保たれます（冒頭はなるべく実画面 `http://127.0.0.1:8000/#tour=1` を映し、Artifact は補助テロップとして使うのがおすすめです）：

```text
画面録画（1920x1080）で小さな画面の審査員が見てもすぐ読めるように、以下のデザインルールで作成してください：
1. 背景は白または明るいグレー（#F8FAFC）、文字は濃いネイビー（#0F172A）で高コントラストにする。
2. 1画面の文字数は原則40〜60字以内に絞り（3. の比較の文と 4. の結びの短縮文62字は例外として全文をそのまま表示してよい）、本文は28px以上、見出しは44px以上にする。
3. 冒頭（0:00–0:20）は左右比較にする。左（赤枠）の見出しは「失敗を再現した比較用の模擬ファイル（AIの実測結果ではない）」とし、中身は「結合セル19→0・罫線定義9→0・用紙A4→レター・7項目不合格」と表示する（左が実際のAI出力ではなく失敗再現用の模擬ファイルであることを左の見出しに常時明記する）。右（緑枠）は「Form-Guard：指定欄だけ置き換え・21項目検査 21/21 合格」とする。
4. 結び（2:50–3:00）の画面は、10秒で無理なく読める以下の短縮文（62字）だけを大きく中央に表示する（見出しのラベル文字は画面に出さず、本文だけを1文字も変えずに表示する）：
   このプロジェクトは、申請担当者の様式崩れの悩みを、指定欄だけの記入と21項目検査・人の承認に変える書類記入エージェントです。
```
- ※**提出フォーム概要欄1文目用の公式定型文（148字・参照用）**:
  - 画面に62字と148字を同時表示すると210字になり文字が小さくなるため、結びの画面表示・読み上げは上の62字短縮文とし、以下の公式148字定型文は提出フォーム概要欄の1文目（または全画面単独テロップ）として1文字も変えずに使用します：
  - `このプロジェクトは、大学・自治体・医療現場などの申請業務で「指定様式の罫線や計算式が崩れると差し戻されるためAIを使えない」と困っている担当者・研究者の課題を、元の白紙様式は一切触らず指定欄だけをピンポイントで書き換え、欄外の不変を自動検査して人が最終承認する形に変える書類記入エージェントです。`
