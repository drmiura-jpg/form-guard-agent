"""Deterministic structural & content guard.

The verifier never trusts the writer. It re-opens the original blank form and the
filled file, re-indexes both independently and proves, check by check:

  ZIP   — same entries, same order, identical ZipInfo metadata; every untouched
          entry (styles.xml, numbering.xml, theme1.xml, …) byte-identical, both
          compressed stream and content (SHA-256).
  XML   — well-formed, namespace declarations preserved, and the *markup
          skeleton* (every tag and attribute, with text-node contents blanked)
          byte-identical: no border, merge, property or style reference moved.
  LAYOUT— table topology (tables/rows/cells, gridSpan, vMerge, tblGrid), borders
          & shading, paragraph/run formatting, page setup; for .xlsx merged
          ranges, widths/heights, formulas, data validations, cell styles.
  TEXT  — all boilerplate text identical; each slot holds exactly its value; no
          marker left except slots explicitly awaiting human input.
  GRD/CON — every written value backed by verbatim memo quotes, numbers/dates
          supported, character / format / option / cap constraints met.

It also produces the Human-in-the-Loop final confirmation checklist.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from lxml import etree

from .constraints import validate_value
from .grounding import ground_value
from .models import AuditCheckItem, EntryHash, FieldMapping, HITLItem, VerificationReport
from .ooxml import (
    W_NS,
    PartIndex,
    UnsafeXMLError,
    UnsupportedXMLError,
    canonical_text_node_token,
    ensure_safe_xml,
    index_part,
    iter_start_tags,
    xmlns_declarations,
)
from .scanner import ScanResult, find_markers, has_marker
from .textnorm import quantities, value_quantities

_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
W = f"{{{W_NS}}}"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _check(check_id, category, description, passed, details="", severity="major", **evidence) -> AuditCheckItem:
    return AuditCheckItem(check_id=check_id, category=category, description=description, passed=bool(passed),
                          details=details, severity=severity, evidence=evidence)


# ------------------------------------------------------------------- skeletons


def skeleton_bytes(idx: PartIndex, cut_containers: Iterable[int] = ()) -> bytes:
    """Markup with every text node's content blanked (and runs of designated empty cells removed)."""
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
    for n in idx.nodes:
        if n.container in cut:
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
        for row in ws.iter_rows():
            for c in row:
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
    filled_path = Path(filled_path)
    checks: list[AuditCheckItem] = []
    mappings = mappings or []
    values = {m.slot_id: m.filled_value for m in mappings if m.is_written}
    pending = [s.slot_id for s in scan.slots if s.slot_id not in values]
    orig_blob = scan.path.read_bytes()
    filled_blob = filled_path.read_bytes()
    stats: dict[str, Any] = {}

    # ---------------------------------------------------------------- ZIP
    entry_hashes: list[EntryHash] = []
    try:
        zo = zipfile.ZipFile(io.BytesIO(orig_blob))
        zf = zipfile.ZipFile(io.BytesIO(filled_blob))
        bad = zf.testzip()
        zip_ok = bad is None
        checks.append(_check("ZIP-01", "zip", "Archive opens and every entry passes its CRC-32 check", zip_ok,
                             "all CRCs valid" if zip_ok else f"corrupt entry: {bad}", "critical"))
    except zipfile.BadZipFile as exc:
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
    wellformed, ns_ok, skel_ok = True, True, True
    skel_details, ns_details, wf_details = [], [], []
    filled_parts: dict[str, PartIndex] = {}
    designated: dict[str, set[int]] = {}
    for s in scan.slots:
        for o in s.occurrences:
            if o.marker_kind == "empty_cell":
                designated.setdefault(o.part_name, set()).add(o.container_index)
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
            filled_parts[part] = index_part(part, data_f, profile)
        except (etree.XMLSyntaxError, UnsafeXMLError, UnsupportedXMLError) as exc:
            wellformed = False
            wf_details.append(f"{part}: {exc}")
            continue
        data_o = scan.parts[part].data
        if _ns_decls(data_o) != _ns_decls(data_f):
            ns_ok = False
            ns_details.append(part)
        so = skeleton_bytes(scan.parts[part], designated.get(part, ()))
        sf = skeleton_bytes(filled_parts[part], designated.get(part, ()))
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
    checks.append(_check(
        "XML-03", "xml", "Markup skeleton byte-identical outside slot text (every tag, attribute, property)",
        skel_ok and wellformed,
        (f"{tags_compared} tags and {n_nodes} text nodes compared; only slot characters differ" if skel_ok and wellformed
         else " | ".join(skel_details)), "critical", tags=tags_compared))
    stats["tags_compared"] = tags_compared

    # ------------------------------------------------------------- LAYOUT
    if scan.file_type == "docx":
        _docx_layout_checks(scan, zo, zf, checks, stats, designated)
    else:
        _xlsx_layout_checks(orig_blob, filled_blob, checks, stats)

    # --------------------------------------------------------------- TEXT
    _text_checks(scan, filled_parts, values, pending, designated, checks, stats)

    # ------------------------------------------------------- GRD / CON / APP
    _content_checks(scan, mappings, values, memo_text, checks, stats)
    _app_check(scan.file_type, filled_blob, checks)
    return _finish(scan, checks, entry_hashes, mappings, pending, stats, subject)


