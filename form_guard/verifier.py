"""Deterministic structural & content guard.

The verifier never trusts the writer. It re-opens the original blank form and the
filled file, re-indexes both independently and proves, check by check:

  ZIP   — same entries, same order, identical ZipInfo metadata; every untouched
          entry (styles.xml, numbering.xml, theme1.xml, …) byte-identical, both
          compressed stream and content (SHA-256).
  XML   — well-formed, namespace declarations preserved, and the *markup
          skeleton* (every tag, attribute — xml:space included — and non-slot
          text, with only the slot text nodes blanked) byte-identical: no
          border, merge, property or style reference moved.
  LAYOUT— table topology (tables/rows/cells, gridSpan, vMerge, tblGrid), borders
          & shading, paragraph/run formatting, page setup; for .xlsx merged
          ranges, widths/heights, formulas, data validations, cell styles.
  TEXT  — all boilerplate text identical; each slot holds exactly its value; no
          marker left except slots explicitly awaiting human input.
  GRD/CON — every written value backed by verbatim memo quotes, numbers/dates
          supported, character / format / option / cap constraints met.

Printed text checkboxes (``□ 合意済み　　□ 協議中…``): only the □ of the approved option(s) may have become ■. Those
characters — and only those — are put back to the blank form's bytes before the skeleton and boilerplate comparison,
so an unselected □ that changed, or any other character of the cell, fails XML-03 and TEXT-01; TEXT-02 requires ■
exactly on the approved options and CON-03 at most one ■ when the options are exclusive.

Fixed PDF forms are verified by ``pdf_support.verify_pdf`` with the same 21 check ids.

Coordinate-designated empty cells get their own strict checks. Word: the cell may gain
exactly one run holding one ``<w:t>``, formatted exactly like the blank cell's paragraph
mark — no drawing, field, hyperlink or table, and its ``w:pPr`` unchanged (XML-03,
LAYOUT-03). Excel: the cell may only become an inline string with its ``s`` style kept, the
rows holding designated cells keep their attributes and content (a created row carries only
``r``); reverting the designated cells must give back the blank sheet byte for byte, so no
formula or other cell can have changed (XML-03, LAYOUT-02, LAYOUT-04, TEXT-02).

It also produces the Human-in-the-Loop final confirmation checklist.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from lxml import etree

from .constraints import ADVANCE_NOTICE_PREFIX, DATE_CODES, checkbox_hold, validate_value
from .grounding import NUMBER_DATE_CODES, ground_value
from .models import AuditCheckItem, EntryHash, FieldMapping, HITLItem, VerificationReport
from .ooxml import (
    S_NS,
    W_NS,
    XML_NS,
    PartIndex,
    UnsafeXMLError,
    UnsupportedXMLError,
    canonical_text_node_token,
    check_xlsx_limits,
    ensure_safe_xml,
    index_part,
    inherited_cell_style,
    iter_start_tags,
    xmlns_declarations,
)
from .scanner import ScanResult, era_date_template, find_markers, has_marker
from .textnorm import quantities, value_quantities
from .ziputil import ZipSurgeryError, check_archive

_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
W = f"{{{W_NS}}}"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _check(check_id, category, description, passed, details="", severity="major", **evidence) -> AuditCheckItem:
    return AuditCheckItem(check_id=check_id, category=category, description=description, passed=bool(passed),
                          details=details, severity=severity, evidence=evidence)


# ------------------------------------------------------------------- skeletons


def template_spans(scan: ScanResult, part: str) -> dict[int, tuple[int, int, str]]:
    """Word containers designated as a printed 「令和　　年　　月　　日」: container index -> (start, end, slot id) of the
    skeleton in the blank form's text (those characters are slot text, like a marker's)."""
    idx = scan.parts.get(part)
    out: dict[int, tuple[int, int, str]] = {}
    if idx is None or scan.file_type != "docx":
        return out
    for s in scan.slots:
        for o in s.occurrences:
            if o.part_name == part and o.marker_kind == "date_template" and 0 <= o.container_index < len(idx.containers):
                span = era_date_template(idx.container_text(idx.containers[o.container_index]))
                if span is not None:
                    out[o.container_index] = (span[0], span[1], s.slot_id)
    return out


def slot_node_keys(idx: PartIndex, templates: Optional[dict[int, tuple[int, int, str]]] = None) -> set[tuple[int, int]]:
    """``(container index, position within the container)`` of every text node that holds marker characters (or the
    characters of a designated 「令和　　年　　月　　日」 skeleton, ``templates``).

    Computed on the *original* part; the writer never adds or removes text nodes inside a marker
    container, so the same keys address the corresponding nodes of the filled part.
    """
    keys: set[tuple[int, int]] = set()
    templates = templates or {}
    for c in idx.containers:
        hits = [(h.start, h.end) for h in find_markers(idx.container_text(c))]
        if c.idx in templates:
            hits.append(templates[c.idx][:2])
        if not hits:
            continue
        for k, (_ni, ns, ne) in enumerate(idx.node_layout(c)):
            if any(ns < he and ne > hs for hs, he in hits):
                keys.add((c.idx, k))
    return keys


def skeleton_bytes(idx: PartIndex, cut_containers: Iterable[int] = (),
                   slot_keys: Optional[set[tuple[int, int]]] = None) -> bytes:
    """The part with only slot text blanked.

    Every text node that carries marker characters becomes an empty canonical token (its
    xml:space dropped, since the writer may have to add it) and the runs of designated empty
    cells are removed. All other bytes — including the exact start tag (xml:space) and the text
    of every non-slot node — are kept, so any change outside the slots shows up.
    ``slot_keys=None`` blanks every text node (structure-only comparison).
    """
    edits: list[tuple[int, int, bytes]] = []
    cut = set(cut_containers)
    for ci in sorted(cut):
        if ci >= len(idx.containers):  # structurally different file: nothing to cut, the skeleton check will fail
            continue
        c = idx.containers[ci]
        for rs, re_ in c.runs:
            edits.append((rs, re_, b""))
        if c.empty:
            st = idx.data[c.start_tag[0] : c.start_tag[1]]
            edits.append((c.start_tag[0], c.start_tag[1], st[:-2].rstrip() + b"></" + c.qname.encode() + b">"))
    position: dict[int, int] = {}
    for c in idx.containers:
        for k, n in enumerate(idx.container_nodes(c)):
            position[n.idx] = k
    for n in idx.nodes:
        if n.container in cut:
            continue
        if slot_keys is not None and (n.container, position.get(n.idx, -1)) not in slot_keys:
            continue
        st = idx.data[n.start_tag[0] : n.start_tag[1]]
        edits.append((n.span[0], n.span[1], canonical_text_node_token(st)))
    edits.sort(key=lambda e: (e[0], -e[1]))
    out = bytearray()
    cur = 0
    for s, e, rep in edits:
        if s < cur:
            continue
        out += idx.data[cur:s] + rep
        cur = e
    out += idx.data[cur:]
    return bytes(out)


def _first_divergence(a: bytes, b: bytes) -> int:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


def _snippet(data: bytes, pos: int, width: int = 90) -> str:
    return data[max(0, pos - width // 2) : pos + width // 2].decode("utf-8", "replace")


def _ns_decls(data: bytes) -> list[tuple[str, str]]:
    out = []
    for _, tag in iter_start_tags(data):
        out += xmlns_declarations(tag)
    return out


# ------------------------------------------------------------- docx semantics


def _c14n(el) -> bytes:
    return etree.tostring(el, method="c14n") if el is not None else b""


def docx_structure(xml: bytes) -> dict[str, Any]:
    root = etree.fromstring(xml, _PARSER)
    tables = []
    merges = borders = 0
    for tbl in root.iter(f"{W}tbl"):
        grid = [gc.get(f"{W}w") for gc in tbl.findall(f"{W}tblGrid/{W}gridCol")]
        tblPr = tbl.find(f"{W}tblPr")
        rows = []
        for tr in tbl.findall(f"{W}tr"):
            cells = []
            for tc in tr.findall(f"{W}tc"):
                tcPr = tc.find(f"{W}tcPr")
                span = tc.find(f"{W}tcPr/{W}gridSpan")
                vm = tc.find(f"{W}tcPr/{W}vMerge")
                gs = int(span.get(f"{W}val", "1")) if span is not None else 1
                vmv = (vm.get(f"{W}val") or "continue") if vm is not None else None
                if gs > 1 or vmv:
                    merges += 1
                bd = tc.find(f"{W}tcPr/{W}tcBorders")
                if bd is not None:
                    borders += len(bd)
                cells.append({"span": gs, "vmerge": vmv, "tcPr": _c14n(tcPr)})
            rows.append({"trPr": _c14n(tr.find(f"{W}trPr")), "cells": cells})
        tb = tbl.find(f"{W}tblPr/{W}tblBorders")
        if tb is not None:
            borders += len(tb)
        tables.append({"grid": grid, "tblPr": _c14n(tblPr), "rows": rows})
    sect = [_c14n(s) for s in root.iter(f"{W}sectPr")]
    pg = root.find(f".//{W}sectPr/{W}pgSz")
    page = (pg.get(f"{W}w"), pg.get(f"{W}h")) if pg is not None else None
    return {
        "tables": tables, "merges": merges, "borders": borders, "sectPr": sect, "page": page,
        "n_rows": sum(len(t["rows"]) for t in tables),
        "n_cells": sum(len(r["cells"]) for t in tables for r in t["rows"]),
        "pPr": [_c14n(p.find(f"{W}pPr")) for p in root.iter(f"{W}p")],
        "shading": sum(1 for _ in root.iter(f"{W}shd")),
    }


def _runs_rpr(xml: bytes, skip_paragraphs: set[int]) -> list[bytes]:
    root = etree.fromstring(xml, _PARSER)
    out = []
    for pi, p in enumerate(root.iter(f"{W}p")):
        if pi in skip_paragraphs:
            continue
        for r in p.iter(f"{W}r"):
            out.append(_c14n(r.find(f"{W}rPr")))
    return out


# --------------------------------------------- coordinate-designated Word cells

# Anything but plain text that must never appear in a designated cell unless the blank form already had it.
NON_TEXT_ELEMENTS = frozenset({
    "drawing", "pict", "object", "fldSimple", "instrText", "fldChar", "hyperlink", "tbl", "sym", "AlternateContent",
    "sdt", "ruby", "footnoteReference", "endnoteReference", "commentReference", "ins", "del", "moveFrom", "moveTo",
    "smartTag", "customXml", "oMath", "oMathPara", "control", "txbxContent", "br", "cr", "tab", "ptab",
    "contentPart", "subDoc", "delText", "delInstrText", "embedObject",
})
_REVISION_MARKS = frozenset({"ins", "del", "moveFrom", "moveTo", "rPrChange"})


def _local(el) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def _el_key(el) -> tuple:
    """Namespace-declaration independent structural key: tag, attributes, text and children (recursively)."""
    return (el.tag, tuple(sorted(el.attrib.items())), (el.text or "").strip(),
            tuple(_el_key(c) for c in el if isinstance(c.tag, str)))


def _fmt_key(rpr, strip_revisions: bool = False) -> Optional[tuple]:
    """Run formatting as a comparable key; None when there is none (no or an empty <w:rPr>)."""
    if rpr is None:
        return None
    kids = [c for c in rpr if isinstance(c.tag, str) and not (strip_revisions and _local(c) in _REVISION_MARKS)]
    if not kids and not rpr.attrib:
        return None
    return tuple(sorted(rpr.attrib.items())), tuple(_el_key(c) for c in kids)


def _fmt_text(rpr) -> str:
    if rpr is None or not len(rpr):
        return "none"
    return ", ".join(_local(c) + (f"={c.get(f'{W}val')}" if c.get(f"{W}val") is not None else "")
                     for c in rpr if isinstance(c.tag, str))


def docx_designated_cell_issues(part: str, oidx: PartIndex, data_f: bytes, containers: Iterable[int],
                                slot_by_container: dict[int, str]) -> tuple[list[str], list[str]]:
    """(structure issues → XML-03, formatting issues → LAYOUT-03) for coordinate-designated empty Word cells.

    The filled paragraph may only differ from the blank one by at most one appended run that holds an optional
    <w:rPr> and exactly one <w:t>. The run's formatting must equal the paragraph-mark formatting of the blank
    cell (w:pPr/w:rPr, revision marks aside — what Word gives typed text), and w:pPr must be unchanged.
    """
    struct: list[str] = []
    fmt: list[str] = []
    containers = sorted(containers)
    if not containers:
        return struct, fmt
    try:
        po_all = list(etree.fromstring(oidx.data, _PARSER).iter(f"{W}p"))
        pf_all = list(etree.fromstring(data_f, _PARSER).iter(f"{W}p"))
    except etree.XMLSyntaxError as exc:
        return [f"{part}: {exc}"], []
    if len(po_all) != len(pf_all):
        return [f"{part}: paragraph count {len(po_all)}→{len(pf_all)} — designated cells could not be compared"], []
    for ci in containers:
        c = oidx.containers[ci]
        sid = slot_by_container.get(ci, f"container {ci}")
        po, pf = po_all[c.para_index], pf_all[c.para_index]  # type: ignore[index]
        before = Counter(_local(e) for e in po.iter() if e is not po)
        after = Counter(_local(e) for e in pf.iter() if e is not pf)
        added = sorted(k for k in after if k in NON_TEXT_ELEMENTS and after[k] > before.get(k, 0))
        if added:
            struct.append(f"{sid}: non-text element(s) {', '.join('<w:' + a + '>' for a in added)} inside the "
                          f"designated empty cell")
        runs_o, runs_f = list(po.iter(f"{W}r")), list(pf.iter(f"{W}r"))
        if len(runs_f) < len(runs_o) or any(_el_key(a) != _el_key(b) for a, b in zip(runs_o, runs_f)):
            struct.append(f"{sid}: a run that was already in the blank cell was changed or removed")
        extra = runs_f[len(runs_o):]
        if len(extra) > 1:
            struct.append(f"{sid}: {len(extra)} runs added — at most one run may be added to a designated empty cell")
        for run in extra[:1]:
            if run.getparent() is not pf:
                struct.append(f"{sid}: the added run is wrapped in <w:{_local(run.getparent())}>")
            kids = [_local(k) for k in run if isinstance(k.tag, str)]
            if kids not in (["t"], ["rPr", "t"]):
                struct.append(f"{sid}: the added run holds {kids} — only <w:rPr> and one <w:t> are allowed")
            odd = sorted(etree.QName(a).localname for a in run.attrib if not etree.QName(a).localname.startswith("rsid"))
            if odd:
                struct.append(f"{sid}: unexpected attribute(s) on the added run: {odd}")
            base, got = po.find(f"{W}pPr/{W}rPr"), run.find(f"{W}rPr")
            if _fmt_key(got) != _fmt_key(base, strip_revisions=True):
                fmt.append(f"{sid}: run formatting [{_fmt_text(got)}] differs from the blank cell's paragraph-mark "
                           f"formatting [{_fmt_text(base)}]")
        ppr_o, ppr_f = po.find(f"{W}pPr"), pf.find(f"{W}pPr")
        if (None if ppr_o is None else _el_key(ppr_o)) != (None if ppr_f is None else _el_key(ppr_f)):
            fmt.append(f"{sid}: paragraph properties (w:pPr) of the designated cell were changed or removed")
    return struct, fmt


# -------------------------------------------- coordinate-designated Excel cells


@dataclass
class SheetCellAudit:
    """Result of checking the designated cells of one worksheet part."""

    reverted: bytes                      # the filled part with every designated cell put back as in the blank form
    struct: list[str] = field(default_factory=list)    # XML-03
    style: list[str] = field(default_factory=list)     # LAYOUT-02
    formula: list[str] = field(default_factory=list)   # LAYOUT-04
    text: list[str] = field(default_factory=list)      # TEXT-02
    new_cells: set[str] = field(default_factory=set)   # cells the blank form did not contain
    checked: int = 0


_S_C, _S_IS, _S_T, _XML_SPACE = f"{{{S_NS}}}c", f"{{{S_NS}}}is", f"{{{S_NS}}}t", f"{{{XML_NS}}}space"
_S_ROW, _S_SHEET_DATA = f"{{{S_NS}}}row", f"{{{S_NS}}}sheetData"


def _sheet_cell_elements(root: Any) -> dict[str, Any]:
    """Worksheet cell elements by reference (namespace-aware lxml view; the first <c> per reference, as indexed)."""
    out: dict[str, Any] = {}
    for c in root.iter(_S_C):
        parent = c.getparent()
        if parent is not None and parent.tag == _S_ROW:
            out.setdefault((c.get("r") or "").replace("$", "").upper(), c)
    return out


def _node_key(n: Any) -> tuple:
    """Comparable key of any child node: an element (recursively), a comment or a processing instruction."""
    if isinstance(n.tag, str):
        return _el_key(n)
    if isinstance(n, etree._ProcessingInstruction):
        return ("<?pi?>", n.target, n.text or "")
    return ("<!--comment-->", n.text or "")


def _other_content(el: Any, keep: str) -> list[tuple]:
    """Everything inside ``el`` except its ``keep`` children: other elements, comments, PIs and non-blank text."""
    out = [_node_key(n) for n in el if n.tag != keep]
    out += [("text", t.strip()) for t in [el.text, *(n.tail for n in el)] if t and t.strip()]
    return sorted(out, key=repr)


def _attr_names(names: Iterable[str]) -> list[str]:
    return sorted(etree.QName(k).localname for k in names)


def _rows_by_number(sheet_data: Any) -> dict[int, Any]:
    out: dict[int, Any] = {}
    for row in sheet_data.findall(_S_ROW):
        try:
            out.setdefault(int(row.get("r") or ""), row)  # the first <row> per number, read like index_part does
        except ValueError:
            continue
    return out


def sheet_row_issues(part: str, root_o: Any, root_f: Any, oidx: PartIndex, fidx: PartIndex,
                     refs: dict[str, str]) -> list[str]:
    """XML-03 findings on the containers the revert step restores wholesale: ``<sheetData>`` and every ``<row>``
    that holds designated cells (``refs``: cell ref -> slot id).

    A row created for designated cells is removed again, a self-closing ``<row r="9"/>`` expanded to hold them is put
    back as it was and an expanded ``<sheetData/>`` is restored in one piece — so a row attribute (``hidden``, ``ht``,
    ``s``, ``outlineLevel`` …), anything but ``<c>`` inside such a row, a ``<sheetData>`` attribute or child, or cells /
    rows written out of order would never reach the skeleton comparison. They are checked here instead, before the
    revert: a created row may carry only ``r``; an existing row keeps its attributes and its non-``<c>`` content.
    """
    issues: list[str] = []
    sd_o, sd_f = root_o.find(_S_SHEET_DATA), root_f.find(_S_SHEET_DATA)
    if sd_o is None or sd_f is None:
        return issues  # a <sheetData> that appeared or vanished differs in the skeleton itself
    if dict(sd_o.attrib) != dict(sd_f.attrib):
        changed = [k for k in set(sd_o.attrib) | set(sd_f.attrib) if sd_o.get(k) != sd_f.get(k)]
        issues.append(f"{part}: <sheetData> attributes changed: {_attr_names(changed)}")
    if _other_content(sd_o, _S_ROW) != _other_content(sd_f, _S_ROW):
        issues.append(f"{part}: content other than <row> was added to or changed inside <sheetData>")
    order_o, order_f = list(oidx.rows), list(fidx.rows)
    if order_o == sorted(order_o) and order_f != sorted(order_f):
        issues.append(f"{part}: rows are no longer in ascending order ({order_f})")
    rows_o, rows_f = _rows_by_number(sd_o), _rows_by_number(sd_f)
    for rownum, frow in fidx.rows.items():
        mine = [r for r in frow.cells if r in refs]
        el_f = rows_f.get(rownum)
        if not mine or el_f is None:
            continue
        who = f"{', '.join(dict.fromkeys(refs[r] for r in mine))}: row {rownum}"
        el_o, orow = rows_o.get(rownum), oidx.rows.get(rownum)
        if el_o is None:  # created by the writer for coordinate-designated cells: <row r="N"> and cells only
            odd = [k for k in el_f.attrib if k != "r"]
            if odd:
                issues.append(f"{who} was created for the designated cell(s) but carries attribute(s) "
                              f"{_attr_names(odd)} — a created row may only carry r")
            if _other_content(el_f, _S_C):
                issues.append(f"{who} was created for the designated cell(s) but holds content other than <c>")
        else:
            if dict(el_o.attrib) != dict(el_f.attrib):
                changed = [k for k in set(el_o.attrib) | set(el_f.attrib) if el_o.get(k) != el_f.get(k)]
                issues.append(f"{who} attributes changed: {_attr_names(changed)} (the row's attributes must be kept)")
            if _other_content(el_o, _S_C) != _other_content(el_f, _S_C):
                issues.append(f"{who}: content other than <c> was added to or changed inside the row")
        wrong = [r for r in mine if fidx.cells[r].row != rownum]
        if wrong:
            issues.append(f"{who} holds designated cell(s) {wrong} of another row")
        cols_o = [oidx.cells[r].col for r in (orow.cells if orow is not None else []) if r in oidx.cells]
        cols_f = [fidx.cells[r].col for r in frow.cells if r in fidx.cells]
        if cols_o == sorted(set(cols_o)) and cols_f != sorted(set(cols_f)):
            issues.append(f"{who}: cells are not in column order ({frow.cells})")
    return issues


def inline_string_issues(c_el: Any) -> list[str]:
    """Why a written designated cell is not exactly ``<c …><is><t>value</t></is></c>`` (spreadsheetml namespace):
    ``<is>`` must be the only child node of ``<c>``, ``<t>`` the only child node of ``<is>``, ``<t>`` may carry only
    ``xml:space="preserve"``, and there is no text, tail, comment or processing instruction outside ``<t>``."""
    if c_el is None:
        return ["the <c> element cannot be located in the spreadsheetml namespace"]
    kids = list(c_el)
    if (c_el.text or "") != "" or len(kids) != 1 or kids[0].tag != _S_IS:
        return [f"<c> must contain exactly one spreadsheetml <is> and nothing else (found "
                f"{[etree.QName(k).text if isinstance(k.tag, str) else 'comment/PI' for k in kids]}"
                f"{', loose text' if (c_el.text or '') else ''})"]
    is_el = kids[0]
    t_kids = list(is_el)
    issues = []
    if is_el.attrib:
        issues.append(f"<is> carries attribute(s) {sorted(is_el.attrib)}")
    if (is_el.text or "") != "" or (is_el.tail or "") != "" or len(t_kids) != 1 or t_kids[0].tag != _S_T:
        issues.append(f"<is> must contain exactly one spreadsheetml <t> and nothing else (found "
                      f"{[etree.QName(k).text if isinstance(k.tag, str) else 'comment/PI' for k in t_kids]})")
        return issues
    t_el = t_kids[0]
    odd = sorted(k for k in t_el.attrib if k != _XML_SPACE)
    if odd or t_el.get(_XML_SPACE) not in (None, "preserve"):
        issues.append(f"<t> may carry only xml:space=\"preserve\" (found {sorted(t_el.attrib)})")
    if len(t_el) or (t_el.tail or "") != "":
        issues.append("<t> holds child elements or is followed by loose text")
    return issues


def audit_sheet_cells(part: str, oidx: PartIndex, data_f: bytes, cells: dict[str, str],
                      prepared: dict[str, str]) -> SheetCellAudit:
    """Check the designated cells (ref -> slot id) of a filled worksheet and revert them to the blank form's bytes.

    A written cell must be ``<c …same attributes… t="inlineStr"><is><t>value</t></is></c>`` — in the spreadsheetml
    namespace, ``<is>`` the only child of ``<c>`` and ``<t>`` the only child of ``<is>`` (only ``xml:space`` allowed on
    it), style id ``s`` kept (or, for a cell the blank form did not contain, the row / column style Excel shows there),
    no formula, no value element, no rich text. A designated cell left unfilled must still be absent, or byte-identical
    to the blank form's cell. The rows holding designated cells and ``<sheetData>`` are checked too
    (``sheet_row_issues``). These structural findings (XML-03) are recorded *before* the designated cells are
    reverted, so the caller's comparison of the reverted part with the blank one (any other cell, formula or row)
    can never hide them.
    """
    fidx = index_part(part, data_f, "sheet")
    audit = SheetCellAudit(reverted=data_f)
    if fidx.implicit_refs:
        audit.struct.append(f"{part}: rows/cells without an r attribute — designated cells cannot be located")
        return audit
    root_f = etree.fromstring(data_f, _PARSER)
    elements = _sheet_cell_elements(root_f)
    audit.struct += sheet_row_issues(part, etree.fromstring(oidx.data, _PARSER), root_f, oidx, fidx, cells)
    for ref, sid in sorted(cells.items()):
        oc, fc = oidx.cells.get(ref), fidx.cells.get(ref)
        exp = prepared.get(sid)
        audit.checked += 1
        if oc is None and fc is not None:
            audit.new_cells.add(ref)
        if exp is None:  # unfilled (awaiting human input): absent, or exactly the blank form's cell
            if fc is not None and (oc is None or fidx.data[fc.span[0] : fc.span[1]] != oidx.data[oc.span[0] : oc.span[1]]):
                audit.struct.append(f"{sid}: {ref} was left unfilled but differs from the blank form's cell")
                if fc.children:
                    audit.text.append(f"{sid}: {ref} is awaiting human input but holds content")
            if fc is not None and "f" in fc.children:
                audit.formula.append(f"{sid}: a formula was written into designated cell {ref}")
            continue
        if fc is None:
            if exp:
                audit.text.append(f"{sid}: {ref} should hold «{exp}» but the cell is missing")
            continue
        if "f" in fc.children:
            audit.formula.append(f"{sid}: a formula was written into designated cell {ref}")
        for issue in inline_string_issues(elements.get(ref)):  # namespace-aware, before anything is reverted
            audit.struct.append(f"{sid}: {ref}: {issue}")
        if fc.children != ["is"]:
            audit.struct.append(f"{sid}: {ref} holds {fc.children} — only an inline string <is><t>…</t></is> may be "
                                f"written into a designated cell")
        elif fc.is_container is None:
            audit.struct.append(f"{sid}: {ref} holds no spreadsheetml inline string (<is> in another namespace?)")
            audit.text.append(f"{sid}: {ref} expected «{exp}» but no inline-string text can be read")
        else:
            ic = fidx.containers[fc.is_container]
            raw = fidx.data[ic.span[0] : ic.span[1]]
            if ic.runs or b"rPh" in raw or b"phoneticPr" in raw or len(fidx.container_nodes(ic)) != 1:
                audit.struct.append(f"{sid}: {ref} holds rich text / phonetic runs — a single <t> is expected")
            got = fidx.container_text(ic)
            if got != exp:
                audit.text.append(f"{sid}: {ref} expected «{exp}» got «{got}»")
        expected = dict(oc.attrs) if oc is not None else {"r": ref}
        if oc is None:
            style = inherited_cell_style(oidx, fc.row, fc.col)
            if style:
                expected["s"] = style
        expected["t"] = "inlineStr"
        if fc.attrs.get("s") != expected.get("s"):
            audit.style.append(f"{sid}: {ref} cell style s={expected.get('s') or '—'} → s={fc.attrs.get('s') or '—'} "
                               f"(the cell's format must be kept)")
        other = sorted(k for k in set(expected) | set(fc.attrs) if k != "s" and expected.get(k) != fc.attrs.get(k))
        if other:
            audit.struct.append(f"{sid}: {ref} attributes changed: {other}")
    audit.reverted = _revert_sheet_cells(oidx, fidx, set(cells))
    return audit


def _revert_sheet_cells(oidx: PartIndex, fidx: PartIndex, refs: set[str]) -> bytes:
    """The filled worksheet with each designated cell restored to the blank form's bytes (created cells / rows removed)."""
    edits: list[tuple[int, int, bytes]] = []
    created_rows = 0
    for rownum, frow in fidx.rows.items():
        mine = [r for r in frow.cells if r in refs]
        if not mine:
            continue
        orow = oidx.rows.get(rownum)
        others = [r for r in frow.cells if r not in refs]
        kept = [r for r in mine if r in oidx.cells]
        created = [r for r in mine if r not in oidx.cells]
        if not others and not kept and (orow is None or orow.empty):
            # a row created for the new cells, or a self-closing row expanded to hold them
            edits.append((frow.span[0], frow.span[1], b"" if orow is None else oidx.data[orow.span[0] : orow.span[1]]))
            if orow is None:
                created_rows += 1
            continue
        for r in kept:
            fc, oc = fidx.cells[r], oidx.cells[r]
            edits.append((fc.span[0], fc.span[1], oidx.data[oc.span[0] : oc.span[1]]))
        for r in created:
            fc = fidx.cells[r]
            edits.append((fc.span[0], fc.span[1], b""))
    osd, fsd = oidx.sheet_data, fidx.sheet_data
    if (osd is not None and osd.empty and fsd is not None and not fsd.empty and created_rows
            and created_rows == len(fidx.rows)):
        edits = [(fsd.span[0], fsd.span[1], oidx.data[osd.span[0] : osd.span[1]])]  # <sheetData/> was expanded
    data = fidx.data
    out, cur = bytearray(), 0
    for s, e, rep in sorted(edits):
        if s < cur:
            continue
        out += data[cur:s] + rep
        cur = e
    out += data[cur:]
    return bytes(out)


