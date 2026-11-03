---
name: portal-form-fill
description: "One-sentence workflow from the fictional grant / study-group portal (架空学術振興ポータル at http://127.0.0.1:8000/portal, or GET /api/portal/forms) to a verified, unapproved draft: fetch a blank form — a fixed PDF such as form6_study_group_grant.pdf or a Word / Excel form — discover its boxes with Form-Guard, fill only the boxes the user confirms from a memo (printed □ checkboxes ticked as ■), re-run the 21 checks and open the human sign-off page in Chrome. Use when the user asks to download a form from the portal, fill it from notes and open the review screen, e.g. 「公募ポータルから研究会のPDF様式をダウンロードしてメモの内容で埋めて確認画面を開いて」."
argument-hint: "[form id or title on the portal] [memo path]"
---

# Portal → discover → fill → verify → human sign-off in Chrome

The portal, its calls and every form on it are fictional (written for the demo). Everything you produce is an
**unapproved draft**: a named human reviewer approves it in the web app. You never approve, and you never pick
fields the user did not confirm.

## 0. Preconditions

- The web app is running: `python -m form_guard.cli --serve` (http://127.0.0.1:8000). If it is not, start it in the
  background and wait until `GET /api/health` answers.
- The MCP server `form-guard` (registered in `.mcp.json`) provides `form_guard_discover_blank_cells`,
  `form_guard_fill` and `form_guard_verify`. Without MCP, use the CLI commands below.

## 1. Find the form on the portal

- **Claude in Chrome**: open http://127.0.0.1:8000/portal in a new tab and read the card the user means. Each card
  carries `data-form-id`, `data-sha256`, `data-download`, `data-open` and `data-mcp-form-path`, the printed rules
  (和暦, 上限500,000円, 150字以内, □→■ …) and the 🔒 office-only boxes.
- **Without a browser**: `curl -s http://127.0.0.1:8000/api/portal/forms` returns the same list as JSON
  (`id`, `form`, `sha256`, `download_url`, `open_url`, `mcp_form_path`, `memo_path`, `rules`, `office_only`).
- If the request is ambiguous (several study-group / grant forms), ask which call the user means.

## 2. Download the blank form (into output/, never into samples/)

```bash
mkdir -p output/portal
curl -s -D output/portal/headers.txt -o output/portal/form6_study_group_grant.pdf \
     http://127.0.0.1:8000/api/portal/forms/form6/download
```

Compare the file's SHA-256 with the portal's (`X-Form-Guard-SHA256` header / `data-sha256`). A mismatch means a
different file: stop and tell the user. The blank templates in `samples/blank_forms/` are protected by the
PreToolUse hook — download copies only into `output/`.

## 3. Discover the boxes — the user confirms

Call `form_guard_discover_blank_cells` with `{"form_path": "output/portal/form6_study_group_grant.pdf"}`
(mode `demo` unless the user asks for Claude). For a fixed PDF the boxes are rebuilt from the ruled lines; show the
user a short table: candidate id, label, inferred rule, applicant ✓ / office-only 🔒, and the printed options of
a checkbox (`□ 合意済み　　□ 協議中…`). Office-only boxes (事務局受付欄・受付印・審査結果欄 …) can never be confirmed.
Wait for the user's confirmation; use the `recommended` set only if the user explicitly said so.

CLI equivalent:

```bash
python -m form_guard.cli --discover-blanks output/portal/form6_study_group_grant.pdf --out-dir output/portal
```

## 4. Fill from the memo

`form_guard_fill` with `form_path`, `memo_path` (e.g. `samples/memos/memo6_study_group_grant.txt`, or the user's
memo), the `discovery` report and the `confirmed_ids`. Every value carries verbatim memo quotes; a value that fails
validation or grounding is **not written** and waits for the human. A checkbox ticks only the option the memo
affirms (□ → ■ on that option); an option whose printed note asks for more input (協議中（協議予定日：　年　月　日）) is
always left to the human. For a PDF the blank bytes stay an exact prefix of the draft: the values are an overlay in
one incremental update, and the page's `/MediaBox` and `/Resources` are byte-identical.

CLI equivalent:

```bash
python -m form_guard.cli --form output/portal/form6_study_group_grant.pdf \
    --memo samples/memos/memo6_study_group_grant.txt --out-dir output/portal/run \
    --discovery output/portal/form6_study_group_grant.discovery.json --confirm c01,c02,c03,c04,c05,c06,c07
```

## 5. Verify

`form_guard_verify` with `original_path` (the downloaded blank), `filled_path` (the draft) and `slot_spec_path`
(from the fill result). Report PASS / NEEDS_HUMAN_INPUT / FAIL with every failed check id and the fields awaiting
human input. CLI: `python -m form_guard.cli --verify ORIGINAL FILLED --memo MEMO --slot-spec SPEC`.

## 6. Open the sign-off page in Chrome

Open the `review_url` from the fill result (`http://127.0.0.1:8000/?run_id=…`) in Chrome (Claude in Chrome: a new
tab). The web app re-verifies the exact draft and shows the visual diff, the sources and the checklist. Tell the user
that a named reviewer must confirm every item and approve; then the download unlocks. Do not tick the checklist and
do not press 承認 yourself.

## Rules

- Fictional or authorised data only; no real personal data, credentials or internal documents in memos.
- Only `127.0.0.1` pages in Chrome; do not sign in anywhere and do not submit anything to an external site.
- Never present the draft as approved, verified as true or ready to submit — the human decides.
