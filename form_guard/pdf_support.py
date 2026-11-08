"""Fixed PDF forms: blank-box discovery, surgical overlay fill, the 21 deterministic checks, previews.

A fixed PDF form (a grant application, a municipal form printed to PDF) has no fields: people fill it by placing
text boxes by hand in a PDF editor. Form-Guard does that placement itself, without touching the original:

1. **Discovery** — the ruled lines of each page are rebuilt into a grid of boxes (stroked lines and rectangles, thin
   filled rules); printed text is assigned to the box it sits in. An empty box with a printed label on its left (or
   above it) is a candidate; a box printing ``□ 選択肢　□ 選択肢`` is a checkbox candidate. Labels, group labels and
   section banners decide applicant field vs office-only (事務局・受付印・受付番号・審査…). AcroForm fields are listed
   too, but never filled (they are interactive — a PDF viewer fills them).
2. **Surgical fill** — an *incremental update* is appended after the original ``%%EOF``: one overlay content stream
   per page (each value inside its box, in the page's own Unicode CID font, wrapped in marked content naming the
   slot), two one-byte ``q`` / ``Q`` streams, and a new revision of the page object whose ``/Contents`` becomes
   ``[q, <original streams>, Q, overlay]``. Every original byte stays in place — the blank PDF is an exact prefix of
   the filled one — and every key of the page dictionary except ``/Contents`` (``/MediaBox``, ``/Resources`` …) is
   spliced through byte for byte. A checkbox is ticked by painting ``■`` with the same font, size and origin as the
   printed ``□`` of the chosen option, so the printed square is covered exactly and nothing else changes.
3. **Verification** — :func:`verify_pdf` re-derives everything from the two files and maps the PDF onto the same 21
   check ids as Word / Excel (container, page content, layout, text, grounding, constraints, application), plus
   VIS-01 from the pipeline: a pixel diff of the rendered pages outside the slot boxes.

Coordinates are PDF user space: points, origin at the bottom-left of the page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from .constraints import CHECK_OFF, CHECK_ON, checkbox_constraints, parse_checkbox_options
from .models import AuditCheckItem, EntryHash, FieldMapping, PatchRecord, SlotOccurrence
from .pdf_core import (
    FontInfo,
    Glyph,
    IndirectObject,
    Name,
    PageGraphics,
    PageRef,
    PDFDocument,
    PDFError,
    Ref,
    Stream,
    TextRun,
    check_pdf,
    content_tokens,
    font_resources,
    incremental_update,
    interpret,
    parse_indirect_at,
    pdf_text,
    serialize,
    text_string,
)

PT_TO_PX = 96 / 72
SNAP = 1.2            # pt: ruled lines closer than this are one line; gaps smaller than this are closed
EDGE_TOL = 1.5        # pt: a box edge counts as ruled when a line covers it up to this much
MATCH_TOL = 1.0       # pt: a slot spec's rect must match a ruled box this closely
PADDING = 3.5         # pt: values are written this far inside their box
MIN_BOX = (12.0, 8.0)  # pt: smaller boxes are tick marks or decorations, not fields
MAX_FONT, MIN_FONT = 10.5, 6.5
OVERLAY_TAG = "FormGuard"
OVERLAY_COMMENT = b"% Form-Guard overlay: designated boxes only - incremental update, original page content untouched\n"
NO_LINE_START = set("、。，．,.）)」』】〕〉》！？!?ー…・：；ぁぃぅぇぉっゃゅょァィゥェォッャュョ％")
ALLOWED_OVERLAY_OPS = frozenset({"q", "Q", "BT", "ET", "Tf", "Tm", "Td", "Tj", "TJ", "g", "rg", "BDC", "BMC", "EMC"})


# ============================================================================ the form model


@dataclass
class Cell:
    idx: int
    page: int
    rect: tuple[float, float, float, float]
    table: int = -1
    glyphs: list[Glyph] = field(default_factory=list)
    lines: list[list[Glyph]] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join("".join(g.ch for g in ln) for ln in self.lines).strip()

    @property
    def empty(self) -> bool:
        return not any(g.ch.strip() for g in self.glyphs)

    @property
    def width(self) -> float:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> float:
        return self.rect[3] - self.rect[1]

    @property
    def box_id(self) -> str:
        return f"p{self.page}.c{self.idx}"

    def contains(self, x: float, y: float, tol: float = 0.0) -> bool:
        x0, y0, x1, y1 = self.rect
        return x0 - tol <= x <= x1 + tol and y0 - tol <= y <= y1 + tol


@dataclass
class PageModel:
    index: int
    ref: PageRef
    graphics: PageGraphics
    cells: list[Cell]
    tables: list[tuple[float, float, float, float]]
    n_streams: int
    contents: list[Optional[Ref]]
    fonts: dict[str, FontInfo]
    overlay_font: Optional[str]
    segments: tuple[list, list] = ((), ())

    @property
    def width(self) -> float:
        return self.ref.width

    @property
    def height(self) -> float:
        return self.ref.height


@dataclass
class PDFForm:
    path: Optional[Path]
    data: bytes
    doc: PDFDocument
    pages: list[PageModel]

    @property
    def title(self) -> str:
        info = self.doc.get(self.doc.trailer.get("Info"))
        t = pdf_text(info.get("Title")) if isinstance(info, dict) else ""
        return t.strip() or (self.path.stem if self.path else "form")

    @property
    def doc_text(self) -> str:
        return "\n".join(r.text for p in self.pages for r in p.graphics.runs)

    def cell(self, page: int, idx: int) -> Cell:
        return self.pages[page].cells[idx]


def _glyph_center(g: Glyph) -> tuple[float, float]:
    return (g.x + g.adv / 2, g.y + 0.38 * g.size)


def _cluster(values: list[float], tol: float) -> list[float]:
    groups: list[list[float]] = []
    for v in sorted(values):
        if groups and v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [round(sum(g) / len(g), 3) for g in groups]


def _snap(v: float, reps: list[float], tol: float) -> float:
    best = min(reps, key=lambda r: abs(r - v)) if reps else v
    return best if abs(best - v) <= tol else v


def _merge(intervals: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1] + tol:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def ruled_segments(g: PageGraphics, streams: Optional[set[int]] = None) -> tuple[list, list]:
    """Horizontal (y, x0, x1) and vertical (x, y0, y1) rules: stroked lines / rectangle edges and thin filled bars."""
    hs: list[tuple[float, float, float]] = []
    vs: list[tuple[float, float, float]] = []
    for p in g.paths:
        if streams is not None and p.stream not in streams:
            continue
        if p.stroked:
            for x0, y0, x1, y1 in p.segments:
                if abs(y0 - y1) <= 0.3 and abs(x1 - x0) >= 2.0:
                    hs.append(((y0 + y1) / 2, min(x0, x1), max(x0, x1)))
                elif abs(x0 - x1) <= 0.3 and abs(y1 - y0) >= 2.0:
                    vs.append(((x0 + x1) / 2, min(y0, y1), max(y0, y1)))
        elif p.filled:
            for x0, y0, x1, y1 in p.rects:
                w, h = x1 - x0, y1 - y0
                if h <= 2.0 and w >= 4.0:
                    hs.append(((y0 + y1) / 2, x0, x1))
                elif w <= 2.0 and h >= 4.0:
                    vs.append(((x0 + x1) / 2, y0, y1))
    return hs, vs


def build_cells(hs: list, vs: list, page: int) -> tuple[list[Cell], list[tuple[float, float, float, float]]]:
    """Rebuild the boxes of a ruled table: atomic grid cells merged across missing rules; a merged region is a box
    when it is rectangular and ruled all the way round."""
    if not hs or not vs:
        return [], []
    ys = _cluster([h[0] for h in hs], SNAP)
    xs = _cluster([v[0] for v in vs], SNAP)
    hlines: dict[int, list[tuple[float, float]]] = {}
    vlines: dict[int, list[tuple[float, float]]] = {}
    for y, a, b in hs:
        k = ys.index(_snap(y, ys, SNAP)) if _snap(y, ys, SNAP) in ys else None
        if k is not None:
            hlines.setdefault(k, []).append((_snap(a, xs, SNAP), _snap(b, xs, SNAP)))
    for x, a, b in vs:
        k = xs.index(_snap(x, xs, SNAP)) if _snap(x, xs, SNAP) in xs else None
        if k is not None:
            vlines.setdefault(k, []).append((_snap(a, ys, SNAP), _snap(b, ys, SNAP)))
    hmerged = {k: _merge(v, SNAP) for k, v in hlines.items()}
    vmerged = {k: _merge(v, SNAP) for k, v in vlines.items()}

    def covered(lines: list[tuple[float, float]], a: float, b: float) -> bool:
        return any(lo <= a + EDGE_TOL and hi >= b - EDGE_TOL for lo, hi in lines)

    def has_h(j: int, i: int) -> bool:  # horizontal rule at ys[j] over the atomic column i
        return covered(hmerged.get(j, []), xs[i], xs[i + 1])

    def has_v(i: int, j: int) -> bool:  # vertical rule at xs[i] over the atomic row j
        return covered(vmerged.get(i, []), ys[j], ys[j + 1])

    nx, ny = len(xs) - 1, len(ys) - 1
    if nx <= 0 or ny <= 0 or nx * ny > 40_000:
        return [], []
    parent = list(range(nx * ny))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for j in range(ny):
        for i in range(nx):
            if i + 1 < nx and not has_v(i + 1, j):
                union(j * nx + i, j * nx + i + 1)
            if j + 1 < ny and not has_h(j + 1, i):
                union(j * nx + i, (j + 1) * nx + i)
    groups: dict[int, list[tuple[int, int]]] = {}
    for j in range(ny):
        for i in range(nx):
            groups.setdefault(find(j * nx + i), []).append((i, j))
    cells: list[Cell] = []
    for members in groups.values():
        i0, i1 = min(m[0] for m in members), max(m[0] for m in members)
        j0, j1 = min(m[1] for m in members), max(m[1] for m in members)
        if len(members) != (i1 - i0 + 1) * (j1 - j0 + 1):
            continue  # not rectangular
        ruled = all(has_h(j0, i) and has_h(j1 + 1, i) for i in range(i0, i1 + 1)) and \
            all(has_v(i0, j) and has_v(i1 + 1, j) for j in range(j0, j1 + 1))
        if not ruled:
            continue  # open on one side: page margin, the gap between two tables
        rect = (xs[i0], ys[j0], xs[i1 + 1], ys[j1 + 1])
        if rect[2] - rect[0] < 2 or rect[3] - rect[1] < 2:
            continue
        cells.append(Cell(-1, page, tuple(round(v, 2) for v in rect)))  # type: ignore[arg-type]
    cells.sort(key=lambda c: (-c.rect[3], c.rect[0]))  # reading order: top to bottom, left to right
    for k, c in enumerate(cells):
        c.idx = k
    tables = _tables(cells)
    return cells, tables


def _touch(a: Cell, b: Cell) -> bool:
    ax0, ay0, ax1, ay1 = a.rect
    bx0, by0, bx1, by1 = b.rect
    share_v = (abs(ax1 - bx0) <= SNAP or abs(bx1 - ax0) <= SNAP) and min(ay1, by1) - max(ay0, by0) > SNAP
    share_h = (abs(ay1 - by0) <= SNAP or abs(by1 - ay0) <= SNAP) and min(ax1, bx1) - max(ax0, bx0) > SNAP
    return share_v or share_h


def _tables(cells: list[Cell]) -> list[tuple[float, float, float, float]]:
    """Connected groups of boxes sharing an edge; each cell gets its table index."""
    tid = 0
    for c in cells:
        if c.table >= 0:
            continue
        stack, c.table = [c], tid
        while stack:
            cur = stack.pop()
            for o in cells:
                if o.table < 0 and _touch(cur, o):
                    o.table = tid
                    stack.append(o)
        tid += 1
    boxes = []
    for t in range(tid):
        mine = [c for c in cells if c.table == t]
        boxes.append((min(c.rect[0] for c in mine), min(c.rect[1] for c in mine), max(c.rect[2] for c in mine),
                      max(c.rect[3] for c in mine)))
    return boxes


def assign_text(cells: list[Cell], runs: list[TextRun]) -> None:
    for r in runs:
        if r.invisible:
            continue
        for g in r.glyphs:
            cx, cy = _glyph_center(g)
            best = None
            for c in cells:
                if c.contains(cx, cy) and (best is None or c.width * c.height < best.width * best.height):
                    best = c
            if best is not None:
                best.glyphs.append(g)
    for c in cells:
        lines: list[list[Glyph]] = []
        for g in sorted(c.glyphs, key=lambda g: (-g.y, g.x)):
            if lines and abs(lines[-1][0].y - g.y) <= 0.45 * max(g.size, lines[-1][0].size):
                lines[-1].append(g)
            else:
                lines.append([g])
        c.lines = [sorted(ln, key=lambda g: g.x) for ln in lines]


def _overlay_font(fonts: dict[str, FontInfo], runs: list[TextRun]) -> Optional[str]:
    """The page font the overlay writes with: a Unicode-encoded CID font (UniJIS-UCS2-H …) of the page's own
    resources — the most used one — so /Resources never changes."""
    usable = [n for n, f in fonts.items() if f.unicode_encoding]
    if not usable:
        return None
    counts = {n: sum(len(r.glyphs) for r in runs if r.font == n) for n in usable}
    return sorted(usable, key=lambda n: (-counts[n], n))[0]


def load_form(source: Union[str, Path, bytes], doc: Optional[PDFDocument] = None) -> PDFForm:
    """Parse a PDF (through the gate), interpret every page and rebuild its ruled boxes."""
    path = None if isinstance(source, (bytes, bytearray)) else Path(source)
    data = bytes(source) if isinstance(source, (bytes, bytearray)) else Path(source).read_bytes()
    doc = doc or check_pdf(data)
    pages = []
    for p in doc.pages():
        if p.rotate != 0:
            raise PDFError(f"page {p.index + 1}: rotated pages (/Rotate {p.rotate}) are not supported — refused")
        if any(abs(a - b) > 0.5 for a, b in zip(p.cropbox, p.mediabox)):
            raise PDFError(f"page {p.index + 1}: /CropBox {p.cropbox} differs from /MediaBox {p.mediabox} — refused")
        streams = doc.page_streams(p)
        g = interpret([s.decoded() for _, s in streams], p.resources, doc, p.mediabox)
        if "clipping" in g.notes:
            raise PDFError(f"page {p.index + 1}: clipping paths (W / W*) are not supported — refused")
        hs, vs = ruled_segments(g)
        cells, tables = build_cells(hs, vs, p.index)
        assign_text(cells, g.runs)
        fonts = font_resources(doc, p.resources)
        pages.append(PageModel(p.index, p, g, cells, tables, len(streams), [r for r, _ in streams], fonts,
                               _overlay_font(fonts, g.runs), (hs, vs)))
    return PDFForm(path, data, doc, pages)


# ===================================================================== checkbox options


def _cell_options(cell: Cell, page: Optional[PageModel] = None) -> list[dict[str, Any]]:
    """Checkbox options of a box, each with the geometry of its printed square."""
    if page is not None:
        if any(r.rotated and any(cell.contains(*_glyph_center(g)) for g in r.glyphs) for r in page.graphics.runs):
            return []
    flat = [g for ln in cell.lines for g in ln]
    text = "".join(g.ch for g in flat)
    opts = parse_checkbox_options(text)
    for o in opts:
        g = flat[o["offset"]]
        if page is not None and g.font in page.fonts:
            f = page.fonts[g.font]
            exp_adv = f.width_of_unicode(g.ch) / 1000.0 * g.size
            if abs(g.adv - exp_adv) > 0.05 * g.size:
                return []
        o.update(x=round(g.x, 3), y=round(g.y, 3), size=round(g.size, 3), adv=round(g.adv, 3), font=g.font,
                 raw_mark=g.ch)
    return opts


# ============================================================================== discovery


@dataclass
class _PdfCand:
    cell: Cell
    label_cell: Cell
    direction: str
    label_text: str
    group: str
    section: str
    kind: str
    options: list[dict[str, Any]]


def _neighbors(cells: list[Cell], c: Cell) -> tuple[Optional[Cell], Optional[Cell]]:
    cx, cy = (c.rect[0] + c.rect[2]) / 2, (c.rect[1] + c.rect[3]) / 2
    left = [d for d in cells if d is not c and d.table == c.table and abs(d.rect[2] - c.rect[0]) <= SNAP
            and d.rect[1] - SNAP <= cy <= d.rect[3] + SNAP]
    above = [d for d in cells if d is not c and d.table == c.table and abs(d.rect[1] - c.rect[3]) <= SNAP
             and d.rect[0] - SNAP <= cx <= d.rect[2] + SNAP]
    return (left[0] if left else None), (above[0] if above else None)


def _banners(page: PageModel) -> list[Cell]:
    out = []
    for c in page.cells:
        t = page.tables[c.table] if 0 <= c.table < len(page.tables) else None
        if t and c.text and c.width >= 0.9 * (t[2] - t[0]):
            out.append(c)
    return out


def _section_of(page: PageModel, c: Cell, banners: list[Cell]) -> str:
    above = [b for b in banners if b.table == c.table and b.rect[1] >= c.rect[3] - SNAP and b is not c]
    return min(above, key=lambda b: b.rect[1]).text if above else ""


def _cell_has_image(page: PageModel, c: Cell) -> bool:
    return any(_overlap(c.rect, (im[0], im[1], im[2], im[3])) for im in page.graphics.images)


def _cell_has_acroform(fields: list[dict[str, Any]], c: Cell) -> bool:
    for f in fields:
        if f.get("page") in (None, c.page) and isinstance(f.get("rect"), list) and len(f["rect"]) == 4:
            if _overlap(c.rect, tuple(float(v) for v in f["rect"])):
                return True
    return False


def pdf_candidates(form: PDFForm) -> tuple[list[_PdfCand], list[dict[str, Any]]]:
    """Stage 1 + 3 for a PDF: empty boxes (and checkbox boxes) next to a printed label; rejected boxes with reasons."""
    from .scanner import _is_sentence

    found: list[_PdfCand] = []
    rejected: list[dict[str, Any]] = []
    fields = acroform_fields(form)
    for page in form.pages:
        banners = _banners(page)
        for c in page.cells:
            if c in banners:
                continue
            options = [] if c.empty else _cell_options(c, page)
            if not c.empty and not options:
                continue
            left, above = _neighbors(page.cells, c)
            label_cell, direction = None, ""
            if left is not None and left.text and not left.empty and not _cell_options(left, page):
                label_cell, direction = left, "left"
            elif above is not None and above.text and above not in banners and not _cell_options(above, page):
                label_cell, direction = above, "above"
            if label_cell is None or _is_sentence(label_cell.text):
                continue
            group = ""
            if direction == "left":
                gl, _ = _neighbors(page.cells, label_cell)
                if gl is not None and gl.text and not _is_sentence(gl.text) and not _cell_options(gl, page):
                    group = gl.text.split("\n")[0]
            cand = _PdfCand(c, label_cell, direction, label_cell.text, group, _section_of(page, c, banners),
                            "checkbox_text" if options else "box", options)
            if c.width < MIN_BOX[0] or c.height < MIN_BOX[1]:
                rejected.append({"location": _where(c), "evidence_label": cand.label_text, "page": c.page,
                                 "rect": list(c.rect), "merged_range": "",
                                 "reason": f"box smaller than {MIN_BOX[0]:.0f}×{MIN_BOX[1]:.0f} pt — not a fill target"})
                continue
            if _cell_has_image(page, c):
                rejected.append({"location": _where(c), "evidence_label": cand.label_text, "page": c.page,
                                 "rect": list(c.rect), "merged_range": "",
                                 "reason": "box overlaps an existing image — refusing to write over images"})
                continue
            if _cell_has_acroform(fields, c):
                rejected.append({"location": _where(c), "evidence_label": cand.label_text, "page": c.page,
                                 "rect": list(c.rect), "merged_range": "",
                                 "reason": "box overlaps an interactive AcroForm field — refusing to write over form fields"})
                continue
            if options and any(o["checked"] for o in options):
                rejected.append({"location": _where(c), "evidence_label": cand.label_text, "page": c.page,
                                 "rect": list(c.rect), "merged_range": "",
                                 "reason": "a checkbox option is already printed as ■ — never changed"})
                continue
            found.append(cand)
    return found, rejected


def _where(c: Cell) -> str:
    x0, y0, x1, y1 = c.rect
    return f"page {c.page + 1} › box ({x0:.1f}, {y0:.1f})–({x1:.1f}, {y1:.1f}) pt"


def acroform_fields(form: PDFForm) -> list[dict[str, Any]]:
    """Interactive AcroForm fields and page Widget annotations (listed for the reviewer; filled by a PDF viewer, never by the overlay)."""
    doc = form.doc
    af = doc.get(doc.root.get("AcroForm"))
    out: list[dict[str, Any]] = []
    page_of = {p.ref.num: p.index for p in form.pages}
    seen_rects: set[tuple] = set()

    def walk(ref: Any, prefix: str, depth: int) -> None:
        node = doc.get(ref)
        if not isinstance(node, dict) or depth > 16 or len(out) > 500:
            return
        name = pdf_text(node.get("T")) if node.get("T") is not None else ""
        full = f"{prefix}.{name}" if prefix and name else (name or prefix)
        kids = doc.get(node.get("Kids"))
        if isinstance(kids, list) and kids:
            for k in kids:
                walk(k, full, depth + 1)
            if node.get("Rect") is None:
                return
        rect = doc.get(node.get("Rect"))
        p = node.get("P")
        pg = page_of.get(p.num) if isinstance(p, Ref) else None
        rlist = [float(v) for v in rect] if isinstance(rect, list) and len(rect) == 4 else None
        key = (pg, tuple(round(v, 2) for v in rlist) if rlist else None, full)
        if key in seen_rects:
            return
        seen_rects.add(key)
        out.append({"field": full or "Widget", "type": str(node.get("FT") or node.get("Subtype") or ""),
                    "label": pdf_text(node.get("TU")) or full or "Widget",
                    "page": pg, "rect": rlist,
                    "value": pdf_text(node.get("V")) if isinstance(node.get("V"), bytes) else ""})

    if isinstance(af, dict):
        for f in doc.get(af.get("Fields")) or []:
            walk(f, "", 0)
    for p in form.pages:
        annots = doc.get(p.ref.dict.get("Annots"))
        if isinstance(annots, list):
            for a in annots:
                ad = doc.get(a)
                if isinstance(ad, dict) and (ad.get("Subtype") == "Widget" or ad.get("FT") is not None):
                    rect = doc.get(ad.get("Rect"))
                    rlist = [float(v) for v in rect] if isinstance(rect, list) and len(rect) == 4 else None
                    name = pdf_text(ad.get("T")) if ad.get("T") is not None else "Widget"
                    key = (p.index, tuple(round(v, 2) for v in rlist) if rlist else None, name)
                    if key not in seen_rects:
                        seen_rects.add(key)
                        out.append({"field": name, "type": str(ad.get("FT") or "Widget"), "label": pdf_text(ad.get("TU")) or name,
                                    "page": p.index, "rect": rlist,
                                    "value": pdf_text(ad.get("V")) if isinstance(ad.get("V"), bytes) else ""})
    return out


def discover_pdf(path: Union[str, Path], mode: str = "demo", client: Any = None,
                 model_tier: Optional[str] = "auto", expected_sha256: Optional[str] = None,
                 model: Optional[str] = None) -> dict[str, Any]:
    """Blank-box discovery for a fixed PDF — the same report format as ``scanner.discover_blank_cells``."""
    from collections import Counter
    from datetime import datetime, timezone

    from .constraints import infer_slot_constraints
    from .pdf_core import sha256
    from .scanner import (APPLICANT_REASON, OFFICE_REASON, _assign_slot_ids, _Cand, _classify_with_claude,
                          _label_parts, _office_hits)

    path = Path(path)
    form = load_form(path)
    actual_sha = sha256(form.data)
    if expected_sha256 and expected_sha256.strip().lower() != actual_sha.lower():
        raise ValueError(f"blank form SHA-256 mismatch: expected {expected_sha256.strip().lower()}, got {actual_sha}")
    found, rejected = pdf_candidates(form)
    n_found = len(found) + len(rejected)  # stage 1, before the stage-3 rejections and AcroForm fields are listed
    base_cands = []
    for pc in found:
        base, hints = _label_parts(pc.label_text)
        base_cands.append(_Cand("pdf", (pc.cell.page, pc.cell.idx), pc.label_text, base, hints, pc.group, pc.section,
                                pc.direction, _where(pc.cell), {"page": pc.cell.page, "rect": list(pc.cell.rect)}))
    slot_ids = _assign_slot_ids(base_cands)
    doc_text = form.doc_text
    base_counts = Counter(c.base for c in base_cands)
    candidates: list[dict[str, Any]] = []
    for i, (pc, c, sid) in enumerate(zip(found, base_cands, slot_ids), start=1):
        hits = _office_hits(c.label_text, c.section, c.group, *c.hints)
        labels = [c.label_text] + ([c.group] if c.group else [])
        allowed = [o["label"] for o in pc.options] or None
        inferred = infer_slot_constraints(sid, labels, c.hints, doc_text, allowed, None)
        if pc.options:
            inferred["required_format"], inferred["importance"] = "choice", "high"
            inferred["constraints"] = checkbox_constraints(pc.options)
        aliases = ([c.base] if base_counts[c.base] == 1 else []) + ([c.group + c.base, c.group] if c.group else [])
        candidates.append({
            "candidate_id": f"c{i:02d}", "slot_id": sid, "label": sid.replace("_", " "), "file_type": "pdf",
            "evidence_label": c.label_text, "group_label": c.group, "section": c.section,
            "label_direction": c.direction, "location": c.location, **c.coords, "box": pc.cell.box_id,
            "kind": pc.kind, "options": [{k: o[k] for k in ("label", "detail", "needs_input")} for o in pc.options],
            "merged_range": "", "office_only": bool(hits), "applicant_fillable": not hits, "recommended": not hits,
            "reason": OFFICE_REASON if hits else (APPLICANT_REASON if pc.kind == "box" else
                                                 "applicant field — printed checkbox options next to a printed label"),
            "category": "office_only" if hits else "applicant_fillable", "classified": True, "office_keywords": hits,
            "required_format": inferred["required_format"], "max_chars": inferred["max_chars"],
            "constraints": inferred["constraints"], "aliases": [a for a in dict.fromkeys(aliases) if a and a != sid],
            "classified_by": "rules",
            "safety": {"empty": pc.kind == "box", "formula": False, "merged_continuation": False,
                       "printed_text_untouched": True},
        })
    title = form.title
    engine = _classify_with_claude(candidates, title, mode, client, model_tier, model=model)
    live_usage = engine.pop("live_usage", None)
    fields = acroform_fields(form)
    for f in fields:
        rejected.append({"location": f"AcroForm field «{f['field']}»", "evidence_label": f["label"],
                         "page": f["page"], "rect": f["rect"], "merged_range": "",
                         "reason": "interactive AcroForm field — fill it in a PDF viewer; the overlay only writes into "
                                   "printed boxes"})
    n_office = sum(1 for c in candidates if c["office_only"])
    discovery = {
        "schema": "form-guard/blank-cell-discovery@1",
        "form": path.name, "form_title": title, "file_type": "pdf", "form_sha256": actual_sha,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engine": engine,
        "pdf": {"pages": [{"page": p.index, "mediabox": list(p.ref.mediabox), "boxes": len(p.cells),
                           "tables": len(p.tables), "overlay_font": p.overlay_font} for p in form.pages],
                "acroform_fields": fields, "version": form.doc.version},
        "stages": [
            {"stage": 1, "name": "heuristic candidate extraction (ruled boxes rebuilt from the page's lines)",
             "found": n_found},
            {"stage": 2, "name": "semantic classification & rule inference",
             "applicant_fillable": len(candidates) - n_office, "office_only": n_office, "classified_by": engine["label"]},
            {"stage": 3, "name": "deterministic safety verification", "passed": len(candidates), "rejected": len(rejected)},
            {"stage": 4, "name": "human pre-fill confirmation", "status": "awaiting human confirmation"},
        ],
        "candidates": candidates,
        "rejected": rejected,
        "requires_human_confirmation": True,
        "next_step": ("A human reviewer confirms the boxes to fill; build_confirmed_slot_spec() (CLI: --discovery FILE "
                      "--confirm c01,c02 | recommended) re-checks the blank PDF's SHA-256 and writes the slot spec."),
    }
    if live_usage is not None:
        discovery["live_usage"] = live_usage
    return discovery


def office_cells(form: PDFForm) -> list[tuple[Cell, list[str]]]:
    """Boxes the deterministic rules classify office-only (with the keywords that decided it)."""
    from .scanner import _label_parts, _office_hits

    out = []
    for pc in pdf_candidates(form)[0]:
        base, hints = _label_parts(pc.label_text)
        hits = _office_hits(pc.label_text, pc.section, pc.group, *hints)
        if hits:
            out.append((pc.cell, hits))
    return out


def find_cell(form: PDFForm, page: Any, rect: Any) -> Optional[Cell]:
    if isinstance(page, (bool, float)) or (isinstance(page, str) and not page.strip().isdigit()):
        return None
    try:
        pidx = int(page)
        if not (0 <= pidx < len(form.pages)):
            return None
        p = form.pages[pidx]
        r = [float(v) for v in rect]
    except (TypeError, ValueError, IndexError):
        return None
    if len(r) != 4:
        return None
    for c in p.cells:
        if all(abs(a - b) <= MATCH_TOL for a, b in zip(c.rect, r)):
            return c
    return None


def office_only_pdf_targets(path: Union[str, Path], spec: dict) -> list[str]:
    """Spec targets that designate an office-only box of the blank PDF (fresh deterministic discovery)."""
    from .scanner import _office_hits

    targets = [t for t in (spec.get("targets") or []) if isinstance(t, dict)]
    if not targets:
        return []
    form = load_form(path)
    office = {(c.page, c.idx): hits for c, hits in office_cells(form)}
    out = []
    for t in targets:
        c = find_cell(form, t.get("page", 0), t.get("rect") or [])
        if c is None:
            continue  # scan() reports it and never fills it
        hits = office.get((c.page, c.idx))
        if hits is None:  # not a discovery candidate: look at the printed text around the box
            page = form.pages[c.page]
            left, above = _neighbors(page.cells, c)
            texts = [x.text for x in (left, above) if x is not None] + [_section_of(page, c, _banners(page))]
            hits = _office_hits(*texts) or None
        if hits:
            out.append(f"{t.get('slot_id')} → page {c.page + 1} box {_where(c).split('box ')[-1]} ({', '.join(hits)})")
    return out


# =================================================================================== scan


def scan_pdf(path: Path, spec: dict):
    """ScanResult for a fixed PDF: the boxes designated in the spec (page + rect, optional kind checkbox_text)."""
    from .scanner import ScanResult, _Acc, _label_parts, _make_slot

    form = load_form(path)
    fields = acroform_fields(form)
    accs: dict[str, _Acc] = {}
    warnings: list[str] = []
    claimed: dict[tuple[int, int], str] = {}
    for tgt in spec.get("targets", []) or []:
        if not isinstance(tgt, dict):
            continue
        sid = str(tgt.get("slot_id", "")).strip()
        if "table" in tgt or "cell" in tgt:
            warnings.append(f"coordinate target {sid!r}: table/row/col or sheet/cell coordinates are for Word / Excel — "
                            f"use page + rect for a PDF form")
            continue
        if not sid or "rect" not in tgt:
            warnings.append(f"coordinate target {tgt!r}: slot_id, page and rect are required")
            continue
        c = find_cell(form, tgt.get("page", 0), tgt.get("rect"))
        if c is None:
            warnings.append(f"coordinate target {sid!r}: no ruled box at page {tgt.get('page', 0)} rect "
                            f"{tgt.get('rect')!r} (within {MATCH_TOL} pt)")
            continue
        page = form.pages[c.page]
        if _cell_has_image(page, c):
            warnings.append(f"coordinate target {sid!r}: the box overlaps an existing image — refusing to write over images")
            continue
        if _cell_has_acroform(fields, c):
            warnings.append(f"coordinate target {sid!r}: the box overlaps an interactive AcroForm field — refusing to write over form fields")
            continue
        kind = str(tgt.get("kind") or "box")
        options: list[dict[str, Any]] = []
        if kind == "checkbox_text":
            options = _cell_options(c, page)
            if not options:
                warnings.append(f"coordinate target {sid!r}: the box prints no □ options — not a checkbox")
                continue
            if any(o["checked"] for o in options):
                warnings.append(f"coordinate target {sid!r}: an option is already printed as ■ — refusing to change it")
                continue
        elif not c.empty:
            warnings.append(f"coordinate target {sid!r}: the box is not empty — refusing to write over printed text")
            continue
        if (c.page, c.idx) in claimed:
            warnings.append(f"coordinate target {sid!r}: the box is already designated for {claimed[(c.page, c.idx)]!r}")
            continue
        claimed[(c.page, c.idx)] = sid
        left, above = _neighbors(page.cells, c)
        label_cell = left if left is not None and left.text else above
        label_text = label_cell.text if label_cell is not None else ""
        section = _section_of(page, c, _banners(page))
        acc = accs.setdefault(sid, _Acc(sid))
        occ = SlotOccurrence(
            part_name=f"pdf/page{c.page + 1}", container_index=c.idx, marker="", page_index=c.page,
            marker_kind="checkbox_text" if options else "empty_cell", rect=list(c.rect), box_id=c.box_id,
            location_path=f"{_where(c)} (designated by page coordinates)", context_text=c.text,
            checkbox_options=options,
        )
        acc.occurrences.append(occ)
        base, hints = _label_parts(label_text) if label_text else ("", [])
        acc.labels += ([label_text] if label_text else []) + ([tgt["label"]] if tgt.get("label") else [])
        acc.hints += hints + ([section] if section else [])
        acc.context_label = acc.context_label or (label_text.split("\n")[0] if label_text else "")
        if options:
            acc.__dict__["allowed"] = [o["label"] for o in options]
            acc.__dict__["checkbox"] = options
    result = ScanResult(path, "pdf", [], {}, form.doc_text, spec, warnings)
    result.pdf = form
    result.slots = [_make_slot(a, "pdf") for a in accs.values()]
    return result


# ============================================================================ text layout


def text_width(text: str, font: FontInfo, size: float) -> float:
    return sum(font.width_of_unicode(ch) for ch in text) / 1000.0 * size


def wrap_text(value: str, width: float, font: FontInfo, size: float) -> list[str]:
    """Greedy line breaking that never drops or adds a character (the lines join back into ``value``)."""
    lines: list[str] = []
    cur = ""
    for ch in value:
        if cur and text_width(cur + ch, font, size) > width:
            if ch in NO_LINE_START and len(cur) > 1:  # kinsoku: never start a line with 、。） …
                lines.append(cur[:-1])
                cur = cur[-1] + ch
                continue
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def layout_value(value: str, rect: tuple[float, float, float, float], font: FontInfo
                 ) -> tuple[list[tuple[str, float, float]], float, bool]:
    """([(line, x, baseline y)], font size, fits) — the largest size from 10.5 pt down to 6.5 pt that fits the box."""
    x0, y0, x1, y1 = rect
    aw, ah = x1 - x0 - 2 * PADDING, y1 - y0 - 2 * PADDING
    size = MAX_FONT
    lines: list[str] = [value]
    fits = False
    while size >= MIN_FONT - 1e-9:
        if text_width(value, font, size) <= aw and size <= ah:
            lines, fits = [value], True
            break
        lines = wrap_text(value, aw, font, size)
        lead = size * 1.35
        if size + (len(lines) - 1) * lead <= ah and all(text_width(ln, font, size) <= aw + 1e-6 for ln in lines):
            fits = True
            break
        size = round(size - 0.5, 2)
    size = max(size, MIN_FONT)
    if len(lines) == 1:
        y = (y0 + y1) / 2 - 0.38 * size
        return [(lines[0], x0 + PADDING, y)], size, fits
    lead = size * 1.35
    y = y1 - PADDING - 0.88 * size
    out = []
    for ln in lines:
        out.append((ln, x0 + PADDING, y))
        y -= lead
    return out, size, fits


# ================================================================================ writing


def _hexstr(b: bytes) -> bytes:
    return b"<" + b.hex().upper().encode("ascii") + b">"


def _show(font: str, size: float, x: float, y: float, encoded: bytes) -> bytes:
    from .pdf_core import fmt_num

    return (b"BT " + serialize(Name(font)) + b" " + fmt_num(round(size, 2)) + b" Tf 0 g 1 0 0 1 "
            + fmt_num(round(x, 3)) + b" " + fmt_num(round(y, 3)) + b" Tm " + _hexstr(encoded) + b" Tj ET\n")


def selected_options(value: str, options: list[dict[str, Any]]) -> list[str]:
    """Option labels named by a value (☑ / ■ / □ prefixes and separators ignored), in printed order."""
    from .constraints import normalize_checkbox_value

    labels = [o["label"] for o in options]
    picked = set(normalize_checkbox_value(value, labels))
    return [lab for lab in labels if lab in picked]


@dataclass
class OverlayBlock:
    slot_id: str
    kind: str
    start: int            # bytes of the marked-content block inside the overlay stream
    end: int
    text: str
    old_mark: str = CHECK_OFF


def build_overlay(page: PageModel, items: list[tuple[Any, SlotOccurrence, str]]) -> tuple[bytes, list[OverlayBlock]]:
    """The overlay content stream of one page: one marked-content block per slot, inside q … Q."""
    if page.overlay_font is None:
        raise PDFError(f"page {page.index + 1}: no Unicode CID font (UniJIS-UCS2-H …) in the page resources for the "
                       f"overlay to reuse — Form-Guard never changes a page's /Resources, so this PDF is refused")
    font = page.fonts[page.overlay_font]
    out = bytearray(OVERLAY_COMMENT + b"q\n")
    blocks: list[OverlayBlock] = []
    for slot, occ, value in items:
        start = len(out)
        out += b"/" + OVERLAY_TAG.encode() + b" <</Slot " + text_string(slot.slot_id) + b">> BDC\n"
        old_mark = CHECK_OFF
        if occ.marker_kind == "checkbox_text":
            chosen = selected_options(value, occ.checkbox_options)
            marks_used = []
            for o in occ.checkbox_options:
                if o["label"] not in chosen:
                    continue
                marks_used.append(str(o.get("raw_mark") or CHECK_OFF))
                fname = o["font"] if o.get("font") in page.fonts and page.fonts[o["font"]].unicode_encoding \
                    else page.overlay_font
                out += _show(fname, o["size"], o["x"], o["y"], page.fonts[fname].encode(CHECK_ON))
            text = "、".join(chosen)
            if marks_used:
                old_mark = marks_used[0]
        else:
            lines, size, _fits = layout_value(value, tuple(occ.rect), font)  # type: ignore[arg-type]
            for ln, x, y in lines:
                out += _show(page.overlay_font, size, x, y, font.encode(ln))
            text = value
        out += b"EMC\n"
        blocks.append(OverlayBlock(slot.slot_id, occ.marker_kind, start, len(out), text, old_mark))
    out += b"Q\n"
    return bytes(out), blocks


def _stream_body(data: bytes) -> bytes:
    return serialize({"Length": len(data)}) + b"\nstream\n" + data + b"\nendstream"


def _contents_value(refs: list[Ref]) -> bytes:
    return b"[" + b" ".join(f"{r.num} {r.gen} R".encode() for r in refs) + b"]"


def splice_page(doc: PDFDocument, page_num: int, contents: bytes) -> bytes:
    """The page dictionary exactly as written in the file, with only the /Contents value replaced."""
    obj = doc.indirect(page_num)
    if obj is None or obj.dict_span is None:
        raise PDFError(f"page object {page_num} cannot be located")
    a, b = obj.dict_span
    raw = doc.data[a:b]
    if "Contents" in obj.dict_spans:
        s, e = obj.dict_spans["Contents"]
        return doc.data[a:s] + contents + doc.data[e:b]
    return raw[:-2].rstrip() + b" /Contents " + contents + b" >>"


def write_pdf(scan, values: dict[str, str], out_path: Union[str, Path]):
    """Write a filled copy: the blank PDF bytes followed by one incremental update with the overlay."""
    from .writer import WriteResult, prepare_value

    out_path = Path(out_path)
    if out_path.resolve() == Path(scan.path).resolve() or (
            out_path.exists() and Path(scan.path).exists() and out_path.samefile(scan.path)):
        raise ValueError("output must differ from the blank template")
    form: PDFForm = scan.pdf if scan.pdf is not None else load_form(scan.path)
    known = {s.slot_id for s in scan.slots}
    prepared = {k: prepare_value(v, "pdf") for k, v in values.items() if v is not None and str(v).strip() and k in known}
    per_page: dict[int, list[tuple[Any, SlotOccurrence, str]]] = {}
    for s in scan.slots:
        if s.slot_id not in prepared:
            continue
        for o in s.occurrences:
            per_page.setdefault(int(o.page_index or 0), []).append((s, o, prepared[s.slot_id]))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pending = [s.slot_id for s in scan.slots if s.slot_id not in prepared]
    if not per_page:
        out_path.write_bytes(form.data)
        return WriteResult(out_path=out_path, patches=[], modified_parts=[], written_slots=[], pending_slots=pending,
                           values=prepared)
    doc = form.doc
    next_num = max([int(doc.trailer.get("Size") or 0)] + [n + 1 for n in doc.xref])
    objects: dict[int, bytes] = {}
    plan: list[tuple[int, int, list[OverlayBlock]]] = []
    for pidx in sorted(per_page):
        page = form.pages[pidx]
        overlay, blocks = build_overlay(page, per_page[pidx])
        q_ref, qq_ref, ov_ref = Ref(next_num), Ref(next_num + 1), Ref(next_num + 2)
        next_num += 3
        objects[q_ref.num], objects[qq_ref.num] = _stream_body(b"q\n"), _stream_body(b"Q\n")
        objects[ov_ref.num] = _stream_body(overlay)
        if any(r is None for r in page.contents):
            raise PDFError(f"page {pidx + 1}: a content stream is not an indirect object")
        refs = [q_ref] + [r for r in page.contents if r is not None] + [qq_ref, ov_ref]
        objects[page.ref.num] = splice_page(doc, page.ref.num, _contents_value(refs))
        plan.append((pidx, ov_ref.num, blocks))
    blob, spans = incremental_update(form.data, doc, objects)
    out_path.write_bytes(blob)
    patches: list[PatchRecord] = []
    for pidx, ov_num, blocks in plan:
        head = blob.find(b"\nstream\n", spans[ov_num][0]) + len(b"\nstream\n")
        for blk in blocks:
            box = blk.kind == "checkbox_text"
            patches.append(PatchRecord(part_name=f"pdf/page{pidx + 1}", slot_id=blk.slot_id,
                                       kind="pdf_mark" if box else "pdf_text",
                                       byte_start=head + blk.start, byte_end=head + blk.end,
                                       old_text=blk.old_mark if box else "",
                                       new_text=f"{CHECK_ON} {blk.text}" if box else blk.text))
    return WriteResult(out_path=out_path, patches=patches, modified_parts=[f"pdf/page{p + 1}" for p in sorted(per_page)],
                       written_slots=sorted({p.slot_id for p in patches}), pending_slots=pending, values=prepared)


# ================================================================================ reading


@dataclass
class FilledPage:
    page: PageModel                       # the filled file's page (all streams interpreted)
    original_refs: list[Optional[Ref]]
    overlay_streams: set[int]             # stream indices that are not original content nor the q / Q wrappers
    wrapper_ok: bool
    contents_note: str


def _overlay_layout(orig: PageModel, filled: PDFDocument, fpage: PageRef) -> tuple[set[int], bool, str, list[Any]]:
    """Which content streams of the filled page are Form-Guard's overlay; whether /Contents is exactly
    [q, <original streams>, Q, overlay…]."""
    refs = [r for r, _ in filled.page_streams(fpage)]
    orig_refs = list(orig.contents)
    n = len(orig_refs)
    if refs == orig_refs:
        return set(), True, "unchanged", refs

    def is_stream(r: Any, payload: bytes) -> bool:
        s = filled.get(r)
        return isinstance(s, Stream) and s.dict.get("Filter") is None and s.raw == payload

    ok = (len(refs) >= n + 3 and is_stream(refs[0], b"q\n") and refs[1 : n + 1] == orig_refs
          and is_stream(refs[n + 1], b"Q\n"))
    overlay = set(range(n + 2, len(refs))) if ok else {i for i, r in enumerate(refs) if r not in orig_refs}
    note = ("[q, original stream(s), Q, overlay]" if ok else
            f"/Contents is {[str(r) for r in refs]} — expected q, {[str(r) for r in orig_refs]}, Q, then the overlay")
    return overlay, ok, note, refs


def read_overlay(orig_form: PDFForm, filled_blob: bytes) -> tuple[PDFDocument, dict[int, FilledPage]]:
    doc = PDFDocument(filled_blob)
    out: dict[int, FilledPage] = {}
    fpages = doc.pages()
    for p in fpages:
        if p.index >= len(orig_form.pages):
            continue
        orig = orig_form.pages[p.index]
        overlay, ok, note, refs = _overlay_layout(orig, doc, p)
        streams = doc.page_streams(p)
        g = interpret([s.decoded() for _, s in streams], p.resources, doc, p.mediabox)
        fonts = font_resources(doc, p.resources)
        model = PageModel(p.index, p, g, orig.cells, orig.tables, len(streams), refs, fonts, orig.overlay_font)
        out[p.index] = FilledPage(model, list(orig.contents), overlay, ok, note)
    return doc, out


def overlay_runs(fp: FilledPage) -> list[TextRun]:
    return [r for r in fp.page.graphics.runs if r.stream in fp.overlay_streams]


def run_slot(r: TextRun) -> Optional[str]:
    for tag, props in reversed(r.tags):
        if tag == OVERLAY_TAG and isinstance(props, dict) and props.get("Slot") is not None:
            return pdf_text(props.get("Slot"))
    return None


def infer_pdf_values(scan, filled_path: Union[str, Path]) -> dict[str, str]:
    """Read the values back out of a filled PDF's overlay (by the slot named in each marked-content block)."""
    form: PDFForm = scan.pdf if scan.pdf is not None else load_form(scan.path)
    try:
        _doc, pages = read_overlay(form, Path(filled_path).read_bytes())
    except PDFError:
        return {}
    by_slot: dict[str, list[TextRun]] = {}
    for fp in pages.values():
        for r in overlay_runs(fp):
            sid = run_slot(r)
            if sid:
                by_slot.setdefault(sid, []).append(r)
    values: dict[str, str] = {}
    for s in scan.slots:
        runs = by_slot.get(s.slot_id)
        if not runs:
            continue
        occ = s.occurrences[0]
        if occ.marker_kind == "checkbox_text":
            picked = [o["label"] for o in occ.checkbox_options
                      if any(_mark_at(r, o) for r in runs)]
            if picked:
                values[s.slot_id] = "、".join(picked)
            continue
        ordered = sorted(runs, key=lambda r: (-round(r.glyphs[0].y, 1), r.glyphs[0].x))
        values[s.slot_id] = "".join(r.text for r in ordered)
    return values