# ------------------------------------------------------------- xlsx semantics


def xlsx_structure(path_or_bytes) -> dict[str, Any]:
    import openpyxl

    src = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, bytes) else path_or_bytes
    wb = openpyxl.load_workbook(src, data_only=False)
    sheets = []
    for ws in wb.worksheets:
        merged = sorted(str(r) for r in ws.merged_cells.ranges)
        widths = {k: v.width for k, v in sorted(ws.column_dimensions.items())}
        heights = {k: v.height for k, v in sorted(ws.row_dimensions.items()) if v.height}
        formulas, styles, borders = {}, {}, 0
        # sparse: only the cells present in the file (iter_rows() would walk the whole used rectangle)
        for c in sorted(ws._cells.values(), key=lambda c: (c.row, c.column)):
            if c.data_type == "f":
                formulas[c.coordinate] = str(c.value)
            b = c.border
            sides = tuple(getattr(getattr(b, s), "style", None) for s in ("left", "right", "top", "bottom"))
            borders += sum(1 for s in sides if s)
            fill = c.fill.fgColor.rgb if c.fill and c.fill.fill_type else None
            styles[c.coordinate] = (c.number_format, c.font.b, c.font.sz, c.font.name, fill, sides,
                                    c.alignment.horizontal, c.alignment.vertical, c.alignment.wrap_text)
        dvs = sorted((str(dv.sqref), dv.type, dv.formula1) for dv in ws.data_validations.dataValidation)
        ps = ws.page_setup
        sheets.append({
            "title": ws.title, "merged": merged, "widths": widths, "heights": heights, "formulas": formulas,
            "styles": styles, "borders": borders, "validations": dvs,
            "page": (ps.paperSize, ps.orientation, ps.fitToWidth, str(ws.print_area)),
        })
    return {"sheets": sheets}


