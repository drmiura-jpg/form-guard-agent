"""Read-only discovery of fillable slots in .docx / .xlsx forms.

Slots are designated by markers typed into the blank form — ``{{研究課題番号}}`` or
``[記入欄: 法人番号]`` — or, for official forms that must not be edited at all, by the
coordinates of an *empty* cell in a sidecar ``<form>.slots.json`` file: a Word table
cell (``table`` / ``row`` / ``col``) or an Excel cell (``sheet`` / ``cell``).

For unmarked blank forms, ``discover_blank_cells`` proposes candidate cells in four
stages (heuristics → classification → deterministic safety checks → human
confirmation) and ``build_confirmed_slot_spec`` turns the cells a human confirmed into
such a ``.slots.json`` spec.

The scanner never writes anything. It records where each marker lives (part,
paragraph / table / row / grid column, or sheet cell), the surrounding labels, and
the constraints printed on the form (character limits, formats, option lists).
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from .constraints import infer_slot_constraints
from .models import FormSlot, SlotOccurrence
from .ooxml import (
    MAX_XLSX_COL,
    MAX_XLSX_ROW,
    CellInfo,
    PartIndex,
    TableInfo,
    check_xlsx_limits,
    docx_story_parts,
    index_part,
    split_cell_ref,
    xlsx_sheet_parts,
)

BRACE_RE = re.compile(r"\{\{\s*([^{}\t\n￼]{1,80}?)\s*\}\}")
KINYU_RE = re.compile(r"[\[［]\s*記入欄\s*[:：]\s*([^\[\]［］\t\n￼]{1,80}?)\s*[\]］]")
MARKER_ANY_RE = re.compile(f"(?:{BRACE_RE.pattern})|(?:{KINYU_RE.pattern})")


@dataclass
class MarkerHit:
    slot_id: str
    kind: str
    start: int
    end: int
    text: str


def find_markers(text: str) -> list[MarkerHit]:
    hits = [MarkerHit(m.group(1).strip(), "braces", m.start(), m.end(), m.group(0)) for m in BRACE_RE.finditer(text)]
    hits += [MarkerHit(m.group(1).strip(), "kinyuran", m.start(), m.end(), m.group(0)) for m in KINYU_RE.finditer(text)]
    hits.sort(key=lambda h: h.start)
    out: list[MarkerHit] = []
    for h in hits:
        if out and h.start < out[-1].end:
            continue
        out.append(h)
    return out


def strip_markers(text: str) -> str:
    return MARKER_ANY_RE.sub("", text)


def has_marker(text: str) -> bool:
    return MARKER_ANY_RE.search(text) is not None


@dataclass
class ScanResult:
    path: Path
    file_type: str
    slots: list[FormSlot]
    parts: dict[str, PartIndex]
    doc_text: str
    spec: dict
    warnings: list[str] = field(default_factory=list)
    sheets: list[tuple[int, str, str]] = field(default_factory=list)

    @property
    def target_parts(self) -> list[str]:
        return sorted({o.part_name for s in self.slots for o in s.occurrences})

    def slot(self, slot_id: str) -> FormSlot:
        for s in self.slots:
            if s.slot_id == slot_id:
                return s
        raise KeyError(slot_id)

    @property
    def title(self) -> str:
        return str(self.spec.get("form_title") or self.path.stem)


def load_spec(form_path: Path, spec: Union[dict, str, Path, None]) -> dict:
    if isinstance(spec, dict):
        return spec
    if spec:
        return json.loads(Path(spec).read_text(encoding="utf-8"))
    side = form_path.with_suffix(".slots.json")
    if side.exists():
        return json.loads(side.read_text(encoding="utf-8"))
    return {}


def detect_file_type(path: Path) -> str:
    """docx | xlsx — after the archive safety gate (path traversal, ZIP bomb, DTD / entity declarations)."""
    from .ziputil import check_archive

    names = {i.filename for i in check_archive(Path(path))}
    if "word/document.xml" in names:
        return "docx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    raise ValueError(f"{path.name}: not a .docx or .xlsx package")


def sha256_file(path: Union[str, Path]) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def scan(path: Union[str, Path], spec: Union[dict, str, Path, None] = None) -> ScanResult:
    path = Path(path)
    file_type = detect_file_type(path)
    spec_d = load_spec(path, spec)
    bound = spec_d.get("form_sha256")
    if bound and bound != sha256_file(path):
        # a spec built from human-confirmed discovery is bound to the exact blank form it was confirmed on
        raise ValueError("blank form changed after discovery — re-run discovery (the slot spec belongs to another "
                         "version of this form)")
    if file_type == "docx":
        result = _scan_docx(path, spec_d)
    else:
        result = _scan_xlsx(path, spec_d)
    _finalise_slots(result)
    return result


# ------------------------------------------------------------------------ docx


class _Acc:
    """Accumulates occurrences + context for one slot id."""

    def __init__(self, slot_id: str):
        self.slot_id = slot_id
        self.occurrences: list[SlotOccurrence] = []
        self.labels: list[str] = []
        self.hints: list[str] = []
        self.context_label = ""


def _cell_text(idx: PartIndex, cell: CellInfo) -> str:
    return "\n".join(idx.container_text(idx.containers[ci]) for ci in cell.containers).strip()


def _cell_at(table: TableInfo, row: int, grid_col: int) -> Optional[CellInfo]:
    for c in table.rows[row]:
        if c.col_idx <= grid_col < c.col_idx + c.grid_span:
            return c
    return None


def _resolve_vmerge(table: TableInfo, cell: CellInfo) -> CellInfo:
    cur = cell
    while cur.vmerge == "continue" and cur.row_idx > 0:
        above = _cell_at(table, cur.row_idx - 1, cur.col_idx)
        if above is None:
            break
        cur = above
    return cur


def _grid_width(table: TableInfo) -> int:
    return max((c.col_idx + c.grid_span for row in table.rows for c in row), default=0)


def _docx_cell_context(idx: PartIndex, cell: CellInfo) -> tuple[list[str], Optional[str], Optional[str], list[str]]:
    table = idx.tables[cell.table_idx]
    row = table.rows[cell.row_idx]
    width = _grid_width(table)
    labels: list[str] = []
    for other in sorted((c for c in row if c.col_idx < cell.col_idx), key=lambda c: -c.col_idx):
        src = _resolve_vmerge(table, other)
        txt = _cell_text(idx, src)
        if has_marker(txt):
            break
        if txt:
            labels.insert(0, txt)
    col_header = section = None
    for r in range(cell.row_idx - 1, -1, -1):
        cells = table.rows[r]
        if len(cells) == 1 and cells[0].grid_span >= width:
            section = _cell_text(idx, cells[0])
            break
        if any(has_marker(_cell_text(idx, c)) for c in cells):
            continue
        above = _cell_at(table, r, cell.col_idx)
        if above is not None:
            txt = _cell_text(idx, _resolve_vmerge(table, above))
            col_header = txt or None
        break
    if section is None:
        for r in range(cell.row_idx - 1, -1, -1):
            cells = table.rows[r]
            if len(cells) == 1 and cells[0].grid_span >= width:
                section = _cell_text(idx, cells[0])
                break
    hints = [strip_markers(idx.container_text(idx.containers[ci])).strip() for ci in cell.containers]
    return labels, col_header, section, [h for h in hints if h]


def _scan_docx(path: Path, spec: dict) -> ScanResult:
    parts: dict[str, PartIndex] = {}
    accs: dict[str, _Acc] = {}
    doc_texts: list[str] = []
    warnings: list[str] = []
    with zipfile.ZipFile(path) as zf:
        for part in docx_story_parts(zf):
            idx = index_part(part, zf.read(part), "word")
            parts[part] = idx
            stem = part.rsplit("/", 1)[-1].removesuffix(".xml")
            for c in idx.containers:
                text = idx.container_text(c)
                doc_texts.append(text)
                for hit in find_markers(text):
                    acc = accs.setdefault(hit.slot_id, _Acc(hit.slot_id))
                    occ = _docx_occurrence(idx, c, stem, hit.text, hit.kind, text)
                    acc.occurrences.append(occ)
                    if c.cell is not None:
                        labels, col_header, section, hints = _docx_cell_context(idx, c.cell)
                        acc.labels += labels + ([col_header] if col_header else [])
                        acc.hints += hints + ([section] if section else [])
                        if not acc.context_label:
                            acc.context_label = " › ".join(labels + ([col_header] if col_header else []))
                    else:
                        before = text[: hit.start]
                        label = re.split(r"[\t　]", before.strip())[-1].strip(" ：:　") if before.strip() else ""
                        if label:
                            acc.labels.append(label)
                            acc.context_label = acc.context_label or label
                        acc.hints.append(strip_markers(text).strip())
        for tgt in spec.get("targets", []) or []:
            _add_coordinate_target(parts.get(tgt.get("part", "word/document.xml")), tgt, accs, warnings)
    result = ScanResult(path, "docx", [], parts, "\n".join(doc_texts), spec, warnings)
    result.slots = [_make_slot(a, "docx") for a in accs.values()]
    return result


def _docx_occurrence(idx: PartIndex, c, stem: str, marker: str, kind: str, text: str) -> SlotOccurrence:
    prefix = "" if stem == "document" else f"{stem}:"
    if c.cell is not None:
        cell = c.cell
        k = cell.containers.index(c.idx) if c.idx in cell.containers else 0
        path = (f"{idx.part_name} › tbl[{cell.table_idx}] › tr[{cell.row_idx}] › tc[grid {cell.col_idx}"
                f"{f'+{cell.grid_span}' if cell.grid_span > 1 else ''}] › p[{k}]")
        box = f"{prefix}t{cell.table_idx}.r{cell.row_idx}.c{cell.col_idx}"
        return SlotOccurrence(
            part_name=idx.part_name, container_index=c.idx, location_path=path, marker=marker, marker_kind=kind,
            paragraph_index=c.para_index, table_idx=cell.table_idx, row_idx=cell.row_idx, col_idx=cell.col_idx,
            box_id=box, context_text=text,
        )
    return SlotOccurrence(
        part_name=idx.part_name, container_index=c.idx, location_path=f"{idx.part_name} › p[{c.para_index}]",
        marker=marker, marker_kind=kind, paragraph_index=c.para_index, box_id=f"{prefix}p{c.para_index}",
        context_text=text,
    )


_UNSAFE_PARAGRAPH_TOKENS = (b"fldSimple", b"fldChar", b"instrText", b":sdt", b"hyperlink", b"drawing", b":pict",
                            b":object")


def _add_coordinate_target(idx: Optional[PartIndex], tgt: dict, accs: dict, warnings: list[str]) -> None:
    slot_id = str(tgt.get("slot_id", "")).strip()
    if "cell" in tgt and "table" not in tgt:
        warnings.append(f"coordinate target {slot_id!r}: sheet/cell coordinates are for .xlsx — use table/row/col "
                        f"for a .docx form")
        return
    if idx is None or not slot_id:
        warnings.append(f"coordinate target {tgt!r}: part not found")
        return
    try:
        table = idx.tables[int(tgt["table"])]
        cell = _cell_at(table, int(tgt["row"]), int(tgt["col"]))
    except (KeyError, IndexError, ValueError, TypeError):
        cell = None
    if cell is None or not cell.containers:
        warnings.append(f"coordinate target {slot_id!r}: cell (table={tgt.get('table')}, row={tgt.get('row')}, "
                        f"col={tgt.get('col')}) not found")
        return
    if _cell_text(idx, cell):
        warnings.append(f"coordinate target {slot_id!r}: cell is not empty — refusing to overwrite printed text")
        return
    if cell.vmerge == "continue":
        warnings.append(f"coordinate target {slot_id!r}: cell is a vertically merged continuation — designate the "
                        f"first cell of the merge")
        return
    c = idx.containers[cell.containers[0]]
    if any(tok in idx.data[c.span[0] : c.span[1]] for tok in _UNSAFE_PARAGRAPH_TOKENS):
        warnings.append(f"coordinate target {slot_id!r}: the cell holds a field, content control or object — "
                        f"refusing to write into it")
        return
    acc = accs.setdefault(slot_id, _Acc(slot_id))
    occ = _docx_occurrence(idx, c, "document", "", "empty_cell", "")
    occ.location_path += " (empty cell designated by coordinates)"
    acc.occurrences.append(occ)
    labels, col_header, section, hints = _docx_cell_context(idx, cell)
    acc.labels += labels + ([col_header] if col_header else []) + ([tgt["label"]] if tgt.get("label") else [])
    acc.hints += hints + ([section] if section else [])
    acc.context_label = acc.context_label or " › ".join(labels + ([col_header] if col_header else []))


# ------------------------------------------------------------------------ xlsx


def _scan_xlsx(path: Path, spec: dict) -> ScanResult:
    import openpyxl
    from openpyxl.utils import get_column_letter
    from openpyxl.utils.cell import coordinate_from_string, column_index_from_string

    parts: dict[str, PartIndex] = {}
    accs: dict[str, _Acc] = {}
    warnings: list[str] = []
    with zipfile.ZipFile(path) as zf:
        sheets = xlsx_sheet_parts(zf)
        check_xlsx_limits(zf, sheets)  # before any library expands a range: XFD1048576 / whole-column merges
        names = set(zf.namelist())
        sst_refs: dict[int, list[tuple[int, str, str, str]]] = {}
        if "xl/sharedStrings.xml" in names:
            sst_refs = _shared_string_refs(zf, sheets)
            parts["xl/sharedStrings.xml"] = index_part("xl/sharedStrings.xml", zf.read("xl/sharedStrings.xml"), "sst")
        for _, _, part in sheets:
            parts[part] = index_part(part, zf.read(part), "sheet")

    wb = openpyxl.load_workbook(path, data_only=False)
    doc_texts: list[str] = []
    sheet_ctx = {}
    for sidx, sname, _ in sheets:
        ws = wb[sname]
        merged = {}
        for rng in ws.merged_cells.ranges:
            for r in range(rng.min_row, rng.max_row + 1):
                for col in range(rng.min_col, rng.max_col + 1):
                    merged[(r, col)] = (rng.min_row, rng.min_col)
        # Data validations are looked up per slot cell instead of being expanded cell by cell, so a
        # whole-column rule (D1:D1048576) costs nothing. Ranges are clamped to the form limits.
        rules: list[tuple[str, object, list[tuple[int, int, int, int]]]] = []
        for dv in ws.data_validations.dataValidation:
            boxes = [(rng.min_row, rng.min_col, min(rng.max_row, MAX_XLSX_ROW), min(rng.max_col, MAX_XLSX_COL))
                     for rng in dv.sqref.ranges]
            if dv.type == "list" and dv.formula1 and dv.formula1.startswith('"'):
                opts = [o.strip() for o in dv.formula1.strip('"').split(",") if o.strip()]
                rules.append(("list", opts, boxes))
            elif dv.type == "textLength" and dv.formula1 and dv.operator in ("lessThanOrEqual", "between"):
                lim = dv.formula2 if dv.operator == "between" else dv.formula1
                try:
                    rules.append(("len", int(lim), boxes))
                except (TypeError, ValueError):
                    pass
        # sparse: only cells present in the file, in reading order (iter_rows() walks the used rectangle)
        for cell in sorted(ws._cells.values(), key=lambda c: (c.row, c.column)):
            if isinstance(cell.value, str):
                doc_texts.append(cell.value)
        sheet_ctx[sname] = (ws, merged, rules)

    def cell_value(ws, merged, r, col) -> str:
        tr, tc = merged.get((r, col), (r, col))
        c = ws._cells.get((tr, tc))  # read-only lookup: ws.cell() would create empty cells while scanning
        return "" if c is None or c.value is None else str(c.value)

    def rule_for(rules, kind, rc):
        for k, val, boxes in reversed(rules):  # the last matching rule wins, as when they were expanded
            if k == kind and any(r0 <= rc[0] <= r1 and c0 <= rc[1] <= c1 for r0, c0, r1, c1 in boxes):
                return val
        return None

    def context(sidx, sname, ref):
        ws, merged, rules = sheet_ctx[sname]
        letters, r = coordinate_from_string(ref)
        col = column_index_from_string(letters)
        top = merged.get((r, col), (r, col))
        labels: list[str] = []
        c = top[1] - 1
        while c >= 1:
            if merged.get((r, c), (r, c))[1] == top[1]:
                c -= 1
                continue
            txt = cell_value(ws, merged, r, c).strip()
            if has_marker(txt):
                break
            if txt and txt not in labels:
                labels.insert(0, txt)
            c = merged.get((r, c), (r, c))[1] - 1
        header = None
        for rr in range(top[0] - 1, 0, -1):
            row_texts = [cell_value(ws, merged, rr, cc) for cc in range(1, ws.max_column + 1)]
            if any(has_marker(t) for t in row_texts):
                continue
            txt = cell_value(ws, merged, rr, top[1]).strip()
            header = txt or None
            break
        box = f"s{sidx}!{get_column_letter(top[1])}{top[0]}"
        return labels, header, box, rule_for(rules, "list", top), rule_for(rules, "len", top)

    def add(part_name, container_index, sidx, sname, ref, hit, text, via):
        acc = accs.setdefault(hit.slot_id, _Acc(hit.slot_id))
        labels, header, box, opts, mlen = context(sidx, sname, ref)
        acc.occurrences.append(SlotOccurrence(
            part_name=part_name, container_index=container_index,
            location_path=f"{part_name} › sheet '{sname}' › {ref}{via}", marker=hit.text, marker_kind=hit.kind,
            cell_ref=ref, sheet_name=sname, sheet_index=sidx, box_id=box, context_text=text,
        ))
        acc.labels += labels + ([header] if header else [])
        acc.hints.append(strip_markers(text).strip())
        acc.context_label = acc.context_label or " › ".join(labels + ([header] if header else []))
        if opts:
            acc.__dict__.setdefault("allowed", opts)
        if mlen:
            acc.__dict__.setdefault("maxlen", mlen)

    for sidx, sname, part in sheets:
        idx = parts[part]
        for c in idx.containers:
            text = idx.container_text(c)
            for hit in find_markers(text):
                if c.cell_ref:
                    add(part, c.idx, sidx, sname, c.cell_ref, hit, text, " (inline string)")
    if "xl/sharedStrings.xml" in parts:
        idx = parts["xl/sharedStrings.xml"]
        for c in idx.containers:
            text = idx.container_text(c)
            hits = find_markers(text)
            if not hits:
                continue
            refs = sst_refs.get(c.si_index or 0, [])
            if not refs:
                warnings.append(f"shared string #{c.si_index} contains a marker but no cell uses it")
            for hit in hits:
                for sidx, sname, _part, ref in refs:
                    add("xl/sharedStrings.xml", c.idx, sidx, sname, ref, hit, text, f" (sharedStrings si[{c.si_index}])")

    claimed: dict[tuple[str, str], str] = {}

    def add_target(tgt: dict) -> None:
        """An empty worksheet cell designated by coordinates: {"slot_id", "sheet": 0 | "name", "cell": "C5"}."""
        slot_id = str(tgt.get("slot_id", "")).strip()
        if "table" in tgt and "cell" not in tgt:
            warnings.append(f"coordinate target {slot_id!r}: table/row/col coordinates are for .docx — use sheet + "
                            f"cell for an .xlsx form")
            return
        if not slot_id or not tgt.get("cell"):
            warnings.append(f"coordinate target {tgt!r}: slot_id and cell are required")
            return
        sheet = _resolve_sheet(sheets, tgt.get("sheet", 0))
        if sheet is None:
            warnings.append(f"coordinate target {slot_id!r}: sheet {tgt.get('sheet')!r} not found")
            return
        sidx, sname, part = sheet
        ref = str(tgt["cell"]).replace("$", "").strip().upper()
        rc = split_cell_ref(ref)
        if rc is None or rc[0] > MAX_XLSX_ROW or rc[1] > MAX_XLSX_COL:
            warnings.append(f"coordinate target {slot_id!r}: {tgt['cell']!r} is not a cell reference inside the "
                            f"form limits ({MAX_XLSX_ROW} rows × {MAX_XLSX_COL} columns)")
            return
        r, col = rc
        idx = parts[part]
        if idx.implicit_refs:
            warnings.append(f"coordinate target {slot_id!r}: sheet '{sname}' has rows/cells without an r attribute — "
                            f"cells cannot be addressed safely")
            return
        ws, merged, _rules = sheet_ctx[sname]
        top = merged.get((r, col), (r, col))
        if top != (r, col):
            warnings.append(f"coordinate target {slot_id!r}: {ref} is a merged continuation — designate the top-left "
                            f"cell {get_column_letter(top[1])}{top[0]} of the merged range")
            return
        el = idx.cells.get(ref)
        oc = ws._cells.get((r, col))  # read-only lookup
        if (el is not None and "f" in el.children) or (oc is not None and oc.data_type == "f"):
            warnings.append(f"coordinate target {slot_id!r}: {ref} contains a formula — formulas are never fill targets")
            return
        if (el is not None and el.children) or (oc is not None and oc.value not in (None, "")):
            warnings.append(f"coordinate target {slot_id!r}: {ref} is not empty — refusing to overwrite printed text")
            return
        if (part, ref) in claimed:
            warnings.append(f"coordinate target {slot_id!r}: {ref} is already designated for {claimed[(part, ref)]!r}")
            return
        claimed[(part, ref)] = slot_id
        acc = accs.setdefault(slot_id, _Acc(slot_id))
        labels, header, box, opts, mlen = context(sidx, sname, ref)
        acc.occurrences.append(SlotOccurrence(
            part_name=part, container_index=-1, marker="", marker_kind="empty_cell", cell_ref=ref, sheet_name=sname,
            sheet_index=sidx, box_id=box, context_text="",
            location_path=f"{part} › sheet '{sname}' › {ref} (empty cell designated by coordinates)",
        ))
        acc.labels += labels + ([header] if header else []) + ([tgt["label"]] if tgt.get("label") else [])
        acc.context_label = acc.context_label or " › ".join(labels + ([header] if header else []))
        if opts:
            acc.__dict__.setdefault("allowed", opts)
        if mlen:
            acc.__dict__.setdefault("maxlen", mlen)

    for tgt in spec.get("targets", []) or []:
        add_target(tgt)
    result = ScanResult(path, "xlsx", [], parts, "\n".join(doc_texts), spec, warnings, sheets)
    result.slots = [_make_slot(a, "xlsx") for a in accs.values()]
    return result


def _resolve_sheet(sheets: list[tuple[int, str, str]], key: Any) -> Optional[tuple[int, str, str]]:
    """A sheet given by workbook index (0, "0") or by name."""
    for s in sheets:
        if isinstance(key, str) and s[1] == key:
            return s
    try:
        i = int(key)
    except (TypeError, ValueError):
        return None
    return next((s for s in sheets if s[0] == i), None)


def _shared_string_refs(zf: zipfile.ZipFile, sheets) -> dict[int, list[tuple[int, str, str, str]]]:
    from lxml import etree

    from .ooxml import S_NS

    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    refs: dict[int, list[tuple[int, str, str, str]]] = {}
    for sidx, sname, part in sheets:
        root = etree.fromstring(zf.read(part), parser)
        for c in root.iter(f"{{{S_NS}}}c"):
            if c.get("t") == "s":
                v = c.find(f"{{{S_NS}}}v")
                if v is not None and v.text and v.text.strip().isdigit():
                    refs.setdefault(int(v.text), []).append((sidx, sname, part, c.get("r", "")))
    return refs


# ---------------------------------------------------------------------- shared


def _make_slot(acc: _Acc, file_type: str) -> FormSlot:
    first = acc.occurrences[0]
    return FormSlot(
        slot_id=acc.slot_id,
        label=acc.slot_id.replace("_", " "),
        location_path=first.location_path,
        file_type=file_type,  # type: ignore[arg-type]
        current_text=first.marker,
        context_label=acc.context_label,
        hints=_dedupe([h for h in acc.hints if h]),
        occurrences=acc.occurrences,
        constraints={"_labels": _dedupe([x for x in acc.labels if x]),
                     "_allowed": acc.__dict__.get("allowed"), "_maxlen": acc.__dict__.get("maxlen")},
    )


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _finalise_slots(result: ScanResult) -> None:
    spec_slots: dict[str, Any] = result.spec.get("slots", {}) or {}
    for slot in result.slots:
        labels = slot.constraints.pop("_labels", [])
        allowed = slot.constraints.pop("_allowed", None)
        maxlen = slot.constraints.pop("_maxlen", None)
        inferred = infer_slot_constraints(slot.slot_id, labels, slot.hints, result.doc_text, allowed, maxlen)
        slot.required_format = inferred["required_format"]
        slot.max_chars = inferred["max_chars"]
        slot.constraints = inferred["constraints"]
        slot.importance = inferred["importance"]
        ov = spec_slots.get(slot.slot_id, {})
        if ov:
            slot.aliases = list(ov.get("aliases", []))
            slot.description = ov.get("description", "")
            if "label" in ov:
                slot.label = ov["label"]
            if "max_chars" in ov:
                slot.max_chars = ov["max_chars"]
            if "required_format" in ov:
                slot.required_format = ov["required_format"]
                slot.importance = "normal" if ov["required_format"] == "text" else "high"
            if "importance" in ov:
                slot.importance = ov["importance"]
            if "required_format" in ov and "constraints" in ov:
                slot.constraints = dict(ov["constraints"])  # a complete rule set (e.g. a confirmed discovery spec)
            else:
                slot.constraints.update(ov.get("constraints", {}))
    order = {s: i for i, s in enumerate(spec_slots)}
    result.slots.sort(key=lambda s: (order.get(s.slot_id, 10_000), _first_pos(s)))


def _first_pos(slot: FormSlot) -> tuple:
    o = slot.occurrences[0]
    return (o.part_name != "word/document.xml", o.sheet_index or 0, o.container_index)


# ======================================================== unmarked blank-cell discovery
#
# Stage 1  heuristic candidates: empty cells with a printed label immediately to the left
#          (or immediately above, in a column-header table)
# Stage 2  classification: applicant field vs office-only field (deterministic rules, plus
#          Claude in live mode — Claude can only make a cell *less* fillable), unique slot ids,
#          printed rules (format, character limit, options)
# Stage 3  deterministic safety: genuinely empty, no formula / field, not a merged continuation
# Stage 4  human gate: nothing is filled until a reviewer confirms cells and
#          build_confirmed_slot_spec() re-checks the blank form's SHA-256

OFFICE_KEYWORDS = ("事務局", "受付印", "受付番号", "受付日", "受付欄", "審査欄", "決裁", "検印", "処理欄",
                   "記入不要", "記入しないで", "記入しないこと")
OFFICE_REASON = "office/administrative use only — excluded from applicant fill"
APPLICANT_REASON = "applicant field — empty cell next to a printed label"
MAX_LABEL_CHARS = 40
_BRACKETS_RE = re.compile(r"[（(][^（）()]*[）)]")
_SECTION_NO_RE = re.compile(r"^\s*(?:[0-9０-９]+\s*[．.、)）]?|[（(][0-9０-９]+[）)]|[一二三四五六七八九十]+\s*[．.、])\s*")
_SLOT_ID_JUNK = re.compile(r"[\s　{}\[\]［］<>\"'※*＊:：]")


@dataclass
class _Cand:
    file_type: str
    order: tuple
    label_text: str
    base: str
    hints: list[str]
    group: str = ""
    section: str = ""
    direction: str = "left"
    location: str = ""
    coords: dict = field(default_factory=dict)
    rejected: str = ""
    merged_range: str = ""
    allowed: Optional[list[str]] = None
    maxlen: Optional[int] = None


def _label_parts(text: str) -> tuple[str, list[str]]:
    """'受付番号（事務局記入欄）\\n（半角）' -> ('受付番号', ['（事務局記入欄）', '（半角）'])."""
    lines = [ln.strip() for ln in text.replace("\r", "\n").split("\n") if ln.strip()]
    if not lines:
        return "", []
    first = lines[0]
    base = _BRACKETS_RE.sub("", first).strip(" 　:：※*＊")
    return (base or first.strip(" 　:：")), _BRACKETS_RE.findall(first) + lines[1:]


def _is_sentence(text: str) -> bool:
    first = text.strip().split("\n")[0].strip()
    return not first or len(first) > MAX_LABEL_CHARS or first.endswith("。") or has_marker(text)


def _section_name(text: str) -> str:
    first = text.strip().split("\n")[0] if text.strip() else ""
    return _BRACKETS_RE.sub("", _SECTION_NO_RE.sub("", first)).strip(" 　:：※")


def _office_hits(*texts: str) -> list[str]:
    blob = " ".join(t for t in texts if t)
    return [k for k in OFFICE_KEYWORDS if k in blob]


def _package_title(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            core = zf.read("docProps/core.xml").decode("utf-8", "replace")
        m = re.search(r"<dc:title>([^<]*)</dc:title>", core)
        if m and m.group(1).strip():
            return html.unescape(m.group(1).strip())
    except (KeyError, zipfile.BadZipFile):
        pass
    return path.stem


def _docx_blank_candidates(path: Path) -> tuple[list[_Cand], str]:
    with zipfile.ZipFile(path) as zf:
        idx = index_part("word/document.xml", zf.read("word/document.xml"), "word")
    doc_text = "\n".join(idx.container_text(c) for c in idx.containers)

    def text_of(t: TableInfo, cell: CellInfo) -> str:
        return _cell_text(idx, _resolve_vmerge(t, cell))

    def is_banner(t: TableInfo, r: int) -> bool:
        cells = t.rows[r]
        return len(cells) == 1 and cells[0].grid_span >= _grid_width(t)

    def label_for(t: TableInfo, cell: CellInfo) -> tuple[Optional[CellInfo], str]:
        left = _cell_at(t, cell.row_idx, cell.col_idx - 1) if cell.col_idx > 0 else None
        if left is not None and text_of(t, left):
            return left, "left"
        r = cell.row_idx - 1
        while r >= 0 and not is_banner(t, r):  # column-header table: walk up through empty cells
            above = _cell_at(t, r, cell.col_idx)
            if above is None:
                break
            if text_of(t, above):
                return above, "above"
            r -= 1
        return None, ""

    def section_of(t: TableInfo, row: int) -> str:
        for r in range(row - 1, -1, -1):
            if is_banner(t, r):
                return _cell_text(idx, t.rows[r][0])
        return ""

    cands: list[_Cand] = []
    for t in idx.tables:
        for ri, row in enumerate(t.rows):
            if is_banner(t, ri):
                continue
            for cell in row:
                if not cell.containers or _cell_text(idx, cell):
                    continue
                origin = _resolve_vmerge(t, cell)
                if origin is not cell and _cell_text(idx, origin):
                    continue  # the lower part of a vertically merged printed label
                label_cell, direction = label_for(t, cell)
                if label_cell is None:
                    continue
                label_text = text_of(t, label_cell)
                if _is_sentence(label_text):
                    continue
                base, hints = _label_parts(label_text)
                group = ""
                if direction == "left" and label_cell.col_idx > 0:
                    g = _cell_at(t, label_cell.row_idx, label_cell.col_idx - 1)
                    gt = text_of(t, g) if g is not None else ""
                    if gt and not _is_sentence(gt):
                        group = _label_parts(gt)[0]
                rejected = ""
                if cell.vmerge == "continue":
                    rejected = "vertically merged continuation (vMerge) — only the first cell of the merge can be filled"
                else:
                    c0 = idx.containers[cell.containers[0]]
                    if any(tok in idx.data[c0.span[0] : c0.span[1]] for tok in _UNSAFE_PARAGRAPH_TOKENS):
                        rejected = "the cell holds a field, content control or object — never a fill target"
                span = f"+{cell.grid_span}" if cell.grid_span > 1 else ""
                cands.append(_Cand(
                    "docx", (0, t.table_idx, ri, cell.col_idx), label_text, base, hints, group, section_of(t, ri),
                    direction, f"word/document.xml › tbl[{t.table_idx}] › tr[{ri}] › tc[grid {cell.col_idx}{span}]",
                    {"part": "word/document.xml", "table": t.table_idx, "row": ri, "col": cell.col_idx}, rejected))
    return cands, doc_text


def _xlsx_blank_candidates(path: Path) -> tuple[list[_Cand], str]:
    import openpyxl
    from openpyxl.utils import get_column_letter

    with zipfile.ZipFile(path) as zf:
        sheets = xlsx_sheet_parts(zf)
        check_xlsx_limits(zf, sheets)
        raw = {part: index_part(part, zf.read(part), "sheet") for _, _, part in sheets if part in zf.namelist()}
    wb = openpyxl.load_workbook(path, data_only=False)
    cands: list[_Cand] = []
    texts: list[str] = []
    for sidx, sname, part in sheets:
        if part not in raw:
            continue
        ws, sheet_idx = wb[sname], raw[part]
        boxes: dict[tuple[int, int], tuple[int, int, int, int]] = {}
        for rng in ws.merged_cells.ranges:
            b = (rng.min_row, rng.min_col, rng.max_row, rng.max_col)
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    boxes[(r, c)] = b
        max_row, max_col = min(ws.max_row, MAX_XLSX_ROW), min(ws.max_column, MAX_XLSX_COL)
        rules: list[tuple[str, object, list[tuple[int, int, int, int]]]] = []
        for dv in ws.data_validations.dataValidation:
            bx = [(g.min_row, g.min_col, g.max_row, g.max_col) for g in dv.sqref.ranges]
            if dv.type == "list" and dv.formula1 and dv.formula1.startswith('"'):
                rules.append(("list", [o.strip() for o in dv.formula1.strip('"').split(",") if o.strip()], bx))
            elif dv.type == "textLength" and dv.formula1 and dv.operator in ("lessThanOrEqual", "between"):
                try:
                    rules.append(("len", int(dv.formula2 if dv.operator == "between" else dv.formula1), bx))
                except (TypeError, ValueError):
                    pass

        def box(r: int, c: int) -> tuple[int, int, int, int]:
            return boxes.get((r, c), (r, c, r, c))

        def origin_cell(r: int, c: int):
            b = box(r, c)
            return ws._cells.get((b[0], b[1]))  # read-only lookup: ws.cell() would create cells

        def literal(r: int, c: int) -> str:
            x = origin_cell(r, c)
            if x is None or x.value is None or x.data_type == "f":
                return ""
            return str(x.value).strip()

        def is_formula(r: int, c: int) -> bool:
            x = origin_cell(r, c)
            return x is not None and x.data_type == "f"

        def is_label(r: int, c: int) -> bool:
            x = origin_cell(r, c)
            return (x is not None and x.data_type != "f" and isinstance(x.value, str) and bool(x.value.strip())
                    and not _is_sentence(x.value))

        def is_banner(r: int) -> bool:
            b = box(r, 1)
            return literal(r, 1) != "" and b[3] - b[1] + 1 >= max(2, int(max_col * 0.75))

        def section_of(r: int) -> str:
            for rr in range(r - 1, 0, -1):
                if is_banner(rr) and not _is_sentence(literal(rr, 1)):
                    return literal(rr, 1)
            return ""

        def rule_for(kind: str, r: int, c: int):
            for k, val, bx in reversed(rules):
                if k == kind and any(r0 <= r <= r1 and c0 <= c <= c1 for r0, c0, r1, c1 in bx):
                    return val
            return None

        seen: set[tuple[int, int]] = set()

        def consider(r: int, c: int, label_rc: tuple[int, int], direction: str) -> None:
            if (r, c) in seen or literal(r, c):
                return
            seen.add((r, c))
            b = box(r, c)
            ref = f"{get_column_letter(c)}{r}"
            label_text = str(origin_cell(*label_rc).value)
            base, hints = _label_parts(label_text)
            group = ""
            lb = box(*label_rc)
            if direction == "left" and lb[1] > 1 and is_label(lb[0], lb[1] - 1):
                group = _label_parts(literal(lb[0], lb[1] - 1))[0]
            rejected = ""
            top_ref = f"{get_column_letter(b[1])}{b[0]}"
            rng = f"{top_ref}:{get_column_letter(b[3])}{b[2]}" if b != (r, c, r, c) else ""
            if (b[0], b[1]) != (r, c):
                rejected = f"merged continuation of {rng} — only the top-left cell {top_ref} can hold a value"
            elif is_formula(r, c):
                rejected = f"formula cell ({origin_cell(r, c).value}) — formulas are never fill targets"
            elif sheet_idx.implicit_refs:
                rejected = "the worksheet has rows/cells without an r attribute — cells cannot be addressed safely"
            else:
                el = sheet_idx.cells.get(ref)
                if el is not None and el.children:
                    rejected = "the cell already holds content"
            cands.append(_Cand(
                "xlsx", (sidx, 0, r, c), label_text, base, hints, group, section_of(r), direction,
                f"{part} › sheet '{sname}' › {ref}", {"sheet": sidx, "sheet_name": sname, "cell": ref}, rejected,
                merged_range=rng, allowed=rule_for("list", r, c), maxlen=rule_for("len", r, c)))

        cells = sorted(ws._cells.values(), key=lambda x: (x.row, x.column))
        for x in cells:  # (a) the cell right of a printed label (each row of a multi-row label)
            b = box(x.row, x.column)
            if (b[0], b[1]) != (x.row, x.column) or x.row > max_row or not is_label(x.row, x.column):
                continue
            for rr in range(b[0], b[2] + 1):
                if b[3] + 1 <= max_col:
                    consider(rr, b[3] + 1, (x.row, x.column), "left")
        for x in cells:  # (b) an empty table cell below a column header (no label on its left)
            r, c = x.row, x.column
            if (r, c) in seen or x.value is not None or r > max_row or c > max_col or literal(r, c):
                continue
            if box(r, c)[:2] != (r, c):
                continue
            if c > 1 and (literal(r, c - 1) or is_formula(r, c - 1)):
                continue
            rr = r - 1
            while rr >= 1 and not is_banner(rr):
                if is_label(rr, c):
                    consider(r, c, box(rr, c)[:2], "above")
                    break
                if literal(rr, c) or is_formula(rr, c) or origin_cell(rr, c) is None:
                    break
                rr -= 1
        texts += [str(x.value) for x in cells if isinstance(x.value, str)]
    return cands, "\n".join(texts)


def _assign_slot_ids(cands: list[_Cand]) -> list[str]:
    """Unique slot ids: a duplicated label gets its group / section as prefix (研究代表者_氏名 vs 事務担当者_氏名),
    then a running number if it is still ambiguous."""
    def clean(s: str) -> str:
        return _SLOT_ID_JUNK.sub("", s)

    bases = [clean(c.base) or "記入欄" for c in cands]
    counts = Counter(bases)
    ids = []
    for c, b in zip(cands, bases):
        prefix = clean(c.group or _section_name(c.section)) if counts[b] > 1 else ""
        ids.append(f"{prefix}_{b}" if prefix and prefix != b else b)
    totals, running, out = Counter(ids), Counter(), []
    for sid in ids:
        if totals[sid] > 1:
            running[sid] += 1
            out.append(f"{sid}_{running[sid]}")
        else:
            out.append(sid)
    return out


def discover_blank_cells(path: Union[str, Path], mode: str = "demo", client: Any = None,
                         model_tier: Optional[str] = "auto") -> dict[str, Any]:
    """Propose fillable empty cells of an *unmarked* blank form (no {{markers}}, no .slots.json).

    Returns a discovery report for the human pre-fill confirmation gate; nothing is written.
    ``mode`` = demo (deterministic rules) | live (Claude classifies the labels too) | auto.
    ``model_tier`` = auto (Haiku) | opus | sonnet | haiku; None = ``FORM_GUARD_MODEL_TIER`` or auto.
    """
    path = Path(path)
    file_type = detect_file_type(path)
    form_sha = sha256_file(path)
    found, doc_text = _docx_blank_candidates(path) if file_type == "docx" else _xlsx_blank_candidates(path)
    found.sort(key=lambda c: c.order)
    kept = [c for c in found if not c.rejected]
    slot_ids = _assign_slot_ids(kept)
    candidates: list[dict[str, Any]] = []
    base_counts = Counter(c.base for c in kept)
    for i, (c, sid) in enumerate(zip(kept, slot_ids), start=1):
        hits = _office_hits(c.label_text, c.section, c.group, *c.hints)
        labels = [c.label_text] + ([c.group] if c.group else [])
        inferred = infer_slot_constraints(sid, labels, c.hints, doc_text, c.allowed, c.maxlen)
        aliases = ([c.base] if base_counts[c.base] == 1 else []) + ([c.group + c.base, c.group] if c.group else [])
        cand: dict[str, Any] = {
            "candidate_id": f"c{i:02d}", "slot_id": sid, "label": sid.replace("_", " "), "file_type": file_type,
            "evidence_label": c.label_text, "group_label": c.group, "section": c.section,
            "label_direction": c.direction, "location": c.location, **c.coords,
            "merged_range": c.merged_range, "office_only": bool(hits), "applicant_fillable": not hits,
            "recommended": not hits, "reason": OFFICE_REASON if hits else APPLICANT_REASON,
            "office_keywords": hits, "required_format": inferred["required_format"],
            "max_chars": inferred["max_chars"], "constraints": inferred["constraints"],
            "aliases": [a for a in dict.fromkeys(aliases) if a and a != sid],
            "classified_by": "rules",
            "safety": {"empty": True, "formula": False, "merged_continuation": False},
        }
        candidates.append(cand)
    engine = _classify_with_claude(candidates, _package_title(path), mode, client, model_tier)
    rejected = [{"location": c.location, "evidence_label": c.label_text, "reason": c.rejected, **c.coords,
                 "merged_range": c.merged_range} for c in found if c.rejected]
    n_office = sum(1 for c in candidates if c["office_only"])
    return {
        "schema": "form-guard/blank-cell-discovery@1",
        "form": path.name,
        "form_title": _package_title(path),
        "file_type": file_type,
        "form_sha256": form_sha,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engine": engine,
        "stages": [
            {"stage": 1, "name": "heuristic candidate extraction", "found": len(found)},
            {"stage": 2, "name": "semantic classification & rule inference",
             "applicant_fillable": len(candidates) - n_office, "office_only": n_office, "classified_by": engine["label"]},
            {"stage": 3, "name": "deterministic safety verification", "passed": len(candidates),
             "rejected": len(rejected)},
            {"stage": 4, "name": "human pre-fill confirmation", "status": "awaiting human confirmation"},
        ],
        "candidates": candidates,
        "rejected": rejected,
        "requires_human_confirmation": True,
        "next_step": ("A human reviewer confirms the cells to fill; build_confirmed_slot_spec() (CLI: --discovery FILE "
                      "--confirm c01,c02 | recommended) re-checks the blank form's SHA-256 and writes the slot spec."),
    }


def _classify_with_claude(candidates: list[dict[str, Any]], title: str, mode: str, client: Any,
                          model_tier: Optional[str]) -> dict[str, Any]:
    """Stage 2 in live mode: Claude classifies each printed label. The rules always win towards safety —
    Claude can mark a cell office-only or not-a-field, never turn an office cell into an applicant field."""
    rules = {"mode": "demo", "label": "deterministic rules (offline)", "model": None, "fallback_reason": None}
    requested = (mode or "demo").lower()
    if not candidates or requested == "demo":
        return rules
    from .mapper import ClaudeEngine, LiveEngineError, resolve_mode

    resolved = resolve_mode(requested) if client is None else "live"
    if resolved != "live":
        return rules
    engine = ClaudeEngine(client=client, model_tier=model_tier)
    brief = [{"candidate_id": c["candidate_id"], "printed_label": c["evidence_label"], "group_label": c["group_label"],
              "section": c["section"], "label_position": c["label_direction"]} for c in candidates]
    try:
        verdicts = engine.classify_blank_cells(brief, title)
    except LiveEngineError as exc:
        if requested == "live":
            raise
        return {**rules, "label": rules["label"] + " — live classification failed", "fallback_reason": str(exc)}
    model = engine.model_for("classify")
    for c in candidates:
        v = verdicts.get(c["candidate_id"]) or {}
        role, why = v.get("role"), str(v.get("reason", ""))[:200]
        c["classified_by"] = f"rules + {model}"
        c["claude_role"] = role
        if role == "office_only" and not c["office_only"]:
            c.update(office_only=True, applicant_fillable=False, recommended=False,
                     reason=f"{OFFICE_REASON} (Claude: {why})")
        elif role == "not_a_field" and not c["office_only"]:
            c.update(applicant_fillable=False, recommended=False, reason=f"not an input field (Claude: {why})")
    return {"mode": "live", "label": f"rules + Claude ({model})", "model": model, "fallback_reason": None,
            "usage": engine.usage}


def recommended_ids(discovery: dict[str, Any]) -> list[str]:
    """The candidates discovery recommends (applicant fields, never office-only) — what ``recommended`` confirms."""
    return [c["candidate_id"] for c in discovery.get("candidates", []) if c.get("recommended")]


def build_confirmed_slot_spec(discovery: dict[str, Any], confirmed_ids: list[str], current_form_path: Union[str, Path],
                              allow_office_only: bool = False) -> dict[str, Any]:
    """Stage 4: turn the cells a human confirmed into a .slots.json spec for run_pipeline(slot_spec=...)."""
    if sha256_file(current_form_path) != discovery.get("form_sha256"):
        raise ValueError("blank form changed after discovery — re-run discovery")
    ids = list(dict.fromkeys(str(i).strip() for i in (confirmed_ids or []) if str(i).strip()))
    if not ids:
        raise ValueError("no confirmed fillable slots — at least 1 slot must be confirmed by the human reviewer")
    by_id = {c["candidate_id"]: c for c in discovery.get("candidates", [])}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise ValueError(f"unknown candidate id(s): {', '.join(unknown)} — rejected cells can never be confirmed")
    office = [i for i in ids if by_id[i].get("office_only")]
    if office and not allow_office_only:
        raise ValueError(f"{', '.join(office)}: {OFFICE_REASON} — confirming it needs an explicit reviewer override")
    order = {c["candidate_id"]: n for n, c in enumerate(discovery.get("candidates", []))}
    targets: list[dict[str, Any]] = []
    slots: dict[str, Any] = {}
    for cid in sorted(ids, key=lambda i: order[i]):
        c = by_id[cid]
        if c["file_type"] == "docx":
            targets.append({"slot_id": c["slot_id"], "part": c.get("part", "word/document.xml"), "table": c["table"],
                            "row": c["row"], "col": c["col"], "label": c["label"]})
        else:
            targets.append({"slot_id": c["slot_id"], "sheet": c["sheet"], "cell": c["cell"], "label": c["label"]})
        slots[c["slot_id"]] = {
            "label": c["label"], "aliases": list(c.get("aliases", [])),
            "description": " › ".join(x for x in (_section_name(c.get("section", "")), c.get("group_label", ""),
                                                  c["evidence_label"].split("\n")[0]) if x),
            "required_format": c["required_format"], "max_chars": c["max_chars"],
            "constraints": dict(c.get("constraints") or {}),
        }
    return {
        "form_title": discovery.get("form_title", ""),
        "form_sha256": discovery["form_sha256"],
        "source": "blank-cell discovery confirmed by a human reviewer",
        "confirmed_candidates": sorted(ids, key=lambda i: order[i]),
        "office_only_override": bool(office),
        "targets": targets,
        "slots": slots,
    }
