# Built with Claude: Japan 2026 — 別PC完全引き継ぎ＆「デモ＞プレゼン」Guardrailsライブ実演・3分動画設計書

作成日：2026-11-08（日）
公式提出締切：**2026-11-08（日）20:00 JST（時間厳守）**

---

## 0. このファイルの目的（別PCのClaudeアプリ／Claude Codeにそのまま読ませる用）

- **1枚でハッカソン全状況を共有**：開発環境のドキュメントとローカルフォルダ（`C:\hackathon\form-guard-agent`）にある現在地・審査基準・Form-Guard Agent の全仕様を集約しています（仕様・手順の正本は [README.md](../README.md)・[BUILT_WITH_CLAUDE_CODE.md](BUILT_WITH_CLAUDE_CODE.md)・[SUBMISSION_FORM_DRAFT_JA.md](SUBMISSION_FORM_DRAFT_JA.md) を参照）。
- **単体ファイルで即実演可能**：別PCの Claude デスクトップアプリ（Claude Desktop）または Claude Code にこの `.md` ファイルを渡せば、**「いま何を作っていて、どの Guardrails（安全機構）をどう実演し、3分動画のどこにライブ録画を入れるか」** の要点を把握したうえで即座に実演を進められます。

---

## 1. ハッカソンの現在地と「デモ＞プレゼン」最優先方針

### 1-1. 大会概要・締切・審査基準（配点）
- **大会名**：Claude Code ハッカソン `Built with Claude: Japan 2026`（Anthropic / Cerebral Valley 主催、2026-11-02（月）キックオフ発表値：応募 5,541件 → 採択 250名、倍率 22.16倍）
- **公式提出締切**：**2026-11-08（日）20:00 JST**（11/8（日）20:00〜11/9（月）一次審査でトップ6選出、11/10（火）13:00〜14:30 JST 最終審査・閉会式）
- **運営からの締切12時間前公式リマインド（2026-11-08（日）08:00頃通知・最重要5項目）**：
  1. **GitHub リポジトリは Public（公開）** に設定すること
  2. **デモ動画は 3分（180秒）以内** かつ誰でも閲覧可能（YouTube 限定公開 または Loom `Anyone with the link can view`）にすること
  3. **「デモ ＞ プレゼン。スライドではなく、実際に使っている姿を見たい。」**
  4. **概要欄は 500字以内** ＋ 冒頭は一文で **『このプロジェクトは、〔誰〕の〔課題〕を〔どう変える〕〔形〕です。』** と書くこと（提出済みの概要は改行込み461字・改行除き457字・冒頭一文146字：`docs/SUBMISSION_FORM_DRAFT_JA.md` の `#4` 推奨案 4-B）
  5. 審査員用のアプリケーションリンクがあればログイン情報・再現手順を記載すること
- **審査基準（4区分・計100%）**：
  1. **インパクト（30%）**：日本の具体的な現場課題から出発しているか、誰がどんな場面で困っているか、ハッカソン後も使い続けられるか
  2. **デモ（25%）**：「誰の、どんな課題を、どう解決するか」が冒頭で明確か、**実際の操作の流れを通じて価値が分かるか**、最後まで引き込まれるか
  3. **Claude の活用（25%）**：Claude を創造的に使い、Claude Code を開発の中心に据え、モデル（Haiku / Sonnet / Opus 5.5）や機能（Skills・サブエージェント・MCP・フック）を目的に応じて使い分けているか
  4. **深さと実行力（20%）**：試行錯誤を重ねて磨き上げたか、困っている人が安心してそのまま使える形（Guardrails・検査・人間承認）に仕上がっているか

### 1-2. プロダクト「Form-Guard Agent」の完成状況（現在地）
- **提出した概要の1文目（146字）**：
  - このプロジェクトは、行政・大学・施設の指定様式（Word・Excel・固定PDF）を扱う担当者の「AIで下書きすると罫線や結合セル・定型文まで壊れて差し戻される」課題を、指定欄だけを書き換え21項目＋視覚比較で構造不変と原文根拠を確かめてから人間が承認する形に変える書類記入エージェントです。