# ----------------------------------------------------------------- main entry


def verify(
    scan: ScanResult,
    filled_path: Union[str, Path],
    mappings: Optional[list[FieldMapping]] = None,
    memo_text: str = "",
    subject: str = "form_guard",
) -> VerificationReport:
    """Verify a filled file against the original blank form described by ``scan``."""
    if scan.file_type == "pdf":
        from .pdf_support import verify_pdf

        return verify_pdf(scan, filled_path, mappings, memo_text, subject)
    filled_path = Path(filled_path)
    checks: list[AuditCheckItem] = []
    mappings = mappings or []
    values = {m.slot_id: m.filled_value for m in mappings if m.is_written}
    pending = [s.slot_id for s in scan.slots if s.slot_id not in values]
    orig_blob = scan.path.read_bytes()
    filled_blob = filled_path.read_bytes()
    stats: dict[str, Any] = {"filled_sha256": _sha(filled_blob)}  # the exact bytes this report is about

    # ---------------------------------------------------------------- ZIP
    entry_hashes: list[EntryHash] = []
    try:
        zo = zipfile.ZipFile(io.BytesIO(orig_blob))
        zf = zipfile.ZipFile(io.BytesIO(filled_blob))
        check_archive(zf)  # path traversal / ZIP bomb / DTD gate before testzip() decompresses anything
        bad = zf.testzip()
        zip_ok = bad is None
        checks.append(_check("ZIP-01", "zip", "Archive opens and every entry passes its CRC-32 check", zip_ok,
                             "all CRCs valid" if zip_ok else f"corrupt entry: {bad}", "critical"))
    except (zipfile.BadZipFile, ZipSurgeryError) as exc:
        checks.append(_check("ZIP-01", "zip", "Archive opens and every entry passes its CRC-32 check", False,
                             str(exc), "critical"))
        return _finish(scan, checks, entry_hashes, mappings, pending, stats, subject)
    names_o, names_f = [i.filename for i in zo.infolist()], [i.filename for i in zf.infolist()]
    same_names = names_o == names_f
    added, removed = sorted(set(names_f) - set(names_o)), sorted(set(names_o) - set(names_f))
    checks.append(_check("ZIP-02", "zip", "Same ZIP entries in the same order", same_names,
                         f"{len(names_o)} entries, identical order" if same_names else
                         f"added {added or '—'}; removed {removed or '—'}" + ("; order changed" if not added and not removed else ""),
                         "critical", added=added, removed=removed))
    raw_o = _raw_map(orig_blob)
    raw_f = _raw_map(filled_blob)
    targets = set(scan.target_parts)
    meta_diffs, untouched_diffs = [], []
    infos_f = {i.filename: i for i in zf.infolist()}
    for info in zo.infolist():
        name = info.filename
        if name not in infos_f:
            entry_hashes.append(EntryHash(name=name, role="removed", original_sha256=_sha(zo.read(name))))
            continue
        fi = infos_f[name]
        co, cf = zo.read(name), zf.read(name)
        meta_same = _meta(info) == _meta(fi)
        if not meta_same:
            meta_diffs.append(name)
        role = "target" if name in targets else "untouched"
        raw_same = raw_o.get(name) == raw_f.get(name)
        eh = EntryHash(name=name, role=role, original_sha256=_sha(co), filled_sha256=_sha(cf), identical=co == cf,
                       raw_identical=raw_same, metadata_identical=meta_same, size=len(cf))
        entry_hashes.append(eh)
        if role == "untouched" and not (eh.identical and raw_same):
            untouched_diffs.append(name)
    for name in added:
        entry_hashes.append(EntryHash(name=name, role="added", filled_sha256=_sha(zf.read(name))))
    checks.append(_check("ZIP-03", "zip", "ZipInfo metadata identical (timestamps, flags, method, attributes, extra)",
                         not meta_diffs, "identical for all entries" if not meta_diffs else f"differs: {meta_diffs}",
                         "major", differing=meta_diffs))
    n_untouched = sum(1 for e in entry_hashes if e.role == "untouched")
    key_parts = [n for n in ("word/styles.xml", "word/numbering.xml", "word/fontTable.xml", "word/theme/theme1.xml",
                             "word/settings.xml", "xl/styles.xml", "xl/workbook.xml", "xl/theme/theme1.xml",
                             "[Content_Types].xml") if n in names_o]
    checks.append(_check(
        "ZIP-04", "zip", "Every non-target entry is byte-for-byte identical (raw compressed stream + SHA-256 content)",
        not untouched_diffs and not removed,
        (f"{n_untouched} entries copied verbatim incl. {', '.join(key_parts)}" if not untouched_diffs and not removed
         else f"changed: {untouched_diffs + removed}"), "critical", changed=untouched_diffs, key_parts=key_parts))
    modified = sorted(e.name for e in entry_hashes if e.role in ("target", "untouched") and not e.identical)
    unexpected = [m for m in modified if m not in targets]
    checks.append(_check("ZIP-05", "zip", "Only parts that contain designated slots were modified", not unexpected and not added,
                         f"modified: {modified or 'none'}" if not unexpected else f"unexpected modifications: {unexpected}",
                         "critical", modified=modified))
    stats["entries"] = len(names_o)
    stats["modified_parts"] = modified

    # ---------------------------------------------------------------- XML
    from .writer import prepare_value

    prepared = {k: prepare_value(v, scan.file_type) for k, v in values.items()}
    wellformed, ns_ok, skel_ok = True, True, True
    skel_details, ns_details, wf_details = [], [], []
    filled_parts: dict[str, PartIndex] = {}
    designated: dict[str, set[int]] = {}         # Word: paragraph container indices of designated empty cells
    sheet_cells: dict[str, dict[str, str]] = {}  # Excel: part -> {cell ref: slot id}
    slot_by_container: dict[str, dict[int, str]] = {}
    checkboxes: dict[str, dict[int, tuple[str, list[dict]]]] = {}  # part -> container -> (slot id, printed options)
    for s in scan.slots:
        for o in s.occurrences:
            if o.marker_kind == "checkbox_text":
                checkboxes.setdefault(o.part_name, {})[o.container_index] = (s.slot_id, o.checkbox_options)
                continue
            if o.marker_kind not in ("empty_cell", "date_template"):
                continue
            if scan.file_type == "docx":
                if o.marker_kind == "empty_cell":  # a Word 「令和　年　月　日」 is checked like a marker (template_spans)
                    designated.setdefault(o.part_name, set()).add(o.container_index)
                    slot_by_container.setdefault(o.part_name, {})[o.container_index] = s.slot_id
            elif o.cell_ref:  # an Excel 「令和　年　月　日」 cell is rewritten like a designated empty cell
                sheet_cells.setdefault(o.part_name, {})[o.cell_ref] = s.slot_id
    templates = {part: template_spans(scan, part) for part in targets}
    slot_keys = {part: slot_node_keys(scan.parts[part], templates[part]) for part in targets if part in scan.parts}
    cell_struct: list[str] = []   # designated cells: structure (XML-03)
    cell_format: list[str] = []   # Word designated cells: formatting (LAYOUT-03)
    box_text: list[str] = []      # checkboxes: ■ not exactly on the approved options (TEXT-02)
    box_marks: dict[str, set[str]] = {}  # checkboxes: options ticked in the file (CON-03)
    sheet_audits: dict[str, SheetCellAudit] = {}
    tags_compared = 0
    for part in sorted(targets):
        if part not in names_f:
            wellformed = False
            wf_details.append(f"{part} missing")
            continue
        data_f = zf.read(part)
        try:
            ensure_safe_xml(data_f, part)
            etree.fromstring(data_f, _PARSER)
            profile = scan.parts[part].profile
            if part in checkboxes:  # put the approved ■ back to the blank form's □ before anything is compared
                data_f, issues, marks = revert_checkboxes(part, scan.parts[part], data_f, checkboxes[part], prepared)
                box_text += issues
                box_marks.update(marks)
            if part in sheet_cells:
                audit = audit_sheet_cells(part, scan.parts[part], data_f, sheet_cells[part], prepared)
                sheet_audits[part] = audit
                cell_struct += audit.struct
                filled_parts[part] = index_part(part, audit.reverted, profile)  # compare the rest of the sheet as-is
            else:
                filled_parts[part] = index_part(part, data_f, profile)
        except (etree.XMLSyntaxError, UnsafeXMLError, UnsupportedXMLError) as exc:
            wellformed = False
            wf_details.append(f"{part}: {exc}")
            continue
        data_o = scan.parts[part].data
        if _ns_decls(data_o) != _ns_decls(data_f):
            ns_ok = False
            ns_details.append(part)
        if part in designated:
            st, fm = docx_designated_cell_issues(part, scan.parts[part], data_f, designated[part],
                                                 slot_by_container.get(part, {}))
            cell_struct += st
            cell_format += fm
        so = skeleton_bytes(scan.parts[part], designated.get(part, ()), slot_keys.get(part, set()))
        sf = skeleton_bytes(filled_parts[part], designated.get(part, ()), slot_keys.get(part, set()))
        tags_compared += sum(1 for _ in iter_start_tags(data_o))
        if so != sf:
            skel_ok = False
            pos = _first_divergence(so, sf)
            skel_details.append(f"{part}: first divergence at skeleton byte {pos}: original «{_snippet(so, pos)}» vs "
                                f"filled «{_snippet(sf, pos)}»")
    checks.append(_check("XML-01", "xml", "Modified XML parts are well-formed (no DTD, no entity tricks)", wellformed,
                         "; ".join(wf_details) or f"{len(filled_parts)} part(s) parsed", "critical"))
    checks.append(_check("XML-02", "xml", "Namespace declarations preserved exactly", ns_ok and wellformed,
                         "identical prefixes and URIs" if ns_ok else f"changed in {ns_details}", "critical"))
    n_nodes = sum(len(scan.parts[p].nodes) for p in targets if p in scan.parts)
    n_cells = sum(len(v) for v in designated.values()) + sum(len(v) for v in sheet_cells.values())
    xml_ok = skel_ok and wellformed and not cell_struct
    checks.append(_check(
        "XML-03", "xml", "Markup skeleton byte-identical outside slot text (every tag, attribute, property)", xml_ok,
        (f"{tags_compared} tags and {n_nodes} text nodes compared (xml:space and text of every non-slot node included); "
         f"only slot characters differ" + (f"; {n_cells} designated empty cell(s) hold plain text only" if n_cells else "")
         if xml_ok else " | ".join(skel_details + cell_struct[:6])), "critical", tags=tags_compared,
        designated_cell_issues=cell_struct[:20]))
    stats["tags_compared"] = tags_compared
    stats["designated_cells"] = n_cells

    # ------------------------------------------------------------- LAYOUT
    if scan.file_type == "docx":
        _docx_layout_checks(scan, zo, zf, checks, stats, designated, cell_format)
    else:
        _xlsx_layout_checks(orig_blob, filled_blob, checks, stats, scan, sheet_audits)

    # --------------------------------------------------------------- TEXT
    _text_checks(scan, filled_parts, values, pending, designated, checks, stats, slot_keys, sheet_audits, templates,
                 box_text)

    # ------------------------------------------------------- GRD / CON / APP
    _content_checks(scan, mappings, values, memo_text, checks, stats)
    exclusive = [f"{s.slot_id}: {len(box_marks.get(s.slot_id, ()))} options ticked "
                 f"(■ on {'、'.join(sorted(box_marks.get(s.slot_id, ())))}) — only one is allowed"
                 for s in scan.slots if s.constraints.get("checkbox") and s.constraints.get("exclusive", True)
                 and len(box_marks.get(s.slot_id, ())) > 1]
    if exclusive:
        fail_check(checks, "CON-03", exclusive)
    if checkboxes:
        stats["checkboxes"] = sum(len(v) for v in checkboxes.values())
    _app_check(scan.file_type, filled_blob, checks, expected_sheet_values(scan, prepared))
    return _finish(scan, checks, entry_hashes, mappings, pending, stats, subject)


