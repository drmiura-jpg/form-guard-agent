"""Surgical XML node injection.

Only the characters of each slot marker are replaced, in place, inside the
original part bytes:

* A marker split across several runs (``<w:t>{{研究</w:t>`` + ``<w:t>代表者</w:t>`` +
  ``<w:t>氏名}}</w:t>``) is coalesced: the value goes into the first run's ``<w:t>``
  (keeping that run's ``<w:rPr>``) and the marker fragments in later runs are
  emptied. No run, property, border, merge or namespace is added or removed.
* Text before/after the marker inside the same node stays byte-identical.
* ``xml:space="preserve"`` is added to a ``<w:t>`` only when its new text needs it.
* Coordinate-designated empty Word cells get exactly one new run, formatted with the
  paragraph-mark properties (``w:pPr/w:rPr``) that Word would use for typed text.
* Coordinate-designated empty Excel cells get an inline string. An existing empty
  ``<c r="C5" s="2"/>`` keeps every attribute (``s`` included) and only gains
  ``t="inlineStr"``; a cell that does not exist yet is created in column order inside its
  row (the row is created if needed) with the style Excel would show there (row or column style).
* A printed text checkbox (``□ 合意済み　　□ 協議中…`` in one ``<w:t>`` / ``<t>``) is ticked by replacing the bytes
  of the chosen option's □ with ■ — ``E2 96 A1`` → ``E2 96 A0`` (or ``&#9633;`` → ``&#9632;``), the same length —
  and nothing else: the unselected □, the spaces and the printed notes stay byte-identical.
* Fixed PDF forms are filled by ``pdf_support.write_pdf`` (an incremental update; the blank PDF stays an exact prefix).

Every untouched ZIP entry is copied as raw bytes (see ``ziputil``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from lxml import etree

from .models import PatchRecord
from .ooxml import (
    Container,
    PartIndex,
    SheetCell,
    UnsupportedXMLError,
    add_preserve_attr,
    escape_text,
    inherited_cell_style,
    needs_preserve,
    raw_char_offsets,
    sanitize_xml_text,
    split_cell_ref,
)
from .scanner import ScanResult, find_markers
from .ziputil import read_raw_zip, rewrite_zip


@dataclass
class WriteResult:
    out_path: Path
    patches: list[PatchRecord] = field(default_factory=list)
    modified_parts: list[str] = field(default_factory=list)
    written_slots: list[str] = field(default_factory=list)
    pending_slots: list[str] = field(default_factory=list)
    values: dict[str, str] = field(default_factory=dict)


def prepare_value(value: str, file_type: str) -> str:
    v = sanitize_xml_text(value).strip()
    if file_type in ("docx", "pdf"):
        v = re.sub(r"\s*\n\s*", " ", v)
    return v


CHECK_OFF_BYTES = {b"\xe2\x96\xa1": b"\xe2\x96\xa0", b"&#9633;": b"&#9632;", b"&#x25A1;": b"&#x25A0;",
                   b"&#x25a1;": b"&#x25a0;"}


def checkbox_offsets(text: str, options: list[dict], value: str) -> list[int]:
    """Character offsets (in the container text) of the □ of every option the value ticks."""
    from .constraints import CHECK_OFF, normalize_checkbox_value

    labels = [o["label"] for o in options]
    picked = set(normalize_checkbox_value(value, labels))
    out = []
    for o in options:
        if o["label"] in picked:
            if o["offset"] >= len(text) or text[o["offset"]] != CHECK_OFF:
                raise UnsupportedXMLError(f"checkbox option «{o['label']}»: no printed □ at its position")
            out.append(o["offset"])
    return out


def _checkbox_patches(idx: PartIndex, c: Container, slot_id: str, offsets: list[int]) -> list[_BytePatch]:
    """Replace the bytes of one □ character per offset with ■ (same length; nothing else in the node changes)."""
    out = []
    layout = idx.node_layout(c)
    for off in offsets:
        hit = next(((ni, ns) for ni, ns, ne in layout if ns <= off < ne), None)
        if hit is None:
            raise UnsupportedXMLError(f"checkbox □ at offset {off} is not backed by a text node")
        ni, ns = hit
        node = idx.nodes[ni]
        raw = idx.data[node.content[0] : node.content[1]]
        offs = raw_char_offsets(raw)
        a, b = node.content[0] + offs[off - ns], node.content[0] + offs[off - ns + 1]
        old = idx.data[a:b]
        new = CHECK_OFF_BYTES.get(old)
        if new is None:
            raise UnsupportedXMLError(f"checkbox □ written as {old!r} — refusing to rewrite it")
        out.append(_BytePatch(a, b, new, PatchRecord(part_name=idx.part_name, slot_id=slot_id, kind="checkbox",
                                                     byte_start=a, byte_end=b, old_text="□", new_text="■")))
    return out


@dataclass
class _BytePatch:
    start: int
    end: int
    new: bytes
    record: PatchRecord
    extra: list[PatchRecord] = field(default_factory=list)  # further slots written by the same byte edit


_RPR_DROP = re.compile(rb"<(\w+:)?(ins|del|moveFrom|moveTo)\b[^>]*/>|<(\w+:)?rPrChange\b.*?</(\w+:)?rPrChange>", re.S)


def patch_part(idx: PartIndex, values: dict[str, str], file_type: str,
               empty_cell_targets: dict[int, str] | None = None,
               sheet_cell_targets: dict[str, str] | None = None,
               checkbox_targets: dict[int, tuple[str, list[int]]] | None = None) -> tuple[bytes, list[PatchRecord]]:
    """Return (new_part_bytes, patch_records) for one indexed OOXML part.

    ``empty_cell_targets``: Word paragraph container index -> slot id; ``sheet_cell_targets``: worksheet cell
    reference ("C5") -> slot id; ``checkbox_targets``: container index -> (slot id, offsets of the □ to tick).
    """
    data = idx.data
    node_edits: dict[int, list[tuple[int, int, str, str]]] = {}
    for c in idx.containers:
        text = idx.container_text(c)
        if "{{" not in text and "記入欄" not in text:
            continue
        layout = idx.node_layout(c)
        for hit in find_markers(text):
            if hit.slot_id not in values:
                continue
            value = values[hit.slot_id]
            involved = [(ni, ns, ne) for ni, ns, ne in layout if ns < hit.end and ne > hit.start]
            if not involved:
                raise UnsupportedXMLError(f"marker {hit.text} not backed by text nodes")
            for k, (ni, ns, ne) in enumerate(involved):
                ls, le = max(hit.start, ns) - ns, min(hit.end, ne) - ns
                rep = value if k == 0 else ""
                if ls == le and not rep:
                    continue
                node_edits.setdefault(ni, []).append((ls, le, rep, hit.slot_id))

    patches: list[_BytePatch] = []
    for ni, edits in node_edits.items():
        node = idx.nodes[ni]
        raw = data[node.content[0] : node.content[1]]
        offs = raw_char_offsets(raw)
        if len(offs) - 1 != len(node.text):
            raise UnsupportedXMLError(f"{idx.part_name}: text node {ni} could not be mapped to bytes")
        edits.sort(key=lambda e: e[0])
        for a, b in zip(edits, edits[1:]):
            if a[1] > b[0]:
                raise UnsupportedXMLError("overlapping marker edits")
        new_text = node.text
        for ls, le, rep, slot_id in sorted(edits, key=lambda e: -e[0]):
            new_text = new_text[:ls] + rep + new_text[le:]
            bstart, bend = node.content[0] + offs[ls], node.content[0] + offs[le]
            patches.append(_BytePatch(bstart, bend, escape_text(rep), PatchRecord(
                part_name=idx.part_name, slot_id=slot_id, kind="text", byte_start=bstart, byte_end=bend,
                old_text=node.text[ls:le], new_text=rep)))
        if needs_preserve(new_text) and not node.preserve:
            st = data[node.start_tag[0] : node.start_tag[1]]
            patches.append(_BytePatch(node.start_tag[0], node.start_tag[1], add_preserve_attr(st), PatchRecord(
                part_name=idx.part_name, slot_id=edits[0][3], kind="attr", byte_start=node.start_tag[0],
                byte_end=node.start_tag[1], old_text="", new_text='xml:space="preserve"')))

    for cidx, slot_id in (empty_cell_targets or {}).items():
        if slot_id not in values:
            continue
        patches.append(_insert_run_patch(idx, idx.containers[cidx], slot_id, values[slot_id]))
    if sheet_cell_targets:
        patches += _sheet_cell_patches(idx, sheet_cell_targets, values)
    for cidx, (slot_id, offsets) in (checkbox_targets or {}).items():
        patches += _checkbox_patches(idx, idx.containers[cidx], slot_id, offsets)

    patches.sort(key=lambda p: p.start)
    for a, b in zip(patches, patches[1:]):
        if a.end > b.start:
            raise UnsupportedXMLError(f"{idx.part_name}: overlapping byte patches")
    out = bytearray()
    cursor = 0
    for p in patches:
        out += data[cursor : p.start] + p.new
        cursor = p.end
    out += data[cursor:]
    return bytes(out), [r for p in patches for r in (p.record, *p.extra)]


def _insert_run_patch(idx: PartIndex, c: Container, slot_id: str, value: str) -> _BytePatch:
    prefix = c.qname.split(":")[0] + ":" if ":" in c.qname else ""
    p = prefix.encode()
    rpr = _RPR_DROP.sub(b"", c.ppr_rpr) if c.ppr_rpr else b""
    t_open = b"<" + p + b"t" + (b' xml:space="preserve"' if needs_preserve(value) else b"") + b">"
    run = b"<" + p + b"r>" + rpr + t_open + escape_text(value) + b"</" + p + b"t></" + p + b"r>"
    data = idx.data
    if c.empty:
        st = data[c.start_tag[0] : c.start_tag[1]]
        new = st[:-2].rstrip() + b">" + run + b"</" + c.qname.encode() + b">"
        rec = PatchRecord(part_name=idx.part_name, slot_id=slot_id, kind="expand_empty", byte_start=c.start_tag[0],
                          byte_end=c.start_tag[1], old_text="", new_text=value)
        return _BytePatch(c.start_tag[0], c.start_tag[1], new, rec)
    at = c.end_tag[0]  # type: ignore[index]
    rec = PatchRecord(part_name=idx.part_name, slot_id=slot_id, kind="insert_run", byte_start=at, byte_end=at,
                      old_text="", new_text=value)
    return _BytePatch(at, at, run, rec)


# ------------------------------------------------------------------ worksheet cells

_T_ATTR_RE = re.compile(rb"""(\s)t\s*=\s*(?:"[^"]*"|'[^']*')""")