def _mark_at(r: TextRun, o: dict[str, Any], tol: float = 0.05) -> bool:
    return any(g.ch == CHECK_ON and abs(g.x - o["x"]) <= tol and abs(g.y - o["y"]) <= tol and abs(g.size - o["size"]) <= tol
               for g in r.glyphs)


# ============================================================================ verification


def _rect_contains(outer: tuple, inner: tuple, tol: float = 0.25) -> bool:
    return (inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol and inner[2] <= outer[2] + tol
            and inner[3] <= outer[3] + tol)


def _overlap(a: tuple, b: tuple) -> bool:
    return min(a[2], b[2]) - max(a[0], b[0]) > 0.3 and min(a[3], b[3]) - max(a[1], b[1]) > 0.3


def _glyph_box(g: Glyph) -> tuple[float, float, float, float]:
    return (g.x, g.y - 0.12 * g.size, g.x + g.adv, g.y + 0.88 * g.size)


def _grid_signature(g: PageGraphics, streams: Optional[set[int]] = None) -> tuple:
    hs, vs = ruled_segments(g, streams)
    return (tuple(sorted((round(y, 1), round(a, 1), round(b, 1)) for y, a, b in hs)),
            tuple(sorted((round(x, 1), round(a, 1), round(b, 1)) for x, a, b in vs)))


