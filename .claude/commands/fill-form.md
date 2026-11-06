---
description: 様式（Word/Excel/PDF）を材料（メモ・メール）から Form-Guard で記入し、人の確認画面のリンクを返す
argument-hint: "[様式のパス or ポータルの様式名] [材料のパス or 貼った文章]"
---

# /fill-form — Form-Guard でおまかせ記入

引数: $ARGUMENTS

次の順で進めてください。各段階の結果は日本語で短く報告します。

1. **準備の確認**: `curl.exe -s http://127.0.0.1:8000/api/health` で Web 画面が動いているか確認する。動いていなければ
   `python -m form_guard.cli --serve` をバックグラウンドで起動して待つ。`live_available` が true なら本番の Claude、
   false ならお試しモード（API キー無し）で動くことを伝える。
2. **様式**: 引数のファイルを使う。ポータルの様式名なら `portal-form-fill` スキルの手順で `output/portal/` にダウンロードする
   （`samples/` は書き換えない）。
3. **空欄探し**: MCP `form_guard_discover_blank_cells` を呼び、候補を表にして見せる（欄名・推定ルール・申請者が書く欄 ✓／事務局欄 🔒）。
   **どの欄に書くかは必ず人に確かめる**（「おすすめの欄で」と言われたら推奨の欄だけ）。事務局欄は選ばない。
4. **記入**: MCP `form_guard_fill` に様式・材料・discovery・確定した欄 ID を渡す。材料に無い値は推測しない（空欄のまま人の入力待ち）。
5. **検査**: MCP `form_guard_verify` で検査し、PASS／NEEDS_HUMAN_INPUT／FAIL と、入力待ちの欄・不合格の検査を報告する。
6. **人の確認へ**: 結果の `start_url`（かんたん画面 `http://127.0.0.1:8000/start?run_id=…`）をブラウザで開くよう案内する。
   詳しく見たい人には `review_url`（`/?run_id=…`）も伝える。

## 決まり
- 承認ボタンは押さない。確認項目にもチェックしない。下書きを「承認済み」「提出できる」と言わない（最後に決めるのは人）。
- 患者さんの情報・個人情報・未公開の資料は扱わない。架空の内容か、本人が使ってよいと言った資料だけを使う。
- 本番の Claude（API）を使うと費用がかかる。`live_available` が true のときは、最初の1回だけ「本番の AI で記入してよいか」を確かめる。