def _prefix(qname: str) -> bytes:
    return (qname.split(":")[0] + ":").encode() if ":" in qname else b""


def _inline_string(p: bytes, value: str) -> bytes:
    t_open = b"<" + p + b"t" + (b' xml:space="preserve"' if needs_preserve(value) else b"") + b">"
    return b"<" + p + b"is>" + t_open + escape_text(value) + b"</" + p + b"t></" + p + b"is>"


def _new_cell(p: bytes, ref: str, style: Optional[str], value: str) -> bytes:
    s = b' s="' + style.encode() + b'"' if style else b""
    return b"<" + p + b'c r="' + ref.encode() + b'"' + s + b' t="inlineStr">' + _inline_string(p, value) + b"</" + p + b"c>"


def _fill_existing_cell(data: bytes, cell: SheetCell, value: str) -> bytes:
    """<c r="C5" s="2"/> -> <c r="C5" s="2" t="inlineStr"><is><t>value</t></is></c> (every attribute kept)."""
    st = data[cell.start_tag[0] : cell.start_tag[1]]
    body = st[:-2].rstrip() if cell.empty else st[:-1]
    if _T_ATTR_RE.search(body):
        body = _T_ATTR_RE.sub(rb'\1t="inlineStr"', body, count=1)
    else:
        body += b' t="inlineStr"'
    return body + b">" + _inline_string(_prefix(cell.qname), value) + b"</" + cell.qname.encode() + b">"