def fail_check(checks: list[AuditCheckItem], check_id: str, issues: list[str]) -> None:
    """Mark an already recorded check failed with the given findings (kept with any earlier findings)."""
    for c in checks:
        if c.check_id == check_id:
            keep = [] if c.passed else [c.details]
            c.passed = False
            c.details = "; ".join(keep + issues)
            return


def revert_checkboxes(part: str, oidx: PartIndex, data_f: bytes, boxes: dict[int, tuple[str, list[dict]]],
                      prepared: dict[str, str]) -> tuple[bytes, list[str], dict[str, set[str]]]:
    """(filled part with only the approved ■ put back to the blank form's □, TEXT-02 findings, ticked options).

    Each approved option's character is restored from the blank part's own bytes at the same character offset, so
    the result equals the blank part byte for byte iff nothing but the approved ticks changed."""
    from .constraints import CHECK_ON, normalize_checkbox_value

    try:
        fidx = index_part(part, data_f, oidx.profile)
    except (UnsafeXMLError, UnsupportedXMLError) as exc:
        return data_f, [f"{part}: {exc}"], {}
    if len(fidx.containers) != len(oidx.containers):
        return data_f, [f"{part}: container count {len(oidx.containers)}→{len(fidx.containers)}"], {}
    issues: list[str] = []
    marks: dict[str, set[str]] = {}
    edits: list[tuple[int, int, bytes]] = []
    for ci, (sid, options) in sorted(boxes.items()):
        co, cf = oidx.containers[ci], fidx.containers[ci]
        to, tf = oidx.container_text(co), fidx.container_text(cf)
        labels = [o["label"] for o in options]
        approved = set(normalize_checkbox_value(prepared[sid], labels)) if sid in prepared else set()
        want = sorted(o["offset"] for o in options if o["label"] in approved)
        marks[sid] = {o["label"] for o in options if o["offset"] < len(tf) and tf[o["offset"]] == CHECK_ON}
        expected = "".join(CHECK_ON if i in want else ch for i, ch in enumerate(to))
        if tf != expected:
            issues.append(f"{sid}: expected «{expected[:48]}» got «{tf[:48]}» (■ must be exactly on "
                          f"{'、'.join(o for o in labels if o in approved) or 'no option'})")
        if len(tf) != len(to):
            continue
        for off in want:
            if tf[off] != CHECK_ON:
                continue
            fspan, ospan = _char_bytes(fidx, cf, off), _char_bytes(oidx, co, off)
            if fspan is None or ospan is None:
                continue
            edits.append((fspan[0], fspan[1], oidx.data[ospan[0] : ospan[1]]))
    out, cur = bytearray(), 0
    for s, e, rep in sorted(edits):
        out += data_f[cur:s] + rep
        cur = e
    out += data_f[cur:]
    return bytes(out), issues, marks


