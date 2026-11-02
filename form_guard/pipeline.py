"""End-to-end orchestration: Scan -> Map & Self-Correct -> Surgical Write -> Verify -> Visual Diff & HITL."""

from __future__ import annotations

import difflib
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from . import __version__
from .grounding import locate_quote, make_span
from .mapper import (
    DemoEngine,
    LiveEngineError,
    MappingRun,
    human_mapping,
    make_engine,
    map_fields,
    resolve_mode,
)
from .models import FieldMapping, VerificationReport
from .scanner import ScanResult, find_markers, scan
from .verifier import compare_naive, verify
from .visual_diff import (
    Layout,
    Overlay,
    char_range_rects,
    compare_layouts,
    layout_docx,
    layout_xlsx,
    render_png,
    svg_document,
)
from .writer import WriteResult, fill_form, prepare_value


@dataclass
class PipelineResult:
    run_id: str
    form_path: Path
    filled_path: Path
    memo_text: str
    scan: ScanResult
    mappings: list[FieldMapping]
    write: WriteResult
    report: VerificationReport
    naive_report: Optional[VerificationReport]
    previews: dict[str, str]
    visual: dict[str, Any]
    engine: dict[str, Any]
    timings: dict[str, float]
    demo_id: Optional[str] = None
    out_dir: Optional[Path] = None
    naive_path: Optional[Path] = None
    builtin_demo: bool = False  # an unmodified built-in demo: sample form + sample memo, no reviewer edits
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def to_dict(self, include_previews: bool = True) -> dict[str, Any]:
        memo_lines = self.memo_text.split("\n")
        d: dict[str, Any] = {
            "run_id": self.run_id,
            "tool": f"Form-Guard Agent {__version__}",
            "created_at": self.created_at,
            "demo_id": self.demo_id,
            "is_builtin_demo": self.builtin_demo,
            "form": {"name": self.form_path.name, "title": self.scan.title,
                     "title_en": self.scan.spec.get("form_title_en", ""), "file_type": self.scan.file_type,
                     "sha256": _sha_file(self.form_path)},
            # the SHA-256 of the exact bytes the verifier checked (not whatever is on disk later)
            "filled": {"name": self.filled_path.name,
                       "sha256": self.report.stats.get("filled_sha256") or _sha_file(self.filled_path)},
            "engine": self.engine,
            "memo": {"text": self.memo_text, "lines": len(memo_lines), "sha256": hashlib.sha256(self.memo_text.encode()).hexdigest()},
            "slots": [s.model_dump() for s in self.scan.slots],
            "mappings": [m.model_dump() for m in self.mappings],
            "patches": [p.model_dump() for p in self.write.patches],
            "modified_parts": self.write.modified_parts,
            "report": self.report.model_dump(),
            "naive_report": self.naive_report.model_dump() if self.naive_report else None,
            "visual": self.visual,
            "timings_ms": {k: round(v * 1000, 1) for k, v in self.timings.items()},
            "scan_warnings": self.scan.warnings,
        }
        if include_previews:
            d["previews"] = self.previews
        return d


def _sha_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest() if Path(p).exists() else ""


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


# ---------------------------------------------------------------- mapping step


def _map(scan_r: ScanResult, memo: str, mode: str, model: Optional[str], client: Any) -> tuple[MappingRun, dict]:
    requested = (mode or "auto").lower()
    resolved = resolve_mode(requested) if client is None else ("live" if requested in ("auto", "live") else "demo")
    engine = make_engine(resolved, model, client)
    info: dict[str, Any] = {"requested_mode": requested, "mode": resolved, "label": engine.label,
                            "model": getattr(engine, "model", None), "fallback_reason": None}
    try:
        run = map_fields(scan_r.slots, memo, engine, scan_r.title)
    except LiveEngineError as exc:
        if requested != "auto":
            raise
        info.update(mode="demo", label=DemoEngine.label + " — live call failed, used offline engine",
                    fallback_reason=str(exc))
        run = map_fields(scan_r.slots, memo, DemoEngine(), scan_r.title)
    info["usage"] = run.usage
    info["rounds"] = run.rounds
    return run, info


def is_builtin_demo(demo_id: Optional[str], form_path: Path, memo_text: str, mappings: list[FieldMapping]) -> bool:
    """True only for an unmodified built-in demo: its sample form, its sample memo, no reviewer-entered values."""
    from .samples import MEMOS, demo_by_id

    if not demo_id:
        return False
    try:
        demo = demo_by_id(demo_id)
    except KeyError:
        return False
    return (Path(form_path).name == demo["form"] and memo_text == MEMOS.get(demo["memo"])
            and not any(m.status == "human_entered" for m in mappings))


