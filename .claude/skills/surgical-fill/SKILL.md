---
name: surgical-fill
description: "Fill a blank .docx/.xlsx form from a free-text memo with Form-Guard — every value backed by verbatim memo quotes, written by byte-range XML injection into the designated fields only (or the empty cells a human confirmed on an unmarked form) — and hand the unapproved draft to a human for sign-off. Use when the user asks to fill a form from notes, an e-mail or meeting minutes, or to fill the cells confirmed after blank-cell discovery."
argument-hint: "<form> <memo> [out-dir]"
---

# Surgical fill — grounded values, nothing else touched

## Before filling

1. Scan the form first (skill `form-scan`). For an unmarked blank form, the user must have confirmed the discovery
   candidates; do not pick cells yourself.
2. Read the memo once yourself, or ask for an independent reading: the `memo-extractor` agent (read-only, blind
   extraction before it sees the slots) or the `field-mapper` agent (rule-aware proposal with 和暦, caps and
   condensation). Neither writes anything.

## Fill

```bash
# a form with markers / a .slots.json
python -m form_guard.cli --form samples/blank_forms/form1_research_plan_change.docx \
    --memo samples/memos/memo1_research_change.txt --out-dir output/form1 --mode auto

# an unmarked form: the cells the user confirmed during discovery
python -m form_guard.cli --form samples/blank_forms/form4_unmarked_blank.docx \
    --memo samples/memos/memo4_equipment_use.txt --out-dir output/form4 \
    --discovery output/form4_unmarked_blank.discovery.json --confirm c01,c02,c03,c04,c05,c06,c07
```

- `--mode auto` uses Claude when `ANTHROPIC_API_KEY` is set (in a git-ignored `.env`), else the deterministic
  offline engine. `--model-tier auto` routes Haiku → structured fields, Sonnet → standard text / amounts / names,
  Opus → long-text condensation and every self-correction after a validator rejection (a Haiku or Sonnet draft the
  verifier rejects is escalated to Opus). `--model-tiering off` uses one model for every call. When the API answers
  `not_found_error` (404) for a model id, the engine retries along its fallback chain and records the requested
  tier and the model that actually served each field in `audit_log.json`.
- What the pipeline does: each proposal carries verbatim quotes; an independent validator checks quotes, numbers,
  dates (real calendar date, era, （曜）), names, IDs, options and printed rules; failures are repaired (和暦 with
  weekday, `483万6千円 → 4,836,000円`, half-width IDs, deletion-only condensation) or sent back to Claude with the
  findings. A value that still fails is **not written** — it becomes a suggestion for the reviewer.
- The writer replaces only marker characters (or inserts one run / one inline-string cell into a designated empty
  cell); every other ZIP entry is copied as raw bytes.

## After filling

- Report the status line, every `⚠ awaiting human input` field (Form-Guard does not guess) and every `✗` check.
- `--set SLOT=VALUE` is only for values **the human typed**; never use it to push your own value through.
- The output is an **unapproved draft**. Tell the user to review `output/<run>/report.html` and sign off in the web
  app (`python -m form_guard.cli --serve`): the download unlocks only after a named reviewer confirms every
  checklist item, bound to the revision on screen and the verified SHA-256.
- For an independent check, run the `structural-audit` skill or the `form-auditor` / `structure-auditor` agent.

## MCP alternative

`form_guard_fill` (MCP server `form-guard`) writes the same unapproved draft into `output/mcp_runs/<run id>/` and
returns `approval_status: UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF`, the report and the HITL checklist. It never
writes inside `samples/`, never accepts reviewer-entered values and never confirms office-only cells.

## Rules

- Fictional or authorised data only; never real personal data, internal documents or secrets.
- Never present the draft as approved, verified as true, or ready to submit. Humans decide.
