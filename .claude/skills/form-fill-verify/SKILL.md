---
name: form-fill-verify
description: "Fill a Word/Excel form from a free-text memo with Form-Guard and prove the result in one pass: grounded mapping with verbatim memo quotes, surgical byte-range XML injection, the 21-item deterministic structural and grounding verification, and interpretation of PASS / NEEDS_HUMAN_INPUT / FAIL. Use when asked to fill, complete or check a form against a memo, to re-verify any filled copy (made by Form-Guard, by hand or by another tool), or to explain why a field was not filled. Never guesses missing facts."
argument-hint: "<form> <memo> [out-dir]"
---

# Fill and verify a form

Scan first (`form-scan` skill) if the form is new to you; an unmarked form needs the user's confirmed cells.

## 1. Fill

```bash
python -m form_guard.cli --form <form> --memo <memo.txt> --out-dir output/<run> [--mode auto|demo|live] [--model-tier auto|opus|sonnet|haiku] [--naive <naive copy>] [--png]
python -m form_guard.cli --form <form> --memo <memo.txt> --out-dir output/<run> --mode live --model-tiering off
python -m form_guard.cli --all-demos          # the fictional demos -> output/index.html
```

- `--mode auto` (default) uses Claude when `ANTHROPIC_API_KEY` is set (shell or the git-ignored `.env`), otherwise
  the deterministic offline engine; `demo` forces the offline engine (reproducible, no cost); `live` requires the
  key.
- `--model-tier auto` (default, or `--model-tiering auto`): `claude-haiku-4-5-20251001` for structured fields,
  `claude-sonnet-5` for standard text / amounts / names, `claude-opus-5-5` with adaptive thinking for long-text
  condensation and for every self-correction — a Haiku or Sonnet draft the verifier rejects is escalated to Opus.
  `--model-tiering off` (or `--model-tier opus`) uses one model for every call. If the API returns
  `not_found_error` (404) for a model id, the engine falls back along `MODEL_FALLBACK_CHAIN` (for example
  `claude-sonnet-5` → `claude-sonnet-4-5`) and records both ids in `audit_log.json`.
- The output folder receives `filled_<form>`, `audit_log.json`, `preview_*.svg` (`.png` with `--png`) and
  `report.html`. The blank form is never modified. Exit code 1 means FAIL.
- MCP alternative: `form_guard_fill {form_path, memo_text | memo_path, mode?, model_tier?}` writes an
  **unapproved draft** under `output/mcp_runs/<run id>/` and returns the status, every field with its quotes and
  line numbers, the model that drafted it, and the checklist.

## 2. What happens inside (to explain a result)

1. **Scan** — slots and printed rules.
2. **Map & self-correct** — each value comes with verbatim memo quotes. An independent validator checks quotes,
   numbers, dates (real calendar date, era, weekday), names, IDs, options and printed rules, then repairs
   deterministically (`2026年11月2日（月）` → `令和8年11月2日（月）`, `483万6千円` → `4,836,000円`, full-width →
   half-width IDs, re-extraction under a printed cap, deletion-only condensation that never drops a condition) or
   sends the findings back to Claude. What still fails is not written.
3. **Surgical write** — only the marker characters change inside the XML bytes; every other ZIP entry is copied as
   raw compressed bytes.
4. **Verify** — 21 deterministic checks; the **visual diff** adds VIS-01 when layout drift or overflow is found.

## 3. Verify any filled copy independently

```bash
python -m form_guard.cli --verify <blank original> <filled copy> --memo <memo.txt> [--set SLOT=VALUE]
```

- Without `--memo`, a matching `audit_log.json` (same SHA-256) next to the filled file supplies the memo. With
  neither, only structure and printed rules are verified — say so; never claim the values are grounded.
- `--set SLOT=VALUE` declares a value that the human reviewer attests to.
- MCP `form_guard_verify {original_path, filled_path, memo_text | memo_path}` also runs VIS-01.

## 4. Interpret the status

| Status | Meaning | What to do |
|---|---|---|
| `PASS` | all checks passed; every field written and grounded | hand over for human sign-off — not finished until a named reviewer approves the checklist |
| `NEEDS_HUMAN_INPUT` | structure intact, but at least one field has no grounded value (not in the memo, a condensation that would drop a condition, a calendar or weekday conflict) | list the pending fields with the reason and ask the human for the fact; re-run with `--set SLOT=VALUE` only for values the human gives you |
| `FAIL` | a structural or grounding check failed | quote the ✗ lines verbatim, do not deliver the file, and never patch the filled file by hand |

Check groups: `ZIP-01…05` archive safety and byte identity · `XML-01…03` well-formedness, namespaces, markup
skeleton (incl. `xml:space`) · `LAYOUT-01…04` tables, borders, merges, styles, page setup · `TEXT-01…03`
boilerplate, slot values, leftover markers · `GRD-01…02` quotes and numbers · `CON-01…03` printed rules ·
`APP-01` opens in an independent library · `VIS-01` layout drift / overflow.

## 5. Rules

- Never guess or invent a value. A blank field is acceptable; an invented one is not.
- Never pass an AI-proposed value through `--set`: it records the value as entered by the human.
- Never edit the blank form or the filled file directly; re-run the pipeline instead.
- In your summary, cite the memo line numbers behind every written value and list pending fields explicitly.