def _sheet_cell_patches(idx: PartIndex, targets: dict[str, str], values: dict[str, str]) -> list[_BytePatch]:
    if idx.profile != "sheet":
        raise UnsupportedXMLError(f"{idx.part_name}: cell coordinates need a worksheet part")
    if idx.implicit_refs:
        raise UnsupportedXMLError(f"{idx.part_name}: rows/cells without an r attribute cannot be addressed safely")
    data = idx.data
    out: list[_BytePatch] = []
    by_row: dict[int, list[tuple[int, str, str, str]]] = {}
    for ref, slot_id in targets.items():
        if slot_id not in values:
            continue
        rc = split_cell_ref(ref)
        if rc is None:
            raise UnsupportedXMLError(f"{ref!r} is not a cell reference")
        by_row.setdefault(rc[0], []).append((rc[1], ref, slot_id, values[slot_id]))

    def patch(start: int, end: int, new: bytes, kind: str, slots: list[tuple[str, str]]) -> _BytePatch:
        recs = [PatchRecord(part_name=idx.part_name, slot_id=sid, kind=kind, byte_start=start, byte_end=end,
                            old_text="", new_text=v) for sid, v in slots]
        return _BytePatch(start, end, new, recs[0], recs[1:])

    sd = idx.sheet_data
    if sd is None:
        raise UnsupportedXMLError(f"{idx.part_name}: worksheet has no <sheetData>")
    p = _prefix(sd.qname)
    new_rows: dict[int, list[tuple[bytes, list[tuple[str, str]]]]] = {}  # insertion offset (-1: <sheetData/>) -> rows
    for rownum in sorted(by_row):
        missing = []
        for col, ref, slot_id, value in sorted(by_row[rownum]):
            cell = idx.cells.get(ref)
            if cell is None:
                missing.append((col, ref, slot_id, value))
                continue
            if cell.children:
                raise UnsupportedXMLError(f"{idx.part_name} {ref}: cell is not empty — refusing to overwrite it")
            out.append(patch(cell.span[0], cell.span[1], _fill_existing_cell(data, cell, value), "fill_cell",
                             [(slot_id, value)]))
        if not missing:
            continue
        row = idx.rows.get(rownum)
        cells = [(col, _new_cell(p, ref, inherited_cell_style(idx, rownum, col), value), (slot_id, value))
                 for col, ref, slot_id, value in missing]
        if row is None:  # the row does not exist yet: create it, in row order, inside <sheetData>
            later = [r for r in sorted(idx.rows) if r > rownum]
            at = idx.rows[later[0]].start_tag[0] if later else (-1 if sd.empty else sd.end_tag[0])  # type: ignore[index]
            row_bytes = (b"<" + p + b'row r="' + str(rownum).encode() + b'">' + b"".join(c for _, c, _ in cells)
                         + b"</" + p + b"row>")
            new_rows.setdefault(at, []).append((row_bytes, [s for _, _, s in cells]))
            continue
        if row.empty:  # <row r="5" .../> becomes <row r="5" ...>cells</row>
            st = data[row.start_tag[0] : row.start_tag[1]]
            new = st[:-2].rstrip() + b">" + b"".join(c for _, c, _ in cells) + b"</" + row.qname.encode() + b">"
            out.append(patch(row.start_tag[0], row.start_tag[1], new, "insert_cell", [s for _, _, s in cells]))
            continue
        groups: dict[int, list[tuple[bytes, tuple[str, str]]]] = {}
        for col, cbytes, slot in cells:  # keep column order: insert before the first later cell, else before </row>
            nxt = next((idx.cells[r] for r in row.cells if idx.cells[r].col > col), None)
            at = nxt.start_tag[0] if nxt is not None else row.end_tag[0]  # type: ignore[index]
            groups.setdefault(at, []).append((cbytes, slot))
        for at, chunks in groups.items():
            out.append(patch(at, at, b"".join(c for c, _ in chunks), "insert_cell", [s for _, s in chunks]))
    for at, rows in new_rows.items():
        slots = [s for _, ss in rows for s in ss]
        blob = b"".join(r for r, _ in rows)
        if at == -1:
            st = data[sd.start_tag[0] : sd.start_tag[1]]
            new = st[:-2].rstrip() + b">" + blob + b"</" + sd.qname.encode() + b">"
            out.append(patch(sd.start_tag[0], sd.start_tag[1], new, "insert_cell", slots))
        else:
            out.append(patch(at, at, blob, "insert_cell", slots))
    return out