def reconstruct_mappings(scan_r: ScanResult, values: dict[str, str], memo: str,
                         recorded: Optional[list[FieldMapping]] = None,
                         attested: Optional[dict[str, str]] = None) -> list[FieldMapping]:
    """Mappings for verifying a filled copy whose values were read back from the file (CLI ``--verify``).

    * A value recorded in a matching audit log keeps its recorded mapping (quotes, or reviewer entry).
    * A value declared by the reviewer (``--set SLOT=VALUE``) counts as reviewer-entered.
    * With a memo, every other value must be supported by the memo: the offline engine locates the
      evidence for that field and the verifier re-checks the file's value against it — a value the
      memo contradicts (JR-2099-999 vs ＪＲ－２０２６－０４７) fails GRD-01 / GRD-02.
    * Without a memo the values are only ``recovered``: structure is verified, grounding is not.
    """
    from .writer import prepare_value

    ft = scan_r.file_type
    recorded_by = {m.slot_id: m for m in (recorded or [])}
    attested = attested or {}
    engine: dict[str, FieldMapping] = {}
    if memo.strip():
        engine = {m.slot_id: m for m in map_fields(scan_r.slots, memo, DemoEngine(), scan_r.title).mappings}
    out: list[FieldMapping] = []
    for slot in scan_r.slots:
        sid = slot.slot_id
        if sid not in values:
            continue  # the marker is still in the file: the field is pending
        v = values[sid]
        rec = recorded_by.get(sid)
        if rec is not None and rec.is_written and prepare_value(rec.filled_value, ft) == v:
            out.append(rec)
            continue
        if sid in attested and prepare_value(attested[sid], ft) == v:
            out.append(human_mapping(slot, attested[sid]))
            continue
        if not memo.strip():
            out.append(FieldMapping(slot_id=sid, filled_value=v, status="recovered", char_count=len(v), engine="verify",
                                    normalizations=["recovered_unverified"],
                                    reasoning="Read back from the filled file; no memo or audit log to check it against."))
            continue
        e = engine.get(sid)
        if e is not None and e.is_written and prepare_value(e.filled_value, ft) == v:
            out.append(e)
            continue
        # The file holds something other than what the memo supports for this field: check the file's
        # value against that field's evidence (or, if the engine found none, a verbatim occurrence).
        spans = list(e.source_spans) if e is not None and e.source_spans else []
        if not spans:
            loc = locate_quote(memo, v)
            spans = [make_span(memo, loc.start, loc.end)] if loc else []
        out.append(FieldMapping(
            slot_id=sid, filled_value=v, status="filled", source_spans=spans, char_count=len(v), engine="verify",
            source_quote=" … ".join(sp.text for sp in spans),
            source_line_range=(min(sp.line_start for sp in spans), max(sp.line_end for sp in spans)) if spans else None,
            reasoning=(f"File value differs from what the memo supports for this field («{e.filled_value or e.suggested_value}»)"
                       if e is not None and (e.filled_value or e.suggested_value) else "No evidence found for this field in the memo."),
        ))
    return out


def apply_overrides(scan_r: ScanResult, mappings: list[FieldMapping], overrides: dict[str, str]) -> list[FieldMapping]:
    by_id = {m.slot_id: m for m in mappings}
    for sid, value in (overrides or {}).items():
        if sid not in by_id or value is None or not str(value).strip():
            continue
        hm = human_mapping(scan_r.slot(sid), str(value))
        if hm.errors:
            prev = by_id[sid]
            by_id[sid] = prev.model_copy(update={"status": "needs_human_input", "filled_value": "",
                                                 "suggested_value": str(value).strip(), "errors": hm.errors,
                                                 "warnings": ["reviewer value rejected by the form's constraints"],
                                                 "attempts": prev.attempts + hm.attempts})
        else:
            by_id[sid] = hm
    return [by_id[m.slot_id] for m in mappings]


# ---------------------------------------------------------------- previews


