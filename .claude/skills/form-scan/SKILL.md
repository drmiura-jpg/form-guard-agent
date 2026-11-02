---
name: form-scan
description: Scan a blank Word (.docx) or Excel (.xlsx) form with Form-Guard to list its fillable slots and the rules printed on the form, and — for an unmarked blank form without {{markers}} — propose the empty cells to fill (applicant fields vs office-only cells) for human confirmation. Use before filling any form, or when the user asks what a form requires.
---

# Form scan — find what a form asks for

Form-Guard never edits the blank form while scanning. Run every command from the repository root with the
project's virtual environment active.

## 1. Forms with designated slots

```bash
python -m form_guard.cli --scan samples/blank_forms/form2_municipal_equipment_subsidy.docx
```

A slot is designated by `{{項目名}}`, `[記入欄: 項目名]`, or by the coordinates of an empty cell in a sidecar
`<form>.slots.json` (Word: `table` / `row` / `col`; Excel: `sheet` / `cell`, e.g. `{"sheet": 0, "cell": "C5"}`).
For each slot, report to the user:

- `slot_id`, `format` (`wareki_date`, `wareki_period`, `yen`, `digits`, `pattern`, `choice`, `person_name`, `text`)
  and `max_chars` — these come from text printed on the form (`（200字以内）`, `（13桁）`, `例：JR-2026-001`,
  `上限1,000,000円`, `千円未満切捨て`, drop-down lists), not from guesses;
- the location path (part › table › row › cell, or sheet › cell) and every `!` warning (a refused target,
  for example a formula cell or a merged continuation).

## 2. Unmarked blank forms (0 slots)

If the scan reports `0 slots`, run the 4-stage discovery instead:

```bash
python -m form_guard.cli --discover-blanks samples/blank_forms/form5_unmarked_blank.xlsx --mode demo --out-dir output
```

1. Stage 1 finds empty cells with a printed label immediately to the left (or above, in a column-header table).
2. Stage 2 marks each cell `applicant_fillable` or `office_only` (事務局・受付印・受付番号・審査欄・決裁・検印 …),
   gives duplicate labels unique ids (`研究代表者_氏名` / `事務担当者_氏名`) and infers the printed rules.
   With `--mode live`, Claude (Haiku under `--model-tier auto`) also classifies the labels; it can only make a
   cell *less* fillable, never turn an office cell into an applicant field.
3. Stage 3 rejects formula cells, merged continuations and cells that already hold content.
4. Stage 4 is the human gate: show the user the candidate table (`c01 ✓ …`, `c06 ✗ … office/administrative use
   only`) and the rejected cells, and **let the user choose**. Never confirm cells on the user's behalf.

The discovery report (`output/<form>.discovery.json`) records the blank form's SHA-256; confirmation is refused if
the form changes afterwards. Continue with the `surgical-fill` skill once the user has picked the cells.

## MCP alternative

The `form-guard` MCP server (`.mcp.json`) exposes the same steps as `form_guard_scan` and
`form_guard_discover_blank_cells`.

## Rules

- Use only the fictional sample forms or forms the user is entitled to use. No real personal data.
- Do not modify the blank form (not even to add markers) unless the user asks for that explicitly.
