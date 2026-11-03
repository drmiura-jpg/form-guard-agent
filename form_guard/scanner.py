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

import copy
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

from .constraints import infer_slot_constraints, infer_template_constraints
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


# 「令和　　年　　月　　日」: an era date printed with blanks for the applicant to complete (an optional （　） for the
# weekday). A cell that holds nothing else is a fill target: the whole skeleton is replaced by the 和暦 date.
_TEMPLATE_BLANK = "[ 　_＿]+"
ERA_DATE_TEMPLATE_RE = re.compile(rf"(?:令和|平成|昭和){_TEMPLATE_BLANK}年{_TEMPLATE_BLANK}月{_TEMPLATE_BLANK}日"
                                  r"(?:[ 　]*[（(][ 　_＿]*[）)])?")
TEMPLATE_REASON = "applicant field — printed era-date template next to a printed label; the template is replaced by the date"


def era_date_template(text: str) -> Optional[tuple[int, int]]:
    """``(start, end)`` of an unfilled era-date skeleton such as 「令和　　年　　月　　日」 or 「令和   年   月   日（　）」
    when it is all the text holds (blanks around it allowed, no digit filled in yet), else None."""
    s = (text or "").strip()
    if not s or not ERA_DATE_TEMPLATE_RE.fullmatch(s):
        return None
    start = len(text) - len(text.lstrip())
    return start, start + len(s)


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
    pdf: Any = None  # fixed PDF forms: the parsed form (pdf_support.PDFForm) the slots were found on

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


def _spec_object(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):  # a JSON string is data, never a second path to open
        kind = {str: "string", list: "array", bool: "boolean", int: "number", float: "number"}.get(type(data), "null")
        raise ValueError(f"slot spec JSON must be an object ({path.name} holds a JSON {kind})")
    return data


def load_spec(form_path: Path, spec: Union[dict, str, Path, None]) -> dict:
    """The slot spec as a dict: given directly, read from a .json path, or the form's sidecar <form>.slots.json."""
    if isinstance(spec, dict):
        return spec
    if spec:
        return _spec_object(Path(spec))
    side = form_path.with_suffix(".slots.json")
    if side.exists():
        return _spec_object(side)
    return {}


def is_pdf(path: Union[str, Path]) -> bool:
    with open(path, "rb") as fh:
        return fh.read(5) == b"%PDF-"


def detect_file_type(path: Path) -> str:
    """docx | xlsx | pdf — after the archive safety gate (path traversal, ZIP bomb, DTD / entity declarations) or,
    for a PDF, the PDF gate (encryption, active content, compressed cross-references, decompression bombs)."""
    from .ziputil import check_archive

    if is_pdf(path):
        from .pdf_core import PDFError, check_pdf

        try:
            check_pdf(Path(path))
        except PDFError as exc:
            raise ValueError(f"{Path(path).name}: {exc}") from exc
        return "pdf"
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
    elif file_type == "pdf":
        from .pdf_support import scan_pdf

        result = scan_pdf(path, spec_d)
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


def _template_container(idx: PartIndex, cell: CellInfo) -> Optional[int]:
    """The paragraph of a Word cell that holds an unfilled era-date skeleton (「令和　　年　　月　　日」) when no other
    paragraph of the cell has text, else None."""
    texts = [(ci, idx.container_text(idx.containers[ci])) for ci in cell.containers]
    filled = [(ci, t) for ci, t in texts if t.strip()]
    if len(filled) == 1 and era_date_template(filled[0][1]):
        return filled[0][0]
    return None


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


def docx_target_coords(tgt: dict) -> Optional[tuple[int, int, int]]:
    """(table, row, col) of a .docx coordinate target as non-negative integers, or None.

    A slot spec may come from anyone: a negative index would silently address the last table / row / cell in Python
    (``tables[-1]``), so negative, boolean, fractional and non-numeric coordinates are refused, never wrapped around.
    """
    out: list[int] = []
    for key in ("table", "row", "col"):
        v = tgt.get(key)
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            n = v
        elif isinstance(v, str) and v.strip().isdigit():
            n = int(v.strip())
        else:
            return None
        if n < 0:
            return None
        out.append(n)
    return out[0], out[1], out[2]