def _char_bytes(idx: PartIndex, c, off: int) -> Optional[tuple[int, int]]:
    """Byte range of the character at ``off`` of a container's text (inside its text node)."""
    from .ooxml import raw_char_offsets

    for ni, ns, ne in idx.node_layout(c):
        if ns <= off < ne:
            node = idx.nodes[ni]
            offs = raw_char_offsets(idx.data[node.content[0] : node.content[1]])
            return node.content[0] + offs[off - ns], node.content[0] + offs[off - ns + 1]
    return None


def expected_sheet_values(scan: ScanResult, prepared: dict[str, str]) -> list[tuple[str, str, Optional[str], str]]:
    """(sheet name, cell, expected text, slot id) for every Excel slot cell: a designated empty cell holds its
    value (None while it awaits human input); a cell with markers holds its printed text with the markers replaced."""
    if scan.file_type != "xlsx":
        return []
    from .constraints import CHECK_ON, normalize_checkbox_value

    out: dict[tuple[str, str], tuple[str, str, Optional[str], str]] = {}
    for s in scan.slots:
        for o in s.occurrences:
            if not o.cell_ref or not o.sheet_name:
                continue
            if o.marker_kind == "checkbox_text":  # the printed options with ■ on the approved one(s)
                picked = set(normalize_checkbox_value(prepared.get(s.slot_id, ""), [x["label"] for x in o.checkbox_options]))
                want = {x["offset"] for x in o.checkbox_options if x["label"] in picked}
                exp = "".join(CHECK_ON if i in want else ch for i, ch in enumerate(o.context_text))
            elif o.marker_kind == "empty_cell":
                exp: Optional[str] = prepared.get(s.slot_id)
            elif o.marker_kind == "date_template":  # the date, or the printed 「令和　年　月　日」 while it awaits input
                exp = prepared.get(s.slot_id, o.marker)
            else:
                idx = scan.parts.get(o.part_name)
                if idx is None or not 0 <= o.container_index < len(idx.containers):
                    continue
                exp = _expected_text(idx.container_text(idx.containers[o.container_index]), prepared)
            out.setdefault((o.sheet_name, o.cell_ref), (o.sheet_name, o.cell_ref, exp, s.slot_id))
    return list(out.values())


def _raw_map(blob: bytes) -> dict[str, str]:
    from .ziputil import ZipSurgeryError, read_raw_zip

    try:
        return {e.name: e.raw_sha256 for e in read_raw_zip(blob).entries}
    except ZipSurgeryError:
        return {}


def _meta(i: zipfile.ZipInfo) -> tuple:
    return (i.filename, i.date_time, i.compress_type, i.flag_bits, i.create_system, i.create_version,
            i.extract_version, i.external_attr, i.internal_attr, i.extra, i.comment)


def _docx_layout_checks(scan, zo, zf, checks, stats, designated, cell_format=()):
    part = "word/document.xml"
    try:
        so = docx_structure(zo.read(part))
        sf = docx_structure(zf.read(part))
    except (KeyError, etree.XMLSyntaxError) as exc:
        checks.append(_check("LAYOUT-01", "layout", "Table topology identical", False, str(exc), "critical"))
        return
    topo = lambda s: [(t["grid"], [[(c["span"], c["vmerge"]) for c in r["cells"]] for r in t["rows"]]) for t in s["tables"]]
    same_topo = topo(so) == topo(sf)
    checks.append(_check(
        "LAYOUT-01", "layout", "Table topology identical (tables, rows, cells, gridSpan, vMerge, tblGrid)", same_topo,
        (f"{len(so['tables'])} tables, {so['n_rows']} rows, {so['n_cells']} cells, {so['merges']} merged cells — identical"
         if same_topo else f"tables {len(so['tables'])}→{len(sf['tables'])}, rows {so['n_rows']}→{sf['n_rows']}, "
         f"cells {so['n_cells']}→{sf['n_cells']}, merged cells {so['merges']}→{sf['merges']}"), "critical",
        original={"tables": len(so["tables"]), "rows": so["n_rows"], "cells": so["n_cells"], "merges": so["merges"]},
        filled={"tables": len(sf["tables"]), "rows": sf["n_rows"], "cells": sf["n_cells"], "merges": sf["merges"]}))
    props = lambda s: [(t["tblPr"], [(r["trPr"], [c["tcPr"] for c in r["cells"]]) for r in t["rows"]]) for t in s["tables"]]
    same_borders = props(so) == props(sf) and so["borders"] == sf["borders"]
    checks.append(_check(
        "LAYOUT-02", "layout", "Borders, shading and cell properties identical (tblBorders, tcBorders, shd, tcW)",
        same_borders,
        (f"{so['borders']} border definitions and {so['shading']} shading fills — identical" if same_borders else
         f"border definitions {so['borders']}→{sf['borders']}, shading {so['shading']}→{sf['shading']}"), "critical",
        original_borders=so["borders"], filled_borders=sf["borders"]))
    skip = {scan.parts[part].containers[ci].para_index for ci in designated.get(part, set())} if part in scan.parts else set()
    same_ppr = so["pPr"] == sf["pPr"]
    try:
        same_rpr = _runs_rpr(zo.read(part), skip) == _runs_rpr(zf.read(part), skip)
    except etree.XMLSyntaxError:
        same_rpr = False
    cell_format = list(cell_format)
    n_designated = sum(len(v) for v in designated.values())
    fmt_ok = same_ppr and same_rpr and not cell_format
    checks.append(_check(
        "LAYOUT-03", "layout", "Paragraph and run formatting identical (pPr / rPr)", fmt_ok,
        (f"{len(so['pPr'])} paragraphs checked" + (f"; {n_designated} designated empty cell(s) formatted exactly like "
                                                    f"the blank cell (pPr, paragraph-mark rPr)" if n_designated else "")
         if fmt_ok else
         "; ".join([f"paragraph properties {'ok' if same_ppr else 'changed'}, run properties {'ok' if same_rpr else 'changed'}"]
                   + cell_format[:6])), "major", designated_cell_issues=cell_format[:20]))
    same_sect = so["sectPr"] == sf["sectPr"]
    checks.append(_check("LAYOUT-04", "layout", "Page setup identical (size, margins, header/footer references)", same_sect,
                         f"page {so['page']} twips — identical" if same_sect else f"page {so['page']} → {sf['page']}", "major",
                         original_page=so["page"], filled_page=sf["page"]))
    stats.update({"tables": len(so["tables"]), "rows": so["n_rows"], "cells": so["n_cells"], "merges": so["merges"],
                  "borders": so["borders"]})


def _xlsx_layout_checks(orig_blob, filled_blob, checks, stats, scan=None, sheet_audits=None):
    try:
        with zipfile.ZipFile(io.BytesIO(filled_blob)) as z:
            check_xlsx_limits(z)
    except (UnsupportedXMLError, KeyError, etree.XMLSyntaxError) as exc:
        checks.append(_check("LAYOUT-01", "layout", "Sheet topology identical (sheets, merged ranges)", False,
                             f"not compared: {exc}", "critical"))
        return
    try:
        so, sf = xlsx_structure(orig_blob), xlsx_structure(filled_blob)
    except Exception as exc:  # noqa: BLE001 - a hostile file fails the check; it never crashes the verifier
        checks.append(_check("LAYOUT-01", "layout", "Sheet topology identical (sheets, merged ranges)", False,
                             f"not compared: openpyxl cannot read the file ({type(exc).__name__}: {exc})", "critical"))
        return
    sheet_audits = sheet_audits or {}
    cell_style = [i for a in sheet_audits.values() for i in a.style]
    cell_formula = [i for a in sheet_audits.values() for i in a.formula]
    # cells the writer created for designated targets: their style is checked by audit_sheet_cells
    new_cells: dict[int, set[str]] = {}
    for sidx, _name, part in (scan.sheets if scan is not None else []):
        if part in sheet_audits:
            new_cells[sidx] = sheet_audits[part].new_cells
    for i, sheet in enumerate(sf["sheets"]):
        for ref in new_cells.get(i, ()):
            sheet["styles"].pop(ref, None)
    pairs = list(zip(so["sheets"], sf["sheets"]))
    same_count = len(so["sheets"]) == len(sf["sheets"])
    merged_ok = same_count and all(a["merged"] == b["merged"] for a, b in pairs)
    n_m_o = sum(len(s["merged"]) for s in so["sheets"])
    n_m_f = sum(len(s["merged"]) for s in sf["sheets"])
    checks.append(_check("LAYOUT-01", "layout", "Sheet topology identical (sheets, merged ranges)", merged_ok,
                         f"{len(so['sheets'])} sheet(s), {n_m_o} merged ranges — identical" if merged_ok else
                         f"sheets {len(so['sheets'])}→{len(sf['sheets'])}, merged ranges {n_m_o}→{n_m_f}", "critical",
                         original={"merges": n_m_o}, filled={"merges": n_m_f}))
    style_ok = same_count and all(a["styles"] == b["styles"] for a, b in pairs) and not cell_style
    b_o = sum(s["borders"] for s in so["sheets"])
    b_f = sum(s["borders"] for s in sf["sheets"])
    n_cells = sum(a.checked for a in sheet_audits.values())
    checks.append(_check("LAYOUT-02", "layout", "Borders, fills, fonts and number formats identical for every cell",
                         style_ok, (f"{b_o} cell border edges and all cell styles — identical"
                                    + (f"; style id (s) of {n_cells} designated cell(s) kept" if n_cells else ""))
                         if style_ok else "; ".join([f"border edges {b_o}→{b_f}; cell styles changed"] + cell_style[:6]),
                         "critical", original_borders=b_o, filled_borders=b_f, designated_cell_issues=cell_style[:20]))
    dims_ok = same_count and all(a["widths"] == b["widths"] and a["heights"] == b["heights"] for a, b in pairs)
    checks.append(_check("LAYOUT-03", "layout", "Column widths and row heights identical", dims_ok,
                         "identical" if dims_ok else "changed", "major"))
    f_o = sum(len(s["formulas"]) for s in so["sheets"])
    f_f = sum(len(s["formulas"]) for s in sf["sheets"])
    logic_ok = same_count and all(a["formulas"] == b["formulas"] and a["validations"] == b["validations"]
                                  and a["page"] == b["page"] for a, b in pairs) and not cell_formula
    v_o = sum(len(s["validations"]) for s in so["sheets"])
    v_f = sum(len(s["validations"]) for s in sf["sheets"])
    checks.append(_check("LAYOUT-04", "layout", "Formulas, drop-down validations and print setup identical", logic_ok,
                         f"{f_o} formulas, {v_o} data validations, print area — identical" if logic_ok else
                         "; ".join([f"formulas {f_o}→{f_f}, validations {v_o}→{v_f}"] + cell_formula[:6]), "critical",
                         original={"formulas": f_o, "validations": v_o}, filled={"formulas": f_f, "validations": v_f},
                         designated_cell_issues=cell_formula[:20]))
    stats.update({"sheets": len(so["sheets"]), "merges": n_m_o, "borders": b_o, "formulas": f_o, "validations": v_o})


