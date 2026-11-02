"""Reproduce the "Measured workflow comparison" table in docs/BUILT_WITH_CLAUDE_CODE.md.

    python docs/measure_workflow.py            # offline demo engine, fictional sample forms only

For each of the five fictional sample forms it runs the full pipeline (unmarked forms: discovery, then the
recommended cells confirmed as a reviewer would) and prints, as a Markdown table:

* the work a person would do by hand — fields to locate, characters to transcribe, and the printed elements a
  line-by-line comparison has to cover (counted from the files, not timed);
* what Form-Guard measured — wall-clock time per run on this machine, automated checks passed, and the items left
  for the human sign-off.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from form_guard.pipeline import run_pipeline  # noqa: E402
from form_guard.samples import DEMOS, UNMARKED_FORMS, demo_paths, generate_samples, read_memo, unmarked_paths  # noqa: E402
from form_guard.scanner import build_confirmed_slot_spec, discover_blank_cells  # noqa: E402


def measure(samples_dir: Path, out_dir: Path) -> list[dict]:
    rows = []
    jobs = [(d["id"], d["form"], demo_paths(d, samples_dir), None) for d in DEMOS]
    jobs += [(u["id"], u["form"], unmarked_paths(u, samples_dir), "discover") for u in UNMARKED_FORMS]
    for form_id, name, p, how in jobs:
        t0 = time.perf_counter()
        spec, n_candidates = None, 0
        if how == "discover":
            d = discover_blank_cells(p["form"])
            n_candidates = len(d["candidates"])
            spec = build_confirmed_slot_spec(d, [c["candidate_id"] for c in d["candidates"] if c["recommended"]], p["form"])
        r = run_pipeline(p["form"], read_memo(p["memo"]), out_dir / form_id, mode="demo", slot_spec=spec,
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
            "hitl": len(rep.hitl_checklist), "pending": len(rep.pending_slots), "ms": round(total_ms),
            "stage_ms": {k: round(v * 1000) for k, v in r.timings.items()},
        })
    return rows


def markdown(rows: list[dict]) -> str:
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


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        generate_samples(base / "samples", with_comparisons=False)
        print(markdown(measure(base / "samples", base / "out")))