def _raw_map(blob: bytes) -> dict[str, str]:
    from .ziputil import ZipSurgeryError, read_raw_zip

    try:
        return {e.name: e.raw_sha256 for e in read_raw_zip(blob).entries}
    except ZipSurgeryError:
        return {}


def _meta(i: zipfile.ZipInfo) -> tuple:
    return (i.filename, i.date_time, i.compress_type, i.flag_bits, i.create_system, i.create_version,
            i.extract_version, i.external_attr, i.internal_attr, i.extra, i.comment)


def _docx_layout_checks(scan, zo, zf, checks, stats, designated):
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
    checks.append(_check("LAYOUT-03", "layout", "Paragraph and run formatting identical (pPr / rPr)", same_ppr and same_rpr,
                         f"{len(so['pPr'])} paragraphs checked" if same_ppr and same_rpr else
                         f"paragraph properties {'ok' if same_ppr else 'changed'}, run properties {'ok' if same_rpr else 'changed'}",
                         "major"))
    same_sect = so["sectPr"] == sf["sectPr"]
    checks.append(_check("LAYOUT-04", "layout", "Page setup identical (size, margins, header/footer references)", same_sect,
                         f"page {so['page']} twips — identical" if same_sect else f"page {so['page']} → {sf['page']}", "major",
                         original_page=so["page"], filled_page=sf["page"]))
    stats.update({"tables": len(so["tables"]), "rows": so["n_rows"], "cells": so["n_cells"], "merges": so["merges"],
                  "borders": so["borders"]})


def _xlsx_layout_checks(orig_blob, filled_blob, checks, stats):
    so, sf = xlsx_structure(orig_blob), xlsx_structure(filled_blob)
    pairs = list(zip(so["sheets"], sf["sheets"]))
    same_count = len(so["sheets"]) == len(sf["sheets"])
    merged_ok = same_count and all(a["merged"] == b["merged"] for a, b in pairs)
    n_m_o = sum(len(s["merged"]) for s in so["sheets"])
    n_m_f = sum(len(s["merged"]) for s in sf["sheets"])
    checks.append(_check("LAYOUT-01", "layout", "Sheet topology identical (sheets, merged ranges)", merged_ok,
                         f"{len(so['sheets'])} sheet(s), {n_m_o} merged ranges — identical" if merged_ok else
                         f"sheets {len(so['sheets'])}→{len(sf['sheets'])}, merged ranges {n_m_o}→{n_m_f}", "critical",
                         original={"merges": n_m_o}, filled={"merges": n_m_f}))
    style_ok = same_count and all(a["styles"].keys() == b["styles"].keys() and
                                  all(a["styles"][k] == b["styles"][k] for k in a["styles"]) for a, b in pairs)
    b_o = sum(s["borders"] for s in so["sheets"])
    b_f = sum(s["borders"] for s in sf["sheets"])
    checks.append(_check("LAYOUT-02", "layout", "Borders, fills, fonts and number formats identical for every cell",
                         style_ok, f"{b_o} cell border edges and all cell styles — identical" if style_ok else
                         f"border edges {b_o}→{b_f}; cell styles changed", "critical",
                         original_borders=b_o, filled_borders=b_f))
    dims_ok = same_count and all(a["widths"] == b["widths"] and a["heights"] == b["heights"] for a, b in pairs)
    checks.append(_check("LAYOUT-03", "layout", "Column widths and row heights identical", dims_ok,
                         "identical" if dims_ok else "changed", "major"))
    f_o = sum(len(s["formulas"]) for s in so["sheets"])
    f_f = sum(len(s["formulas"]) for s in sf["sheets"])
    logic_ok = same_count and all(a["formulas"] == b["formulas"] and a["validations"] == b["validations"]
                                  and a["page"] == b["page"] for a, b in pairs)
    v_o = sum(len(s["validations"]) for s in so["sheets"])
    v_f = sum(len(s["validations"]) for s in sf["sheets"])
    checks.append(_check("LAYOUT-04", "layout", "Formulas, drop-down validations and print setup identical", logic_ok,
                         f"{f_o} formulas, {v_o} data validations, print area — identical" if logic_ok else
                         f"formulas {f_o}→{f_f}, validations {v_o}→{v_f}", "critical",
                         original={"formulas": f_o, "validations": v_o}, filled={"formulas": f_f, "validations": v_f}))
    stats.update({"sheets": len(so["sheets"]), "merges": n_m_o, "borders": b_o, "formulas": f_o, "validations": v_o})