- **実装・検証済みの状態**：
  - 対応形式：Word（`.docx`）、Excel（`.xlsx`）、固定レイアウト PDF（`.pdf`）の付属デモ全10様式（`form1`〜`form10`、すべて自作の架空様式・架空メモ）
  - 自動テスト：`.venv` で **`pytest` 全277件合格（277 passed, 0 skipped）**、オフラインプログラム45試行＋実ブラウザ16試行すべて合格、公開実ファイル計190件の3段階限定構造試験（合成ダミーメモによるオフライン検査で構造改変0件・詳細は `docs/PUBLIC_BENCHMARK_190_MANIFEST.json`）まで完了
  - 動画の現状：完成・限定公開済みの2分44秒（`163.967秒`・1080p）デモ動画（`https://youtu.be/4MvCEiBFhgU`）および既存の175.00秒ベース動画あり。**「Claude アプリ／Claude Code で実際に Guardrails を使って対話→ピンポイント記入→検査→ブラウザ人間承認→実機Word確認を行うライブ録画」の全体設計と再現手順を本ファイルにまとめています。**

---

## 2. 別PCへローカルフォルダを渡す構成と最短セットアップ手順

- **前提環境**：Windows ＋ **Python 3.10 以上**（`python --version` で確認）＋ Claude Desktop（または Claude Code）
- **軽量版（推奨・約1.3 MB）**：`form-guard-agent-slim-code-only.zip`
  - 含まれるもの：`form_guard/`（本体・MCP・Web UI）、`samples/`（架空様式 `form1`〜`form10`＆メモ）、`.claude/`（2段フック・6 Skills・4 Subagents・`/fill-form`）、`.mcp.json`、`requirements.txt`、`scripts/hook_guard.py`、`scripts/setup_remote_demo.ps1`、`CLAUDE.md`、`docs/`（本ファイル `REMOTE_CLAUDE_LIVE_DEMO_MASTER_JA.md` 含む）
- **完全版（約43.7 MB）**：`form-guard-agent-remote-demo-pack.zip`
  - 含まれるもの：上記に加え、全スクリーンショットやスライド一式も含むフルパッケージ

### 別PCでのセットアップ＆起動手順

- **手順1**：別PCで `git clone https://github.com/drmiura-jpg/form-guard-agent.git C:\hackathon\form-guard-agent`（または ZIP を **`C:\hackathon\form-guard-agent`** に解凍）します。
- **手順2**：PowerShell を開いて以下のコマンドを実行すると、Python 仮想環境（`.venv`）構築・パッケージ導入・Claude Desktop の MCP 設定統合（`-ConfigureClaudeDesktop` 指定時のみ・既存設定のバックアップ付き）・ローカル Web サーバー（`http://127.0.0.1:8000`、`-AllowLive` 指定で MCP 下書きのブラウザ取り込み有効）の起動とヘルスチェック（`/api/health`・`/start`）確認まで自動で進みます（途中で `[NG]` が出た場合は表示された理由を解消して再実行します）。

```powershell
cd C:\hackathon\form-guard-agent
# 録画PCでブラウザ側（/#tour=1 や /#tour=3）が誤って有料APIを呼ばないよう、シェル環境変数のキーと FORM_GUARD_FORCE_DEMO をクリア
Remove-Item Env:FORM_GUARD_API_KEY, Env:ANTHROPIC_API_KEY, Env:ANTHROPIC_AUTH_TOKEN, Env:FORM_GUARD_FORCE_DEMO -ErrorAction SilentlyContinue
# ポート8000を待受中の本リポジトリ .venv\Scripts\python.exe のみ停止（別アプリ・別リポジトリは止めない）
$expPy = [IO.Path]::GetFullPath('.\.venv\Scripts\python.exe'); Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction Ignore | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { $w = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $_) -ErrorAction Ignore; if ($w -and $w.CommandLine -and ($w.CommandLine.IndexOf($expPy, [StringComparison]::OrdinalIgnoreCase) -ge 0)) { Stop-Process -Id $_ -Force } }
# Claude Desktop 設定登録 (-ConfigureClaudeDesktop) ＆ MCP 下書きのブラウザ取り込み有効 (-AllowLive) で起動
powershell -ExecutionPolicy Bypass -File .\scripts\setup_remote_demo.ps1 -ConfigureClaudeDesktop -AllowLive
```
※別フォルダに置いた単体スクリプトから実行する場合は `powershell -ExecutionPolicy Bypass -File .\setup_remote_demo.ps1 -RepoDir "C:\hackathon\form-guard-agent" -ConfigureClaudeDesktop -AllowLive` のように `-RepoDir` を指定します。また、外部 AI API を使わないのは `mode="demo"` を指定した Form-Guard の処理（および API キー未設定時の Web 画面）であり、Claude Desktop のチャットに入力した文面自体は Claude サービスで処理されます。収録前に `http://127.0.0.1:8000/api/health` で `live_available=false`・`default_mode=demo`（詳細画面のエンジン表示が「オフライン（API キーなし）」）になっていることを確認してください。