def _expected_text(text: str, values: dict[str, str]) -> str:
    out, cur = [], 0
    for h in find_markers(text):
        out.append(text[cur : h.start])
        out.append(values.get(h.slot_id, h.text))
        cur = h.end
    out.append(text[cur:])
    return "".join(out)


def _text_checks(scan, filled_parts, values, pending, designated, checks, stats, slot_keys=None, sheet_audits=None,
                 templates=None, box_text=None):
    from .writer import prepare_value

    slot_keys = slot_keys or {}
    templates = templates or {}
    prepared = {k: prepare_value(v, scan.file_type) for k, v in values.items()}
    boiler_ok, slot_ok = True, True
    boiler_bad, slot_bad = [], []
    n_boiler = n_slot = 0
    residual = []
    n_slot += sum(1 for s in scan.slots for o in s.occurrences if o.marker_kind == "checkbox_text")
    if box_text:  # a checkbox whose ■ is not exactly on the approved option(s)
        slot_ok = False
        slot_bad += box_text
    if not scan.slots:
        slot_ok = False
        slot_bad.append("0 fillable slots found — designate {{markers}}, [記入欄: ...], or cell coordinates in .slots.json "
                        "(unmarked form: run --discover-blanks and confirm the cells to fill)")
    for audit in (sheet_audits or {}).values():  # designated Excel cells (the rest of the sheet is compared below)
        n_slot += audit.checked
        if audit.text:
            slot_ok = False
            slot_bad += audit.text
    for part, fidx in filled_parts.items():
        oidx = scan.parts[part]
        if len(oidx.containers) != len(fidx.containers):
            boiler_ok = slot_ok = False
            boiler_bad.append(f"{part}: container count {len(oidx.containers)}→{len(fidx.containers)}")
            continue
        dset = designated.get(part, set())
        keys = slot_keys.get(part, set())
        tmpl = templates.get(part, {})
        slot_by_container = {o.container_index: s.slot_id for s in scan.slots for o in s.occurrences
                             if o.part_name == part and o.marker_kind == "empty_cell"}
        for co, cf in zip(oidx.containers, fidx.containers):
            to, tf = oidx.container_text(co), fidx.container_text(cf)
            if co.idx in dset:
                n_slot += 1
                sid = slot_by_container.get(co.idx)
                exp = prepared.get(sid, "") if sid else ""
                if tf != exp:
                    slot_ok = False
                    slot_bad.append(f"{sid}: expected «{exp}» got «{tf}»")
                continue
            # Compare run by run (<w:t> / <t>), not just per paragraph: moving one character from a bold
            # run into the next normal run keeps the paragraph text but changes the printed form.
            no, nf = oidx.container_nodes(co), fidx.container_nodes(cf)
            fixed_o = [n.text for k, n in enumerate(no) if (co.idx, k) not in keys]
            fixed_f = [n.text for k, n in enumerate(nf) if (co.idx, k) not in keys]
            if co.idx in tmpl:  # a printed 「令和　　年　　月　　日」: the skeleton becomes the date, the rest stays
                ts, te, sid = tmpl[co.idx]
                n_slot += 1
                exp = to[:ts] + prepared[sid] + to[te:] if sid in prepared else to
                if tf != exp:
                    slot_ok = False
                    slot_bad.append(f"{sid}: expected «{exp}» got «{tf}»")
                if len(no) != len(nf) or fixed_o != fixed_f:
                    boiler_ok = False
                    boiler_bad.append(f"«{to[:40]}»: printed text next to the date template changed run by run")
                continue
            if has_marker(to):
                n_slot += 1
                exp = _expected_text(to, prepared)
                if tf != exp:
                    slot_ok = False
                    slot_bad.append(f"«{to[:30]}» expected «{exp[:60]}» got «{tf[:60]}»")
                if len(no) != len(nf) or fixed_o != fixed_f:
                    boiler_ok = False
                    boiler_bad.append(f"«{to[:40]}»: printed text next to the marker changed run by run")
            else:
                n_boiler += 1
                if tf != to:
                    boiler_ok = False
                    boiler_bad.append(f"«{to[:40]}» → «{tf[:40]}»")
                elif len(no) != len(nf) or fixed_o != fixed_f:
                    boiler_ok = False
                    boiler_bad.append(f"«{to[:40]}»: same text, but characters moved between runs "
                                      f"{[n.text for n in no]} → {[n.text for n in nf]}")
            for h in find_markers(tf):
                residual.append(h.slot_id)
    for part, oidx in scan.parts.items():
        if part in filled_parts:
            continue
        n_boiler += sum(1 for c in oidx.containers if not has_marker(oidx.container_text(c)))
    checks.append(_check("TEXT-01", "text", "All boilerplate text unchanged (notes, legal clauses, labels)", boiler_ok,
                         f"{n_boiler} non-slot text blocks identical" if boiler_ok else "; ".join(boiler_bad[:6]),
                         "critical", altered=boiler_bad[:20]))
    checks.append(_check("TEXT-02", "text", "Each slot contains exactly its approved value (text around markers intact)",
                         slot_ok, f"{n_slot} slot text blocks match the approved values" if slot_ok else "; ".join(slot_bad[:6]),
                         "critical", slots=len(scan.slots)))
    unexpected = sorted(set(residual) - set(pending))
    checks.append(_check("TEXT-03", "text", "No unreplaced markers except fields awaiting human input",
                         not unexpected, (f"pending human input: {', '.join(pending)}" if pending else "no markers left")
                         if not unexpected else f"unreplaced: {unexpected}", "critical", pending=pending))
    stats.update({"boilerplate_blocks": n_boiler, "slot_blocks": n_slot})


NOT_VERIFIED = "structural verification only (no memo provided — grounding not verified)"


def _content_checks(scan, mappings, values, memo, checks, stats):
    ungrounded, unsupported, limits, formats, options = [], [], [], [], []
    why_ungrounded, why_unsupported = [], []
    by_id = {s.slot_id: s for s in scan.slots}
    for m in mappings:
        if not m.is_written or m.slot_id not in by_id:
            continue
        slot = by_id[m.slot_id]
        if m.status == "filled":
            quotes = [sp.text for sp in m.source_spans] or ([m.source_quote] if m.source_quote else [])
            g = ground_value(m.filled_value, quotes, memo, slot, near=[sp.start_char for sp in m.source_spans] or None)
            # Every error-severity grounding issue fails a check: numbers / dates → GRD-02; everything
            # else (quote missing, a name / ID / option the quote does not state) → GRD-01.
            errs = [i for i in g.issues if i.severity == "error"]
            other = [i for i in errs if i.code not in NUMBER_DATE_CODES]
            numdate = [i for i in errs if i.code in NUMBER_DATE_CODES]
            if other:
                ungrounded.append(m.slot_id)
                why_ungrounded.append(f"{m.slot_id}: {other[0].message}")
            if numdate:
                unsupported.append(m.slot_id)
                why_unsupported.append(f"{m.slot_id}: {numdate[0].message}")
        for i in validate_value(slot, m.filled_value):
            if i.severity != "error":
                continue
            target = limits if i.code == "max_chars" else formats if i.code in DATE_CODES else options
            target.append(f"{m.slot_id}: {i.message}")
        if m.status == "filled":  # an AI value may never tick an option that needs a human (協議中 + its date)
            options += [f"{m.slot_id}: {i.message}" for i in checkbox_hold(slot, m.filled_value)]
    n_filled = sum(1 for m in mappings if m.status == "filled" and m.is_written)
    n_human = sum(1 for m in mappings if m.status == "human_entered" and m.is_written)
    n_recovered = sum(1 for m in mappings if m.status == "recovered" and m.is_written)
    unverified = n_recovered > 0 and n_filled == 0 and not ungrounded and not unsupported
    if unverified:
        ok1 = NOT_VERIFIED + f" — {n_recovered} value(s) read back from the file"
        ok2 = NOT_VERIFIED
    else:
        ok1 = (f"{n_filled} values grounded" + (f"; {n_human} human-entered (attested by reviewer)" if n_human else "")
               + (f"; {n_recovered} recovered without provenance (not verified)" if n_recovered else ""))
        ok2 = "all numbers/dates supported"
    sev = "info" if unverified else "critical"
    checks.append(_check("GRD-01", "grounding", "Every AI-filled value is backed by verbatim quotes found in the memo",
                         not ungrounded, ok1 if not ungrounded else "ungrounded: " + "; ".join(why_ungrounded[:6]),
                         sev, ungrounded=ungrounded, not_verified=unverified))
    checks.append(_check("GRD-02", "grounding", "Numbers and dates in values appear in the quoted evidence", not unsupported,
                         ok2 if not unsupported else "unsupported: " + "; ".join(why_unsupported[:6]), sev,
                         unsupported=unsupported, not_verified=unverified))
    checks.append(_check("CON-01", "constraints", "Character limits printed on the form are respected", not limits,
                         "all within limits" if not limits else "; ".join(limits), "major"))
    checks.append(_check("CON-02", "constraints", "Formats (Japanese era dates, yen amounts, ID patterns) are valid", not formats,
                         "all formats valid" if not formats else "; ".join(formats), "major"))
    checks.append(_check("CON-03", "constraints", "Options, caps and rounding rules are respected", not options,
                         "all options / caps / rounding valid" if not options else "; ".join(options), "major"))
    stats["ungrounded"] = len(ungrounded)


def _app_check(file_type, blob, checks, expected_cells=()):
    """APP-01: an independent OOXML library opens the file; for .xlsx, openpyxl must also read every slot cell back as
    exactly the expected text (a cell another reader sees differently fails here, whatever our own index says)."""
    wrong: list[str] = []
    try:
        if file_type == "docx":
            import docx

            d = docx.Document(io.BytesIO(blob))
            detail = f"python-docx opened it: {len(d.paragraphs)} paragraphs, {len(d.tables)} tables"
        else:
            import openpyxl

            wb = openpyxl.load_workbook(io.BytesIO(blob))

            def norm(v):
                return None if v is None or v == "" else v

            for sheet, ref, exp, sid in expected_cells:
                got = wb[sheet][ref].value if sheet in wb.sheetnames else None
                if norm(got) != norm(exp):
                    wrong.append(f"{sid}: openpyxl reads {sheet}!{ref} as «{got}», expected «{exp}»")
            detail = f"openpyxl opened it: sheets {wb.sheetnames}" + (
                f"; {len(expected_cells)} slot cell(s) read back as the approved text" if expected_cells else "")
        ok = not wrong
        if wrong:
            detail = "; ".join(wrong[:6])
    except Exception as exc:  # noqa: BLE001 - any failure here is the finding
        ok, detail = False, f"failed to open: {exc}"
    checks.append(_check("APP-01", "application", "File opens in an independent OOXML library", ok, detail, "critical",
                         read_back=wrong[:20]))


def _finish(scan, checks, entry_hashes, mappings, pending, stats, subject) -> VerificationReport:
    def ok(prefix):
        items = [c for c in checks if c.check_id.startswith(prefix)]
        return bool(items) and all(c.passed for c in items)

    failed = [c for c in checks if not c.passed]
    hard_fail = any(c.severity in ("critical", "major") for c in failed)
    status = "FAIL" if hard_fail else ("NEEDS_HUMAN_INPUT" if pending else "PASS")
    report = VerificationReport(
        file_type=scan.file_type,  # type: ignore[arg-type]
        zip_integrity_passed=ok("ZIP"),
        xml_schema_wellformed=all(c.passed for c in checks if c.check_id in ("XML-01", "XML-02")),
        non_target_xml_unchanged=all(c.passed for c in checks if c.check_id in ("ZIP-04", "ZIP-05", "XML-03", "TEXT-01")),
        style_and_borders_unchanged=ok("LAYOUT"),
        all_slots_filled=not pending,
        ungrounded_claims_count=int(stats.get("ungrounded", 0)),
        checks=checks,
        overall_status=status,  # type: ignore[arg-type]
        passed_count=sum(1 for c in checks if c.passed),
        failed_count=len(failed),
        pending_slots=pending,
        entry_hashes=entry_hashes,
        stats=stats,
        subject=subject,
    )
    report.hitl_checklist = build_hitl_checklist(scan, mappings, report)
    return report