def _add_coordinate_target(idx: Optional[PartIndex], tgt: dict, accs: dict, warnings: list[str]) -> None:
    slot_id = str(tgt.get("slot_id", "")).strip()
    if "cell" in tgt and "table" not in tgt:
        warnings.append(f"coordinate target {slot_id!r}: sheet/cell coordinates are for .xlsx — use table/row/col "
                        f"for a .docx form")
        return
    if idx is None or not slot_id:
        warnings.append(f"coordinate target {tgt!r}: part not found")
        return
    coords = docx_target_coords(tgt)
    try:
        table = idx.tables[coords[0]] if coords else None
        cell = _cell_at(table, coords[1], coords[2]) if table is not None and coords else None
    except IndexError:
        cell = None
    if cell is None or not cell.containers:
        warnings.append(f"coordinate target {slot_id!r}: cell (table={tgt.get('table')}, row={tgt.get('row')}, "
                        f"col={tgt.get('col')}) not found")
        return
    if is_checkbox_target(tgt):
        _add_docx_checkbox(idx, cell, slot_id, tgt, accs, warnings)
        return
    template = _template_container(idx, cell) if _cell_text(idx, cell) else None
    if _cell_text(idx, cell) and template is None:
        warnings.append(f"coordinate target {slot_id!r}: cell is not empty — refusing to overwrite printed text")
        return
    if cell.vmerge == "continue":
        warnings.append(f"coordinate target {slot_id!r}: cell is a vertically merged continuation — designate the "
                        f"first cell of the merge")
        return
    c = idx.containers[template if template is not None else cell.containers[0]]
    if any(tok in idx.data[c.span[0] : c.span[1]] for tok in _UNSAFE_PARAGRAPH_TOKENS):
        warnings.append(f"coordinate target {slot_id!r}: the cell holds a field, content control or object — "
                        f"refusing to write into it")
        return
    acc = accs.setdefault(slot_id, _Acc(slot_id))
    if template is not None:  # 「令和　　年　　月　　日」: only the skeleton's characters are replaced, like a marker
        text = idx.container_text(c)
        span = era_date_template(text)
        occ = _docx_occurrence(idx, c, "document", text[span[0] : span[1]], "date_template", text)  # type: ignore[index]
        occ.location_path += " (printed era-date template designated by coordinates)"
    else:
        occ = _docx_occurrence(idx, c, "document", "", "empty_cell", "")
        occ.location_path += " (empty cell designated by coordinates)"
    acc.occurrences.append(occ)
    labels, col_header, section, hints = _docx_cell_context(idx, cell)
    acc.labels += labels + ([col_header] if col_header else []) + ([tgt["label"]] if tgt.get("label") else [])
    acc.hints += hints + ([section] if section else [])
    acc.context_label = acc.context_label or " › ".join(labels + ([col_header] if col_header else []))


def is_checkbox_target(tgt: dict) -> bool:
    return str(tgt.get("kind") or tgt.get("type") or "").lower() in ("checkbox_text", "checkbox")