def _expected_text(text: str, values: dict[str, str]) -> str:
    out, cur = [], 0
    for h in find_markers(text):
        out.append(text[cur : h.start])
        out.append(values.get(h.slot_id, h.text))
        cur = h.end
    out.append(text[cur:])
    return "".join(out)


def _text_checks(scan, filled_parts, values, pending, designated, checks, stats):
    from .writer import prepare_value

    prepared = {k: prepare_value(v, scan.file_type) for k, v in values.items()}
    boiler_ok, slot_ok = True, True
    boiler_bad, slot_bad = [], []
    n_boiler = n_slot = 0
    residual = []
    for part, fidx in filled_parts.items():
        oidx = scan.parts[part]
        if len(oidx.containers) != len(fidx.containers):
            boiler_ok = slot_ok = False
            boiler_bad.append(f"{part}: container count {len(oidx.containers)}→{len(fidx.containers)}")
            continue
        dset = designated.get(part, set())
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
            if has_marker(to):
                n_slot += 1
                exp = _expected_text(to, prepared)
                if tf != exp:
                    slot_ok = False
                    slot_bad.append(f"«{to[:30]}» expected «{exp[:60]}» got «{tf[:60]}»")
            else:
                n_boiler += 1
                if tf != to:
                    boiler_ok = False
                    boiler_bad.append(f"«{to[:40]}» → «{tf[:40]}»")
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
                         "critical"))
    unexpected = sorted(set(residual) - set(pending))
    checks.append(_check("TEXT-03", "text", "No unreplaced markers except fields awaiting human input",
                         not unexpected, (f"pending human input: {', '.join(pending)}" if pending else "no markers left")
                         if not unexpected else f"unreplaced: {unexpected}", "critical", pending=pending))
    stats.update({"boilerplate_blocks": n_boiler, "slot_blocks": n_slot})


def _content_checks(scan, mappings, values, memo, checks, stats):
    ungrounded, unsupported, limits, formats, options = [], [], [], [], []
    by_id = {s.slot_id: s for s in scan.slots}
    for m in mappings:
        if not m.is_written or m.slot_id not in by_id:
            continue
        slot = by_id[m.slot_id]
        if m.status == "filled":
            quotes = [sp.text for sp in m.source_spans] or ([m.source_quote] if m.source_quote else [])
            g = ground_value(m.filled_value, quotes, memo, slot, near=[sp.start_char for sp in m.source_spans] or None)
            if g.ungrounded:
                ungrounded.append(m.slot_id)
            if any(i.code in ("number_unsupported", "date_unsupported") for i in g.issues):
                unsupported.append(m.slot_id)
        for i in validate_value(slot, m.filled_value):
            if i.severity != "error":
                continue
            {"max_chars": limits, "format": formats}.get(i.code, options).append(f"{m.slot_id}: {i.message}")
    n_filled = sum(1 for m in mappings if m.status == "filled" and m.is_written)
    n_human = sum(1 for m in mappings if m.status == "human_entered")
    checks.append(_check("GRD-01", "grounding", "Every AI-filled value is backed by verbatim quotes found in the memo",
                         not ungrounded, (f"{n_filled} values grounded" + (f"; {n_human} human-entered (attested by reviewer)" if n_human else ""))
                         if not ungrounded else f"ungrounded: {ungrounded}", "critical", ungrounded=ungrounded))
    checks.append(_check("GRD-02", "grounding", "Numbers and dates in values appear in the quoted evidence", not unsupported,
                         "all numbers/dates supported" if not unsupported else f"unsupported in: {unsupported}", "critical"))
    checks.append(_check("CON-01", "constraints", "Character limits printed on the form are respected", not limits,
                         "all within limits" if not limits else "; ".join(limits), "major"))
    checks.append(_check("CON-02", "constraints", "Formats (和暦 dates, yen amounts, ID patterns) are valid", not formats,
                         "all formats valid" if not formats else "; ".join(formats), "major"))
    checks.append(_check("CON-03", "constraints", "Options, caps and rounding rules are respected", not options,
                         "all options / caps / rounding valid" if not options else "; ".join(options), "major"))
    stats["ungrounded"] = len(ungrounded)


