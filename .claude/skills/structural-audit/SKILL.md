---
name: structural-audit
description: Audit a filled .docx/.xlsx against its blank original with Form-Guard's 21 deterministic checks (plus VIS-01 visual drift / cell overflow) and walk the human reviewer through the HITL checklist. Use after any fill, before sign-off, or when the user asks whether a filled form was altered.
---

# Structural audit — prove that nothing else changed

```bash
python -m form_guard.cli --verify samples/blank_forms/form2_municipal_equipment_subsidy.docx \
    output/form2/filled_form2_municipal_equipment_subsidy.docx --memo samples/memos/memo2_subsidy_application.txt
```

Without `--memo` (and without a matching `audit_log.json` next to the filled file) grounding is reported as
"structural verification only — grounding not verified"; say so explicitly instead of calling the file verified.

## Reading the result

| IDs | What a failure means |
|---|---|
| ZIP-01…05 | the package was rebuilt, an untouched part (styles, numbering, theme) changed, or an unexpected part was modified |
| XML-01…03 | malformed XML, changed namespaces, or a tag / attribute / non-slot text changed — including a drawing, field, hyperlink, table or extra run inside a designated empty Word cell, or anything but an inline string in a designated Excel cell |
| LAYOUT-01…04 | table topology, merges, borders, shading, pPr/rPr (designated Word cells must keep the blank cell's paragraph-mark formatting), page setup; for .xlsx: merged ranges, cell styles (the `s` id of a designated cell), widths/heights, formulas, data validation |
| TEXT-01…03 | boilerplate text changed, a slot holds something other than its approved value, a marker is left over |
| GRD-01…02 | a value is not backed by verbatim memo quotes, or a number / date / name / ID / option does not match them |
| CON-01…03 | printed character limits, formats (和暦, （曜）, yen, ID patterns), options, caps and rounding |
| APP-01 | the file does not open in an independent OOXML library |
| VIS-01 | (only if found) layout drift outside the fields or a value overflowing its cell |

Quote the `details` text of each failed check; never summarise a FAIL as "minor".

## HITL checklist

For the human reviewer, list every field with its value, the verbatim source quote and line range, and the flags
(`和暦に変換`, `字数制限のため抽出要約` with the full deleted text, `年は本文から推定`, `出所不明・根拠未検証` …). The two global
items — visual comparison and personal responsibility for the content — are always required. Approval and
download happen only in the web app.

## Independent auditor

Delegate to the `form-auditor` subagent when the fill was produced in the same conversation: it re-runs the
verifier from scratch, cannot edit files, and reports only what the checks show.
