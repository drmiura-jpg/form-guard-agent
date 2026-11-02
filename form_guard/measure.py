"""Measured workflow comparison over the five fictional sample forms.

Used by ``docs/measure_workflow.py`` (the table in docs/BUILT_WITH_CLAUDE_CODE.md and README.md) and by
``GET /api/claude-suite`` (the web UI's "Claude Code 連携 · Architecture" tab).

For each form it runs the full pipeline with the offline engine (unmarked forms: discovery, then the recommended
cells confirmed as a reviewer would) and reports

* the work a person would do by hand — fields to locate, characters to transcribe, and the printed elements a
  line-by-line comparison has to cover (counted from the files, not timed);
* what Form-Guard measured — wall-clock time per run on this machine, automated checks passed, and the items left
  for the human sign-off.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .pipeline import run_pipeline
from .samples import DEMOS, UNMARKED_FORMS, demo_paths, read_memo, unmarked_paths
from .scanner import build_confirmed_slot_spec, discover_blank_cells, recommended_ids


def measure(samples_dir: Path, out_dir: Path) -> list[dict[str, Any]]:
    rows = []
    jobs = [(d["id"], d["form"], demo_paths(d, samples_dir), None) for d in DEMOS]
    jobs += [(u["id"], u["form"], unmarked_paths(u, samples_dir), "discover") for u in UNMARKED_FORMS]
    for form_id, name, p, how in jobs:
        t0 = time.perf_counter()
        spec, n_candidates = None, 0
        if how == "discover":
            d = discover_blank_cells(p["form"])
            n_candidates = len(d["candidates"])
            spec = build_confirmed_slot_spec(d, recommended_ids(d), p["form"])
        r = run_pipeline(p["form"], read_memo(p["memo"]), Path(out_dir) / form_id, mode="demo", slot_spec=spec,
                         naive_path=p.get("naive"), demo_id=form_id if how is None else None)
        total_ms = (time.perf_counter() - t0) * 1000
        rep, st = r.report, r.report.stats
        rows.append({
            "form": form_id, "file": name, "fields": len(r.scan.slots), "candidates": n_candidates,
            "written": sum(1 for m in r.mappings if m.is_written),
            "chars": sum(len(m.filled_value) for m in r.mappings if m.is_written),
            "tags": st.get("tags_compared", 0), "text_blocks": st.get("boilerplate_blocks", 0),
            "entries": st.get("entries", 0), "merges": st.get("merges", 0), "borders": st.get("borders", 0),
            "status": rep.overall_status, "passed": rep.passed_count, "total": rep.passed_count + rep.failed_count,
            "hitl": len(rep.hitl_checklist), "pending": len(rep.pending_slots), "pending_slots": list(rep.pending_slots),
            "ms": round(total_ms), "stage_ms": {k: round(v * 1000) for k, v in r.timings.items()},
        })
    return rows


def totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Sums over all forms (the "41 fields, 2,119 XML tags, …" line)."""
    out: dict[str, Any] = {"forms": len(rows)}
    for key in ("fields", "written", "chars", "tags", "text_blocks", "merges", "borders", "entries", "hitl", "pending"):
        out[key] = sum(r[key] for r in rows)
    out["all_checks_passed"] = all(r["passed"] == r["total"] for r in rows)
    out["checks_per_form"] = sorted({r["total"] for r in rows})
    out["pending_slots"] = [f"{r['form']}:{s}" for r in rows for s in r["pending_slots"]]
    out["ms_min"], out["ms_max"] = (min(r["ms"] for r in rows), max(r["ms"] for r in rows)) if rows else (0, 0)
    return out


def markdown(rows: list[dict[str, Any]]) -> str:
    out = ["| Form | Fields to set up by hand | Characters to transcribe | Printed elements to re-check line by line "
           "| Form-Guard wall-clock (measured) | Automated checks | Left for human sign-off |",
           "|---|---|---|---|---|---|---|"]
    for r in rows:
        setup = (f"{r['fields']} (locate {r['candidates']} empty cells, decide office vs applicant)" if r["candidates"]
                 else f"{r['fields']} (markers / coordinates already designated)")
        review = (f"{r['tags']:,} XML tags · {r['text_blocks']} printed text blocks · {r['merges']} merged cells/ranges · "
                  f"{r['borders']} border definitions · {r['entries']} package parts")
        out.append(f"| {r['form']} `{r['file']}` | {setup} | {r['chars']} | {review} | {r['ms']:,} ms | "
                   f"{r['passed']}/{r['total']} ({r['status']}) | {r['hitl']} checklist items"
                   + (f", {r['pending']} field(s) to enter" if r["pending"] else "") + " |")
    return "\n".join(out)