def fill_form(scan: ScanResult, values: dict[str, str], out_path: Union[str, Path]) -> WriteResult:
    """Write a filled copy of ``scan.path``; slots missing from ``values`` keep their marker."""
    if scan.file_type == "pdf":
        from .pdf_support import write_pdf

        return write_pdf(scan, values, out_path)
    out_path = Path(out_path)
    prepared = {k: prepare_value(v, scan.file_type) for k, v in values.items() if v is not None and str(v).strip()}
    known = {s.slot_id for s in scan.slots}
    prepared = {k: v for k, v in prepared.items() if k in known}
    raw = read_raw_zip(scan.path.read_bytes())
    replacements: dict[str, bytes] = {}
    records: list[PatchRecord] = []
    for part in scan.target_parts:
        idx = scan.parts[part]
        designated = [(o, s.slot_id) for s in scan.slots for o in s.occurrences
                      if o.part_name == part and o.marker_kind == "empty_cell"]
        if scan.file_type == "docx":
            empty_targets, cell_targets = {o.container_index: sid for o, sid in designated}, None
        else:
            empty_targets, cell_targets = None, {o.cell_ref: sid for o, sid in designated if o.cell_ref}
        boxes = {}
        for s in scan.slots:
            for o in s.occurrences:
                if o.part_name == part and o.marker_kind == "checkbox_text" and s.slot_id in prepared:
                    text = idx.container_text(idx.containers[o.container_index])
                    boxes[o.container_index] = (s.slot_id, checkbox_offsets(text, o.checkbox_options,
                                                                            prepared[s.slot_id]))
        new_bytes, recs = patch_part(idx, prepared, scan.file_type, empty_targets, cell_targets, boxes)
        if recs:
            etree.fromstring(new_bytes, etree.XMLParser(resolve_entities=False, no_network=True))
            replacements[part] = new_bytes
            records += recs
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(rewrite_zip(raw, replacements))
    written = sorted({r.slot_id for r in records})
    return WriteResult(
        out_path=out_path, patches=records, modified_parts=sorted(replacements), written_slots=written,
        pending_slots=[s.slot_id for s in scan.slots if s.slot_id not in prepared], values=prepared,
    )