def _forward_scan(blob: bytes) -> dict[int, list[int]]:
    """Every ``N G obj`` header found by reading the file front to back (no cross-reference table involved)."""
    from .pdf_core import _OBJ_HEAD_RE

    found: dict[int, list[int]] = {}
    for m in re.finditer(rb"(?:(?<=[\r\n])|^)(\d+)[ ]+(\d+)[ ]+obj\b", blob):
        try:
            obj = parse_indirect_at(blob, m.start(), lambda r: None)
        except PDFError:
            obj = None
        if obj is None and not _OBJ_HEAD_RE.match(blob, m.start()):
            continue
        found.setdefault(int(m.group(1)), []).append(m.start())
    return found


def verify_pdf(scan, filled_path: Union[str, Path], mappings: Optional[list[FieldMapping]] = None,
               memo_text: str = "", subject: str = "form_guard"):
    """The 21 deterministic checks for a filled fixed PDF (same ids and categories as Word / Excel)."""
    from .verifier import _check, _content_checks, _finish, _sha

    filled_path = Path(filled_path)
    mappings = mappings or []
    values = {m.slot_id: m.filled_value for m in mappings if m.is_written}
    pending = [s.slot_id for s in scan.slots if s.slot_id not in values]
    form: PDFForm = scan.pdf if scan.pdf is not None else load_form(scan.path)
    orig = form.data
    blob = filled_path.read_bytes()
    stats: dict[str, Any] = {"filled_sha256": _sha(blob), "pdf": True}
    checks: list[AuditCheckItem] = []
    entry_hashes: list[EntryHash] = []
    from .writer import prepare_value

    prepared = {k: prepare_value(v, "pdf") for k, v in values.items()}

    # ---------------------------------------------------------------- ZIP-01..05: PDF container
    try:
        fdoc = check_pdf(blob)
        fdoc.pages()
        n_obj = len(fdoc.in_use())
        checks.append(_check("ZIP-01", "zip", "PDF opens: header, every cross-reference section, trailer and object parse; "
                             "no encryption or active content", True,
                             f"PDF-{fdoc.version}, {len(fdoc.sections)} cross-reference section(s), {n_obj} objects, "
                             f"all offsets resolve", "critical", sections=len(fdoc.sections), objects=n_obj))
    except PDFError as exc:
        checks.append(_check("ZIP-01", "zip", "PDF opens: header, every cross-reference section, trailer and object parse; "
                             "no encryption or active content", False, str(exc), "critical"))
        return _finish(scan, checks, entry_hashes, mappings, pending, stats, subject)
    prefix = blob[: len(orig)] == orig
    pos = next((i for i in range(min(len(orig), len(blob))) if orig[i] != blob[i]), min(len(orig), len(blob)))
    checks.append(_check("ZIP-02", "zip", "The blank PDF is an exact byte prefix of the filled file (incremental update — "
                         "no original byte rewritten)", prefix,
                         f"all {len(orig):,} original bytes unchanged; {len(blob) - len(orig):,} bytes appended"
                         if prefix else f"original bytes changed from byte {pos:,} on", "critical",
                         original_bytes=len(orig), appended=len(blob) - len(orig)))
    odoc = form.doc
    n_orig_sections = len(odoc.sections)
    new_sections = fdoc.sections[n_orig_sections:] if prefix else fdoc.sections
    base_ok = prefix and len(fdoc.sections) >= n_orig_sections and all(
        a.offset == b.offset and a.entries == b.entries for a, b in zip(odoc.sections, fdoc.sections))
    chain_ok = base_ok and (len(new_sections) == 0 or new_sections[0].trailer.get("Prev") == odoc.startxref)
    trailer_ok = all(fdoc.trailer.get(k) == odoc.trailer.get(k) for k in ("Root", "Info"))
    checks.append(_check("ZIP-03", "zip", "Original cross-reference sections and trailer kept (same objects at the same "
                         "offsets, same /Root and /Info; the update chains to them through /Prev)",
                         chain_ok and trailer_ok and len(new_sections) <= 1,
                         (f"{n_orig_sections} original section(s) reproduced; {len(new_sections)} incremental update "
                          f"(/Prev {odoc.startxref})") if chain_ok and trailer_ok and len(new_sections) <= 1 else
                         (f"{len(new_sections)} update sections appended (expected at most 1)" if chain_ok and trailer_ok
                          else "the original cross-references / trailer (/Root, /Info) are not kept"), "major"))
    redefined: set[int] = set()
    freed: set[int] = set()
    for sec in new_sections:
        for num, e in sec.entries.items():
            if num == 0:
                continue
            if not e.in_use:
                freed.add(num)
            elif num in odoc.xref and odoc.xref[num].in_use:
                redefined.add(num)
    target_pages = {o.page_index for s in scan.slots for o in s.occurrences}
    target_nums = {form.pages[p].ref.num for p in target_pages if p is not None and p < len(form.pages)}
    unexpected = sorted(n for n in redefined if n not in target_nums)
    untouched = [n for n in odoc.in_use() if n not in redefined and n not in freed]
    for n in odoc.in_use():
        o = odoc.indirect(n)
        raw = orig[o.span[0] : o.span[1]] if o else b""
        role = "target" if n in redefined and n in target_nums else ("removed" if n in freed else "untouched")
        try:
            fo = fdoc.indirect(n)  # the latest definition in the filled file (the original one unless re-defined)
        except PDFError:
            fo = None
        fraw = blob[fo.span[0] : fo.span[1]] if fo is not None else b""
        entry_hashes.append(EntryHash(name=f"obj {n} ({_obj_kind(o.value) if o else '?'})", role=role,  # type: ignore[arg-type]
                                      original_sha256=_sha(raw), filled_sha256=_sha(fraw), identical=raw == fraw,
                                      raw_identical=raw == fraw, metadata_identical=True, size=len(fraw)))
    new_objs = sorted(n for sec in new_sections for n, e in sec.entries.items()
                      if e.in_use and (n not in odoc.xref or not odoc.xref[n].in_use))
    for n in new_objs:
        o = fdoc.indirect(n)
        entry_hashes.append(EntryHash(name=f"obj {n} (added: {_obj_kind(o.value) if o else '?'})", role="added",
                                      filled_sha256=_sha(blob[o.span[0] : o.span[1]]) if o else "",
                                      size=(o.span[1] - o.span[0]) if o else 0))
    kinds = sorted({_obj_kind(odoc.indirect(n).value) for n in untouched if odoc.indirect(n) is not None})
    checks.append(_check("ZIP-04", "zip", "Every non-target object byte-identical (catalog, page tree, fonts, resources, "
                         "content streams are never re-defined)", prefix and not unexpected and not freed,
                         (f"{len(untouched)} objects untouched incl. {', '.join(kinds)}" if prefix and not unexpected
                          and not freed else f"re-defined: {unexpected or '—'}; freed: {sorted(freed) or '—'}"),
                         "critical", changed=unexpected, freed=sorted(freed)))
    # new objects must be exactly what the overlay needs: q / Q wrappers and one overlay stream per modified page
    try:
        _fd, fpages = read_overlay(form, blob)
    except PDFError as exc:
        fpages = {}
        checks.append(_check("ZIP-05", "zip", "Only pages that hold designated boxes were modified", False,
                             f"the filled pages cannot be read: {exc}", "critical"))
    else:
        used = set()
        for fp in fpages.values():
            used |= {r.num for r in fp.page.contents if isinstance(r, Ref) and r.num in new_objs}
        stray = [n for n in new_objs if n not in used]
        modified_pages = sorted(p.index for p in form.pages if p.ref.num in redefined)
        ok5 = not stray and not unexpected and set(modified_pages) <= target_pages
        checks.append(_check("ZIP-05", "zip", "Only pages that hold designated boxes were modified; every added object "
                             "is part of their overlay", ok5,
                             (f"modified: {['page ' + str(p + 1) for p in modified_pages] or 'none'}; added objects "
                              f"{new_objs or 'none'} (q / Q wrappers + overlay)") if ok5 else
                             f"unexpected: re-defined {unexpected or '—'}, added but unused {stray or '—'}", "critical",
                             modified=[f"pdf/page{p + 1}" for p in modified_pages], added=new_objs))
    stats["entries"] = len(odoc.in_use())
    stats["modified_parts"] = [f"pdf/page{p.index + 1}" for p in form.pages if p.ref.num in redefined]

    # ---------------------------------------------------------------- XML-01..03: page content
    stream_issues, dict_issues, overlay_issues = [], [], []
    hashes = []
    office = office_cells(form)
    office_hits: list[str] = []
    slot_rects: dict[str, list[tuple[int, tuple]]] = {}
    occ_by_slot: dict[str, SlotOccurrence] = {}
    for s in scan.slots:
        for o in s.occurrences:
            slot_rects.setdefault(s.slot_id, []).append((int(o.page_index or 0), tuple(o.rect or ())))
            occ_by_slot.setdefault(s.slot_id, o)
    marks: dict[str, set[str]] = {}
    written_text: dict[str, list[TextRun]] = {}
    n_runs_overlay = 0
    for p in form.pages:
        fp = fpages.get(p.index)
        fref = next((fpage for fpage in fdoc.pages() if fpage.index == p.index), None)
        if fp is None or fref is None:
            stream_issues.append(f"page {p.index + 1} missing in the filled file")
            continue
        for k, (r, s) in enumerate(odoc.page_streams(p.ref)):
            fs = fdoc.get(r) if isinstance(r, Ref) else None
            raw_same = isinstance(fs, Stream) and fs.raw == s.raw and fs.dict == s.dict
            hashes.append(_sha(s.decoded()))
            if not raw_same:
                stream_issues.append(f"page {p.index + 1}: content stream {r} changed")
        if fp.original_refs and any(r not in fp.page.contents for r in fp.original_refs):
            stream_issues.append(f"page {p.index + 1}: original content stream(s) no longer referenced by /Contents")
        if p.ref.num in redefined:
            oobj, fobj = odoc.indirect(p.ref.num), fdoc.indirect(p.ref.num)
            ok_keys = _same_keys_except(orig, oobj, blob, fobj, "Contents")
            if ok_keys:
                dict_issues.append(f"page {p.index + 1}: {ok_keys}")
            if not fp.wrapper_ok:
                dict_issues.append(f"page {p.index + 1}: {fp.contents_note}")
        elif fp.overlay_streams:
            dict_issues.append(f"page {p.index + 1}: content streams added without a new page revision")
        # overlay content: allowed operators only, every glyph inside its slot's box
        for op_issue in _overlay_ops(fdoc, fp):
            overlay_issues.append(f"page {p.index + 1}: {op_issue}")
        if any(pa.stream in fp.overlay_streams for pa in fp.page.graphics.paths) or \
                any(im[4] in fp.overlay_streams for im in fp.page.graphics.images):
            overlay_issues.append(f"page {p.index + 1}: the overlay draws lines, boxes or images")
        printed = [g for r in p.graphics.runs for g in r.glyphs if g.ch.strip()]
        orig_images = [im[:4] for im in p.graphics.images]
        acro_rects = [tuple(float(v) for v in f["rect"]) for f in acroform_fields(form)
                      if f.get("page") in (None, p.index) and isinstance(f.get("rect"), list) and len(f["rect"]) == 4]
        for r in overlay_runs(fp):
            n_runs_overlay += 1
            sid = run_slot(r)
            if sid is None or sid not in slot_rects:
                overlay_issues.append(f"page {p.index + 1}: overlay text «{r.text[:20]}» is not inside a block of a "
                                      f"designated slot")
                continue
            if r.invisible or r.rotated or any(c > 0.25 for c in r.color):
                overlay_issues.append(f"{sid}: overlay text «{r.text[:20]}» is invisible, rotated or non-dark (color {r.color})")
                continue
            rects = [rc for pg, rc in slot_rects[sid] if pg == p.index]
            occ = occ_by_slot[sid]
            if occ.marker_kind == "checkbox_text":
                for gl in r.glyphs:
                    hit = [o["label"] for o in occ.checkbox_options if gl.ch == CHECK_ON and abs(gl.x - o["x"]) <= 0.05
                           and abs(gl.y - o["y"]) <= 0.05 and abs(gl.size - o["size"]) <= 0.05]
                    if not hit:
                        overlay_issues.append(f"{sid}: a mark «{gl.ch}» at ({gl.x:.1f}, {gl.y:.1f}) does not cover a "
                                              f"printed □ of this checkbox")
                    marks.setdefault(sid, set()).update(hit)
                continue
            for gl in r.glyphs:
                gb = _glyph_box(gl)
                if not any(_rect_contains(rc, gb) for rc in rects):
                    overlay_issues.append(f"{sid}: «{gl.ch}» at ({gl.x:.1f}, {gl.y:.1f}) lies outside its box")
                    break
            for gl in r.glyphs:
                if any(_overlap(_glyph_box(gl), _glyph_box(pg)) for pg in printed):
                    overlay_issues.append(f"{sid}: overlay text covers printed text near ({gl.x:.1f}, {gl.y:.1f})")
                    break
                if any(_overlap(_glyph_box(gl), im) for im in orig_images):
                    overlay_issues.append(f"{sid}: overlay text covers an existing image near ({gl.x:.1f}, {gl.y:.1f})")
                    break
                if any(_overlap(_glyph_box(gl), ar) for ar in acro_rects):
                    overlay_issues.append(f"{sid}: overlay text overlaps an interactive AcroForm field near ({gl.x:.1f}, {gl.y:.1f})")
                    break
            written_text.setdefault(sid, []).append(r)
        for c, kw in office:
            if c.page != p.index:
                continue
            for r in overlay_runs(fp):
                if any(_overlap(_glyph_box(gl), c.rect) for gl in r.glyphs):
                    office_hits.append(f"page {p.index + 1}: overlay text inside the office-only box "
                                       f"«{_label_for(p, c)}» ({', '.join(kw)})")
                    break
    checks.append(_check("XML-01", "xml", "Original page content stream(s) byte-identical and still painted first "
                         "(raw bytes and decoded SHA-256)", not stream_issues,
                         (f"{len(hashes)} content stream(s) unchanged — SHA-256 " + ", ".join(h[:16] + "…" for h in hashes))
                         if not stream_issues else "; ".join(stream_issues[:6]), "critical", content_sha256=hashes))
    checks.append(_check("XML-02", "xml", "Page dictionaries preserved: every key except /Contents byte-identical "
                         "(/MediaBox, /Resources …); /Contents = q + original stream(s) + Q + overlay", not dict_issues,
                         (f"{len(stats['modified_parts'])} page dictionary revision(s): /MediaBox, /Resources and every "
                          f"other key spliced through byte for byte" if stats["modified_parts"] else
                          "no page dictionary re-defined") if not dict_issues else "; ".join(dict_issues[:6]), "critical"))
    checks.append(_check("XML-03", "xml", "Overlay restricted to the approved slot boxes (text operators only; every "
                         "glyph inside its box; checkbox marks exactly over a printed □)", not overlay_issues,
                         (f"{n_runs_overlay} overlay text run(s) checked glyph by glyph against "
                          f"{sum(len(v) for v in slot_rects.values())} designated box(es)") if not overlay_issues else
                         "; ".join(overlay_issues[:6]), "critical", designated_cell_issues=overlay_issues[:20]))
    stats["designated_cells"] = sum(len(v) for v in slot_rects.values())

    # ---------------------------------------------------------------- LAYOUT-01..04
    fpage_refs = fdoc.pages()
    same_count = len(fpage_refs) == len(form.pages)
    checks.append(_check("LAYOUT-01", "layout", "Page count and page tree identical", same_count and not unexpected,
                         f"{len(form.pages)} page(s), page tree unchanged" if same_count and not unexpected else
                         f"pages {len(form.pages)}→{len(fpage_refs)}", "critical",
                         original={"pages": len(form.pages)}, filled={"pages": len(fpage_refs)}))
    boxes_ok, box_notes = same_count, []
    for p, fpr in zip(form.pages, fpage_refs):
        if (p.ref.mediabox, p.ref.cropbox, p.ref.rotate) != (fpr.mediabox, fpr.cropbox, fpr.rotate):
            boxes_ok = False
            box_notes.append(f"page {p.index + 1}: MediaBox {p.ref.mediabox}→{fpr.mediabox}")
    page0 = form.pages[0].ref
    a4 = abs(page0.width - 595.28) < 0.6 and abs(page0.height - 841.89) < 0.6
    checks.append(_check("LAYOUT-02", "layout", "Page geometry identical (MediaBox, CropBox, Rotate)", boxes_ok,
                         (f"MediaBox {page0.width:.2f} × {page0.height:.2f} pt" + (" (A4)" if a4 else "") + " — identical")
                         if boxes_ok else "; ".join(box_notes), "critical",
                         original_page=[page0.width, page0.height]))
    grid_ok, n_rules = True, 0
    for p in form.pages:
        fp = fpages.get(p.index)
        if fp is None:
            grid_ok = False
            continue
        orig_sig = _grid_signature(p.graphics)
        filled_sig = _grid_signature(fp.page.graphics)
        n_rules += len(orig_sig[0]) + len(orig_sig[1])
        if orig_sig != filled_sig:
            grid_ok = False
    n_cells = sum(len(p.cells) for p in form.pages)
    checks.append(_check("LAYOUT-03", "layout", "Printed table grid identical (every ruled line and box re-derived from "
                         "the filled page)", grid_ok,
                         f"{n_rules} ruled segments, {n_cells} boxes in {sum(len(p.tables) for p in form.pages)} table(s) "
                         f"— identical" if grid_ok else "ruled lines or boxes differ", "major"))
    office_designated = [f"{s.slot_id}" for s in scan.slots for o in s.occurrences
                         if any(o.page_index == c.page and o.container_index == c.idx for c, _ in office)]
    lay4_ok = not office_hits and not office_designated
    checks.append(_check("LAYOUT-04", "layout", "Office-only boxes untouched (事務局・受付印・受付番号・審査…): nothing drawn "
                         "inside them and no slot designates one", lay4_ok,
                         (f"{len(office)} office-only box(es) locked: " + "、".join(_label_for(form.pages[c.page], c)
                                                                                     for c, _ in office))
                         if lay4_ok else "; ".join((office_hits + [f"{x} designates an office-only box"
                                                                    for x in office_designated])[:6]), "critical",
                         office_boxes=len(office)))
    stats.update({"pages": len(form.pages), "boxes": n_cells, "rules": n_rules, "merges": 0, "borders": n_rules,
                  "tags_compared": sum(len(p.graphics.operators) for p in form.pages)})

    # ---------------------------------------------------------------- TEXT-01..03
    boiler_issues = []
    n_blocks = 0
    for p in form.pages:
        fp = fpages.get(p.index)
        if fp is None:
            continue
        o_runs = [(r.text, round(r.glyphs[0].x, 2), round(r.glyphs[0].y, 2)) for r in p.graphics.runs]
        f_runs = [(r.text, round(r.glyphs[0].x, 2), round(r.glyphs[0].y, 2))
                  for r in fp.page.graphics.runs if r.stream not in fp.overlay_streams]
        n_blocks += len(o_runs)
        if o_runs != f_runs:
            changed = [t for t, *_ in o_runs if t not in {x for x, *_ in f_runs}]
            boiler_issues.append(f"page {p.index + 1}: printed text changed: {changed[:3]}")
    boiler_issues += [i for i in overlay_issues if "covers printed text" in i]
    checks.append(_check("TEXT-01", "text", "All printed text unchanged (labels, notes, checkbox options) — the overlay "
                         "covers no printed character except a ticked □", not boiler_issues,
                         f"{n_blocks} printed text runs identical" if not boiler_issues else "; ".join(boiler_issues[:6]),
                         "critical", altered=boiler_issues[:20]))
    slot_issues = []
    for s in scan.slots:
        occ = s.occurrences[0]
        exp = prepared.get(s.slot_id)
        if occ.marker_kind == "checkbox_text":
            want = set(selected_options(exp, occ.checkbox_options)) if exp else set()
            got = marks.get(s.slot_id, set())
            if want != got:
                slot_issues.append(f"{s.slot_id}: expected ■ on {sorted(want) or 'none'}, found ■ on {sorted(got) or 'none'}")
            continue
        runs = sorted(written_text.get(s.slot_id, []), key=lambda r: (-round(r.glyphs[0].y, 1), r.glyphs[0].x))
        got_text = "".join(r.text for r in runs)
        if (exp or "") != got_text:
            slot_issues.append(f"{s.slot_id}: expected «{(exp or '')[:40]}» got «{got_text[:40]}»")
    stray_slots = sorted({run_slot(r) or "?" for fp in fpages.values() for r in overlay_runs(fp)} - set(slot_rects))
    if stray_slots:
        slot_issues.append(f"overlay blocks for unknown slots: {stray_slots}")
    checks.append(_check("TEXT-02", "text", "Each slot box shows exactly its approved value (checkbox: ■ exactly on the "
                         "approved option)", not slot_issues and bool(scan.slots),
                         f"{len(scan.slots)} slot box(es) match the approved values" if not slot_issues and scan.slots else
                         "; ".join(slot_issues[:6]) or "0 fillable slots — designate the boxes (page + rect) in "
                         ".slots.json or run --discover-blanks and confirm them", "critical", slots=len(scan.slots)))
    unfilled = [s.slot_id for s in scan.slots if s.slot_id in prepared and s.slot_id not in written_text
                and s.slot_id not in marks]
    unexpected_pending = [sid for sid in unfilled if sid not in pending]
    pending_written = [sid for sid in pending if sid in written_text or sid in marks]
    ok3 = not unexpected_pending and not pending_written
    checks.append(_check("TEXT-03", "text", "No designated box left empty except fields awaiting human input",
                         ok3, (f"pending human input: {', '.join(pending)}" if pending else "every approved value written")
                         if ok3 else f"approved but not written: {unexpected_pending or '—'}; written while pending: "
                                     f"{pending_written or '—'}", "critical", pending=pending))
    stats.update({"boilerplate_blocks": n_blocks, "slot_blocks": len(scan.slots)})

    # ---------------------------------------------------------------- GRD / CON (shared) + checkbox rules on the file
    _content_checks(scan, mappings, values, memo_text, checks, stats)
    cb_issues = []
    for s in scan.slots:
        occ = s.occurrences[0]
        if occ.marker_kind != "checkbox_text":
            continue
        got = marks.get(s.slot_id, set())
        if s.constraints.get("exclusive", True) and len(got) > 1:
            cb_issues.append(f"{s.slot_id}: {len(got)} options ticked (■ on {'、'.join(sorted(got))}) — only one is allowed")
    if cb_issues:
        _fail_con03(checks, cb_issues)
    _app_check_pdf(blob, fdoc, checks)
    report = _finish(scan, checks, entry_hashes, mappings, pending, stats, subject)
    return report