---

## 3. 別PCの「Claude デスクトップアプリ」＆「Claude Code」への接続設計

別PCの **Claude デスクトップアプリ（Claude Desktop）** でも、アプリ内・ターミナルの **Claude Code** でも、両方から Form-Guard の MCP ツール（5個）を呼び出せます。

### 3-1. Claude デスクトップアプリ（Claude Desktop）に MCP を繋ぐ設定
- 上記の `setup_remote_demo.ps1 -ConfigureClaudeDesktop` を実行すると（`-ConfigureClaudeDesktop` 指定時のみ）、`%APPDATA%\Claude\claude_desktop_config.json`（既存ファイルは `.bak-日時` に自動退避、破損JSON時は上書きせず停止）に以下の設定が書き込まれます。
- Claude Desktop の作業フォルダがどこであっても確実にモジュールと下書き保存先（`output\mcp_runs\<run_id>`）が一致するよう、`--root` 引数と `PYTHONPATH`・`FORM_GUARD_MCP_ROOT` を絶対パスで指定しています。

```json
{
  "mcpServers": {
    "form-guard": {
      "command": "C:\\hackathon\\form-guard-agent\\.venv\\Scripts\\python.exe",
      "args": ["-m", "form_guard.mcp_server", "--root", "C:\\hackathon\\form-guard-agent"],
      "cwd": "C:\\hackathon\\form-guard-agent",
      "env": {
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": "C:\\hackathon\\form-guard-agent",
        "FORM_GUARD_MCP_ROOT": "C:\\hackathon\\form-guard-agent"
      }
    }
  }
}
```
- **再起動と事前許可のコツ**：
  - 設定反映時はウィンドウの「×」だけでなく、**タスクトレイ（右下）の Claude アイコンを右クリックして「Quit（終了）」してから再起動** します。
  - チャット入力欄の「＋」またはツールアイコン（Connectors）に次の5ツール（`form_guard_list_demos` / `form_guard_scan` / `form_guard_discover_blank_cells` / `form_guard_fill` / `form_guard_verify`）が表示されていることを確認します（出ない場合は `%APPDATA%\Claude\logs\mcp*.log` を確認）。
  - **リハーサル時に1回ツールを実行し、「常に許可（Always allow）」を選んでおく** と、本番テイクの録画中に許可ダイアログが出ずスムーズに進行します。

### 3-2. Claude Code（CLI または Desktop 内 Claude Code タブ）で動かす場合
- **重要（動作先と仮想環境）**：MCP 5ツールは Claude Desktop の通常チャットでも Claude Code でも同様に動きますが、`.claude/settings.json` の2段階フック（`PreToolUse` / `PostToolUse`）は **Claude Code 固有の機能** です。
- CLI の Claude Code で実演する場合は、リポジトリ直下で `claude`（または `scripts\launch_clean_demo.cmd --remote`）を起動します。`.mcp.json` の `python` に依存が無くても、MCP サーバーが `.venv` で起動し直します（`form_guard/mcp_server.py:781-807`）。初回起動時にフォルダの信頼と `.mcp.json` の承認を済ませておきます。

---

## 4. デモ動画で実演する「5つの Guardrails（安全機構）」と収録範囲の整理