def _add_docx_checkbox(idx: PartIndex, cell: CellInfo, slot_id: str, tgt: dict, accs: dict, warnings: list[str]) -> None:
    """A Word cell printing "□ 選択肢　□ 選択肢": the slot ticks one option by turning its □ into ■ in place."""
    from .constraints import parse_checkbox_options

    hits = [(ci, parse_checkbox_options(idx.container_text(idx.containers[ci]))) for ci in cell.containers]
    hits = [(ci, opts) for ci, opts in hits if opts]
    if len(hits) != 1:
        warnings.append(f"coordinate target {slot_id!r}: the cell prints {'no' if not hits else 'several lines of'} "
                        f"□ options — a checkbox needs exactly one line of options")
        return
    ci, options = hits[0]
    if any(o["checked"] for o in options):
        warnings.append(f"coordinate target {slot_id!r}: an option is already printed as ■ — refusing to change it")
        return
    if cell.vmerge == "continue":
        warnings.append(f"coordinate target {slot_id!r}: cell is a vertically merged continuation — designate the "
                        f"first cell of the merge")
        return
    c = idx.containers[ci]
    if any(tok in idx.data[c.span[0] : c.span[1]] for tok in _UNSAFE_PARAGRAPH_TOKENS):
        warnings.append(f"coordinate target {slot_id!r}: the cell holds a field, content control or object — "
                        f"refusing to write into it")
        return
    text = idx.container_text(c)
    acc = accs.setdefault(slot_id, _Acc(slot_id))
    occ = _docx_occurrence(idx, c, "document", text, "checkbox_text", text)
    occ.location_path += " (printed □ options designated by coordinates)"
    occ.checkbox_options = options
    acc.occurrences.append(occ)
    labels, col_header, section, _hints = _docx_cell_context(idx, cell)
    acc.labels += labels + ([col_header] if col_header else []) + ([tgt["label"]] if tgt.get("label") else [])
    acc.hints += [section] if section else []
    acc.context_label = acc.context_label or " › ".join(labels + ([col_header] if col_header else []))
    acc.__dict__["allowed"] = [o["label"] for o in options]


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
        if is_checkbox_target(tgt):
            add_checkbox(tgt, slot_id, sidx, sname, part, ref, el)
            return
        template = xlsx_template_cell(el, oc)
        if not template and ((el is not None and el.children) or (oc is not None and oc.value not in (None, ""))):
            warnings.append(f"coordinate target {slot_id!r}: {ref} is not empty — refusing to overwrite printed text")
            return
        if (part, ref) in claimed:
            warnings.append(f"coordinate target {slot_id!r}: {ref} is already designated for {claimed[(part, ref)]!r}")
            return
        claimed[(part, ref)] = slot_id
        acc = accs.setdefault(slot_id, _Acc(slot_id))
        labels, header, box, opts, mlen = context(sidx, sname, ref)
        acc.occurrences.append(SlotOccurrence(
            part_name=part, container_index=-1, marker=template, marker_kind="date_template" if template else "empty_cell",
            cell_ref=ref, sheet_name=sname, sheet_index=sidx, box_id=box, context_text=template,
            location_path=f"{part} › sheet '{sname}' › {ref} "
                          + ("(printed era-date template designated by coordinates — the cell is rewritten as the date)"
                             if template else "(empty cell designated by coordinates)"),
        ))
        acc.labels += labels + ([header] if header else []) + ([tgt["label"]] if tgt.get("label") else [])
        acc.context_label = acc.context_label or " › ".join(labels + ([header] if header else []))
        if opts:
            acc.__dict__.setdefault("allowed", opts)
        if mlen:
            acc.__dict__.setdefault("maxlen", mlen)

    def add_checkbox(tgt: dict, slot_id: str, sidx: int, sname: str, part: str, ref: str, el) -> None:
        """A worksheet cell printing "□ 選択肢　□ 選択肢" (inline string, or a shared string no other cell uses)."""
        from .constraints import parse_checkbox_options

        if el is None:
            warnings.append(f"coordinate target {slot_id!r}: {ref} is empty — a checkbox cell prints its □ options")
            return
        cont_part, ci = None, None
        if el.attrs.get("t") == "inlineStr" and el.is_container is not None:
            cont_part, ci = part, el.is_container
        elif el.attrs.get("t") == "s" and "xl/sharedStrings.xml" in parts:
            si = next((k for k, refs in sst_refs.items() for (s_i, _n, _p, rr) in refs if s_i == sidx and rr == ref), None)
            if si is None:
                warnings.append(f"coordinate target {slot_id!r}: {ref} shared string not found")
                return
            if len(sst_refs.get(si, [])) != 1:
                warnings.append(f"coordinate target {slot_id!r}: {ref} shares its text with {len(sst_refs[si]) - 1} other "
                                f"cell(s) — ticking it would change them too; refused")
                return
            cont_part = "xl/sharedStrings.xml"
            ci = next((c.idx for c in parts[cont_part].containers if c.si_index == si), None)
        if cont_part is None or ci is None:
            warnings.append(f"coordinate target {slot_id!r}: {ref} is not a text cell — a checkbox cell prints its □ "
                            f"options as text")
            return
        cidx = parts[cont_part]
        text = cidx.container_text(cidx.containers[ci])
        options = parse_checkbox_options(text)
        if not options:
            warnings.append(f"coordinate target {slot_id!r}: {ref} prints no □ options — not a checkbox")
            return
        if any(o["checked"] for o in options):
            warnings.append(f"coordinate target {slot_id!r}: an option in {ref} is already printed as ■ — refusing to "
                            f"change it")
            return
        if cidx.containers[ci].runs or len(cidx.container_nodes(cidx.containers[ci])) != 1:
            warnings.append(f"coordinate target {slot_id!r}: {ref} holds rich text — a checkbox needs one plain text node")
            return
        if (part, ref) in claimed:
            warnings.append(f"coordinate target {slot_id!r}: {ref} is already designated for {claimed[(part, ref)]!r}")
            return
        claimed[(part, ref)] = slot_id
        acc = accs.setdefault(slot_id, _Acc(slot_id))
        labels, header, box, _opts, _mlen = context(sidx, sname, ref)
        acc.occurrences.append(SlotOccurrence(
            part_name=cont_part, container_index=ci, marker=text, marker_kind="checkbox_text", cell_ref=ref,
            sheet_name=sname, sheet_index=sidx, box_id=box, context_text=text, checkbox_options=options,
            location_path=f"{part} › sheet '{sname}' › {ref} (printed □ options designated by coordinates)",
        ))
        acc.labels += labels + ([header] if header else []) + ([tgt["label"]] if tgt.get("label") else [])
        acc.context_label = acc.context_label or " › ".join(labels + ([header] if header else []))
        acc.__dict__["allowed"] = [o["label"] for o in options]

    for tgt in spec.get("targets", []) or []:
        add_target(tgt)
    result = ScanResult(path, "xlsx", [], parts, "\n".join(doc_texts), spec, warnings, sheets)
    result.slots = [_make_slot(a, "xlsx") for a in accs.values()]
    return result