def _fail_con03(checks: list[AuditCheckItem], issues: list[str]) -> None:
    for c in checks:
        if c.check_id == "CON-03":
            c.passed = False
            c.details = "; ".join(([] if c.details.startswith("all options") else [c.details]) + issues)
            return


def _label_for(p: PageModel, c: Cell) -> str:
    left, above = _neighbors(p.cells, c)
    lab = left if left is not None and left.text else above
    return lab.text.split("\n")[0] if lab is not None else c.box_id


def _obj_kind(v: Any) -> str:
    if isinstance(v, Stream):
        t = v.dict.get("Type") or v.dict.get("Subtype")
        return f"{t} stream" if t else "content stream"
    if isinstance(v, dict):
        return str(v.get("Type") or v.get("Subtype") or "dictionary")
    return type(v).__name__


def _same_keys_except(orig: bytes, oobj: Optional[IndirectObject], blob: bytes, fobj: Optional[IndirectObject],
                      except_key: str) -> str:
    """'' if every top-level key of the two dictionaries (but ``except_key``) has byte-identical value bytes."""
    if oobj is None or fobj is None:
        return "page object missing"
    ok, fk = oobj.dict_spans, fobj.dict_spans
    keys = (set(ok) | set(fk)) - {except_key}
    diff = sorted(k for k in keys if k not in ok or k not in fk or orig[ok[k][0] : ok[k][1]] != blob[fk[k][0] : fk[k][1]])
    return f"keys changed: {['/' + k for k in diff]}" if diff else ""


