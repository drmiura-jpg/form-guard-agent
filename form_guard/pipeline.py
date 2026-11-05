"""End-to-end orchestration: Scan -> Map & Self-Correct -> Surgical Write -> Verify -> Visual Diff & HITL."""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
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
    advance_notice_warnings,
    human_mapping,
    make_engine,
    map_fields,
    normalize_reviewer_value,
    resolve_mode,
)
from .models import FieldMapping, PatchRecord, VerificationReport
from .scanner import ScanResult, era_date_template, find_markers, scan
from .textnorm import clean_text
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
    memo_notes: list[str] = field(default_factory=list)  # invisible characters removed / NFD kana composed at intake
    adapt_history: list[dict[str, Any]] = field(default_factory=list)  # auto-adapt rounds (slot_id, old/new max_chars)

    @property
    def verified_sha256(self) -> str:
        """SHA-256 of the exact filled bytes the verifier checked."""
        return str(self.report.stats.get("filled_sha256", ""))

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
            "memo": {"text": self.memo_text, "lines": len(memo_lines), "sha256": hashlib.sha256(self.memo_text.encode()).hexdigest(),
                     **({"normalized": list(self.memo_notes)} if self.memo_notes else {})},
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
        if self.adapt_history:
            d["auto_adapt"] = self.adapt_history
        if include_previews:
            d["previews"] = self.previews
        return d


def _sha_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest() if Path(p).exists() else ""


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


# ---------------------------------------------------------------- mapping step


def _map(scan_r: ScanResult, memo: str, mode: str, model: Optional[str], client: Any,
         model_tier: Optional[str] = None, slots: Optional[list[FormSlot]] = None) -> tuple[MappingRun, dict]:
    requested = (mode or "auto").lower()
    resolved = resolve_mode(requested) if client is None else ("live" if requested in ("auto", "live") else "demo")
    engine = make_engine(resolved, model, client, model_tier=model_tier)
    info: dict[str, Any] = {"requested_mode": requested, "mode": resolved, "label": engine.label,
                            "model": getattr(engine, "model", None), "model_tier": getattr(engine, "model_tier", None),
                            "fallback_reason": None}
    target_slots = scan_r.slots if slots is None else slots
    try:
        run = map_fields(target_slots, memo, engine, scan_r.title)
    except LiveEngineError as exc:
        if requested != "auto":
            raise
        partial = copy.deepcopy(getattr(engine, "usage", None) or {})  # the calls that succeeded before the failure
        info.update(mode="demo", label=DemoEngine.label + " — live call failed, used offline engine",
                    fallback_reason=str(exc))
        run = map_fields(target_slots, memo, DemoEngine(), scan_r.title)
        if partial.get("requests", 0) > 0 or partial.get("calls"):
            # the values come from the offline engine, but the API was called and billed: never report 0 calls
            for key, empty in (("requests", 0), ("input_tokens", 0), ("output_tokens", 0),
                               ("cache_creation_input_tokens", 0), ("cache_read_input_tokens", 0),
                               ("models", []), ("calls", []), ("model_fallbacks", [])):
                run.usage[key] = partial.get(key, empty)
            run.usage["live_attempt_failed"] = True
    info["usage"] = run.usage
    info["rounds"] = run.rounds
    return run, info