Form-Guard Agent が備える5つの安全機構のうち、**今回の3ターンライブ実演で画面上に直接映るのは ②・③（字数整形と和暦正規化）・④・⑤ の4つ** です（①のフック遮断は Claude Code タブで撮る場合の任意カット、③のうち上限超過の自己修正と未記載法人番号の保留は Web 画面の `#tour=3`［デモ2］で補足紹介できる機能です）。

| 区分 | Guardrail（安全機構） | 画面上で何が起きるか（実演での見せ場） | 収録場所・対応ツール |
|---|---|---|---|
| **①** | **白紙原本の改変阻止フック（補助ガード）** | Claude Code の指定ツール（`Write`/`Edit`/`Bash`等）が `samples/blank_forms/*` を直接変更しようとした際、検知可能な直接操作を実行前に `deny` で止める補助ガード（内部エラー時は通過するため、最終的な原本不変は④のバイト照合で裏付ける） | 【任意カット】Claude Code (`.claude/settings.json` / `scripts/hook_guard.py`) |
| **②** | **4段階空欄発見＆事務局欄ロック** | マーカー無しの白紙Wordから空欄9個を自動検出し、「申請者が書く7欄（✓）」と「総務部専用の2欄（[LOCK] 記入禁止）」に自動仕分け（デモ5 Excelでは `=SUM()` 数式セルも自動除外） | **【ライブ実演・ターン1】** MCP `form_guard_discover_blank_cells` |
| **③** | **原文根拠・字数整形・推測ゼロ** | ターン2では用務内容を上限100字以内（59字）に整え、西暦を和暦「令和8年」に正規化（別タブ `#tour=3` のデモ2では上限100万円超過の自動再抽出と、メモに無い法人番号を捏造せず `needs_human_input` に残す動作も確認可能） | **【ライブ実演・ターン2】** MCP `form_guard_fill`（`mode="demo"` オフライン規則／本番API時は3階層モデル） |
| **④** | **7区分・21項目検査＆視覚ずれ 0 px** | 指定欄のバイト範囲だけを書き換え、未変更ZIPパーツは生バイト保持。罫線・結合セル・定型文・内蔵プレビュー配置比較での欄外ずれ `0 px`（付属様式実測値）を `21/21 PASS` で即時確認 | **【ライブ実演・ターン2】** MCP `form_guard_verify` (`VIS-01`) |
| **⑤** | **代理承認の禁止＆人間承認ゲート** | MCPには承認ツールを設けずAIによる代理承認を運用上禁止しており、返るのは未承認下書き（`UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF`）と確認URLのみ。Web画面で人が全確認項目にチェックを入れ、氏名を入れて承認して初めてダウンロードが解錠（最終表示・印刷は実機Wordで人が確認） | **【ライブ実演・ターン3】** Web UI (`http://127.0.0.1:8000/start?run_id=...`) |

---

## 5. ライブ録画用：ベストなデモ実演シナリオ（コピペでそのまま使える3ターン台本）

- **おすすめの画面配置**：画面（1920×1080 フルHD・表示倍率100〜125%）を左右分割（**左半分：Claude デスクトップアプリ／Claude Code、右半分：ブラウザ `http://127.0.0.1:8000/start`**）にします。
- **おすすめの尺（編集目標：合計 116秒＝0:26〜2:22）**：
  - ターン1（39秒枠：0:26–1:05）＋ ターン2（43秒枠：1:05–1:48）＋ ターン3（34秒枠：1:48–2:22）＝ **編集目標 116秒**。
  - 3ターンを通しでライブ録画（OBS Studio または Windows 標準 Snipping Tool / `Win + Alt + R`）し、AIの思考待機時間をカット編集（「待機時間を省略」と表示）して 116秒枠に合わせます。本番前に1回リハーサルし、2〜3テイク撮って一番テンポが良いものを採用します。

### 🎬 ターン1（39秒枠：0:26–1:05）：白紙の出張申請書とメモを渡し、Guardrail ②（空欄発見＆総務部欄ロック）を発動させる

**【Claude に貼り付けるプロンプト①（様式の欄名と一致する7項目メモ）】**
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

