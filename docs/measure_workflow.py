"""Reproduce the "Measured workflow comparison" table in docs/BUILT_WITH_CLAUDE_CODE.md and README.md.

    python docs/measure_workflow.py            # offline demo engine, fictional sample forms only

For each of the five fictional sample forms it runs the full pipeline (unmarked forms: discovery, then the
recommended cells confirmed as a reviewer would) and prints, as a Markdown table:

* the work a person would do by hand — fields to locate, characters to transcribe, and the printed elements a
  line-by-line comparison has to cover (counted from the files, not timed);
* what Form-Guard measured — wall-clock time per run on this machine, automated checks passed, and the items left
  for the human sign-off.

The measuring code lives in ``form_guard/measure.py``; the web UI shows the same numbers (GET /api/claude-suite).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from form_guard.measure import markdown, measure, totals  # noqa: E402,F401  (re-exported for the tests)
from form_guard.samples import generate_samples  # noqa: E402

if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        generate_samples(base / "samples", with_comparisons=False)
        rows = measure(base / "samples", base / "out")
        t = totals(rows)
        print(markdown(rows))
        print(f"\nTotals: {t['forms']} forms · {t['fields']} designated fields · {t['written']} values "
              f"({t['chars']} characters) · {t['tags']:,} XML tags · {t['text_blocks']} printed text blocks · "
              f"{t['merges']} merged cells/ranges · {t['borders']} border definitions · {t['hitl']} checklist items · "
              f"{t['ms_min']}–{t['ms_max']} ms per form · left for the human: {', '.join(t['pending_slots']) or 'none'}")