def _app_check(file_type, blob, checks):
    try:
        if file_type == "docx":
            import docx

            d = docx.Document(io.BytesIO(blob))
            detail = f"python-docx opened it: {len(d.paragraphs)} paragraphs, {len(d.tables)} tables"
        else:
            import openpyxl

            wb = openpyxl.load_workbook(io.BytesIO(blob))
            detail = f"openpyxl opened it: sheets {wb.sheetnames}"
        ok = True
    except Exception as exc:  # noqa: BLE001 - any failure here is the finding
        ok, detail = False, f"failed to open: {exc}"
    checks.append(_check("APP-01", "application", "File opens in an independent OOXML library", ok, detail, "critical"))


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
}


def build_hitl_checklist(scan: ScanResult, mappings: list[FieldMapping], report: VerificationReport) -> list[HITLItem]:
    items: list[HITLItem] = []
    by_id = {m.slot_id: m for m in mappings}
    for s in scan.slots:
        m = by_id.get(s.slot_id)
        if m is None or not m.is_written:
            items.append(HITLItem(
                item_id=f"slot:{s.slot_id}", kind="pending_input", slot_id=s.slot_id, label=s.label,
                prompt=f"「{s.label}」はメモに根拠が無いため未記入です。正しい値を入力してください / "
                       f"No evidence in the memo — please enter this value yourself.",
                value=(m.suggested_value if m else ""), importance=s.importance,
                flags=["needs_input"] + (["has_suggestion"] if m and m.suggested_value else []), required=True,
            ))
            continue
        flags = list(dict.fromkeys(m.normalizations))
        if m.status == "human_entered":
            flags.append("human")
        if any("year_inferred" in w or "年は推定" in w for w in m.warnings):
            flags.append("year_inferred")
        if m.confidence and m.confidence < 0.7:
            flags.append("low_conf")
        lr = m.source_line_range
        where = f"L{lr[0]}" + (f"–L{lr[1]}" if lr and lr[1] != lr[0] else "") if lr else ""
        prompt = (f"「{s.label}」＝「{m.filled_value}」が原文{(' ' + where) if where else ''}の記載と一致することを確認 / "
                  f"Confirm this value against the quoted source.") if m.status == "filled" else (
                  f"「{s.label}」＝「{m.filled_value}」（担当者が入力）を確認 / Confirm the value you entered.")
        items.append(HITLItem(
            item_id=f"slot:{s.slot_id}", kind="field", slot_id=s.slot_id, label=s.label, value=m.filled_value,
            source_quote=m.source_quote, source_line_range=m.source_line_range, importance=s.importance,
            flags=flags, prompt=prompt, required=True,
        ))
    items.append(HITLItem(
        item_id="global:visual", kind="global", required=True,
        prompt="比較表示で、指定欄以外（罫線・結合セル・注意事項・誓約文）に変更が無いことを目視確認した / "
               "I reviewed the visual diff: nothing outside the designated fields changed.",
    ))
    items.append(HITLItem(
        item_id="global:responsibility", kind="global", required=True,
        prompt="記入内容の事実確認と提出の最終判断は自分（人間）が行う。AIの記入は下書きである / "
               "I am responsible for the final content and submission; AI output is only a draft.",
    ))
    return items


def flag_label(flag: str) -> str:
    return _FLAG_TEXT.get(flag, flag)


def infer_values_from_filled(scan: ScanResult, filled_path: Union[str, Path]) -> dict[str, str]:
    """Recover the values written into a filled copy by matching each slot container against its template."""
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
        if len(fidx.containers) != len(oidx.containers):
            continue
        empty_targets = {o.container_index: s.slot_id for s in scan.slots for o in s.occurrences
                         if o.part_name == part and o.marker_kind == "empty_cell"}
        for co, cf in zip(oidx.containers, fidx.containers):
            to, tf = oidx.container_text(co), fidx.container_text(cf)
            if co.idx in empty_targets and tf:
                values.setdefault(empty_targets[co.idx], tf)
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
        for row in ws.iter_rows(values_only=True):
            vals = ["" if v is None else str(v) for v in row]
            if any(vals):
                out.append("\t".join(vals))
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