def _overlay_ops(doc: PDFDocument, fp: FilledPage) -> list[str]:
    issues = []
    refs = fp.page.contents
    for i in sorted(fp.overlay_streams):
        r = refs[i] if i < len(refs) else None
        s = doc.get(r) if r is not None else None
        if not isinstance(s, Stream):
            issues.append(f"overlay stream {i} is not a stream")
            continue
        try:
            data = s.decoded()
            tokens = list(content_tokens(data))
            ops = [op for op, _args, _sp in tokens]
        except PDFError as exc:
            issues.append(f"overlay stream unreadable: {exc}")
            continue
        bad = sorted(set(ops) - ALLOWED_OVERLAY_OPS)
        if bad:
            issues.append(f"overlay uses operators {bad} (only text operators are allowed)")
        for op, args, _sp in tokens:
            if op in ("g", "rg"):
                nums = [float(x) for x in args if isinstance(x, (int, float)) and not isinstance(x, bool)]
                if not nums or any(v > 0.25 or v < 0.0 for v in nums):
                    issues.append(f"overlay sets non-dark fill color {op} {nums}")
        depth = 0
        for op in ops:
            depth += 1 if op == "q" else -1 if op == "Q" else 0
            if depth < 0:
                issues.append("overlay pops a graphics state it did not save (Q without q)")
                break
        if depth != 0:
            issues.append("overlay leaves the graphics state unbalanced")
        tags = 0
        for op in ops:
            tags += 1 if op in ("BDC", "BMC") else -1 if op == "EMC" else 0
        if tags != 0:
            issues.append("overlay marked content is unbalanced")
    return issues


