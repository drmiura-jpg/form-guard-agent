"""Surgical XML node injection.

Only the characters of each slot marker are replaced, in place, inside the
original part bytes:

* A marker split across several runs (``<w:t>{{研究</w:t>`` + ``<w:t>代表者</w:t>`` +
  ``<w:t>氏名}}</w:t>``) is coalesced: the value goes into the first run's ``<w:t>``
  (keeping that run's ``<w:rPr>``) and the marker fragments in later runs are
  emptied. No run, property, border, merge or namespace is added or removed.
* Text before/after the marker inside the same node stays byte-identical.
* ``xml:space="preserve"`` is added to a ``<w:t>`` only when its new text needs it.
* Coordinate-designated empty cells get exactly one new run, formatted with the
  paragraph-mark properties (``w:pPr/w:rPr``) that Word would use for typed text.

Every untouched ZIP entry is copied as raw bytes (see ``ziputil``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

from lxml import etree

from .models import PatchRecord
from .ooxml import (
    Container,
    PartIndex,
    UnsupportedXMLError,
    add_preserve_attr,
    escape_text,
    needs_preserve,
    raw_char_offsets,
    sanitize_xml_text,
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
    if file_type == "docx":
        v = re.sub(r"\s*\n\s*", " ", v)
    return v


@dataclass
class _BytePatch:
    start: int
    end: int
    new: bytes
    record: PatchRecord


_RPR_DROP = re.compile(rb"<(\w+:)?(ins|del|moveFrom|moveTo)\b[^>]*/>|<(\w+:)?rPrChange\b.*?</(\w+:)?rPrChange>", re.S)


def patch_part(idx: PartIndex, values: dict[str, str], file_type: str,
               empty_cell_targets: dict[int, str] | None = None) -> tuple[bytes, list[PatchRecord]]:
    """Return (new_part_bytes, patch_records) for one indexed OOXML part."""
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
    return bytes(out), [p.record for p in patches]


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


def fill_form(scan: ScanResult, values: dict[str, str], out_path: Union[str, Path]) -> WriteResult:
    """Write a filled copy of ``scan.path``; slots missing from ``values`` keep their marker."""
    out_path = Path(out_path)
    prepared = {k: prepare_value(v, scan.file_type) for k, v in values.items() if v is not None and str(v).strip()}
    known = {s.slot_id for s in scan.slots}
    prepared = {k: v for k, v in prepared.items() if k in known}
    raw = read_raw_zip(scan.path.read_bytes())
    replacements: dict[str, bytes] = {}
    records: list[PatchRecord] = []
    for part in scan.target_parts:
        idx = scan.parts[part]
        empty_targets = {o.container_index: s.slot_id for s in scan.slots for o in s.occurrences
                         if o.part_name == part and o.marker_kind == "empty_cell"}
        new_bytes, recs = patch_part(idx, prepared, scan.file_type, empty_targets)
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
