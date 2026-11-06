# Claude Code で Form-Guard を使う説明書（コピペでそのまま使える）

Claude Code に日本語で頼むだけで、**様式の空欄探し → 記入 → 検査** まで Claude が進めます。
人がするのは「どの欄に書くかを選ぶ」と「最後に確認して承認する」の2つだけです。

---

## 0. 最初の1回だけの準備（5分）

| # | やること | コマンド／操作 |
|---|---|---|
| 1 | このフォルダで Claude Code を開く | `cd C:\hackathon\form-guard-agent` → `claude`（ハッカソン用は `claude-hackathon`） |
| 2 | MCP の使用を許可する | 初回に「form-guard MCP サーバーを使いますか？」と聞かれたら **許可** |
| 3 | （本番の AI を使うときだけ）API キーを保存 | 別の窓で `pwsh scripts/setup_api_key.ps1` → 画面の質問に答える |
| 4 | Web 画面を起動しておく | 別の窓で `python -m form_guard.cli --serve`（Claude に任せてもOK） |

> 💡 API キーは `.env`（GitHub に上がらない設定ファイル）に保存され、**Form-Guard の Web 画面と MCP サーバーだけ**が使います。
> Claude Code 本体は今までどおりのプラン（Max）のままです。キーが無くても「お試しモード」で全部動きます（費用ゼロ）。

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
| 本番の AI で | 「本番の Claude で記入して（費用がかかってもOK）」 |

---

## 3. Claude が裏でやること（MCP ツール）

| 段階 | MCP ツール | 中身 |
|---|---|---|
| ① 空欄探し | `form_guard_discover_blank_cells` | 白紙の様式から記入欄を探し、事務局の欄（🔒）を外す |
| ② 欄を選ぶ | （あなたに質問） | 候補を表で見せる → あなたが「おすすめの欄で」と答える |
| ③ 記入 | `form_guard_fill` | 材料にあることだけを、選んだ欄だけに書く（無いものは空欄のまま） |
| ④ 検査 | `form_guard_verify` | 指定の欄以外が変わっていないか、21項目を機械で検査 |
| ⑤ 確認画面 | — | かんたん画面のリンク `start_url` を返す |

---

## 4. 最後にあなたがすること

1. Claude が返した **`http://127.0.0.1:8000/start?run_id=…`** をブラウザで開く
2. 「詳細レポートでチェック」で、各欄の値と根拠を見て **チェックを押す**
3. 名前を入れて **承認してダウンロード**

> Claude は承認ボタンを押しません。最後に決めるのは必ず人です。

---

## 5. 困ったとき

| 症状 | 対処 |
|---|---|
| 「Claude（本番の AI）」が選べない | `.env` が無い → `pwsh scripts/setup_api_key.ps1` → Web 画面を再起動 |
| MCP のツールが出てこない | Claude Code をこのフォルダで開き直す。`/mcp` で `form-guard` が connected か確認 |
| 一時公開 URL で本番の AI が動かない | 仕様です（他人に API 費用を使われないよう、公開中はお試しモード固定） |