まず Form-Guard の MCP（form_guard_discover_blank_cells, form_path="samples/blank_forms/form7_business_trip.docx", mode="demo"）で白紙様式の空欄を調べて、「申請者が記入する欄」と「総務部専用の欄（ロック対象）」を表で見せてください。まだ記入はしないでください。
```

**【画面で見せるポイント（39秒枠：0:26–1:05）】**
- Claude が `form_guard_discover_blank_cells` を呼び出し、マーカー無しの白紙 Word から **全9空欄** を自動発見します。
- **`c01〜c07`（申請日・申請者氏名・所属部署・出張先・出張期間・用務内容［100字以内］・概算旅費）＝ 申請者記入欄（✓）** と、**`c08 承認印（所属長）・c09 旅費精算番号` ＝ 総務部専用欄（[LOCK] 記入しない）** に自動で仕分けられた表が出ます。

---

### 🎬 ターン2（43秒枠：1:05–1:48）：ピンポイント記入と21項目検査を実行し、Guardrail ③・④を発動させる

**【Claude に貼り付けるプロンプト②（引数受け渡し明記版）】**

```text
ありがとうございます。申請者が書く推奨欄 c01〜c07 の7欄で確定します（c08・c09 は空欄維持）。
・記入：memo_text（上の申請メモ全文）、discovery（手順1の結果）、confirmed_ids=["c01","c02","c03","c04","c05","c06","c07"]、mode="demo" を form_guard_fill に渡してピンポイント記入してください。
・再検証：返ってきた draft_path と slot_spec_path を form_guard_verify（original_path="samples/blank_forms/form7_business_trip.docx"）に渡して検査し、記入値・21項目検査結果・未承認下書き状態・start_url を短く出してください。
```

**【画面で見せるポイント（43秒枠：1:05–1:48）】**
- Claude が `form_guard_fill` と `form_guard_verify` を連続実行します（開発環境での実機検証済み出力：西暦が **和暦「令和8年11月28日（土）」**・出張期間が **「令和8年12月3日（木）～令和8年12月4日（金）」** に正規化され、用務内容 **59字（上限100字以内）** を含む7欄すべてが `filled`・入力待ち `0件` になります）。
- **7区分・21項目の構造検査すべて合格（21/21 PASS）**、内蔵プレビュー配置比較での指定欄外のずれ **`0 px`**、`c08・c09` は空欄のまま保護されます。
- ステータスが **`UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF`（未承認下書き・人の確認待ち）** となり、`http://127.0.0.1:8000/start?run_id=...` のリンクが出力されます。

---

### 🎬 ターン3（34秒枠：1:48–2:22）：出力された `start_url` を開き、右画面のブラウザで Guardrail ⑤（視覚比較＆人間承認ロック解除）を実演する

**【ブラウザで見せる操作（34秒枠：1:48–2:22）】**
- **手順A（詳細レポート起動）**：Claude が出した `http://127.0.0.1:8000/start?run_id=...` を右半分のブラウザで開き、`✅ 機械の検査にすべて合格しました（21/21項目合格・欄外のずれ 0か所・記入7欄）` を見せます。
- **手順B（左右プレビュー比較）**：「白紙の原本」と「記入後のプレビュー」を並べ、緑枠の7欄だけに文字が入り、総務部欄（赤枠ロック）や罫線が変わっていないことを見せます。
- **手順C（確認項目チェック＆承認ダウンロード）**：「③ あなたが目で確かめること」の **確認項目をすべてチェック** → 確認者名に `山田 太郎` と入力 → **「✅ 承認してダウンロード」** をクリック → ロックが外れて保存された `.docx` を実機の Word で開いて最終表示を確認し完了です！

---

## 6. 3分（175秒）デモ動画の全体タイムライン設計（プレゼン最小・ライブデモ最大構成）

既存の175秒動画の冒頭（0:00–0:26）と終盤（190件検証・結び）を組み合わせるか、全編を通しで収録して **中核の116秒（0:26–2:22）を今回のライブデモ録画にする構成** です（※既存V6動画 `FormGuard_Agent_Demo_175s_1080p.mp4` の後半を流用する場合は、2:22–2:35 が既存の承認画面にあたるため、ライブ側のターン3を 2:35 までゆったり見せて 2:35–2:55 の20秒［190件検証＋結び］をつなぐと重複ゼロで綺麗につながります）。

