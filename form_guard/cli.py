"""Command line interface.

    python -m form_guard.cli --all-demos                 # run all 3 fictional demos -> output/
    python -m form_guard.cli --form F --memo M --out-dir D [--mode demo|live|auto] [--set 法人番号=...]
    python -m form_guard.cli --scan FORM                 # list the slots a form exposes
    python -m form_guard.cli --discover-blanks FORM      # unmarked blank form: propose empty cells to fill
    python -m form_guard.cli --form F --memo M --discovery F.discovery.json --confirm c01,c02   # fill confirmed cells
    python -m form_guard.cli --verify ORIGINAL FILLED [--memo M]  # audit any filled copy against its blank original
    python -m form_guard.cli --generate-samples          # (re)write samples/ deterministically
    python -m form_guard.cli --serve [--port 8000]       # web UI at http://127.0.0.1:8000

Live mode: --model-tier auto (default; Haiku / Sonnet / Opus routed per field) | opus | sonnet | haiku.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from . import DEFAULT_MODEL, __version__

MODEL_TIER_CHOICES = ["auto", "opus", "sonnet", "haiku"]


def _console_safe() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def _parse_sets(items: list[str]) -> dict[str, str]:
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"--set expects SLOT=VALUE, got {it!r}")
        k, v = it.split("=", 1)
        out[k.strip()] = v
    return out


def _summary_line(result) -> dict:
    rep = result.report
    maps = result.mappings
    return {
        "id": result.demo_id or result.form_path.stem,
        "title": result.scan.title,
        "title_en": result.scan.spec.get("form_title_en", ""),
        "status": rep.overall_status,
        "filled": sum(1 for m in maps if m.is_written),
        "pending": len(rep.pending_slots),
        "pending_slots": rep.pending_slots,
        "checks_passed": rep.passed_count,
        "checks_total": rep.passed_count + rep.failed_count,
        "naive_failures": result.naive_report.failed_count if result.naive_report else None,
        "dir": result.out_dir.name if result.out_dir else "",
        "filled_name": result.filled_path.name,
        "engine": result.engine.get("label"),
    }


def _print_result(result, out_root: Optional[Path] = None) -> None:
    s = _summary_line(result)
    print(f"[{s['id']}] {s['title']}")
    print(f"   {s['status']} · {s['filled']}/{len(result.mappings)} fields filled · "
          f"{s['checks_passed']}/{s['checks_total']} checks passed"
          + (f" · naive regeneration: {s['naive_failures']} failed checks" if s["naive_failures"] is not None else ""))
    for m in result.mappings:
        if m.status == "needs_human_input":
            why = "not stated in the memo" if not m.suggested_value else "could not be validated"
            print(f"   ⚠ {m.slot_id}: awaiting human input ({why}) — Form-Guard does not guess")
    for c in result.report.checks:
        if not c.passed:
            print(f"   ✗ {c.check_id} {c.description}: {c.details[:160]}")
    for step in (result.engine.get("usage") or {}).get("routing", []):
        plan = "planned in live auto (offline: no call) · " if step.get("planned_in_live_auto") else ""
        print(f"   · {plan}{step['task']} → {step['model']}: {', '.join(step['slots'])}")
    rel = result.out_dir / "report.html" if result.out_dir else None
    if rel is not None:
        try:
            rel = rel.relative_to(Path.cwd())
        except ValueError:
            pass
        print(f"   → {rel}")


def run_all_demos(out_dir: Path, mode: str, model: Optional[str], samples_dir: Optional[Path], regenerate: bool,
                  png: bool, client=None, model_tier: Optional[str] = None) -> tuple[list, int]:
    from .pipeline import run_pipeline
    from .report import build_index_html
    from .samples import DEMOS, demo_paths, ensure_samples, generate_samples, read_memo

    if regenerate:
        generate_samples(samples_dir)
    base = ensure_samples(samples_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for demo in DEMOS:
        p = demo_paths(demo, base)
        r = run_pipeline(p["form"], read_memo(p["memo"]), out_dir / demo["id"], mode=mode, model=model,
                         naive_path=p["naive"], demo_id=demo["id"], png=png, client=client, model_tier=model_tier)
        results.append(r)
    summaries = [_summary_line(r) for r in results]
    (out_dir / "index.html").write_text(build_index_html(summaries), encoding="utf-8", newline="\n")
    (out_dir / "summary.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    failures = sum(1 for r in results if r.report.overall_status == "FAIL")
    return results, failures


def _print_discovery(d: dict) -> None:
    cands = d["candidates"]
    rec = [c for c in cands if c["recommended"]]
    print(f"{d['form']} ({d['file_type']}) — {len(cands)} candidate cells ({len(rec)} recommended), "
          f"{len(d['rejected'])} rejected · stage 2: {d['engine']['label']}")
    print(f"   blank form SHA-256 {d['form_sha256'][:16]}… (confirmation is refused if the form changes)")
    for c in cands:
        mark = "✓" if c["recommended"] else "✗"
        where = c.get("cell") or f"table {c.get('table')} row {c.get('row')} col {c.get('col')}"
        rule = c["required_format"] + (f", ≤{c['max_chars']} chars" if c.get("max_chars") else "")
        note = "" if c["recommended"] else f"  — {c['reason']}"
        print(f"   {c['candidate_id']} {mark} {c['slot_id']:<14} {rule:<22} {where}{note}")
    for r in d["rejected"]:
        print(f"   rejected: {r['location']} — {r['reason']}")


def _confirmed_ids(discovery: dict, confirm: str) -> list[str]:
    from .scanner import recommended_ids

    if confirm.strip().lower() == "recommended":
        return recommended_ids(discovery)
    return [x.strip() for x in confirm.split(",") if x.strip()]


def main(argv: Optional[list[str]] = None) -> int:
    _console_safe()
    ap = argparse.ArgumentParser(prog="form-guard", description="Form-Guard Agent — fill only the designated fields, "
                                 "prove nothing else changed, keep a human in the loop.")
    act = ap.add_mutually_exclusive_group()
    act.add_argument("--all-demos", action="store_true", help="run the 3 fictional demo forms end-to-end")
    act.add_argument("--form", type=Path, help="blank .docx/.xlsx form to fill")
    act.add_argument("--scan", type=Path, metavar="FORM", help="list the slots discovered in a form")
    act.add_argument("--discover-blanks", type=Path, metavar="FORM",
                     help="unmarked blank form: propose the empty cells to fill (nothing is written)")
    act.add_argument("--verify", nargs=2, type=Path, metavar=("ORIGINAL", "FILLED"), help="audit a filled copy")
    act.add_argument("--generate-samples", action="store_true", help="(re)generate samples/ deterministically")
    act.add_argument("--serve", action="store_true", help="start the web UI")
    ap.add_argument("--memo", type=Path, help="source memo (UTF-8 text)")
    ap.add_argument("--out-dir", type=Path, default=Path("output"))
    ap.add_argument("--mode", choices=["auto", "demo", "live"], default="auto",
                    help="auto = Claude if ANTHROPIC_API_KEY is set, else the offline demo engine")
    ap.add_argument("--model", default=None, help=f"pin one Claude model for every call (default: routed; "
                                                  f"{DEFAULT_MODEL} for long text and self-correction)")
    ap.add_argument("--model-tier", choices=MODEL_TIER_CHOICES, default=None,
                    help="auto (default, or FORM_GUARD_MODEL_TIER) = Haiku for structured fields, Sonnet for standard "
                         "text, Opus for long text and self-correction; or one tier for every call")
    ap.add_argument("--slot-spec", type=Path, help="optional <form>.slots.json (aliases, coordinate targets)")
    ap.add_argument("--discovery", type=Path, help="discovery report written by --discover-blanks (use with --confirm)")
    ap.add_argument("--confirm", metavar="IDS", help="candidate ids the reviewer confirmed (c01,c02,…) or 'recommended'")
    ap.add_argument("--naive", type=Path, help="optional naive-regeneration file to audit for comparison")
    ap.add_argument("--set", action="append", default=[], metavar="SLOT=VALUE",
                    help="reviewer-entered value (with --verify: a value the reviewer attests to)")
    ap.add_argument("--samples-dir", type=Path, default=None)
    ap.add_argument("--regenerate-samples", action="store_true")
    ap.add_argument("--png", action="store_true", help="also write PNG previews (needs Pillow + a CJK font)")
    ap.add_argument("--json", action="store_true", help="print a JSON summary")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args(argv)

    if a.serve:
        import uvicorn

        print(f"Form-Guard Agent {__version__} — web UI on http://{a.host}:{a.port}  (Ctrl+C to stop)")
        uvicorn.run("form_guard.server:app", host=a.host, port=a.port, log_level="warning")
        return 0

    if a.generate_samples:
        from .samples import generate_samples

        written = generate_samples(a.samples_dir)
        for name, p in sorted(written.items()):
            print(f"  wrote {p}")
        return 0

    if a.scan:
        from .scanner import scan

        r = scan(a.scan, a.slot_spec)
        print(f"{r.path.name} ({r.file_type}) — {len(r.slots)} slots in {r.target_parts}")
        for s in r.slots:
            cons = {k: v for k, v in s.constraints.items()}
            print(f"  • {s.slot_id:<16} format={s.required_format:<13} max_chars={s.max_chars} occurrences={len(s.occurrences)}"
                  f" {cons if cons else ''}\n      {s.location_path}")
        for w in r.warnings:
            print(f"  ! {w}")
        if not r.slots:
            print("  0 slots: no {{markers}}, [記入欄: …] or coordinate targets — for an unmarked blank form run "
                  "--discover-blanks FORM")
        return 0

    if a.discover_blanks:
        from .scanner import discover_blank_cells

        d = discover_blank_cells(a.discover_blanks, mode=a.mode, model_tier=a.model_tier)
        _print_discovery(d)
        a.out_dir.mkdir(parents=True, exist_ok=True)
        out = a.out_dir / f"{a.discover_blanks.stem}.discovery.json"
        out.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"   → {out}")
        print(f"Next (human confirmation): python -m form_guard.cli --form {a.discover_blanks} --memo MEMO "
              f"--discovery {out} --confirm c01,c02,…  (or --confirm recommended)")
        if a.json:
            print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0

    if a.verify:
        from .pipeline import verify_filled_copy
        from .samples import read_memo

        original, filled = a.verify
        out = verify_filled_copy(original, filled, read_memo(a.memo) if a.memo else None, a.slot_spec,
                                 _parse_sets(a.set), memo_name=str(a.memo) if a.memo else "")
        rep = out.report
        print(f"{filled.name} vs {original.name}: {rep.overall_status} ({rep.passed_count} passed, {rep.failed_count} failed)"
              + ("" if out.grounding_verified else " — grounding NOT verified (structure only)"))
        print(f"  grounding basis: {out.basis}" + (f"; audit log: {out.audit_note}" if out.audit_note else "")
              + ("" if out.memo_text else " — pass --memo MEMO (or keep audit_log.json next to the filled file) to check sources"))
        for c in rep.checks:
            mark = "–" if c.evidence.get("not_verified") else ("✓" if c.passed else "✗")
            print(f"  {mark} {c.check_id:<10} {c.description} — {c.details[:150]}")
        return 1 if rep.overall_status == "FAIL" else 0

    if a.all_demos:
        print(f"Form-Guard Agent {__version__} — running 3 fictional demo forms (mode: {a.mode})")
        results, failures = run_all_demos(a.out_dir, a.mode, a.model, a.samples_dir, a.regenerate_samples, a.png,
                                          model_tier=a.model_tier)
        print(f"Engine: {results[0].engine.get('label')}"
              + (f" (fallback: {results[0].engine['fallback_reason']})" if results[0].engine.get("fallback_reason") else ""))
        for r in results:
            _print_result(r)
        pending = [f"{r.demo_id}:{s}" for r in results for s in r.report.pending_slots]
        print(f"\nSummary: {len(results)} demos · {failures} with structural/grounding failures · "
              f"{len(pending)} field(s) awaiting human input{(' (' + ', '.join(pending) + ')') if pending else ''}")
        print(f"Open {a.out_dir / 'index.html'} or run: python -m form_guard.cli --serve")
        if a.json:
            print(json.dumps([_summary_line(r) for r in results], ensure_ascii=False, indent=2))
        return 1 if failures else 0

    if a.form:
        if not a.memo:
            ap.error("--form requires --memo")
        from .pipeline import run_pipeline
        from .samples import read_memo
        from .scanner import build_confirmed_slot_spec

        spec = a.slot_spec
        if a.discovery or a.confirm:
            if not (a.discovery and a.confirm):
                ap.error("--discovery and --confirm go together")
            if a.slot_spec:
                ap.error("use either --slot-spec or --discovery/--confirm")
            discovery = json.loads(a.discovery.read_text(encoding="utf-8"))
            try:
                spec = build_confirmed_slot_spec(discovery, _confirmed_ids(discovery, a.confirm), a.form)
            except ValueError as exc:
                print(f"refused before filling: {exc}", file=sys.stderr)
                return 2
            a.out_dir.mkdir(parents=True, exist_ok=True)
            sp = a.out_dir / f"{a.form.stem}.confirmed.slots.json"
            sp.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
            print(f"Confirmed {len(spec['targets'])} cell(s): {', '.join(t['slot_id'] for t in spec['targets'])} → {sp}")
        r = run_pipeline(a.form, read_memo(a.memo), a.out_dir, mode=a.mode, model=a.model, naive_path=a.naive,
                         overrides=_parse_sets(a.set), slot_spec=spec, png=a.png, model_tier=a.model_tier)
        print(f"Engine: {r.engine.get('label')}")
        _print_result(r)
        print(f"   filled file: {r.filled_path}")
        if a.json:
            print(json.dumps(_summary_line(r), ensure_ascii=False, indent=2))
        return 1 if r.report.overall_status == "FAIL" else 0

    ap.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