def _app_check_pdf(blob: bytes, fdoc: PDFDocument, checks: list[AuditCheckItem]) -> None:
    """APP-01: an independent front-to-back reading of the file agrees with the cross-reference table."""
    from .verifier import _check

    try:
        found = _forward_scan(blob)
        wrong = []
        for n in fdoc.in_use():
            e = fdoc.xref[n]
            if e.offset not in found.get(n, []):
                wrong.append(n)
        last = {n: max(v) for n, v in found.items()}
        stale = [n for n in fdoc.in_use() if last.get(n) is not None and last[n] != fdoc.xref[n].offset]
        ok = not wrong and not stale
        detail = (f"forward scan found {sum(len(v) for v in found.values())} object header(s); all {len(fdoc.in_use())} "
                  f"cross-reference entries point at the latest definition") if ok else \
            f"cross-reference disagrees with the file for objects {(wrong + stale)[:10]}"
    except Exception as exc:  # noqa: BLE001 - any failure here is the finding
        ok, detail = False, f"failed: {exc}"
    checks.append(_check("APP-01", "application", "File opens in an independent reader (front-to-back object scan agrees "
                         "with the cross-reference table)", ok, detail, "critical"))


# ============================================================================== previews


def layout_pdf(src: Union[str, Path, bytes], form: Optional[PDFForm] = None):
    """A visual_diff Layout of a PDF: shading, ruled lines, text glyph by glyph, boxes for the comparison."""
    from .visual_diff import Box, Layout, Page, Prim

    form = form or load_form(src)
    pages, boxes = [], {}
    for p in form.pages:
        w, h = p.width * PT_TO_PX, p.height * PT_TO_PX
        X = lambda x: (x - p.ref.mediabox[0]) * PT_TO_PX  # noqa: E731
        Y = lambda y: (p.ref.mediabox[3] - y) * PT_TO_PX  # noqa: E731
        prims: list = []
        for pa in p.graphics.paths:
            if pa.filled and not pa.stroked:
                for x0, y0, x1, y1 in pa.rects:
                    if (x1 - x0) <= 2.0 or (y1 - y0) <= 2.0:
                        prims.append(Prim("rect", X(x0), Y(y1), max(0.8, (x1 - x0) * PT_TO_PX),
                                          max(0.8, (y1 - y0) * PT_TO_PX), fill=_hex(pa.fill)))
                    else:
                        prims.append(Prim("rect", X(x0), Y(y1), (x1 - x0) * PT_TO_PX, (y1 - y0) * PT_TO_PX,
                                          fill=_hex(pa.fill)))
            if pa.stroked:
                for x0, y0, x1, y1 in pa.segments:
                    prims.append(Prim("line", X(x0), Y(y0), x2=X(x1), y2=Y(y1), stroke=_hex(pa.stroke),
                                      stroke_w=max(0.6, pa.line_width * PT_TO_PX)))
        for r in p.graphics.runs:
            if r.invisible:
                continue
            g0 = r.glyphs[0]
            prims.append(Prim("text", X(g0.x), Y(g0.y), text=r.text, size=r.size * PT_TO_PX, color=_hex(r.color),
                              sans="Go" in (p.fonts.get(r.font).base_font if p.fonts.get(r.font) else ""),
                              xs=[round(X(g.x), 2) for g in r.glyphs]))
        for x0, y0, x1, y1, _s in p.graphics.images:
            prims.append(Prim("rect", X(x0), Y(y1), (x1 - x0) * PT_TO_PX, (y1 - y0) * PT_TO_PX, fill="#d9dce1"))
        pages.append(Page(w, h, prims))
        for c in p.cells:
            x0, y0, x1, y1 = c.rect
            gl = [g for r in p.graphics.runs for g in r.glyphs if c.contains(*_glyph_center(g))]
            ox = max([0.0] + [(g.x + g.adv - x1) * PT_TO_PX for g in gl] + [(x0 - g.x) * PT_TO_PX for g in gl])
            oy = max([0.0] + [(y0 - (g.y - 0.12 * g.size)) * PT_TO_PX for g in gl]
                     + [((g.y + 0.88 * g.size) - y1) * PT_TO_PX for g in gl])
            boxes[c.box_id] = Box(c.box_id, p.index, X(x0), Y(y1), (x1 - x0) * PT_TO_PX, (y1 - y0) * PT_TO_PX, "cell",
                                  "".join(g.ch for g in sorted(gl, key=lambda g: (-g.y, g.x))), ox, oy)
        for k, r in enumerate(p.graphics.runs):  # loose text (outside any box) as paragraph boxes
            if r.invisible or any(c.contains(*_glyph_center(r.glyphs[0])) for c in p.cells):
                continue
            x0, y0, x1, y1 = r.bbox()
            boxes[f"p{p.index}.t{k}"] = Box(f"p{p.index}.t{k}", p.index, X(x0), Y(y1), (x1 - x0) * PT_TO_PX,
                                            (y1 - y0) * PT_TO_PX, "para", r.text)
    page = form.pages[0].ref
    meta = {"page_mm": [round(page.width / 72 * 25.4), round(page.height / 72 * 25.4)],
            "page_pt": [page.width, page.height], "page_px": [round(page.width * PT_TO_PX), round(page.height * PT_TO_PX)]}
    return Layout(pages, boxes, meta=meta)


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(v * 255))):02X}" for v in rgb)