| 時間 | 秒数 | パート | 画面に映すもの（ライブデモ中心） | ナレーション・話す要点 |
|---|---|---|---|---|
| **0:00–0:26** | 26秒 | **① 導入：原体験と課題** | 崩れた比較用模擬書類（結合セル消失・罫線崩壊）と Form-Guard の Before/After スライダー（`/#tour=1`） | 私自身、倫理申請などの書類作成で何度も差し戻され、AIでやっても表や罫線が崩れて「もうAI使うのやめようか」と思いました。元の様式は一切触らず、指定欄だけをピンポイントで書き換えて検査する Form-Guard を作りました。 |
| **0:26–1:05** | 39秒 | **② ライブ実演1：Claudeで相談＆空欄発見・事務局欄ロック** | **【ライブ録画・ターン1】** Claude に白紙の出張申請書とメモを渡し、`form_guard_discover_blank_cells` で申請者7欄と総務部2欄（ロック）を自動仕分けする画面 | 実際の画面で動かします。いつものように Claude に白紙の申請書と今回のメモを渡します。まず Form-Guard の MCP で空欄を自動検出。目印のない白紙から、申請者が書く7つの欄と、総務部専用の2つの欄を自動で見分け、総務部欄は誤記入できないようロックします。 |
| **1:05–1:48** | 43秒 | **③ ライブ実演2：ピンポイント記入・21項目検査** | **【ライブ録画・ターン2】** Claude が `form_guard_fill` → `form_guard_verify` を実行 → 記入7欄・21/21 合格・総務部2欄は空欄・`UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF` と `start_url` 発行 | 用務内容は目的が先に伝わるよう100字以内に整え、確定した7つの欄だけにピンポイントで書き込みます。今回はお試しモードで外部のAI呼び出しはゼロ、本番API接続時は欄ごとにHaiku・Sonnet・Opus 5.5を使い分ける設計です。21項目の検査すべてに合格し、AIが出せるのは未承認の下書きだけです。 |
| **1:48–2:22** | 34秒 | **④ ライブ実演3：ブラウザ引き継ぎ＆人間承認ロック解除** | **【ライブ録画・ターン3】** Claude が出した `start_url` を開く → `/start?run_id=...` で白紙と記入後を並べて確認 → 確認項目を全チェック＆氏名を入れて「✅ 承認してダウンロード」→ Wordで確認 | 発行されたURLを開くと、同じSHA-256の下書きがブラウザに引き継がれます。内蔵プレビューで白紙の原本と並べて指定した7欄以外が変わっていないことを確認し、人が確認項目をすべてチェックして名前を入れ、承認した瞬間に初めてダウンロードのロックが外れます。最終表示はWordで確認します。 |
| **2:22–2:55** | 33秒 | **⑤ 結び：公開190件限定試験＆Take-Home Message** | 公開実ファイル190件の3段階限定試験サマリーと Word/Excel 実機確認 → 最後のメッセージ画面 | 実機のWordとExcel、さらに公開されている実ファイル190件での限定試験（仮の値）でも、構造の改変はゼロでした。様式と根拠の検査は機械が担い、人は本来の中身と最終判断に集中する。最終判断は、いつも人です。 |

---

## 7. 録画前のチェックリスト（撮り直し防止のために）

- [ ] **本番前に10秒のテスト録画・再生確認をしたか**：OBS Studio（画面キャプチャ）等で10秒だけ録画して再生し、左のClaude画面・右のブラウザ画面の文字がはっきり読めるか、マイク音声（または後付けナレーション）がクリアに入っているかを確認する。
- [ ] **個人情報・個人パスが映っていないか**：ターミナルや Claude Code の起動画面に、ユーザー名入りのパスが映らない場所（`C:\hackathon\form-guard-agent` など）に置いたか、ブラウザのブックマークバーや個人アカウントアイコンは隠れているか。
- [ ] **架空データのみか**：画面に映る様式・メモ・名前（`見本 七海`・`山田 太郎`）はすべて架空データになっているか。
- [ ] **動画尺が 180秒（3分00秒）以内か**：公式上限の 180.0秒を超えないよう、170〜175秒で収める。