def _merge_usage(usages: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Combine usage dicts across blank-cell discovery and adaptation rounds without double-counting prior runs."""
    if len(usages) == 1 and usages[0][0] == "map_round_0":
        return copy.deepcopy(usages[0][1])
    has_live = any(("requests" in u or u.get("calls") or u.get("models")) for _, u in usages if isinstance(u, dict))
    merged: dict[str, Any] = {
        "models": [], "calls": [], "model_fallbacks": [], "escalations": [], "routing": [],
    }
    if has_live:
        merged.update({
            "requests": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        })
    by_stage: list[dict[str, Any]] = []
    for stage, u in usages:
        if not isinstance(u, dict):
            continue
        by_stage.append({"stage": stage, "usage": copy.deepcopy(u)})
        if has_live:
            for k in ("requests", "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                merged[k] += int(u.get(k, 0) or 0)
        for m in u.get("models") or []:
            if m and m not in merged["models"]:
                merged["models"].append(m)
        for k in ("calls", "model_fallbacks", "escalations", "routing"):
            merged[k].extend(copy.deepcopy(u.get(k) or []))
        if u.get("live_attempt_failed"):
            merged["live_attempt_failed"] = True
        for k, v in u.items():
            if k not in merged and k not in ("requests", "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                merged[k] = copy.deepcopy(v)
    if len(by_stage) > 1 or (by_stage and by_stage[0]["stage"] == "discovery"):
        merged["usage_by_round"] = by_stage
    return merged


def is_builtin_demo(demo_id: Optional[str], form_path: Path, memo_text: str, mappings: list[FieldMapping]) -> bool:
    """True only for an unmodified built-in demo (marked, or unmarked with reviewer-confirmed cells): its sample
    form, its sample memo, no reviewer-entered values."""
    from .samples import MEMOS, demo_by_id, pdf_by_id, unmarked_by_id

    if not demo_id:
        return False
    demo = None
    for lookup in (demo_by_id, unmarked_by_id, pdf_by_id):
        try:
            demo = lookup(demo_id)
            break
        except KeyError:
            continue
    if demo is None:
        return False
    return (Path(form_path).name == demo["form"] and memo_text == MEMOS.get(demo["memo"])
            and not any(m.status == "human_entered" for m in mappings))


VERIFY_ROUTE = ("verify (read back from the filled file)",
                "no model: the file's value is checked against the memo evidence for this field")


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
        if sid in attested and prepare_value(normalize_reviewer_value(slot, attested[sid])[0], ft) == v:
            out.append(human_mapping(slot, attested[sid]))  # the file holds the normalised value (1234567890123)
            continue
        if not memo.strip():
            out.append(FieldMapping(slot_id=sid, filled_value=v, status="recovered", char_count=len(v), engine="verify",
                                    normalizations=["recovered_unverified"], routed_model=VERIFY_ROUTE[0],
                                    route_reason="no model: read back from the filled file, no memo to check it against",
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
            routed_model=VERIFY_ROUTE[0], route_reason=VERIFY_ROUTE[1],
            source_quote=" … ".join(sp.text for sp in spans),
            source_line_range=(min(sp.line_start for sp in spans), max(sp.line_end for sp in spans)) if spans else None,
            reasoning=(f"File value differs from what the memo supports for this field («{e.filled_value or e.suggested_value}»)"
                       if e is not None and (e.filled_value or e.suggested_value) else "No evidence found for this field in the memo."),
        ))
    return out


AUDIT_LOG_NAMES = ("audit_log.json", "audit_report.json")


def matching_audit_log(filled: Union[str, Path]) -> tuple[Optional[dict], str]:
    """The audit log written next to ``filled`` for exactly this file (same SHA-256), if any."""
    sha = hashlib.sha256(Path(filled).read_bytes()).hexdigest()
    note = ""
    for name in AUDIT_LOG_NAMES:
        p = Path(filled).parent / name
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            note = f"{name} is unreadable — ignored"
            continue
        if isinstance(data, dict) and (data.get("filled") or {}).get("sha256") == sha:
            return data, f"{name} (SHA-256 matches the filled file)"
        note = f"{name} belongs to a different file (SHA-256 mismatch) — ignored"
    return None, note


@dataclass
class VerifyOutcome:
    report: VerificationReport
    scan: ScanResult
    values: dict[str, str]
    memo_text: str
    basis: str
    audit_note: str

    @property
    def grounding_verified(self) -> bool:
        return not any(c.evidence.get("not_verified") for c in self.report.checks)


def verify_filled_copy(original: Union[str, Path], filled: Union[str, Path], memo_text: Optional[str] = None,
                       slot_spec: Union[dict, str, Path, None] = None, attested: Optional[dict[str, str]] = None,
                       memo_name: str = "") -> VerifyOutcome:
    """Audit any filled copy against its blank original (CLI ``--verify``, MCP ``form_guard_verify``).

    Structure and printed rules are always checked. Grounding uses ``memo_text`` if given, else the memo recorded in a
    matching ``audit_log.json`` next to the filled file; without either the report says grounding was not verified.
    """
    from .verifier import infer_values_from_filled

    sc = scan(original, slot_spec)
    values = infer_values_from_filled(sc, filled)
    audit, audit_note = matching_audit_log(filled)
    if memo_text is not None:
        memo = memo_text
    else:
        memo = (audit.get("memo") or {}).get("text", "") if audit else ""
    memo = clean_text(memo.replace("\r\n", "\n").replace("\r", "\n"))[0]
    recorded = [FieldMapping.model_validate(m) for m in audit.get("mappings", [])] if audit else []
    maps = reconstruct_mappings(sc, values, memo, recorded, attested or {})
    maps = advance_notice_warnings(sc.slots, maps, memo)  # printed deadlines between two dates, on the values found
    rep = verify(sc, filled, maps, memo)
    if not memo:
        basis = "none"
    elif memo_text is not None:
        basis = f"memo {memo_name}" if memo_name else "memo provided"
    else:
        basis = "memo recorded in the audit log"
    return VerifyOutcome(rep, sc, values, memo, basis, audit_note)


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
    from .constraints import normalize_checkbox_value

    seen = set()
    for s in scan_r.slots:
        for o in s.occurrences:
            key = (o.part_name, o.container_index)
            if o.marker_kind == "empty_cell" or (o.marker_kind == "date_template" and o.cell_ref):
                v = values.get(s.slot_id)
                para_box = _para_box(o)
                yield s.slot_id, o.box_id, para_box, None, (0, len(v)) if v else None, "filled" if v else "pending"
                continue
            if o.marker_kind == "date_template":  # Word: the printed 「令和　　年　　月　　日」 is replaced by the date
                idx = scan_r.parts[o.part_name]
                span = era_date_template(idx.container_text(idx.containers[o.container_index]))
                if span is None:
                    continue
                v = values.get(s.slot_id)
                new = (span[0], span[0] + len(v)) if v is not None else span
                yield s.slot_id, o.box_id, _para_box(o), span, new, "filled" if v is not None else "pending"
                continue
            if o.marker_kind == "checkbox_text":  # highlight the □ of every option (■ where ticked)
                v = values.get(s.slot_id)
                picked = set(normalize_checkbox_value(v, [x["label"] for x in o.checkbox_options])) if v else set()
                if o.paragraph_index is None:  # an Excel cell: the cell itself
                    yield s.slot_id, o.box_id, None, None, None, "filled" if picked else "pending"
                    continue
                for x in o.checkbox_options:
                    on = x["label"] in picked
                    rng = (x["offset"], x["offset"] + 1)
                    yield s.slot_id, o.box_id, _para_box(o), rng, rng if on or not v else None, \
                        "filled" if on else "pending"
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
    if ft == "pdf":  # fixed PDF: same SVG / PNG pipeline, plus a pixel diff outside the slot boxes
        from .pdf_support import build_pdf_previews

        return build_pdf_previews(result_scan, filled_path, values, naive_path, naive_report, png_dir)
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
    box_slot: dict[str, str] = {}  # box id -> slot id, to name the field behind a VIS-01 failure
    for sid, box, para, orig, new, state in _occurrence_ranges(result_scan, values):
        slot_boxes.add(box)
        box_slot.setdefault(box, sid)
        if para:
            slot_boxes.add(para)
            box_slot.setdefault(para, sid)
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
    visual["grown_slots"] = _grown_slots(blank_layout, filled_layout, box_slot, labels)
    if visual["grown_slots"]:
        visual["identical_outside_slots"] = False
        drift_list = list(visual.get("drift_outside_slots") or [])
        existing_boxes = {d.get("box") for d in drift_list}
        for g in visual["grown_slots"]:
            if g["box"] not in existing_boxes:
                drift_list.append({
                    "box": g["box"], "slot_id": g["slot_id"], "label": g["label"],
                    "issue": f"row/cell grew by +{g['grew_px']}px", "grew_px": g["grew_px"],
                })
        visual["drift_outside_slots"] = drift_list
        visual["drift_count"] = len(drift_list)
    for d in visual.get("drift_outside_slots") or []:
        if d.get("box") in box_slot:  # a value overflowing its own cell: name the field
            d["slot_id"] = box_slot[d["box"]]
            d["label"] = labels.get(d["slot_id"], d["slot_id"])
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


def _grown_slots(blank: Layout, filled: Layout, box_slot: dict[str, str], labels: dict[str, str],
                 tolerance: float = 0.6) -> list[dict[str, Any]]:
    """Slots whose own box is taller in the filled copy: the value wraps onto more lines than the blank cell held,
    so the row grows and everything below it moves (the usual cause of VIS-01 drift in a Word form)."""
    grown: dict[str, dict[str, Any]] = {}
    for bid, sid in box_slot.items():
        b, f = blank.boxes.get(bid), filled.boxes.get(bid)
        if b is None or f is None or b.kind != "cell" or f.h - b.h <= tolerance:
            continue
        px = round(f.h - b.h, 1)
        if sid not in grown or px > grown[sid]["grew_px"]:
            grown[sid] = {"slot_id": sid, "label": labels.get(sid, sid), "box": bid, "grew_px": px}
    return sorted(grown.values(), key=lambda g: -g["grew_px"])


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
    model_tier: Optional[str] = None,
    allow_office_only: bool = False,
    auto_adapt: bool = False,
    max_adapt_rounds: int = 3,
) -> PipelineResult:
    """Scan -> map & self-correct -> surgical write -> verify -> visual diff, for one blank form and memo.

    ``model_tier`` (auto | opus | sonnet | haiku; None = ``FORM_GUARD_MODEL_TIER`` or auto) selects the Claude
    model(s) in live mode; ``slot_spec`` may be a spec built by ``build_confirmed_slot_spec`` for an unmarked form.
    A coordinate target (explicit spec or sidecar .slots.json) that points at an office-only cell of the blank form
    (受付番号・受付印・審査欄・決裁・検印・事務局 …) is refused before anything is written, unless the internal
    ``allow_office_only`` override is passed (MCP and the web app never do).
    When ``auto_adapt`` is True, an unmarked form with 0 slots automatically runs blank-cell discovery and builds an
    in-memory slot spec bound to the blank form's SHA-256; if ``VIS-01`` reports cell overflow or row growth, the
    culprit slots' ``max_chars`` are tightened (up to ``max_adapt_rounds`` times) and re-mapped / re-verified.
    """
    from .scanner import OFFICE_REASON, build_confirmed_slot_spec, discover_blank_cells, office_only_targets, recommended_ids

    if isinstance(max_adapt_rounds, bool) or not isinstance(max_adapt_rounds, int) or max_adapt_rounds < 0:
        raise ValueError("max_adapt_rounds must be a non-negative integer")
    timings: dict[str, float] = {}
    adapt_history: list[dict[str, Any]] = []
    stage_usages: list[tuple[str, dict[str, Any]]] = []
    disc_usage: Optional[dict[str, Any]] = None
    t = time.perf_counter()
    form_path = Path(form_path)
    out_dir = Path(out_dir)
    initial_form_sha = _sha_file(form_path)
    # invisible characters and NFD kana would be copied into the form verbatim: clean the memo once, at intake
    memo_text, memo_notes = clean_text(memo_text.replace("\r\n", "\n").replace("\r", "\n"))
    scan_r = scan(form_path, copy.deepcopy(slot_spec) if isinstance(slot_spec, dict) else slot_spec)
    if auto_adapt and not scan_r.slots and slot_spec is None:
        disc = discover_blank_cells(form_path, mode=mode if mode in ("demo", "live", "auto") else "demo",
                                    client=client, model_tier=model_tier, model=model)
        disc_u = (disc.get("engine") or {}).get("usage") or disc.get("live_usage")
        if isinstance(disc_u, dict) and (disc_u.get("requests", 0) > 0 or disc_u.get("calls")):
            disc_usage = copy.deepcopy(disc_u)
            stage_usages.append(("discovery", disc_usage))
        rec_ids = recommended_ids(disc)
        if rec_ids:
            auto_spec = build_confirmed_slot_spec(disc, rec_ids, form_path, allow_office_only=False)
            auto_spec["source"] = "auto-adapt blank-cell discovery (bound to blank form SHA-256; human review required)"
            auto_spec["human_confirmed"] = False
            scan_r = scan(form_path, auto_spec)
            adapt_history.append({
                "round": 0, "action": "auto_discover_unmarked",
                "form_sha256": auto_spec.get("form_sha256"),
                "confirmed_count": len(rec_ids),
            })
    office = [] if allow_office_only else office_only_targets(form_path, scan_r.spec)
    if office:
        raise ValueError(f"refused before filling — the slot spec designates office-only cell(s): {'; '.join(office)} "
                         f"({OFFICE_REASON})")
    out_dir.mkdir(parents=True, exist_ok=True)
    timings["scan"] = time.perf_counter() - t

    tightened_ids: set[str] = set()
    prev_mappings: list[FieldMapping] = []
    engine_info: dict[str, Any] = {}
    for adapt_round in range((max_adapt_rounds if auto_adapt else 0) + 1):
        t = time.perf_counter()
        if adapt_round == 0:
            if prior is not None:
                mappings, engine_info = [m.model_copy(deep=True) for m in prior[0]], copy.deepcopy(dict(prior[1]))
            else:
                run, engine_info = _map(scan_r, memo_text, mode, model, client, model_tier)
                mappings = run.mappings
                if isinstance(engine_info.get("usage"), dict):
                    stage_usages.append(("map_round_0", engine_info["usage"]))
        else:
            slots_to_remap = [s for s in scan_r.slots if s.slot_id in tightened_ids]
            run, round_info = _map(scan_r, memo_text, mode, model, client, model_tier, slots=slots_to_remap)
            if isinstance(round_info.get("usage"), dict):
                stage_usages.append((f"adapt_round_{adapt_round}", round_info["usage"]))
            if not engine_info or prior is not None:
                engine_info = round_info
            else:
                engine_info["rounds"] = int(engine_info.get("rounds", 0) or 0) + int(round_info.get("rounds", 0) or 0)
                if round_info.get("fallback_reason") and not engine_info.get("fallback_reason"):
                    engine_info.update(mode=round_info.get("mode"), label=round_info.get("label"),
                                       fallback_reason=round_info.get("fallback_reason"))
            remapped_by_id = {m.slot_id: m for m in run.mappings}
            mappings = [
                remapped_by_id[m.slot_id] if (m.slot_id in remapped_by_id and m.status != "human_entered")
                else m.model_copy(deep=True)
                for m in prev_mappings
            ]
        mappings = apply_overrides(scan_r, mappings, overrides or {})
        # printed deadlines that relate two dates (「変更予定日の30日前までに提出」), on the final values incl. edits
        mappings = advance_notice_warnings(scan_r.slots, mappings, memo_text)
        prev_mappings = [m.model_copy(deep=True) for m in mappings]
        timings["map"] = timings.get("map", 0.0) + (time.perf_counter() - t)

        t = time.perf_counter()
        values = {m.slot_id: m.filled_value for m in mappings if m.is_written}
        filled_path = out_dir / f"filled_{form_path.stem}{form_path.suffix}"
        write = fill_form(scan_r, values, filled_path)
        timings["write"] = timings.get("write", 0.0) + (time.perf_counter() - t)

        t = time.perf_counter()
        report = verify(scan_r, filled_path, mappings, memo_text)
        naive_p = Path(naive_path) if naive_path else None
        naive_report = compare_naive(scan_r, naive_p, memo_text) if naive_p and naive_p.exists() else None
        timings["verify"] = timings.get("verify", 0.0) + (time.perf_counter() - t)

        t = time.perf_counter()
        prepared = {k: prepare_value(v, scan_r.file_type) for k, v in values.items()}
        previews, visual = build_previews(scan_r, filled_path, prepared, naive_p, naive_report, out_dir if png else None)
        report.visual_diff_summary = visual
        if not visual.get("identical_outside_slots", True):
            report.checks.append(_drift_check(visual))
            report.failed_count += 1
            report.overall_status = "FAIL"
        timings["visual"] = timings.get("visual", 0.0) + (time.perf_counter() - t)

        if not auto_adapt or visual.get("identical_outside_slots", True) or adapt_round >= max_adapt_rounds:
            break
        adjustments = _tighten_overflow_slots(scan_r, mappings, visual, adapt_round + 1)
        if not adjustments:
            break
        tightened_ids = {a["slot_id"] for a in adjustments}
        adapt_history.extend(adjustments)

    if stage_usages:
        if prior is not None and isinstance(prior[1].get("usage"), dict):
            engine_info["prior_usage"] = copy.deepcopy(prior[1]["usage"])
        engine_info["usage"] = _merge_usage(stage_usages)
        if disc_usage is not None:
            engine_info["discovery_usage"] = disc_usage

    if auto_adapt and adapt_history:
        current_sha = _sha_file(form_path)
        if current_sha != initial_form_sha:
            raise ValueError("blank form changed during auto-adapt — re-run on a stable file")
        existing_sha = scan_r.spec.get("form_sha256")
        if existing_sha and existing_sha != current_sha:
            raise ValueError("blank form SHA-256 does not match the slot spec's form_sha256")
        scan_r.spec["form_sha256"] = current_sha
        scan_r.spec.setdefault("human_confirmed", False)

    result = PipelineResult(
        run_id=run_id or new_run_id(), form_path=form_path, filled_path=filled_path, memo_text=memo_text,
        scan=scan_r, mappings=mappings, write=write, report=report, naive_report=naive_report, previews=previews,
        visual=visual, engine=engine_info, timings=timings, demo_id=demo_id, out_dir=out_dir, naive_path=naive_p,
        builtin_demo=is_builtin_demo(demo_id, form_path, memo_text, mappings), memo_notes=memo_notes,
        adapt_history=adapt_history,
    )
    if write_outputs:
        write_run_outputs(result)
    return result


def _tighten_overflow_slots(scan_r: ScanResult, mappings: list[FieldMapping], visual: dict[str, Any],
                            round_no: int) -> list[dict[str, Any]]:
    """Reduce ``max_chars`` on slots that caused VIS-01 cell overflow or row growth, so the next mapping pass
    condenses or holds them for human review instead of overflowing the form layout."""
    by_id = {m.slot_id: m for m in mappings}
    culprit_ratios: dict[str, float] = {}
    for d in visual.get("drift_outside_slots") or []:
        sid = d.get("slot_id")
        if sid:
            culprit_ratios[sid] = min(culprit_ratios.get(sid, 0.7), 0.65)
    for g in visual.get("grown_slots") or []:
        sid = g.get("slot_id")
        if sid:
            culprit_ratios[sid] = min(culprit_ratios.get(sid, 0.7), 0.6)
    adjustments: list[dict[str, Any]] = []
    for slot in scan_r.slots:
        if slot.slot_id not in culprit_ratios:
            continue
        if slot.required_format == "choice" or slot.constraints.get("checkbox"):
            continue
        m = by_id.get(slot.slot_id)
        if m is not None and m.status == "human_entered":
            continue
        cur_val = (m.filled_value or "") if m else ""
        cur_len = len(cur_val)
        if cur_len <= 1:
            continue
        old_max = slot.max_chars
        ratio = culprit_ratios[slot.slot_id]
        target_max = max(1, min(cur_len - 1, int(cur_len * ratio)))
        if old_max is not None and target_max >= old_max:
            target_max = max(1, old_max - 1)
        if old_max is not None and target_max == old_max:
            continue
        slot.max_chars = target_max
        slot_spec_slots = scan_r.spec.setdefault("slots", {})
        slot_entry = slot_spec_slots.setdefault(slot.slot_id, {"label": slot.label})
        slot_entry["max_chars"] = target_max
        for t_entry in scan_r.spec.get("targets") or []:
            if isinstance(t_entry, dict) and t_entry.get("slot_id") == slot.slot_id:
                t_entry["max_chars"] = target_max
        adjustments.append({
            "round": round_no,
            "action": "tighten_max_chars",
            "slot_id": slot.slot_id,
            "label": slot.label,
            "previous_len": cur_len,
            "old_max_chars": old_max,
            "new_max_chars": target_max,
        })
    return adjustments


def _drift_check(visual: dict[str, Any]):
    from .models import AuditCheckItem

    drift = visual.get("drift_outside_slots") or []
    n_over = visual.get("overflow_count", 0)
    n_moved = (visual.get("drift_count") or 0) - n_over
    parts = []
    if n_over:
        parts.append(f"{n_over} value(s) overflow their cell ("
                     + "; ".join(f"{d['box']}: {d['issue']}" for d in drift if "overflow" in d.get("issue", ""))[:200] + ")")
    px = visual.get("pixel_diff") or {}
    if px.get("method") == "pixel" and px.get("outside"):
        n_moved -= 1  # the pixel finding is listed on its own
        parts.append(f"{px['outside']} px differ outside the designated boxes (pixel diff of the rendered pages)")
    if n_moved > 0:
        parts.append(f"{n_moved} boxes moved (max {visual.get('max_shift_px')}px)")
    if visual.get("pages_blank") != visual.get("pages_filled"):
        parts.append(f"pages {visual.get('pages_blank')}→{visual.get('pages_filled')}")
    # name the field(s) behind the drift: a value that overflows its cell, or one whose cell grew (wrapped text)
    culprits = list(dict.fromkeys([str(d["label"]) for d in drift if "overflow" in d.get("issue", "") and d.get("label")]
                                  + [str(g["label"]) for g in visual.get("grown_slots") or []]))
    if culprits:
        named = "、".join(f"「{c}」" for c in culprits)
        why = (f" — caused by {named}: the value needs more room than its cell in the blank form; shorten it, or "
               f"write this field by hand in Word or Excel / {named}の値が欄に収まりません（値を短くするか、手作業で記入）")
    else:
        why = " — a value is probably too long for its field"
    return AuditCheckItem(check_id="VIS-01", category="visual", passed=False, severity="major",
                          description="Rendered layout outside the designated fields is unchanged and every value fits its cell",
                          details=" · ".join(parts) + why,
                          evidence={"drift": drift, "fields": culprits})


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
    if getattr(result, "adapt_history", None) and result.scan.spec.get("form_sha256") and (
        result.scan.spec.get("targets") or result.scan.spec.get("slots")
    ):
        asp = out / f"{result.form_path.stem}.adapted.slots.json"
        asp.write_text(json.dumps(result.scan.spec, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8", newline="\n")
        paths["adapted_spec"] = asp
    return paths


# ---------------------------------------------------------- web hand-off (MCP draft -> web sign-off)
#
# form_guard_fill (MCP) writes an unapproved draft. web_handoff.json lets the web app load that exact file for the
# human sign-off: a byte copy of the blank form the draft was verified against, the memo, the effective slot spec
# and the mappings. The web app never re-runs a model on import; it re-verifies the bytes and refuses anything that
# no longer matches the audit log.

HANDOFF_NAME = "web_handoff.json"
HANDOFF_SCHEMA = "form-guard/web-handoff@1"
HANDOFF_BLANK_DIR = "_blank"
_PLAIN_NAME = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f]{1,200}\.(?:docx|xlsx|pdf)$", re.I)


class HandoffError(ValueError):
    """A hand-off draft that cannot be loaded for sign-off; ``status`` is the HTTP status the web app answers."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def write_web_handoff(result: PipelineResult, *, created_via: str, approval_status: str,
                      form_display: str = "", review_url: str = "") -> Path:
    """Write web_handoff.json (and a byte copy of the blank form) next to a draft written by ``run_pipeline``."""
    out = result.out_dir or result.filled_path.parent
    blank_dir = out / HANDOFF_BLANK_DIR
    blank_dir.mkdir(parents=True, exist_ok=True)
    blank_blob = result.form_path.read_bytes()
    (blank_dir / result.form_path.name).write_bytes(blank_blob)
    data = {
        "schema": HANDOFF_SCHEMA,
        "run_id": result.run_id,
        "created_via": created_via,
        "created_at": result.created_at,
        "approval_status": approval_status,
        "human_sign_off_required": True,
        "review_url": review_url,
        "form": {"name": result.form_path.name, "display_name": form_display or result.form_path.name,
                 "blank_copy": f"{HANDOFF_BLANK_DIR}/{result.form_path.name}",
                 "sha256": hashlib.sha256(blank_blob).hexdigest(), "file_type": result.scan.file_type},
        "filled": {"name": result.filled_path.name, "sha256": result.verified_sha256},
        "filled_sha256": result.verified_sha256,
        "memo": {"text": result.memo_text, "sha256": hashlib.sha256(result.memo_text.encode()).hexdigest()},
        "slot_spec": result.scan.spec,  # the effective spec (a sidecar .slots.json included): never re-read on import
        "mappings": [m.model_dump() for m in result.mappings],
        "patches": [p.model_dump() for p in result.write.patches],
        "modified_parts": list(result.write.modified_parts),
        "report": {"overall_status": result.report.overall_status, "passed": result.report.passed_count,
                   "failed": result.report.failed_count, "pending_slots": list(result.report.pending_slots)},
        "engine": result.engine,
        "demo_id": result.demo_id,
    }
    p = out / HANDOFF_NAME
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")
    return p


