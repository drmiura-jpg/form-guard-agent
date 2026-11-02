---
name: visual-diff-audit
description: "Visual and layout audit of a Form-Guard fill. Generates deterministic SVG/PNG page previews of the blank original, the filled copy and a naive AI regeneration, checks VIS-01 (zero layout drift outside the designated fields, no Excel value overflowing its cell, same page count), and prepares the human-in-the-loop sign-off checklist. Use when asked to show or compare what changed on the page, to check layout or overflow after a fill, to demonstrate why regenerating a form with AI breaks it, or before handing a filled form to a reviewer."
argument-hint: "<form> <memo> [naive copy]"
---

# Visual diff audit

## 1. Render the previews

```bash
python -m form_guard.cli --form <form> --memo <memo> --out-dir output/<run> --naive <naive copy> --png
python -m form_guard.cli --all-demos --png    # every marked demo
```

The folder receives `preview_blank.svg`, `preview_filled.svg` and `preview_naive.svg` (plus `.png` with `--png`,
which needs Pillow and a CJK font; SVG always works); `report.html` embeds them. Rendering is a deterministic
built-in layout engine — the same input always gives the same picture. Naive regenerations of the marked demos:
`samples/broken_before/broken_naive_ai_form1.docx`, `…form2.docx`, `…form3.xlsx`.

Colours: green = written value · amber = awaiting human input · blue = designated field in the blank · red =
naive-regeneration problem (altered printed text, invented number, lost borders or merged cells). Interactive
views: `python -m form_guard.cli --serve`, then deep links such as `/#demo=form1&tab=visual`,
`/#demo=form3&layout=slider` or `/#demo=form5` (an unmarked form: discovery table, then the confirmed fill).

## 2. Check VIS-01

`audit_log.json` → `visual`:

| Key | Must be |
|---|---|
| `identical_outside_slots` | `true` |
| `drift_count` | `0` — boxes outside the slots that moved, vanished or overflowed |
| `overflow_count` | `0` — Excel cell text wider or taller than its cell |
| `pages_blank` / `pages_filled` | equal |

```bash
python -c "import json,sys; v=json.load(open(sys.argv[1],encoding='utf-8'))['visual']; print({k: v.get(k) for k in ('identical_outside_slots','drift_count','overflow_count','max_shift_px','boxes_compared','pages_blank','pages_filled')})" output/<run>/audit_log.json
```

With drift, `report.checks` holds a failed `VIS-01` listing each box (`cell text overflow (212px wide)`,
`moved 3.0px`): the value is too long for its field. Shorten it within the printed limit or ask the human — never
widen columns, shrink fonts or otherwise change the form. For a copy filled elsewhere, MCP `form_guard_verify`
returns `visual` together with VIS-01.

## 3. Contrast with a naive AI regeneration

```bash
python -m form_guard.cli --verify samples/blank_forms/form1_research_plan_change.docx samples/broken_before/broken_naive_ai_form1.docx --memo samples/memos/memo1_research_change.txt
```

The naive copy FAILS: A4 became US Letter, merged cells and borders are gone, printed clauses are paraphrased and a
number appears that is in neither the memo nor the form. `samples/broken_before/comparison_form*.html` shows the
three versions side by side. Use this contrast to explain why Form-Guard writes surgically instead of regenerating
the document.

## 4. Prepare the human sign-off

`audit_log.json` → `report.hitl_checklist` has one item per written value (value, verbatim quote, memo line range,
flags), one per pending field and a few global items. Present it as a checklist table and call out the flags that
need attention: year inferred, condensed (show `condensed_removed` from the mapping in full), approximate amount,
weekday checked or mismatched. The download in the web UI unlocks only after a named reviewer confirms every item,
bound to the revision on screen and the file's SHA-256 — you never confirm on the human's behalf.