def xlsx_template_cell(el: Any, oc: Any) -> str:
    """The printed text of a worksheet cell that holds only an era-date skeleton (「令和　　年　　月　　日」) as a shared or
    inline string — no formula, no rich content — else "". Such a cell is written as one inline string (the date)."""
    if el is None or oc is None or not isinstance(oc.value, str) or not era_date_template(oc.value):
        return ""
    if el.children not in (["v"], ["is"]) or el.attrs.get("t") not in ("s", "inlineStr"):
        return ""
    return oc.value


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
                     "_allowed": acc.__dict__.get("allowed"), "_maxlen": acc.__dict__.get("maxlen"),
                     "_template": any(o.marker_kind == "date_template" for o in acc.occurrences)},
    )


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _finalise_slots(result: ScanResult) -> None:
    from .constraints import checkbox_constraints, finish_checkbox_constraints

    spec_slots: dict[str, Any] = result.spec.get("slots", {}) or {}
    for slot in result.slots:
        labels = slot.constraints.pop("_labels", [])
        allowed = slot.constraints.pop("_allowed", None)
        maxlen = slot.constraints.pop("_maxlen", None)
        template = slot.constraints.pop("_template", False)
        inferred = infer_slot_constraints(slot.slot_id, labels, slot.hints, result.doc_text, allowed, maxlen)
        if template:  # the printed 「令和　　年　　月　　日」 says what goes in: a 和暦 date
            inferred = infer_template_constraints(slot.slot_id, result.doc_text, inferred)
        slot.required_format = inferred["required_format"]
        slot.max_chars = inferred["max_chars"]
        slot.constraints = inferred["constraints"]
        slot.importance = inferred["importance"]
        box = next((o for o in slot.occurrences if o.marker_kind == "checkbox_text"), None)
        if box is not None:  # printed □ options: a choice among exactly what the form prints
            slot.required_format, slot.importance, slot.max_chars = "choice", "high", None
            slot.constraints = checkbox_constraints(box.checkbox_options)
            slot.current_text = box.context_text
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
        if box is not None:  # whatever the spec says, the options are the printed ones and the field stays a choice
            slot.required_format, slot.max_chars = "choice", None
            slot.constraints = finish_checkbox_constraints(slot.constraints, box.checkbox_options)
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
    template: str = ""  # the printed era-date skeleton the cell holds (「令和　　年　　月　　日」), "" for an empty cell
    options: list[dict] = field(default_factory=list)  # printed □ options: a checkbox candidate


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
        if left is not None and text_of(t, left) and not era_date_template(text_of(t, left)) and not options_of(left):
            return left, "left"
        r = cell.row_idx - 1
        while r >= 0 and not is_banner(t, r):  # column-header table: walk up through empty cells
            above = _cell_at(t, r, cell.col_idx)
            if above is None:
                break
            if text_of(t, above):  # a printed 「令和　年　月　日」 is a field to fill, never another cell's label
                return (None, "") if era_date_template(text_of(t, above)) else (above, "above")
            r -= 1
        return None, ""

    def section_of(t: TableInfo, row: int) -> str:
        for r in range(row - 1, -1, -1):
            if is_banner(t, r):
                return _cell_text(idx, t.rows[r][0])
        return ""

    from .constraints import parse_checkbox_options

    def options_of(cell: CellInfo) -> list[dict]:
        hits = [parse_checkbox_options(idx.container_text(idx.containers[ci])) for ci in cell.containers]
        hits = [h for h in hits if h]
        return hits[0] if len(hits) == 1 and not any(o["checked"] for o in hits[0]) else []

    cands: list[_Cand] = []
    for t in idx.tables:
        for ri, row in enumerate(t.rows):
            if is_banner(t, ri):
                continue
            for cell in row:
                options: list[dict] = []
                if not cell.containers:
                    continue
                template_ci = _template_container(idx, cell) if _cell_text(idx, cell) else None
                if _cell_text(idx, cell) and template_ci is None:
                    options = options_of(cell) if cell.vmerge != "continue" else []
                    if not options:
                        continue  # printed text, not a field (a printed 「令和　　年　　月　　日」 or □ line is one)
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
                    c0 = idx.containers[template_ci if template_ci is not None else cell.containers[0]]
                    if any(tok in idx.data[c0.span[0] : c0.span[1]] for tok in _UNSAFE_PARAGRAPH_TOKENS):
                        rejected = "the cell holds a field, content control or object — never a fill target"
                span = f"+{cell.grid_span}" if cell.grid_span > 1 else ""
                cands.append(_Cand(
                    "docx", (0, t.table_idx, ri, cell.col_idx), label_text, base, hints, group, section_of(t, ri),
                    direction, f"word/document.xml › tbl[{t.table_idx}] › tr[{ri}] › tc[grid {cell.col_idx}{span}]",
                    {"part": "word/document.xml", "table": t.table_idx, "row": ri, "col": cell.col_idx}, rejected,
                    template=_cell_text(idx, cell) if template_ci is not None else "", options=options))
    return cands, doc_text