def _occurrence_ranges(scan_r: ScanResult, values: dict[str, str]):
    """Yield (slot_id, box_id, orig_range, new_range, state) for every slot occurrence."""
    seen = set()
    for s in scan_r.slots:
        for o in s.occurrences:
            key = (o.part_name, o.container_index)
            if o.marker_kind == "empty_cell":
                v = values.get(s.slot_id)
                para_box = _para_box(o)
                yield s.slot_id, o.box_id, para_box, None, (0, len(v)) if v else None, "filled" if v else "pending"
                continue
            if key in seen:
                continue
            seen.add(key)
            idx = scan_r.parts[o.part_name]
            text = idx.container_text(idx.containers[o.container_index])
            shift = 0
            for h in find_markers(text):
                v = values.get(h.slot_id)
                orig = (h.start, h.end)
                if v is None:
                    new = (h.start + shift, h.end + shift)
                    state = "pending"
                else:
                    new = (h.start + shift, h.start + shift + len(v))
                    shift += len(v) - (h.end - h.start)
                    state = "filled"
                box = o.box_id if h.slot_id == s.slot_id else next(
                    (oo.box_id for ss in scan_r.slots if ss.slot_id == h.slot_id for oo in ss.occurrences
                     if (oo.part_name, oo.container_index) == key), o.box_id)
                yield h.slot_id, box, _para_box(o), orig, new, state


def _para_box(o) -> Optional[str]:
    if o.paragraph_index is None:
        return None
    stem = o.part_name.rsplit("/", 1)[-1].removesuffix(".xml")
    return ("" if stem == "document" else f"{stem}:") + f"p{o.paragraph_index}"


def _labels(scan_r: ScanResult) -> dict[str, str]:
    return {s.slot_id: s.label for s in scan_r.slots}


def build_previews(result_scan: ScanResult, filled_path: Path, values: dict[str, str],
                   naive_path: Optional[Path], naive_report: Optional[VerificationReport],
                   png_dir: Optional[Path] = None) -> tuple[dict[str, str], dict[str, Any]]:
    ft = result_scan.file_type
    labels = _labels(result_scan)
    if ft == "docx":
        blank_layout, blank_r = layout_docx(result_scan.path)
        filled_layout, filled_r = layout_docx(filled_path)
    else:
        blank_layout, blank_r = layout_xlsx(result_scan.path), None
        filled_layout, filled_r = layout_xlsx(filled_path), None
    ov_blank: list[Overlay] = []
    ov_filled: list[Overlay] = []
    slot_boxes: set[str] = set()
    for sid, box, para, orig, new, state in _occurrence_ranges(result_scan, values):
        slot_boxes.add(box)
        if para:
            slot_boxes.add(para)
        lab = labels.get(sid, sid)
        if ft == "docx" and para:
            if orig is not None:
                for pg, x, y, w, h in char_range_rects(blank_r, para, *orig):
                    ov_blank.append(Overlay(pg, x, y, w, h, "fg-marker", f"{lab}（記入欄）", sid, box))
            if new is not None:
                cls = "fg-slot" if state == "filled" else "fg-pending"
                tip = f"{lab}: {values.get(sid, '')}" if state == "filled" else f"{lab}: 人の入力待ち / awaiting human input"
                for pg, x, y, w, h in char_range_rects(filled_r, para, *new):
                    ov_filled.append(Overlay(pg, x, y, w, h, cls, tip, sid, box))
        for lay, ovs, cls in ((blank_layout, ov_blank, "fg-marker"), (filled_layout, ov_filled, None)):
            b = lay.boxes.get(box)
            if b is None:
                continue
            if ft == "xlsx":
                c = cls or ("fg-slot" if state == "filled" else "fg-pending")
                tip = f"{lab}: {values.get(sid, '')}" if state == "filled" and cls is None else f"{lab}"
                ovs.append(Overlay(b.page, b.x + 1, b.y + 1, b.w - 2, b.h - 2, c, tip, sid, box))
            elif box.startswith("t") or ".r" in box:
                ovs.append(Overlay(b.page, b.x + 1.5, b.y + 1.5, b.w - 3, b.h - 3, "fg-slot-cell", lab, sid, box))
    visual = compare_layouts(blank_layout, filled_layout, slot_boxes)
    n_filled = sum(1 for v in values.values() if v)
    previews = {
        "blank": svg_document(blank_layout, ov_blank, "Blank original form", "Blank original — designated fields in blue / 空欄の原本（青＝記入欄）", "info"),
        "filled": svg_document(filled_layout, ov_filled, "Form-Guard output",
                               f"Form-Guard — {n_filled} fields filled, layout outside fields "
                               f"{'identical' if visual['identical_outside_slots'] else 'CHANGED'} / 指定欄のみ記入",
                               "ok" if visual["identical_outside_slots"] else "bad"),
    }
    naive_layout = None
    ov_naive: list[Overlay] = []
    if naive_path and naive_path.exists():
        naive_layout = layout_docx(naive_path)[0] if ft == "docx" else layout_xlsx(naive_path)
        ov_naive, banner = naive_overlays(result_scan, blank_layout, naive_layout, naive_report)
        previews["naive"] = svg_document(naive_layout, ov_naive, "Naive AI regeneration (simulated)", banner, "bad")
        visual["naive"] = {"page": naive_layout.meta.get("page_mm") or naive_layout.meta.get("page_px"),
                           "pages": len(naive_layout.pages), "overlays": len(ov_naive)}
    if png_dir is not None:
        pngs = {}
        for name, lay, ovs in (("blank", blank_layout, ov_blank), ("filled", filled_layout, ov_filled),
                               ("naive", naive_layout, ov_naive)):
            if lay is not None:
                p = render_png(lay, ovs, png_dir / f"preview_{name}.png")
                if p:
                    pngs[name] = p.name
        visual["png"] = pngs
    return previews, visual


