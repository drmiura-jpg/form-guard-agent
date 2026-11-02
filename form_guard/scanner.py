"""Read-only discovery of fillable slots in .docx / .xlsx forms.

Slots are designated by markers typed into the blank form — ``{{研究課題番号}}`` or
``[記入欄: 法人番号]`` — or, for official forms that must not be edited at all, by
table coordinates of an *empty* cell in a sidecar ``<form>.slots.json`` file.

The scanner never writes anything. It records where each marker lives (part,
paragraph / table / row / grid column, or sheet cell), the surrounding labels, and
the constraints printed on the form (character limits, formats, option lists).
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
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
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
    if "word/document.xml" in names:
        return "docx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    raise ValueError(f"{path.name}: not a .docx or .xlsx package")


def scan(path: Union[str, Path], spec: Union[dict, str, Path, None] = None) -> ScanResult:
    path = Path(path)
    file_type = detect_file_type(path)
    spec_d = load_spec(path, spec)
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


def _add_coordinate_target(idx: Optional[PartIndex], tgt: dict, accs: dict, warnings: list[str]) -> None:
    slot_id = str(tgt.get("slot_id", "")).strip()
    if idx is None or not slot_id:
        warnings.append(f"coordinate target {tgt!r}: part not found")
        return
    try:
        table = idx.tables[int(tgt["table"])]
        cell = _cell_at(table, int(tgt["row"]), int(tgt["col"]))
    except (KeyError, IndexError, ValueError):
        cell = None
    if cell is None or not cell.containers:
        warnings.append(f"coordinate target {slot_id!r}: cell (table={tgt.get('table')}, row={tgt.get('row')}, "
                        f"col={tgt.get('col')}) not found")
        return
    if _cell_text(idx, cell):
        warnings.append(f"coordinate target {slot_id!r}: cell is not empty — refusing to overwrite printed text")
        return
    c = idx.containers[cell.containers[0]]
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
    result = ScanResult(path, "xlsx", [], parts, "\n".join(doc_texts), spec, warnings, sheets)
    result.slots = [_make_slot(a, "xlsx") for a in accs.values()]
    return result


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
            if "importance" in ov:
                slot.importance = ov["importance"]
            slot.constraints.update(ov.get("constraints", {}))
    order = {s: i for i, s in enumerate(spec_slots)}
    result.slots.sort(key=lambda s: (order.get(s.slot_id, 10_000), _first_pos(s)))


def _first_pos(slot: FormSlot) -> tuple:
    o = slot.occurrences[0]
    return (o.part_name != "word/document.xml", o.sheet_index or 0, o.container_index)