# ------------------------------------------------------------------------ HITL

_FLAG_TEXT = {
    "wareki": "和暦に変換 / converted to 和暦",
    "halfwidth": "半角に統一 / normalised to half-width",
    "yen": "金額表記を統一 / amount normalised",
    "condensed": "字数制限のため抽出要約 / condensed to fit",
    "re_extracted": "制約違反のため再抽出 / re-extracted after a constraint violation",
    "order": "選択肢を様式の順に整列 / options reordered",
    "name": "敬称・注記を除去 / annotations removed",
    "note": "注記を除去 / trailing note removed",
    "year_inferred": "年は本文から推定 / year inferred",
    "weekday_ok": "曜日一致を確認 / weekday consistent",
    "human": "人が入力 / entered by the reviewer",
    "low_conf": "信頼度低 / low confidence",
    "re_anchored": "引用を付け替え / quote re-anchored — check the wording",
    "unsafe_condensation": "要約で条件が欠ける / condensing would drop a condition — review",
    "weekday": "曜日を付記 / weekday added",
    "recovered_unverified": "出所不明・根拠未検証 / recovered from the file — grounding not verified",
    "advance_notice": "提出期限の注意事項を確認 / printed deadline not met by the dates — check them",
    "checkbox": "□/■ の記号を外し選択肢名に統一 / checkbox mark removed — the printed option is named",
}


def _quoted(pieces: list[str]) -> str:
    """「A」「B」「（金）」×2 — consecutive identical pieces are counted, not repeated."""
    out: list[list] = []
    for p in pieces:
        if out and out[-1][0] == p:
            out[-1][1] += 1
        else:
            out.append([p, 1])
    return "".join(f"「{p}」" + (f"×{n}" if n > 1 else "") for p, n in out)


def _removed_pieces(m: FieldMapping) -> list[str]:
    pieces = list(m.condensed_removed)
    if not pieces:
        for w in m.warnings:
            if w.startswith("condensed — removed: "):
                pieces += [x for x in w.removeprefix("condensed — removed: ").split(" ／ ") if x]
    return pieces


def removed_text_note(m: FieldMapping) -> str:
    """HITL disclosure of every piece deleted to meet a character limit (full text, never truncated)."""
    pieces = _removed_pieces(m)
    if not pieces:
        return ""
    return (f"（字数制限のため削除した文：{_quoted(pieces)}— 必要な条件や措置が欠けていないか確認 / "
            f"Condensed for char limit; confirm no essential condition was lost）")


def removed_text_notes(m: FieldMapping) -> tuple[str, str]:
    """``removed_text_note`` in Japanese and in English (the web UI shows the selected language)."""
    pieces = _removed_pieces(m)
    if not pieces:
        return "", ""
    return (f"（字数制限のため削除した文：{_quoted(pieces)}— 必要な条件や措置が欠けていないか確認）",
            f" (deleted to meet the character limit: {_quoted(pieces)} — confirm no essential condition or measure was lost)")


def advance_notice_notes(m: Optional[FieldMapping]) -> tuple[str, str, str]:
    """(recorded prompt, ja, en) notes for a printed deadline that two dates do not meet (mapper.advance_notice_warnings)."""
    notes = [w.removeprefix(ADVANCE_NOTICE_PREFIX).strip() for w in (m.warnings if m is not None else [])
             if w.startswith(ADVANCE_NOTICE_PREFIX)]
    pairs = [(en, ja) if sep else (w, w) for w in notes for en, sep, ja in [w.rpartition(" / ")]]
    return ("".join(f"（⚠ {ja} / {en}）" for en, ja in pairs), "".join(f"（⚠ {ja}）" for _en, ja in pairs),
            "".join(f" (⚠ {en})" for en, _ja in pairs))


def build_hitl_checklist(scan: ScanResult, mappings: list[FieldMapping], report: VerificationReport) -> list[HITLItem]:
    items: list[HITLItem] = []
    by_id = {m.slot_id: m for m in mappings}
    for s in scan.slots:
        m = by_id.get(s.slot_id)
        adv, adv_ja, adv_en = advance_notice_notes(m)
        if m is None or not m.is_written:
            if m is not None and m.suggested_value:
                reason = m.errors[0] if m.errors else "validation failed"
                # validator messages are "English / 日本語" (the Japanese half uses ／, an English one may list values
                # with " / "): each language shows its own half, and the «value»: it is about stays in front of both
                r_en, sep, r_ja = reason.rpartition(" / ")
                head = re.match(r"«[^»]*»: ", reason)
                if sep and head and not r_ja.startswith(head.group(0)):
                    r_ja = head.group(0) + r_ja
                prompt = (f"「{s.label}」は自動検証で確定できなかったため未記入です（{reason}）。候補を確認し、正しい値を"
                          f"入力してください / Could not be verified automatically — review the suggestion and enter "
                          f"the value yourself.")
                ja = (f"「{s.label}」は自動検証で確定できなかったため未記入です（{r_ja if sep else reason}）。候補を確認し、"
                      f"正しい値を入力してください")
                en = (f"«{s.label}» was left empty: it could not be verified automatically ({r_en if sep else reason}). "
                      f"Review the suggestion and enter the value yourself.")
            else:
                prompt = (f"「{s.label}」はメモに根拠が無いため未記入です。正しい値を入力してください / "
                          f"No evidence in the memo — please enter this value yourself.")
                ja = f"「{s.label}」はメモに根拠が無いため未記入です。正しい値を入力してください"
                en = f"«{s.label}» was left empty: the memo gives no evidence for it. Enter the value yourself."
            note_ja, note_en = removed_text_notes(m) if m is not None else ("", "")
            flags = ["needs_input"] + (["has_suggestion"] if m is not None and m.suggested_value else [])
            if m is not None and "unsafe_condensation" in m.normalizations:
                flags.append("unsafe_condensation")
            if adv:
                flags.append("advance_notice")
            items.append(HITLItem(
                item_id=f"slot:{s.slot_id}", kind="pending_input", slot_id=s.slot_id, label=s.label,
                prompt=prompt + (removed_text_note(m) if m is not None else "") + adv, prompt_ja=ja + note_ja + adv_ja,
                prompt_en=en + note_en + adv_en,
                value=(m.suggested_value if m else ""), importance=s.importance, flags=flags, required=True,
            ))
            continue
        flags = list(dict.fromkeys(m.normalizations))
        if m.status == "human_entered":
            flags.append("human")
        if any("year_inferred" in w or "年は推定" in w for w in m.warnings):
            flags.append("year_inferred")
        if m.confidence and m.confidence < 0.7:
            flags.append("low_conf")
        if adv:
            flags.append("advance_notice")
        lr = m.source_line_range
        where = f"L{lr[0]}" + (f"–L{lr[1]}" if lr and lr[1] != lr[0] else "") if lr else ""
        if m.status == "filled":
            prompt = (f"「{s.label}」＝「{m.filled_value}」が原文{(' ' + where) if where else ''}の記載と一致することを確認 / "
                      f"Confirm this value against the quoted source.")
            ja = f"「{s.label}」＝「{m.filled_value}」が原文{(' ' + where) if where else ''}の記載と一致することを確認"
            en = f"Confirm «{s.label}» = «{m.filled_value}» against the quoted source{(' ' + where) if where else ''}."
        elif m.status == "recovered":
            prompt = (f"「{s.label}」＝「{m.filled_value}」（ファイルから読み取った値。根拠は未検証）を確認 / "
                      f"Value read back from the file — its source was not verified.")
            ja = f"「{s.label}」＝「{m.filled_value}」（ファイルから読み取った値。根拠は未検証）を確認"
            en = f"Confirm «{s.label}» = «{m.filled_value}» — read back from the file; its source was not verified."
        else:
            prompt = f"「{s.label}」＝「{m.filled_value}」（担当者が入力）を確認 / Confirm the value you entered."
            ja = f"「{s.label}」＝「{m.filled_value}」（担当者が入力）を確認"
            en = f"Confirm «{s.label}» = «{m.filled_value}» (entered by the reviewer)."
        prompt += removed_text_note(m) + adv
        note_ja, note_en = removed_text_notes(m)
        ja, en = ja + note_ja + adv_ja, en + note_en + adv_en
        items.append(HITLItem(
            item_id=f"slot:{s.slot_id}", kind="field", slot_id=s.slot_id, label=s.label, value=m.filled_value,
            source_quote=m.source_quote, source_line_range=m.source_line_range, importance=s.importance,
            flags=flags, prompt=prompt, prompt_ja=ja, prompt_en=en, required=True,
        ))
    items.append(HITLItem(
        item_id="global:visual", kind="global", required=True,
        prompt="比較表示で、指定欄以外（罫線・結合セル・注意事項・誓約文）に変更が無いことを目視確認した / "
               "I reviewed the visual diff: nothing outside the designated fields changed.",
        prompt_ja="比較表示で、指定欄以外（罫線・結合セル・注意事項・誓約文）に変更が無いことを目視確認した",
        prompt_en="I reviewed the visual diff: nothing outside the designated fields changed.",
    ))
    items.append(HITLItem(
        item_id="global:responsibility", kind="global", required=True,
        prompt="記入内容の事実確認と提出の最終判断は自分（人間）が行う。AIの記入は下書きである / "
               "I am responsible for the final content and submission; AI output is only a draft.",
        prompt_ja="記入内容の事実確認と提出の最終判断は自分（人間）が行う。AIの記入は下書きである",
        prompt_en="I am responsible for the final content and submission; AI output is only a draft.",
    ))
    return items


def flag_label(flag: str) -> str:
    return _FLAG_TEXT.get(flag, flag)


