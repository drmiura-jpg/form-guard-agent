# Claude Code で Form-Guard を使う説明書（コピペでそのまま使える）

Claude Code に日本語で頼むだけで、**様式の空欄探し → 記入 → 検査** まで Claude が進めます。
人がするのは「どの欄に書くかを選ぶ」、（材料に書いていなかった項目があれば STEP 4 で追加入力して再検査する）、そして「最後に STEP 5 で確認して承認する」だけです。

---

## 0. 最初の1回だけの準備

| # | やること | コマンド／操作（そのままコピペ可） |
|---|---|---|
| 1 | このフォルダで Claude Code を開く | `cd C:\hackathon\form-guard-agent` → `claude`（ハッカソン用は `claude-hackathon`） |
| 2 | MCP の使用を許可する | 初回に「form-guard MCP サーバーを使いますか？」と聞かれたら **許可** |
| 3 | （本番の AI を使うときだけ）API キーを保存 | 別の PowerShell 窓で `cd C:\hackathon\form-guard-agent; pwsh scripts/setup_api_key.ps1` → 画面の質問に答える（キー変更後は Claude Code も開き直す） |
| 4 | Web 画面を起動しておく | 別の PowerShell 窓で `cd C:\hackathon\form-guard-agent; if (Test-Path .venv\Scripts\Activate.ps1) { . .venv\Scripts\Activate.ps1 }; python -m form_guard.cli --serve`（Claude に任せてもOK） |

> 💡 API キーは `.env`（GitHub に上がらない設定ファイル）に `FORM_GUARD_API_KEY` として保存され、**Form-Guard の Web 画面と MCP サーバーだけ**が使います（`ANTHROPIC_API_KEY` は書き換えないため、Claude Code 本体は Max 定額枠のままです。念のため Claude Code 起動後に `/status` で Max 表示か確認できます）。
> 💰 **費用の区分**:
> - **お試しモード（`mode="demo"`・MCP の既定／Web 画面トップ `/` は「自動 / Auto」が既定で API キー未設定時は 0円、キー設定後に通信させないときは「オフライン（キー不要）」を選択／かんたん画面 `/start` は「お試しモード」が既定）**: API 通信なし・追加の従量課金は **0円**（Claude Code のチャット自体は Max 定額枠内）。
> - **本番の AI モード（`mode="live"`）**: Form-Guard が Anthropic API（自動振り分け：Haiku 4.5／Sonnet 5／Opus 5.5）を呼ぶため、**API の従量クレジットを消費**します（費用はモデル・文章量・呼び出し回数や再試行回数で決まり、この手順での実費は未計測です。最新の単価は Anthropic の公式料金案内を確認し、実行前に Anthropic Console の Spend Limit で利用上限を設定しておくと安心です）。

> ⚠️ チャットに貼った文章は Claude（Anthropic）に送られます。患者さんの情報・個人情報・未公開の資料は貼らないでください。

---

## 1. いちばん簡単な頼み方（スラッシュコマンド）

Claude Code の入力欄に、こう打つだけです。

```
/fill-form samples/blank_forms/form7_business_trip.docx samples/memos/memo7_business_trip.txt
```

自分の書類なら:

```
/fill-form output/my_form.xlsx  （このあとメール本文を貼る）
```

---

## 2. 普通の文章で頼む（コピペ用）

| やりたいこと | Claude Code にこう言う |
|---|---|
| サンプルで試す | 「`samples/blank_forms/form8_purchase_request.xlsx` を `samples/memos/memo8_purchase_request.txt` の内容で Form-Guard を使って埋めて、確認画面を開いて」 |
| 公募ポータルから | 「公募ポータルから研究会の PDF 様式をダウンロードして、`samples/memos/memo6_study_group_grant.txt` の内容で埋めて確認画面を開いて」 |
| メールから | 「`output/leave.docx` を、下のメールの内容で埋めて。おすすめの欄でいいよ」＋メール本文を貼る |
| 状況を聞く | 「さっきの下書き、入力待ちの欄はある？ 検査は合格した？」 |
| 本番の AI で | 「本番の Claude（`mode="live"`）で記入して（API の従量課金がかかってもOK）」 |

---

## 3. Claude が裏でやること（MCP ツール）

| 段階 | MCP ツール | 中身 |
|---|---|---|
| ① 空欄探し | `form_guard_discover_blank_cells` | 白紙の様式から記入欄を探し、事務局の欄（🔒）を外す（既定は `mode="demo"`） |
| ② 欄を選ぶ | （あなたに質問） | 候補を表で見せる → あなたが「おすすめの欄で」と答える |
| ③ 記入 | `form_guard_fill` | 材料にあることだけを、選んだ欄だけに書く（無いものは空欄のまま） |
| ④ 検査 | `form_guard_verify` | 記入結果の `slot_spec_path` を使って、指定の欄以外が変わっていないか21項目を機械で検査 |
| ⑤ 確認画面 | — | かんたん画面のリンク `start_url` を返す |

---

## 4. 最後にあなたがすること

1. Claude が返した **`http://127.0.0.1:8000/start?run_id=…`** をブラウザで開く
2. **（材料に書いていなかった欄があるとき）** **STEP 4** が開くので、空いている欄に入力して **「反映して再検査」** を押し、機械検査が合格（PASS）になったら **「次へ：詳細レポートでチェック →」** を押す
3. **STEP 5「詳細レポートでチェック」** で、各欄の値と根拠を見て **チェックを押す**
4. 名前を入れて **承認してダウンロード**

> Claude は承認ボタンを押しません。最後に決めるのは必ず人です。

---

## 5. 困ったとき

| 症状 | 対処 |
|---|---|
| 「Claude（本番の AI）」が選べない | `.env` が無い → `cd C:\hackathon\form-guard-agent; pwsh scripts/setup_api_key.ps1` → Web 画面と Claude Code を再起動 |
| MCP のツールが出てこない | Claude Code を `C:\hackathon\form-guard-agent` で開き直す。`/mcp` で `form-guard` が connected か確認 |
| 一時公開 URL で本番の AI が動かない | 仕様です（他人に API 費用を使われないよう、`FORM_GUARD_FORCE_DEMO=1` でお試しモード固定になり、`mode="live"` も遮断されます） |