def naive_overlays(scan_r: ScanResult, blank: Layout, naive: Layout,
                   naive_report: Optional[VerificationReport]) -> tuple[list[Overlay], str]:
    ovs: list[Overlay] = []
    checks = {c.check_id: c for c in (naive_report.checks if naive_report else [])}
    altered = checks.get("TEXT-01").evidence.get("altered", []) if "TEXT-01" in checks else []
    invented = checks.get("GRD-02").evidence.get("invented", []) if "GRD-02" in checks else []
    blank_texts = {b.text for b in blank.boxes.values() if b.text}
    text_boxes = [b for b in naive.boxes.values() if b.text and b.kind in ("para", "cell")]
    used = set()
    for orig in altered:
        best, score = None, 0.0
        for b in text_boxes:
            if b.box_id in used or b.text in blank_texts:
                continue
            r = difflib.SequenceMatcher(None, orig, b.text).ratio()
            if r > score:
                best, score = b, r
        if best is not None and score >= 0.35:
            used.add(best.box_id)
            ovs.append(Overlay(best.page, best.x, best.y, best.w, best.h, "nv-alter",
                               f"原文と異なる / altered printed text — original: 「{orig[:60]}」", "", best.box_id))
    for b in text_boxes:
        hit = [n for n in invented if n in b.text]
        if hit:
            ovs.append(Overlay(b.page, b.x, b.y, b.w, b.h, "nv-invent",
                               f"メモにも様式にも無い数値 / invented number: {', '.join(hit)}", "", b.box_id))
    lay = checks.get("LAYOUT-02")
    if lay is not None and not lay.passed:
        for b in naive.boxes.values():
            if b.kind == "table":
                ovs.append(Overlay(b.page, b.x - 3, b.y - 3, b.w + 6, b.h + 6, "nv-border",
                                   "罫線・結合セルが失われた / borders and merged cells lost", "", b.box_id))
        if not any(b.kind == "table" for b in naive.boxes.values()) and naive.boxes:
            xs = [b.x for b in naive.boxes.values()]
            ys = [b.y for b in naive.boxes.values()]
            x1 = max(b.x + b.w for b in naive.boxes.values())
            y1 = max(b.y + b.h for b in naive.boxes.values())
            ovs.append(Overlay(0, min(xs) - 3, min(ys) - 3, x1 - min(xs) + 6, y1 - min(ys) + 6, "nv-border",
                               "罫線・結合セル・入力規則・計算式が失われた / borders, merges, validation, formulas lost"))
    parts = ["Naive AI (simulated)"]
    if naive.meta.get("page_mm") and blank.meta.get("page_mm") and naive.meta["page_mm"] != blank.meta["page_mm"]:
        parts.append(f"page {naive.meta['page_mm'][0]}×{naive.meta['page_mm'][1]}mm ≠ {blank.meta['page_mm'][0]}×{blank.meta['page_mm'][1]}mm")
    if "LAYOUT-01" in checks and not checks["LAYOUT-01"].passed:
        o, f = checks["LAYOUT-01"].evidence.get("original", {}), checks["LAYOUT-01"].evidence.get("filled", {})
        parts.append(f"merged cells {o.get('merges', '?')}→{f.get('merges', '?')}")
    if altered:
        parts.append(f"{len(altered)} printed texts altered")
    if invented:
        parts.append(f"{len(invented)} invented numbers")
    return ovs, " · ".join(parts)


# ---------------------------------------------------------------- main entry


