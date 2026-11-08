---
name: form-scan
description: "Read-only inspection of a blank Word (.docx), Excel (.xlsx) or fixed PDF (.pdf) form with Form-Guard before any mapping or editing: lists every fillable slot ({{markers}}, [記入欄: …] markers, empty cells or ruled PDF boxes designated by coordinates in <form>.slots.json) with its location and the rules printed on the form (character limits, 和暦 dates, digit counts and ID patterns, yen caps and rounding, drop-down or checkbox options), and — for an unmarked blank form or fixed PDF without markers — runs the 4-stage blank-cell discovery (applicant fields vs office-only cells) for human confirmation. Use when asked what a form contains or requires, before filling a new form, or when a fill reports a missing or unexpected slot."
argument-hint: "<form.docx|form.xlsx|form.pdf> [slot-spec.json]"
---

# Form scan — look before you write

Scanning never modifies anything. Run every command from the repository root with the project's virtual
environment active. Mapping, writing and verification all depend on what the scan finds.

## 1. Forms with designated slots

```bash
python -m form_guard.cli --scan <form>                                # human-readable list
python -m form_guard.cli --scan <form> --json                         # structured JSON (for agents and scripts)
python -m form_guard.cli --scan <form> --slot-spec <form>.slots.json  # explicit spec (auto-detected next to the form)
```

No form at hand? The six fictional, non-medical built-in demos are listed by the MCP tool `form_guard_list_demos`:
`samples/blank_forms/form1_research_plan_change.docx`, `form2_municipal_equipment_subsidy.docx`,
`form3_facility_safety_inspection.xlsx` (marked), `form4_unmarked_blank.docx`, `form5_unmarked_blank.xlsx`
(unmarked Word/Excel) and `form6_study_group_grant.pdf` (fixed PDF). Through MCP: `form_guard_scan {form_path, slot_spec_path?}` returns the same JSON as `--json`.

| Kind | On the form | Notes |
|---|---|---|
| braces | `{{研究課題番号}}` | may be split across Word runs (spell-check, bookmarks) — handled |
| kinyuran | `[記入欄: 法人番号]` | |
| empty cell | nothing printed; `<form>.slots.json` → Word `{"targets": [{"slot_id": "担当者連絡先", "table": 1, "row": 8, "col": 1}]}`, Excel `{"slot_id": "申請日", "sheet": 0, "cell": "C5"}`, PDF `{"slot_id": "申請日", "page": 0, "rect": [172.9, 697.8, 345.8, 720.5]}` (`page` is 0-based) | for official forms that must not be edited; the cell or box must really be empty |

| Printed on the form | Becomes |
|---|---|
| `（200字以内）` | `max_chars: 200` |
| `和暦` / `令和　年　月　日` | `required_format: wareki_date` (`wareki_period` for a from–to range) |
| `（13桁）` | `digits: 13`, pattern `^\d{13}$` |
| `例：JR-2026-001` | a pattern derived from the example, half-width |
| `上限1,000,000円` · `千円未満切捨て` | `max_value: 1000000` · `multiple_of: 1000` |
| `A／B／Cのうち該当するもの` or an Excel drop-down | `required_format: choice`, `allowed_values`, `multi_select` |

Each slot also reports `model_tier`: the Claude model live `--model-tier auto` mode routes it to —
`claude-haiku-4-5-20251001` for structured fields (dates, IDs, choices), `claude-sonnet-5` for standard text, amounts and
names, `claude-opus-5-5` for long-text condensation (and every self-correction). Lines starting with `!` are
warnings ("cell is not empty — refusing to overwrite printed text", "part not found", a refused formula cell or
merged continuation …). Resolve them with the human before filling. Excel: every sheet is scanned; worksheets
larger than 2,000 rows × 200 columns (or 100,000 cells) are refused rather than processed.

## 2. Unmarked blank forms (0 slots) — 4-stage discovery

```bash
python -m form_guard.cli --discover-blanks samples/blank_forms/form5_unmarked_blank.xlsx --mode demo --out-dir output
```

1. Stage 1 finds empty cells with a printed label immediately to the left (or above, in a column-header table).
2. Stage 2 marks each cell `applicant_fillable` or `office_only` (事務局・受付印・受付番号・審査欄・決裁・検印 …),
   gives duplicate labels unique ids (`研究代表者_氏名` / `事務担当者_氏名`) and infers the printed rules. With
   `--mode live`, Claude (Haiku under `--model-tier auto`) also classifies the labels; it can only make a cell
   *less* fillable, never turn an office cell into an applicant field.
3. Stage 3 rejects formula cells, merged continuations and cells that already hold content.
4. Stage 4 is the human gate: show the user the candidate table (`c01 ✓ …`, `c06 ✗ … office/administrative use
   only`) and the rejected cells, and **let the user choose**. Never confirm cells on the user's behalf.

The discovery report (`output/<form>.discovery.json`) records the blank form's SHA-256; confirmation is refused if
the form changes afterwards. MCP: `form_guard_discover_blank_cells {form_path, mode?}`. In the web UI
(`python -m form_guard.cli --serve`) demos 4 and 5 run discovery automatically and show the same table with a
one-click confirm button.

## Report back

Summarise the file type and slot count, then a table `slot_id | format | limit / options | location`, any
warnings, and which facts the memo must contain for every slot to be filled. Continue with the `surgical-fill` or
`form-fill-verify` skill once the slots (or the user's confirmed cells) are known.

## Guardrails

- Never edit a blank form to "fix" a marker. Templates under `samples/blank_forms/` are protected by the project's
  PreToolUse hook; change markers only in their generator (`form_guard/samples.py`) and rebuild with
  `python -m form_guard.cli --generate-samples`, or ask the human.
- Use only the fictional sample forms or forms the user is entitled to use. No real personal data.