def _read_json(path: Path, what: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HandoffError(f"{what} is missing — this folder holds no Form-Guard draft for sign-off", 404) from exc
    except (OSError, ValueError) as exc:
        raise HandoffError(f"{what} is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise HandoffError(f"{what} is not a JSON object")
    return data


def load_handoff_draft(run_dir: Union[str, Path], out_dir: Union[str, Path]) -> tuple[PipelineResult, dict[str, Any]]:
    """Load the unapproved draft in ``run_dir`` for the web sign-off, without running any model.

    The blank form copy and the draft must match the SHA-256 values in web_handoff.json *and* audit_log.json; both
    files are copied into ``out_dir`` (the web run folder) and the copy is verified again from scratch — the 21
    deterministic checks plus VIS-01. Returns (result, hand-off record); a tampered or failing draft raises
    ``HandoffError``.
    """
    from .ziputil import ZipSurgeryError, check_archive

    run_dir, out_dir = Path(run_dir), Path(out_dir)
    handoff = _read_json(run_dir / HANDOFF_NAME, HANDOFF_NAME)
    audit = _read_json(run_dir / "audit_log.json", "audit_log.json")
    if handoff.get("schema") != HANDOFF_SCHEMA:
        raise HandoffError(f"{HANDOFF_NAME}: unknown schema {handoff.get('schema')!r}")
    run_id = str(handoff.get("run_id") or "")
    if not run_id or run_id != str(audit.get("run_id") or "") or run_dir.name != run_id:
        raise HandoffError("run id mismatch between the folder, web_handoff.json and audit_log.json", 409)
    form, filled = handoff.get("form") or {}, handoff.get("filled") or {}
    blank_rel, filled_name = str(form.get("blank_copy") or ""), str(filled.get("name") or "")
    blank_name = blank_rel.removeprefix(f"{HANDOFF_BLANK_DIR}/")
    if not (_PLAIN_NAME.match(blank_name) and _PLAIN_NAME.match(filled_name)) or \
            blank_rel != f"{HANDOFF_BLANK_DIR}/{blank_name}":
        raise HandoffError("web_handoff.json names a file outside its run folder")
    blank_src, filled_src = run_dir / HANDOFF_BLANK_DIR / blank_name, run_dir / filled_name
    try:
        blank_blob, filled_blob = blank_src.read_bytes(), filled_src.read_bytes()
    except OSError as exc:
        raise HandoffError(f"the draft or its blank form is missing: {exc.filename and Path(exc.filename).name}", 409) from exc
    blank_sha, filled_sha = hashlib.sha256(blank_blob).hexdigest(), hashlib.sha256(filled_blob).hexdigest()
    if not blank_sha == form.get("sha256") == (audit.get("form") or {}).get("sha256"):
        raise HandoffError("the blank form copy does not match the SHA-256 recorded for this draft — refused", 409)
    recorded = handoff.get("filled_sha256")
    if not filled_sha == recorded == filled.get("sha256") == (audit.get("filled") or {}).get("sha256"):
        raise HandoffError("the draft was modified after it was written (SHA-256 differs from audit_log.json) — "
                           "refused; run form_guard_fill again", 409)
    memo = str((handoff.get("memo") or {}).get("text") or "")
    if memo != str((audit.get("memo") or {}).get("text") or ""):
        raise HandoffError("the memo in web_handoff.json differs from the one in audit_log.json — refused", 409)
    spec = handoff.get("slot_spec") if isinstance(handoff.get("slot_spec"), dict) else {}
    try:
        mappings = [FieldMapping.model_validate(m) for m in handoff.get("mappings") or []]
        patches = [PatchRecord.model_validate(p) for p in handoff.get("patches") or []]
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError: a malformed hand-off
        raise HandoffError(f"web_handoff.json mappings are malformed: {exc}") from exc
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / HANDOFF_BLANK_DIR).mkdir(exist_ok=True)
    form_path, filled_path = out_dir / HANDOFF_BLANK_DIR / blank_name, out_dir / filled_name
    form_path.write_bytes(blank_blob)
    filled_path.write_bytes(filled_blob)
    try:
        for p in (form_path, filled_path):  # the archive gate, or the PDF gate for a fixed PDF form
            if p.read_bytes()[:5] == b"%PDF-":
                from .pdf_core import check_pdf

                check_pdf(p)
            else:
                check_archive(p)
        scan_r = scan(form_path, spec)
    except (ZipSurgeryError, ValueError) as exc:
        raise HandoffError(f"the draft cannot be re-verified: {exc}") from exc
    if filled_path.suffix.lower() != f".{scan_r.file_type}":
        raise HandoffError("the draft and its blank form are not the same file type")
    t = time.perf_counter()
    values = {m.slot_id: m.filled_value for m in mappings if m.is_written}
    prepared = {k: prepare_value(v, scan_r.file_type) for k, v in values.items()}
    try:
        report = verify(scan_r, filled_path, mappings, memo)
        previews, visual = build_previews(scan_r, filled_path, prepared, None, None)
    except Exception as exc:  # noqa: BLE001 - a file the verifier or the layout engine cannot read is refused
        raise HandoffError(f"the draft cannot be re-verified: {type(exc).__name__}: {exc}") from exc
    report.visual_diff_summary = visual
    if not visual.get("identical_outside_slots", True):
        report.checks.append(_drift_check(visual))
        report.failed_count += 1
        report.overall_status = "FAIL"
    if report.stats.get("filled_sha256") != filled_sha:
        raise HandoffError("the draft changed while it was being verified — refused", 409)
    if report.overall_status == "FAIL":
        failed = ", ".join(c.check_id for c in report.checks if not c.passed)
        raise HandoffError(f"the draft fails re-verification against its blank form ({failed}) — refused")
    write = WriteResult(out_path=filled_path, patches=patches, modified_parts=list(handoff.get("modified_parts") or []),
                        written_slots=sorted(values), values=prepared,
                        pending_slots=[s.slot_id for s in scan_r.slots if s.slot_id not in values])
    engine = handoff.get("engine") if isinstance(handoff.get("engine"), dict) else {}
    result = PipelineResult(
        run_id=run_id, form_path=form_path, filled_path=filled_path, memo_text=memo, scan=scan_r, mappings=mappings,
        write=write, report=report, naive_report=None, previews=previews, visual=visual, engine=engine,
        timings={"verify": time.perf_counter() - t}, demo_id=handoff.get("demo_id"), out_dir=out_dir,
        created_at=str(handoff.get("created_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")),
        builtin_demo=is_builtin_demo(handoff.get("demo_id"), form_path, memo, mappings),
    )
    write_run_outputs(result)
    return result, handoff