def pixel_diff(blank, filled, slot_boxes: set[str], scale: float = 1.0) -> dict[str, Any]:
    """Render both layouts with the same rasteriser and count differing pixels outside the slot boxes."""
    from .visual_diff import render_image

    a = render_image(blank, [], scale)
    b = render_image(filled, [], scale)
    if a is None or b is None:
        return {"method": "unavailable (Pillow or a CJK font missing) — geometric comparison only"}
    from PIL import ImageChops, ImageDraw

    if a.size != b.size:
        return {"method": "pixel", "outside": a.size[0] * a.size[1], "inside": 0, "size_changed": [a.size, b.size]}
    diff = ImageChops.difference(a.convert("L"), b.convert("L")).point(lambda v: 255 if v > 24 else 0)
    total = sum(diff.histogram()[1:])
    mask = diff.copy()
    draw = ImageDraw.Draw(mask)
    gap, oy = 24, 8.0
    offsets = []
    for pg in blank.pages:
        offsets.append(oy)
        oy += pg.height + gap
    for bid in slot_boxes:
        bx = filled.boxes.get(bid) or blank.boxes.get(bid)
        if bx is None:
            continue
        x0 = (8 + bx.x - 2) * scale
        y0 = (offsets[bx.page] + bx.y - 2) * scale
        draw.rectangle([x0, y0, x0 + (bx.w + 4) * scale, y0 + (bx.h + 4) * scale], fill=0)
    outside = sum(mask.histogram()[1:])
    return {"method": "pixel", "outside": outside, "inside": total - outside, "scale": scale}