def infer_values_from_filled(scan: ScanResult, filled_path: Union[str, Path]) -> dict[str, str]:
    """Recover the values written into a filled copy by matching each slot container against its template."""
    if scan.file_type == "pdf":
        from .pdf_support import infer_pdf_values

        return infer_pdf_values(scan, filled_path)
    from .constraints import CHECK_ON

    zf = zipfile.ZipFile(Path(filled_path))
    names = set(zf.namelist())
    values: dict[str, str] = {}
    for part, oidx in scan.parts.items():
        if part not in names:
            continue
        try:
            fidx = index_part(part, zf.read(part), oidx.profile)
        except (UnsafeXMLError, UnsupportedXMLError):
            continue
        if scan.file_type == "xlsx":
            cells = {o.cell_ref: (s.slot_id, o.marker if o.marker_kind == "date_template" else "")
                     for s in scan.slots for o in s.occurrences
                     if o.part_name == part and o.marker_kind in ("empty_cell", "date_template") and o.cell_ref}
            for ref, (sid, printed) in cells.items():  # designated worksheet cells: the inline string written into it
                fc = fidx.cells.get(ref)
                if fc is not None and fc.is_container is not None:
                    v = fidx.container_text(fidx.containers[fc.is_container])
                    if v and v != printed:  # a 「令和　年　月　日」 still printed as it was holds no value
                        values.setdefault(sid, v)
            if cells:  # match the marker containers with the designated cells put back as in the blank form
                fidx = index_part(part, _revert_sheet_cells(oidx, fidx, set(cells)), oidx.profile)
        if len(fidx.containers) != len(oidx.containers):
            continue
        empty_targets = {o.container_index: s.slot_id for s in scan.slots for o in s.occurrences
                         if o.part_name == part and o.marker_kind == "empty_cell" and scan.file_type == "docx"}
        tmpl = template_spans(scan, part)
        boxes = {o.container_index: (s.slot_id, o.checkbox_options) for s in scan.slots for o in s.occurrences
                 if o.part_name == part and o.marker_kind == "checkbox_text"}
        for co, cf in zip(oidx.containers, fidx.containers):
            to, tf = oidx.container_text(co), fidx.container_text(cf)
            if co.idx in empty_targets and tf:
                values.setdefault(empty_targets[co.idx], tf)
                continue
            if co.idx in tmpl:  # the text that took the place of the printed 「令和　　年　　月　　日」
                ts, te, sid = tmpl[co.idx]
                m = re.fullmatch(re.escape(to[:ts]) + "(.*?)" + re.escape(to[te:]), tf, re.S)
                if m and m.group(1) and m.group(1) != to[ts:te]:
                    values.setdefault(sid, m.group(1))
                continue
            if co.idx in boxes:  # the options whose □ became ■
                sid, opts = boxes[co.idx]
                ticked = [o["label"] for o in opts if o["offset"] < len(tf) and tf[o["offset"]] == CHECK_ON]
                if ticked:
                    values.setdefault(sid, "、".join(ticked))
                continue
            hits = find_markers(to)
            if not hits:
                continue
            pattern, cur, ids = "", 0, []
            for h in hits:
                pattern += re.escape(to[cur : h.start]) + "(.*?)"
                ids.append(h.slot_id)
                cur = h.end
            m = re.fullmatch(pattern + re.escape(to[cur:]), tf, re.S)
            if m:
                for sid, val, h in zip(ids, m.groups(), hits):
                    if val and val != h.text:
                        values.setdefault(sid, val)
    return values


# ------------------------------------------------------------- naive compare


def compare_naive(scan: ScanResult, naive_path: Union[str, Path], memo_text: str) -> VerificationReport:
    """Audit a from-scratch regeneration against the blank form (structure, boilerplate, invented numbers)."""
    if scan.file_type == "pdf":
        from .pdf_support import compare_naive_pdf

        return compare_naive_pdf(scan, naive_path, memo_text)
    naive_path = Path(naive_path)
    checks: list[AuditCheckItem] = []
    ob, nb = scan.path.read_bytes(), naive_path.read_bytes()
    zo, zn = zipfile.ZipFile(io.BytesIO(ob)), zipfile.ZipFile(io.BytesIO(nb))
    no, nn = set(zo.namelist()), set(zn.namelist())
    common = sorted(no & nn)
    changed = [n for n in common if zo.read(n) != zn.read(n)]
    style_parts = [n for n in ("word/styles.xml", "xl/styles.xml") if n in common]
    checks.append(_check("ZIP-04", "zip", "Non-target parts byte-identical", not changed and no == nn,
                         f"changed: {', '.join(changed[:8])}" + (f"; added {sorted(nn - no)}" if nn - no else "")
                         + (f"; removed {sorted(no - nn)}" if no - nn else ""), "critical",
                         changed=changed, style_parts_changed=[p for p in style_parts if p in changed]))
    stats: dict[str, Any] = {}
    if scan.file_type == "docx":
        so = docx_structure(zo.read("word/document.xml"))
        sn = docx_structure(zn.read("word/document.xml"))
        checks.append(_check("LAYOUT-01", "layout", "Table topology identical", False if (
            so["n_cells"], so["merges"]) != (sn["n_cells"], sn["merges"]) else True,
            f"cells {so['n_cells']}→{sn['n_cells']}, merged cells {so['merges']}→{sn['merges']}", "critical",
            original={"cells": so["n_cells"], "merges": so["merges"]}, filled={"cells": sn["n_cells"], "merges": sn["merges"]}))
        checks.append(_check("LAYOUT-02", "layout", "Borders and shading identical", so["borders"] == sn["borders"],
                             f"border definitions {so['borders']}→{sn['borders']}, shading {so['shading']}→{sn['shading']}",
                             "critical", original_borders=so["borders"], filled_borders=sn["borders"]))
        checks.append(_check("LAYOUT-04", "layout", "Page setup identical", so["sectPr"] == sn["sectPr"],
                             f"page {so['page']} → {sn['page']} twips", "major",
                             original_page=so["page"], filled_page=sn["page"]))
        orig_texts = [scan.parts[p].container_text(c) for p in scan.parts for c in scan.parts[p].containers]
        naive_texts = [t for t in _docx_texts(nb)]
    else:
        try:
            with zipfile.ZipFile(io.BytesIO(nb)) as z:
                check_xlsx_limits(z)
        except (UnsupportedXMLError, KeyError, etree.XMLSyntaxError) as exc:
            checks.append(_check("LAYOUT-01", "layout", "Merged ranges identical", False, f"not compared: {exc}", "critical"))
            return _finish_naive(scan, checks, stats)
        so, sn = xlsx_structure(ob), xlsx_structure(nb)
        mo, mn = sum(len(s["merged"]) for s in so["sheets"]), sum(len(s["merged"]) for s in sn["sheets"])
        bo, bn = sum(s["borders"] for s in so["sheets"]), sum(s["borders"] for s in sn["sheets"])
        fo, fn_ = sum(len(s["formulas"]) for s in so["sheets"]), sum(len(s["formulas"]) for s in sn["sheets"])
        vo, vn = sum(len(s["validations"]) for s in so["sheets"]), sum(len(s["validations"]) for s in sn["sheets"])
        checks.append(_check("LAYOUT-01", "layout", "Merged ranges identical", mo == mn, f"merged ranges {mo}→{mn}",
                             "critical", original={"merges": mo}, filled={"merges": mn}))
        checks.append(_check("LAYOUT-02", "layout", "Borders and cell styles identical", bo == bn,
                             f"border edges {bo}→{bn}", "critical", original_borders=bo, filled_borders=bn))
        checks.append(_check("LAYOUT-04", "layout", "Formulas and drop-down validations identical", (fo, vo) == (fn_, vn),
                             f"formulas {fo}→{fn_}, data validations {vo}→{vn}", "critical"))
        orig_texts = [scan.parts[p].container_text(c) for p in scan.parts for c in scan.parts[p].containers]
        naive_texts = _xlsx_texts(nb)
    altered = boilerplate_template_diff(orig_texts, naive_texts)
    checks.append(_check("TEXT-01", "text", "All boilerplate text unchanged", not altered,
                         f"{len(altered)} printed text blocks altered or missing" if altered else "all present",
                         "critical", altered=altered[:30]))
    invented = invented_numbers("\n".join(naive_texts), memo_text + "\n" + "\n".join(orig_texts))
    checks.append(_check("GRD-02", "grounding", "No numbers that are absent from both the memo and the blank form",
                         not invented, f"untraceable numbers: {', '.join(invented)}" if invented else "none", "critical",
                         invented=invented))
    guessed = guess_naive_fields(scan, naive_texts if scan.file_type == "xlsx" else _docx_table_pairs(nb))
    violations = []
    for sid, val in guessed.items():
        for i in validate_value(scan.slot(sid), val):
            if i.severity == "error":
                violations.append(f"{scan.slot(sid).label}: «{val[:24]}» — {i.message}")
    checks.append(_check("CON-02", "constraints", "Form constraints respected (best-effort label matching)", not violations,
                         f"{len(violations)} violations" if violations else "none found", "major", violations=violations))
    stats["altered_blocks"] = len(altered)
    stats["invented_numbers"] = invented
    return _finish_naive(scan, checks, stats)


def _finish_naive(scan, checks, stats) -> VerificationReport:
    failed = [c for c in checks if not c.passed]
    return VerificationReport(
        file_type=scan.file_type, zip_integrity_passed=all(c.passed for c in checks if c.category == "zip"),
        xml_schema_wellformed=True, non_target_xml_unchanged=False,
        style_and_borders_unchanged=all(c.passed for c in checks if c.category == "layout"),
        all_slots_filled=True, ungrounded_claims_count=len(stats.get("invented_numbers", [])), checks=checks,
        overall_status="FAIL" if failed else "PASS", passed_count=len(checks) - len(failed), failed_count=len(failed),
        stats=stats, subject="naive",
    )


def _docx_texts(blob: bytes) -> list[str]:
    zf = zipfile.ZipFile(io.BytesIO(blob))
    out = []
    from .ooxml import docx_story_parts

    for part in docx_story_parts(zf):
        idx = index_part(part, zf.read(part), "word")
        out += [idx.container_text(c) for c in idx.containers]
    return out


def _docx_table_pairs(blob: bytes) -> list[str]:
    import docx

    d = docx.Document(io.BytesIO(blob))
    out = []
    for t in d.tables:
        for row in t.rows:
            cells = [c.text for c in row.cells]
            if len(cells) >= 2:
                out.append(f"{cells[0]}\t{cells[1]}")
    for p in d.paragraphs:
        out.append(p.text)
    return out


def _xlsx_texts(blob: bytes) -> list[str]:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(blob))
    out = []
    for ws in wb.worksheets:
        rows: dict[int, dict[int, str]] = {}
        for c in ws._cells.values():  # sparse, like xlsx_structure
            if c.value is not None:
                rows.setdefault(c.row, {})[c.column] = str(c.value)
        lo = ws.min_column
        for r in sorted(rows):
            cols = rows[r]
            out.append("\t".join(cols.get(k, "") for k in range(lo, ws.max_column + 1)))
    return out


def boilerplate_template_diff(orig_texts: list[str], new_texts: list[str]) -> list[str]:
    """Original text blocks (markers as wildcards) that no block of the new file reproduces."""
    pool = "\n".join(new_texts)
    altered = []
    for t in orig_texts:
        core = t.strip()
        if not core or len(re.sub(r"\s", "", core)) < 2:
            continue
        parts = [re.escape(p) for p in re.split(r"\{\{[^{}]*\}\}|[\[［]\s*記入欄[^\]］]*[\]］]", core)]
        pattern = ".+?".join(parts) if len(parts) > 1 else parts[0]
        if not re.search(pattern, pool):
            altered.append(core)
    return altered


def invented_numbers(new_text: str, allowed_text: str) -> list[str]:
    have = quantities(allowed_text)
    out = []
    for txt, val in value_quantities(new_text):
        if val not in have and txt not in out:
            out.append(txt)
    return out


def guess_naive_fields(scan: ScanResult, lines: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in lines:
        if "\t" not in line:
            m = re.match(r"^\s*([^：:]{2,12})[：:]\s*(.+)$", line)
            if not m:
                continue
            label, value = m.group(1), m.group(2)
        else:
            label, value = line.split("\t", 1)
            value = value.split("\t")[0]
        label, value = label.strip(), value.strip()
        if not value:
            continue
        for s in scan.slots:
            keys = [s.slot_id, s.slot_id.split("_")[0]] + s.aliases
            if s.slot_id not in found and any(k and (k == label or (len(k) >= 3 and k in label)) for k in keys):
                found[s.slot_id] = value
                break
    return found