def _xlsx_blank_candidates(path: Path) -> tuple[list[_Cand], str]:
    import openpyxl
    from openpyxl.utils import get_column_letter

    from .constraints import parse_checkbox_options

    with zipfile.ZipFile(path) as zf:
        sheets = xlsx_sheet_parts(zf)
        check_xlsx_limits(zf, sheets)
        raw = {part: index_part(part, zf.read(part), "sheet") for _, _, part in sheets if part in zf.namelist()}
        sst_users = {(s, ref): len(refs) for refs in (_shared_string_refs(zf, sheets).values()
                                                      if "xl/sharedStrings.xml" in zf.namelist() else [])
                     for s, _n, _p, ref in refs}
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
                    and not _is_sentence(x.value) and not era_date_template(x.value)
                    and not parse_checkbox_options(x.value))

        def template_of(r: int, c: int) -> str:
            b = box(r, c)
            return xlsx_template_cell(sheet_idx.cells.get(f"{get_column_letter(b[1])}{b[0]}"), origin_cell(r, c))

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
            template = template_of(r, c)
            lit = "" if template else literal(r, c)
            options = parse_checkbox_options(lit) if lit and (box(r, c)[:2] == (r, c)) else []
            if (r, c) in seen or (lit and (not options or any(o["checked"] for o in options))):
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
            elif options:
                if sst_users.get((sidx, ref), 1) > 1:
                    rejected = "the □ options are a shared string other cells use too — ticking one would tick them all"
            else:
                el = sheet_idx.cells.get(ref)
                if el is not None and el.children and not template:
                    rejected = "the cell already holds content"
            cands.append(_Cand(
                "xlsx", (sidx, 0, r, c), label_text, base, hints, group, section_of(r), direction,
                f"{part} › sheet '{sname}' › {ref}", {"sheet": sidx, "sheet_name": sname, "cell": ref}, rejected,
                merged_range=rng, allowed=rule_for("list", r, c), maxlen=rule_for("len", r, c), template=template,
                options=options))

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
            blank = x.value is None or bool(template_of(r, c))  # empty, or a printed 「令和　年　月　日」 to complete
            if (r, c) in seen or not blank or r > max_row or c > max_col or (literal(r, c) and not template_of(r, c)):
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
                         model_tier: Optional[str] = "auto",
                         expected_sha256: Optional[str] = None) -> dict[str, Any]:
    """Propose fillable empty cells of an *unmarked* blank form (no {{markers}}, no .slots.json).

    Returns a discovery report for the human pre-fill confirmation gate; nothing is written.
    ``mode`` = demo (deterministic rules) | live (Claude classifies the labels too) | auto.
    ``model_tier`` = auto (Haiku) | opus | sonnet | haiku; None = ``FORM_GUARD_MODEL_TIER`` or auto.
    """
    path = Path(path)
    file_type = detect_file_type(path)
    if file_type == "pdf":  # fixed PDF: boxes rebuilt from the ruled lines (pdf_support), same report format
        from .pdf_support import discover_pdf

        return discover_pdf(path, mode=mode, client=client, model_tier=model_tier, expected_sha256=expected_sha256)
    from .constraints import checkbox_constraints

    form_sha = sha256_file(path)
    if expected_sha256 and expected_sha256.strip().lower() != form_sha.lower():
        raise ValueError("blank form SHA-256 does not match expected_sha256 — the file changed before discovery")
    found, doc_text = _docx_blank_candidates(path) if file_type == "docx" else _xlsx_blank_candidates(path)
    if sha256_file(path) != form_sha:
        raise ValueError("blank form changed during discovery — re-run discovery on a stable file")
    found.sort(key=lambda c: c.order)
    kept = [c for c in found if not c.rejected]
    slot_ids = _assign_slot_ids(kept)
    candidates: list[dict[str, Any]] = []
    base_counts = Counter(c.base for c in kept)
    for i, (c, sid) in enumerate(zip(kept, slot_ids), start=1):
        hits = _office_hits(c.label_text, c.section, c.group, *c.hints)
        labels = [c.label_text] + ([c.group] if c.group else [])
        inferred = infer_slot_constraints(sid, labels, c.hints, doc_text, c.allowed, c.maxlen)
        if c.template:  # 「令和　　年　　月　　日」 printed in the cell: a 和暦 date, completed by replacing the skeleton
            inferred = infer_template_constraints(sid, doc_text, inferred)
        if c.options:  # printed □ options: tick one by turning its □ into ■
            inferred.update(required_format="choice", importance="high", max_chars=None,
                            constraints=checkbox_constraints(c.options))
        aliases = ([c.base] if base_counts[c.base] == 1 else []) + ([c.group + c.base, c.group] if c.group else [])
        cand: dict[str, Any] = {
            "candidate_id": f"c{i:02d}", "slot_id": sid, "label": sid.replace("_", " "), "file_type": file_type,
            "evidence_label": c.label_text, "group_label": c.group, "section": c.section,
            "label_direction": c.direction, "location": c.location, **c.coords,
            "merged_range": c.merged_range, "office_only": bool(hits), "applicant_fillable": not hits,
            "recommended": not hits,
            "reason": OFFICE_REASON if hits else (TEMPLATE_REASON if c.template else (
                "applicant field — printed checkbox options next to a printed label" if c.options else APPLICANT_REASON)),
            "category": "office_only" if hits else "applicant_fillable", "classified": True,
            "office_keywords": hits, "required_format": inferred["required_format"],
            "max_chars": inferred["max_chars"], "constraints": inferred["constraints"],
            "aliases": [a for a in dict.fromkeys(aliases) if a and a != sid],
            "classified_by": "rules",
            "safety": {"empty": not (c.template or c.options), "formula": False, "merged_continuation": False},
        }
        if c.template:
            cand["printed_template"] = c.template
            cand["safety"]["printed_template"] = True
        if c.options:
            cand["kind"] = "checkbox_text"
            cand["options"] = [{k: o[k] for k in ("label", "detail", "needs_input")} for o in c.options]
        candidates.append(cand)
    engine = _classify_with_claude(candidates, _package_title(path), mode, client, model_tier)
    # a live classification that failed after the API answered: its calls and tokens stay on record
    live_usage = engine.pop("live_usage", None)
    rejected = [{"location": c.location, "evidence_label": c.label_text, "reason": c.rejected, **c.coords,
                 "merged_range": c.merged_range} for c in found if c.rejected]
    n_office = sum(1 for c in candidates if c["office_only"])
    discovery = {
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
    if live_usage is not None:  # stage 2 fell back to the rules: the API calls made before the failure stay on record
        discovery["live_usage"] = live_usage
    return discovery


UNCLASSIFIED_REASON = "not classified by the model (missing or duplicated in its answer) — never recommended"


def _classify_with_claude(candidates: list[dict[str, Any]], title: str, mode: str, client: Any,
                          model_tier: Optional[str]) -> dict[str, Any]:
    """Stage 2 in live mode: Claude classifies each printed label. The rules always win towards safety —
    Claude can mark a cell office-only or not-a-field, never turn an office cell into an applicant field.

    The answer must classify every candidate exactly once: a candidate the model left out, or listed more than once,
    is marked ``classified: False`` and is neither applicant-fillable nor recommended."""
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
        entries = engine.classify_blank_cells(brief, title)
    except LiveEngineError as exc:
        if requested == "live":
            raise
        return {**rules, "label": rules["label"] + " — live classification failed", "fallback_reason": str(exc),
                "live_usage": copy.deepcopy(getattr(engine, "usage", {}))}  # any call the API answered is kept
    calls = engine.usage.get("calls") or []
    # the id the API served (a 404 fallback or a server-side fallback can differ from the requested tier model)
    model = (calls[-1].get("served_model") or calls[-1].get("model") if calls else None) or engine.model_for("classify")
    known = [c["candidate_id"] for c in candidates]
    counts = Counter(str(e.get("candidate_id")) for e in entries)
    verdicts = {str(e.get("candidate_id")): e for e in entries if counts[str(e.get("candidate_id"))] == 1}
    issues = {"missing": [i for i in known if counts[i] == 0], "duplicated": [i for i in known if counts[i] > 1],
              "unknown": sorted(i for i in counts if i not in set(known))}
    for c in candidates:
        c["classified_by"] = f"rules + {model}"
        v = verdicts.get(c["candidate_id"])
        if v is None:  # left out or answered twice: never leave it recommended
            c.update(classified=False, claude_role=None, applicant_fillable=False, recommended=False,
                     category="office_only" if c["office_only"] else "unclassified",
                     reason=OFFICE_REASON if c["office_only"] else UNCLASSIFIED_REASON)
            continue
        role, why = v.get("role"), str(v.get("reason", ""))[:200]
        c["claude_role"] = role
        if role == "office_only" and not c["office_only"]:
            c.update(office_only=True, applicant_fillable=False, recommended=False, category="office_only",
                     reason=f"{OFFICE_REASON} (Claude: {why})")
        elif role == "not_a_field" and not c["office_only"]:
            c.update(applicant_fillable=False, recommended=False, category="not_a_field",
                     reason=f"not an input field (Claude: {why})")
        elif role not in ("applicant_fillable", "office_only", "not_a_field"):
            c.update(classified=False, applicant_fillable=False, recommended=False,
                     category="office_only" if c["office_only"] else "unclassified",
                     reason=OFFICE_REASON if c["office_only"] else UNCLASSIFIED_REASON)
    return {"mode": "live", "label": f"rules + Claude ({model})", "model": model, "fallback_reason": None,
            "usage": engine.usage, "classification_issues": {k: v for k, v in issues.items() if v}}


def recommended_ids(discovery: dict[str, Any]) -> list[str]:
    """The candidates discovery recommends (applicant fields, never office-only) — what ``recommended`` confirms."""
    return [c["candidate_id"] for c in discovery.get("candidates", []) if c.get("recommended")]


def _cand_key(c: dict[str, Any]) -> Optional[tuple]:
    """Canonical coordinates of a discovery candidate: (part, table, row, grid col) / (sheet index, sheet name, cell)."""
    try:
        if c.get("file_type") == "docx":
            return ("docx", str(c.get("part") or "word/document.xml"), int(c["table"]), int(c["row"]), int(c["col"]))
        if c.get("file_type") == "xlsx":
            return ("xlsx", int(c["sheet"]), str(c.get("sheet_name") or ""),
                    str(c["cell"]).replace("$", "").strip().upper())
        if c.get("file_type") == "pdf":
            rect = [round(float(v), 1) for v in c["rect"]]
            return ("pdf", int(c.get("page", 0)), *rect) if len(rect) == 4 else None
    except (KeyError, TypeError, ValueError):
        pass
    return None


def build_confirmed_slot_spec(discovery: dict[str, Any], confirmed_ids: list[str],
                              current_form_path: Union[str, Path, None] = None,
                              allow_office_only: bool = False) -> dict[str, Any]:
    """Stage 4: turn the cells a human confirmed into a .slots.json spec for run_pipeline(slot_spec=...).

    The discovery report may come from a client (MCP, a saved JSON file), so nothing in it is trusted beyond the
    candidate ids. The blank form must be byte-identical to the one discovery looked at (SHA-256), and a fresh
    deterministic discovery of that very file decides: every confirmed candidate's coordinates must be a blank-cell
    candidate of the form, an office-only cell (fresh rules or the report) needs ``allow_office_only`` (an internal
    CLI override — MCP and the web app never pass it), and a ``not_a_field`` / non-applicant / unclassified cell is
    never fillable. Targets, slot ids and printed rules are built from the fresh candidates, so tampered coordinates
    or constraints in the report have no effect.
    """
    if current_form_path is None:
        current_form_path = discovery.get("form_path")
        if not current_form_path or not Path(str(current_form_path)).is_file():
            raise ValueError("current_form_path is required — the blank form the discovery was made on")
    if sha256_file(current_form_path) != discovery.get("form_sha256"):
        raise ValueError("blank form changed after discovery — re-run discovery")
    ids = list(dict.fromkeys(str(i).strip() for i in (confirmed_ids or []) if str(i).strip()))
    if not ids:
        raise ValueError("no confirmed fillable slots — at least 1 slot must be confirmed by the human reviewer")
    by_id: dict[str, dict[str, Any]] = {}
    for c in discovery.get("candidates", []) or []:
        if isinstance(c, dict) and str(c.get("candidate_id")) not in by_id:
            by_id[str(c.get("candidate_id"))] = c
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise ValueError(f"unknown candidate id(s): {', '.join(unknown)} — rejected cells can never be confirmed")
    fresh = discover_blank_cells(current_form_path, mode="demo")  # deterministic rules on the actual file
    canon = {_cand_key(f): f for f in fresh["candidates"]}
    order = {f["candidate_id"]: n for n, f in enumerate(fresh["candidates"])}
    chosen: dict[str, dict[str, Any]] = {}
    office: list[str] = []
    for cid in ids:
        client = by_id[cid]
        f = canon.get(_cand_key(client))
        if f is None:
            raise ValueError(f"{cid}: its coordinates are not a blank-cell candidate of the current form — re-run "
                             f"discovery (the report was edited or belongs to another form)")
        is_office = bool(f.get("office_only")) or bool(client.get("office_only")) or \
            "office_only" in (f.get("category"), client.get("category"))
        if is_office:
            if not allow_office_only:
                raise ValueError(f"{cid}: {OFFICE_REASON} — confirming it needs an explicit reviewer override")
            office.append(f["candidate_id"])
        if "not_a_field" in (client.get("category"), f.get("category"), client.get("claude_role")):
            raise ValueError(f"{cid}: not an input field (not_a_field) — never fillable, not even with an override")
        if not is_office and (client.get("applicant_fillable") is False or f.get("applicant_fillable") is False
                              or client.get("classified") is False):
            raise ValueError(f"{cid}: not classified as an applicant field — never fillable")
        chosen[f["candidate_id"]] = f
    targets: list[dict[str, Any]] = []
    slots: dict[str, Any] = {}
    for fid in sorted(chosen, key=lambda i: order[i]):
        c = chosen[fid]  # canonical coordinates, slot id and printed rules from the fresh discovery
        if c["file_type"] == "docx":
            targets.append({"slot_id": c["slot_id"], "part": c.get("part", "word/document.xml"), "table": c["table"],
                            "row": c["row"], "col": c["col"], "label": c["label"]})
        elif c["file_type"] == "pdf":
            targets.append({"slot_id": c["slot_id"], "page": c["page"], "rect": list(c["rect"]), "label": c["label"]})
        else:
            targets.append({"slot_id": c["slot_id"], "sheet": c["sheet"], "cell": c["cell"], "label": c["label"]})
        if c.get("kind") == "checkbox_text":
            targets[-1]["kind"] = "checkbox_text"
        slots[c["slot_id"]] = {
            "label": c["label"], "aliases": list(c.get("aliases", [])),
            "description": " › ".join(x for x in (_section_name(c.get("section", "")), c.get("group_label", ""),
                                                  c["evidence_label"].split("\n")[0]) if x),
            "required_format": c["required_format"], "max_chars": c["max_chars"],
            "constraints": dict(c.get("constraints") or {}),
        }
    return {
        "form_title": fresh.get("form_title", ""),
        "form_sha256": fresh["form_sha256"],
        "source": "blank-cell discovery confirmed by a human reviewer",
        "confirmed_candidates": sorted(chosen, key=lambda i: order[i]),
        "office_only_override": bool(office),
        "targets": targets,
        "slots": slots,
    }


# ------------------------------------------------- office-only guard for any slot spec with coordinate targets


def _docx_context_texts(idx: PartIndex, table: TableInfo, cell: CellInfo) -> list[str]:
    """Printed text around a Word cell discovery does not list: nearest text on its left, nearest text above it in
    the same grid column, and the section banner above."""
    texts: list[str] = []
    for other in sorted((c for c in table.rows[cell.row_idx] if c.col_idx < cell.col_idx), key=lambda c: -c.col_idx):
        txt = _cell_text(idx, _resolve_vmerge(table, other))
        if txt:
            texts.append(txt)
            break
    width, header = _grid_width(table), False
    for r in range(cell.row_idx - 1, -1, -1):
        cells = table.rows[r]
        if len(cells) == 1 and cells[0].grid_span >= width:
            texts.append(_cell_text(idx, cells[0]))
            break
        above = _cell_at(table, r, cell.col_idx)
        txt = _cell_text(idx, _resolve_vmerge(table, above)) if above is not None and not header else ""
        if txt:
            texts.append(txt)
            header = True
    return texts


def _xlsx_context_texts(ws, r: int, c: int) -> list[str]:
    """Printed text around an Excel cell discovery does not list: nearest text on its left, nearest text above it,
    and the section banner above (a column-A label merged across most of the sheet)."""
    boxes: dict[tuple[int, int], tuple[int, int, int, int]] = {}
    for rng in ws.merged_cells.ranges:
        b = (rng.min_row, rng.min_col, rng.max_row, rng.max_col)
        if b[0] <= r and b[1] <= c + 1:
            for rr in range(rng.min_row, min(rng.max_row, r) + 1):
                for cc in range(rng.min_col, min(rng.max_col, c) + 1):
                    boxes[(rr, cc)] = b

    def box(rr: int, cc: int) -> tuple[int, int, int, int]:
        return boxes.get((rr, cc), (rr, cc, rr, cc))

    def literal(rr: int, cc: int) -> str:
        b = box(rr, cc)
        x = ws._cells.get((b[0], b[1]))  # read-only lookup
        return "" if x is None or x.value is None or x.data_type == "f" else str(x.value).strip()

    r0, c0 = box(r, c)[:2]
    texts: list[str] = []
    cc = c0 - 1
    while cc >= 1:
        if literal(r0, cc):
            texts.append(literal(r0, cc))
            break
        cc = box(r0, cc)[1] - 1
    width = min(ws.max_column, MAX_XLSX_COL)
    header = False
    for rr in range(r0 - 1, 0, -1):
        b = box(rr, 1)
        if literal(rr, 1) and b[3] - b[1] + 1 >= max(2, int(width * 0.75)):
            texts.append(literal(rr, 1))  # section banner
            break
        if not header and literal(rr, c0):
            texts.append(literal(rr, c0))
            header = True
    return texts


def office_only_targets(form_path: Union[str, Path], spec: Union[dict, str, Path, None] = None) -> list[str]:
    """Coordinate targets of a slot spec (``empty_cell``) that point at an office-only cell of the blank form.

    The spec — explicit, or the form's sidecar .slots.json — may come from anyone, so each target is checked against
    a fresh deterministic discovery of the form: a candidate's printed label, group and section; a cell discovery
    does not list is checked through the nearest printed text on its left, above it and its section banner. Any
    office keyword (事務局・受付印・受付番号・受付日・審査欄・決裁・検印 …) makes the target office-only.
    Returns ``"<slot id> → <location> (<keywords>)"`` per office-only target; an empty list means none.
    """
    path = Path(form_path)
    spec_d = load_spec(path, spec)
    targets = [t for t in (spec_d.get("targets") or []) if isinstance(t, dict)]
    if not targets:
        return []
    file_type = detect_file_type(path)
    if file_type == "pdf":
        from .pdf_support import office_only_pdf_targets

        return office_only_pdf_targets(path, spec_d)
    found, _ = _docx_blank_candidates(path) if file_type == "docx" else _xlsx_blank_candidates(path)
    known = {_cand_key({"file_type": c.file_type, **c.coords}): c for c in found}
    out: list[str] = []
    if file_type == "docx":
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            parts = {p: index_part(p, zf.read(p), "word") for p in
                     {str(t.get("part") or "word/document.xml") for t in targets} if p in names}
        for tgt in targets:
            idx = parts.get(str(tgt.get("part") or "word/document.xml"))
            coords = docx_target_coords(tgt)
            if idx is None or coords is None:
                continue  # scan() reports a cell it cannot find (or a negative coordinate) and never fills it
            try:
                table = idx.tables[coords[0]]
                cell = _cell_at(table, coords[1], coords[2])
            except IndexError:
                continue  # scan() reports a cell it cannot find and never fills it
            if cell is None:
                continue
            c = known.get(("docx", idx.part_name, table.table_idx, cell.row_idx, cell.col_idx))  # type: ignore[union-attr]
            texts = [c.label_text, c.section, c.group, *c.hints] if c else _docx_context_texts(idx, table, cell)  # type: ignore[arg-type]
            hits = _office_hits(*texts)
            if hits:
                out.append(f"{tgt.get('slot_id')} → table {table.table_idx} row {cell.row_idx} col {cell.col_idx} "
                           f"({', '.join(hits)})")
        return out
    import openpyxl

    with zipfile.ZipFile(path) as zf:
        sheets = xlsx_sheet_parts(zf)
        check_xlsx_limits(zf, sheets)
    wb = None
    for tgt in targets:
        sheet = _resolve_sheet(sheets, tgt.get("sheet", 0))
        ref = str(tgt.get("cell") or "").replace("$", "").strip().upper()
        rc = split_cell_ref(ref) if ref else None
        if sheet is None or rc is None or rc[0] > MAX_XLSX_ROW or rc[1] > MAX_XLSX_COL:
            continue  # scan() reports it and never fills it
        c = known.get(("xlsx", sheet[0], sheet[1], ref))
        if c is not None:
            texts = [c.label_text, c.section, c.group, *c.hints]
        else:
            wb = wb or openpyxl.load_workbook(path, data_only=False)
            texts = _xlsx_context_texts(wb[sheet[1]], *rc)
        hits = _office_hits(*texts)
        if hits:
            out.append(f"{tgt.get('slot_id')} → sheet '{sheet[1]}' {ref} ({', '.join(hits)})")
    return out