def run_pipeline(
    form_path: Union[str, Path],
    memo_text: str,
    out_dir: Union[str, Path],
    *,
    mode: str = "auto",
    model: Optional[str] = None,
    client: Any = None,
    naive_path: Union[str, Path, None] = None,
    overrides: Optional[dict[str, str]] = None,
    slot_spec: Union[dict, str, Path, None] = None,
    demo_id: Optional[str] = None,
    run_id: Optional[str] = None,
    prior: Optional[tuple[list[FieldMapping], dict[str, Any]]] = None,
    png: bool = False,
    write_outputs: bool = True,
) -> PipelineResult:
    timings: dict[str, float] = {}
    t = time.perf_counter()
    form_path = Path(form_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    memo_text = memo_text.replace("\r\n", "\n").replace("\r", "\n")
    scan_r = scan(form_path, slot_spec)
    timings["scan"] = time.perf_counter() - t

    t = time.perf_counter()
    if prior is not None:
        mappings, engine_info = [m.model_copy(deep=True) for m in prior[0]], dict(prior[1])
    else:
        run, engine_info = _map(scan_r, memo_text, mode, model, client)
        mappings = run.mappings
    mappings = apply_overrides(scan_r, mappings, overrides or {})
    timings["map"] = time.perf_counter() - t

    t = time.perf_counter()
    values = {m.slot_id: m.filled_value for m in mappings if m.is_written}
    filled_path = out_dir / f"filled_{form_path.stem}{form_path.suffix}"
    write = fill_form(scan_r, values, filled_path)
    timings["write"] = time.perf_counter() - t

    t = time.perf_counter()
    report = verify(scan_r, filled_path, mappings, memo_text)
    naive_p = Path(naive_path) if naive_path else None
    naive_report = compare_naive(scan_r, naive_p, memo_text) if naive_p and naive_p.exists() else None
    timings["verify"] = time.perf_counter() - t

    t = time.perf_counter()
    prepared = {k: prepare_value(v, scan_r.file_type) for k, v in values.items()}
    previews, visual = build_previews(scan_r, filled_path, prepared, naive_p, naive_report, out_dir if png else None)
    report.visual_diff_summary = visual
    if not visual.get("identical_outside_slots", True):
        report.checks.append(_drift_check(visual))
        report.failed_count += 1
        report.overall_status = "FAIL"
    timings["visual"] = time.perf_counter() - t

    result = PipelineResult(
        run_id=run_id or new_run_id(), form_path=form_path, filled_path=filled_path, memo_text=memo_text,
        scan=scan_r, mappings=mappings, write=write, report=report, naive_report=naive_report, previews=previews,
        visual=visual, engine=engine_info, timings=timings, demo_id=demo_id, out_dir=out_dir, naive_path=naive_p,
        builtin_demo=is_builtin_demo(demo_id, form_path, memo_text, mappings),
    )
    if write_outputs:
        write_run_outputs(result)
    return result


def _drift_check(visual: dict[str, Any]):
    from .models import AuditCheckItem

    drift = visual.get("drift_outside_slots") or []
    n_over = visual.get("overflow_count", 0)
    n_moved = (visual.get("drift_count") or 0) - n_over
    parts = []
    if n_over:
        parts.append(f"{n_over} value(s) overflow their cell ("
                     + "; ".join(f"{d['box']}: {d['issue']}" for d in drift if "overflow" in d.get("issue", ""))[:200] + ")")
    if n_moved:
        parts.append(f"{n_moved} boxes moved (max {visual.get('max_shift_px')}px)")
    if visual.get("pages_blank") != visual.get("pages_filled"):
        parts.append(f"pages {visual.get('pages_blank')}→{visual.get('pages_filled')}")
    return AuditCheckItem(check_id="VIS-01", category="visual", passed=False, severity="major",
                          description="Rendered layout outside the designated fields is unchanged and every value fits its cell",
                          details=" · ".join(parts) + " — a value is probably too long for its field",
                          evidence={"drift": drift})


def write_run_outputs(result: PipelineResult) -> dict[str, Path]:
    from .report import build_report_html

    out = result.out_dir or result.filled_path.parent
    paths = {}
    d = result.to_dict(include_previews=False)
    p = out / "audit_log.json"
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2, default=str), encoding="utf-8", newline="\n")
    paths["audit"] = p
    for name, svg in result.previews.items():
        sp = out / f"preview_{name}.svg"
        sp.write_text(svg, encoding="utf-8", newline="\n")
        paths[f"svg_{name}"] = sp
    rp = out / "report.html"
    rp.write_text(build_report_html(result.to_dict()), encoding="utf-8", newline="\n")
    paths["report"] = rp
    return paths