def build_pdf_previews(scan, filled_path: Path, values: dict[str, str], naive_path: Optional[Path],
                       naive_report, png_dir: Optional[Path] = None) -> tuple[dict[str, str], dict[str, Any]]:
    from .visual_diff import Overlay, compare_layouts, render_png, svg_document

    form: PDFForm = scan.pdf if scan.pdf is not None else load_form(scan.path)
    blank_l = layout_pdf(scan.path, form)
    filled_l = layout_pdf(filled_path)
    labels = {s.slot_id: s.label for s in scan.slots}
    ov_b: list = []
    ov_f: list = []
    slot_boxes: set[str] = set()
    S = PT_TO_PX
    for s in scan.slots:
        for o in s.occurrences:
            bx = blank_l.boxes.get(o.box_id)
            if bx is None:
                continue
            slot_boxes.add(o.box_id)
            lab = labels.get(s.slot_id, s.slot_id)
            v = values.get(s.slot_id)
            ov_b.append(Overlay(bx.page, bx.x + 1.5, bx.y + 1.5, bx.w - 3, bx.h - 3, "fg-marker", f"{lab}（記入欄）",
                                s.slot_id, o.box_id))
            if o.marker_kind == "checkbox_text":
                chosen = selected_options(v, o.checkbox_options) if v else []
                ov_f.append(Overlay(bx.page, bx.x + 1.5, bx.y + 1.5, bx.w - 3, bx.h - 3, "fg-slot-cell", lab, s.slot_id,
                                    o.box_id))
                for opt in o.checkbox_options:
                    page_h = form.pages[bx.page].ref.mediabox[3]
                    gx, gy = opt["x"] * S, (page_h - opt["y"] - 0.88 * opt["size"]) * S
                    gs = opt["size"] * S
                    if opt["label"] in chosen:
                        ov_f.append(Overlay(bx.page, gx, gy, opt["adv"] * S, gs, "fg-slot",
                                            f"{lab}: □→■ {opt['label']}", s.slot_id, o.box_id))
                    elif not chosen:
                        ov_f.append(Overlay(bx.page, gx, gy, opt["adv"] * S, gs, "fg-pending",
                                            f"{lab}: 人の入力待ち / awaiting human input", s.slot_id, o.box_id))
                continue
            cls = "fg-slot" if v else "fg-pending"
            tip = f"{lab}: {v}" if v else f"{lab}: 人の入力待ち / awaiting human input"
            ov_f.append(Overlay(bx.page, bx.x + 1.5, bx.y + 1.5, bx.w - 3, bx.h - 3, cls, tip, s.slot_id, o.box_id))
    for c, kw in office_cells(form):
        for lay, ovs in ((blank_l, ov_b), (filled_l, ov_f)):
            bx = lay.boxes.get(c.box_id)
            if bx is not None:
                ovs.append(Overlay(bx.page, bx.x + 2, bx.y + 2, bx.w - 4, bx.h - 4, "fg-locked",
                                   f"事務局専用欄（ロック）/ office-only — locked ({', '.join(kw)})", "", c.box_id))
    visual = compare_layouts(blank_l, filled_l, slot_boxes)
    px = pixel_diff(blank_l, filled_l, slot_boxes)
    visual["pixel_diff"] = px
    if px.get("method") == "pixel" and px.get("outside", 0) > 0:
        visual["identical_outside_slots"] = False
        visual["drift_outside_slots"] = (visual.get("drift_outside_slots") or []) + [
            {"box": "page", "issue": f"{px['outside']} px differ outside the designated boxes (pixel diff)"}]
        visual["drift_count"] = (visual.get("drift_count") or 0) + 1
    n_filled = sum(1 for v in values.values() if v)
    px_ok = px.get("method") == "pixel" and px.get("outside", 0) == 0
    outside_label = "identical (0 px)" if (visual["identical_outside_slots"] and px_ok) else (
        "identical (geometric)" if visual["identical_outside_slots"] else "CHANGED")
    previews = {
        "blank": svg_document(blank_l, ov_b, "Blank original form",
                              "Blank original — designated boxes in blue, office-only boxes locked / 空欄の原本（青＝記入欄）",
                              "info"),
        "filled": svg_document(filled_l, ov_f, "Form-Guard output",
                               f"Form-Guard — {n_filled} boxes filled, page outside the boxes "
                               f"{outside_label} / 指定欄のみ重ね書き",
                               "ok" if visual["identical_outside_slots"] else "bad"),
    }
    naive_l = None
    ov_n: list = []
    if naive_path and Path(naive_path).exists():
        try:
            naive_l = layout_pdf(naive_path)
        except PDFError:
            naive_l = None
        if naive_l is not None:
            ov_n, banner = naive_pdf_overlays(blank_l, naive_l, naive_report)
            previews["naive"] = svg_document(naive_l, ov_n, "Naive AI regeneration (simulated)", banner, "bad")
            visual["naive"] = {"page": naive_l.meta.get("page_mm"), "pages": len(naive_l.pages), "overlays": len(ov_n)}
    if png_dir is not None:
        pngs = {}
        for name, lay, ovs in (("blank", blank_l, ov_b), ("filled", filled_l, ov_f), ("naive", naive_l, ov_n)):
            if lay is not None:
                pth = render_png(lay, ovs, Path(png_dir) / f"preview_{name}.png")
                if pth:
                    pngs[name] = pth.name
        visual["png"] = pngs
    return previews, visual


def naive_pdf_overlays(blank, naive, naive_report) -> tuple[list, str]:
    from .visual_diff import Overlay

    checks = {c.check_id: c for c in (naive_report.checks if naive_report else [])}
    invented = checks["GRD-02"].evidence.get("invented", []) if "GRD-02" in checks else []
    ovs: list = []
    for b in naive.boxes.values():
        if not b.text:
            continue
        hit = [n for n in invented if n in b.text]
        if hit:
            ovs.append(Overlay(b.page, b.x, b.y, b.w, b.h, "nv-invent",
                               f"メモにも様式にも無い数値 / invented number: {', '.join(hit)}", "", b.box_id))
        elif any(k in b.text for k in ("受付番号", "受付印", "事務局")) and "：" in b.text:
            ovs.append(Overlay(b.page, b.x, b.y, b.w, b.h, "nv-alter", "事務局専用欄に記入 / office-only field filled in",
                               "", b.box_id))
    if naive.boxes:
        xs = [b.x for b in naive.boxes.values()]
        ys = [b.y for b in naive.boxes.values()]
        x1 = max(b.x + b.w for b in naive.boxes.values())
        y1 = max(b.y + b.h for b in naive.boxes.values())
        ovs.append(Overlay(0, min(xs) - 4, min(ys) - 4, x1 - min(xs) + 8, y1 - min(ys) + 8, "nv-border",
                           "罫線・記入欄の枠・注意事項が失われた / ruled boxes and printed notes lost"))
    parts = ["Naive AI (simulated)"]
    if naive.meta.get("page_mm") != blank.meta.get("page_mm"):
        parts.append(f"page {naive.meta['page_mm'][0]}×{naive.meta['page_mm'][1]}mm ≠ "
                     f"{blank.meta['page_mm'][0]}×{blank.meta['page_mm'][1]}mm")
    if "LAYOUT-01" in checks and not checks["LAYOUT-01"].passed:
        parts.append(checks["LAYOUT-01"].details)
    if invented:
        parts.append(f"{len(invented)} invented numbers")
    return ovs, " · ".join(parts)


def compare_naive_pdf(scan, naive_path: Union[str, Path], memo_text: str):
    """Audit a from-scratch regeneration of a fixed PDF against the blank form."""
    from .verifier import (_check, _finish_naive, boilerplate_template_diff, guess_naive_fields, invented_numbers)
    from .constraints import validate_value

    form: PDFForm = scan.pdf if scan.pdf is not None else load_form(scan.path)
    nb = Path(naive_path).read_bytes()
    checks: list[AuditCheckItem] = []
    stats: dict[str, Any] = {}
    kept = nb[: len(form.data)] == form.data
    checks.append(_check("ZIP-04", "zip", "Original PDF bytes kept (incremental update)", kept,
                         "the original is an exact prefix" if kept else
                         f"regenerated: none of the original {len(form.data):,} bytes kept as a prefix", "critical"))
    try:
        naive = load_form(nb)
    except PDFError as exc:
        checks.append(_check("LAYOUT-01", "layout", "Ruled boxes identical", False, f"not compared: {exc}", "critical"))
        return _finish_naive(scan, checks, stats)
    nb_boxes, ob_boxes = sum(len(p.cells) for p in naive.pages), sum(len(p.cells) for p in form.pages)
    checks.append(_check("LAYOUT-01", "layout", "Ruled boxes identical", nb_boxes == ob_boxes and
                         _grid_signature(naive.pages[0].graphics) == _grid_signature(form.pages[0].graphics),
                         f"ruled boxes {ob_boxes}→{nb_boxes}", "critical",
                         original={"cells": ob_boxes, "merges": 0}, filled={"cells": nb_boxes, "merges": 0}))
    o, n = form.pages[0].ref, naive.pages[0].ref
    checks.append(_check("LAYOUT-02", "layout", "Page size identical", (o.width, o.height) == (n.width, n.height),
                         f"page {o.width:.2f}×{o.height:.2f} pt → {n.width:.2f}×{n.height:.2f} pt", "critical",
                         original_page=[o.width, o.height], filled_page=[n.width, n.height]))
    naive_lines = [r.text for p in naive.pages for r in p.graphics.runs]
    office_filled = [t for t in naive_lines if re.match(r"^\s*(?:受付番号|受付印|審査|事務局)[^：:]*[：:]\s*\S", t)]
    checks.append(_check("LAYOUT-04", "layout", "Office-only boxes left empty", not office_filled,
                         f"office-only field(s) filled in: {office_filled[:3]}" if office_filled else "none filled",
                         "critical"))
    orig_texts = [r.text for p in form.pages for r in p.graphics.runs]
    altered = boilerplate_template_diff(orig_texts, naive_lines)
    checks.append(_check("TEXT-01", "text", "All printed text unchanged", not altered,
                         f"{len(altered)} printed text blocks altered or missing" if altered else "all present", "critical",
                         altered=altered[:30]))
    invented = invented_numbers("\n".join(naive_lines), memo_text + "\n" + "\n".join(orig_texts))
    checks.append(_check("GRD-02", "grounding", "No numbers that are absent from both the memo and the blank form",
                         not invented, f"untraceable numbers: {', '.join(invented)}" if invented else "none", "critical",
                         invented=invented))
    violations = []
    for sid, val in guess_naive_fields(scan, naive_lines).items():
        for i in validate_value(scan.slot(sid), val):
            if i.severity == "error":
                violations.append(f"{scan.slot(sid).label}: «{val[:24]}» — {i.message}")
    checks.append(_check("CON-02", "constraints", "Form constraints respected (best-effort label matching)", not violations,
                         f"{len(violations)} violations" if violations else "none found", "major", violations=violations))
    stats["altered_blocks"] = len(altered)
    stats["invented_numbers"] = invented
    return _finish_naive(scan, checks, stats)
