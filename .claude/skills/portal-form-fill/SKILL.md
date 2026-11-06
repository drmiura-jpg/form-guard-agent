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
- **Without a browser**: `curl.exe -s http://127.0.0.1:8000/api/portal/forms` (or `urllib.request.urlopen` in Python)
  returns the same list as JSON (`id`, `form`, `sha256`, `download_url`, `open_url`, `mcp_form_path`, `memo_path`,
  `rules`, `office_only`).
- If the request is ambiguous (several study-group / grant forms), ask which call the user means.

## 2. Download the blank form (into output/, never into samples/)

```bash
mkdir -p output/portal
curl.exe -s -D output/portal/headers.txt -o output/portal/form6_study_group_grant.pdf \
     http://127.0.0.1:8000/api/portal/forms/form6/download
python -X utf8 -c "import hashlib, pathlib; p = pathlib.Path('output/portal/form6_study_group_grant.pdf'); print(hashlib.sha256(p.read_bytes()).hexdigest())"
```

Compare the downloaded file's SHA-256 (`python -X utf8 -c "import hashlib, pathlib; ..."`) primarily against the
portal list's `data-sha256` / `/api/portal/forms` `sha256` (and optionally the `X-Form-Guard-SHA256` transport header).
A mismatch means a different file: stop and tell the user. When downloading a form from an external grant or
study-group portal that does not publish an `X-Form-Guard-SHA256` header:
1. Compute the SHA-256 immediately upon download (`hashlib.sha256(p.read_bytes()).hexdigest()`) and save a provenance
   sidecar `output/portal/<file>.source.json` recording `url`, `form_title`, `version`, `sha256` and `downloaded_at`.
2. Pass `expected_sha256` to `form_guard_discover_blank_cells` and verify that the returned `form_sha256` matches the
   sidecar's `sha256`.
3. Re-verify the file's SHA-256 before `form_guard_fill`; stop immediately on any mismatch.

The blank templates in `samples/blank_forms/` are protected by the PreToolUse hook — download copies only into `output/`.

## 3. Discover the boxes — the user confirms

Call `form_guard_discover_blank_cells` with `{"form_path": "output/portal/form6_study_group_grant.pdf", "expected_sha256": "<sha256>"}`
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
do not press 承認 yourself. For first-time users, also give the guided page `http://127.0.0.1:8000/start?run_id=…`
(it opens straight at the human checks; see docs/CHAT_GUIDE_JA.md).

## Rules

- Fictional or authorised data only; no real personal data, credentials or internal documents in memos.
- In Chrome, open only `127.0.0.1` and public call pages of external grant portals explicitly requested by the user.
  Never sign in, type into fields, or submit anything on an external site — only download the blank form (if a portal
  requires login, ask the user to download the blank form into `output/portal/` themselves).
- Never present the draft as approved, verified as true or ready to submit — the human decides.
