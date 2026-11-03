"""Deterministic page previews (SVG, optional PNG) and layout-drift measurement.

A small layout engine reads the OOXML directly — page size and margins, paragraph
and run properties, table grids, gridSpan / vMerge, borders and shading for Word;
column widths, row heights, merged ranges, fonts, fills, borders and alignment for
Excel — and draws the same primitives for every file. Because the blank form, the
naive regeneration and the Form-Guard output are rendered by the *same* engine,
any difference between the previews comes from the files, not the renderer.

These previews are an approximation for review, not a replacement for Word/Excel.
"""

from __future__ import annotations

import html
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence, Union

from lxml import etree

from .ooxml import MAX_XLSX_COL, MAX_XLSX_ROW, PKG_REL_NS, R_NS, W_NS, check_xlsx_limits
from .textnorm import char_width_em

W = f"{{{W_NS}}}"
_P = etree.XMLParser(resolve_entities=False, no_network=True)
PX_PER_TWIP = 1 / 15
PT_TO_PX = 96 / 72
SERIF = "'Yu Mincho','YuMincho','Hiragino Mincho ProN','Noto Serif JP','MS Mincho',serif"
SANS = "'Yu Gothic','YuGothic','Hiragino Sans','Noto Sans JP','Meiryo',sans-serif"
NO_LINE_START = set("、。，．,.）)」』】〕〉》！？!?ー…・：；ぁぃぅぇぉっゃゅょァィゥェォッャュョ％")
NO_LINE_END = set("（(「『【〔〈《")
_HEX_COLOR = re.compile(r"#?([0-9A-Fa-f]{6}|[0-9A-Fa-f]{3})")


def _safe_color(val: Optional[str], default: Optional[str] = "#000000") -> Optional[str]:
    """``#RRGGBB`` from a form's colour attribute, or ``default``.

    Colours come from the uploaded file (w:color, w:shd/@w:fill, border colours, Excel RGB) and
    end up inside SVG attributes, so anything that is not plain hex — ``000000" onload="…``,
    ``<script>`` — is discarded instead of being interpolated. 8-digit Excel ARGB keeps the RGB part.
    """
    if not isinstance(val, str):
        return default
    v = val.strip()
    if len(v) == 8 and re.fullmatch(r"[0-9A-Fa-f]{8}", v):
        v = v[2:]
    m = _HEX_COLOR.fullmatch(v)
    if not m:
        return default
    h = m.group(1)
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    return "#" + h.upper()


def _attr(v: Any) -> str:
    return html.escape(str(v), quote=True)


# ------------------------------------------------------------------ primitives


@dataclass
class Prim:
    kind: str
    x: float
    y: float
    w: float = 0.0
    h: float = 0.0
    x2: float = 0.0
    y2: float = 0.0
    text: str = ""
    size: float = 14.0
    bold: bool = False
    color: str = "#111"
    fill: Optional[str] = None
    stroke: Optional[str] = None
    stroke_w: float = 1.0
    dash: Optional[str] = None
    sans: bool = False
    xs: Optional[list[float]] = None  # text: the x of every glyph (PDF previews place each glyph where the PDF does)


@dataclass
class Box:
    box_id: str
    page: int
    x: float
    y: float
    w: float
    h: float
    kind: str
    text: str = ""
    overflow_x: float = 0.0  # px the text sticks out of an Excel cell sideways (no wrap) …
    overflow_y: float = 0.0  # … or below it (wrapped text taller than the cell)


@dataclass
class Page:
    width: float
    height: float
    prims: list[Prim] = field(default_factory=list)


@dataclass
class Overlay:
    page: int
    x: float
    y: float
    w: float
    h: float
    cls: str
    title: str = ""
    slot_id: str = ""
    box_id: str = ""


@dataclass
class Layout:
    pages: list[Page]
    boxes: dict[str, Box]
    highlights: list[Overlay] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


# ===================================================================== DOCX


def _on(el, attr="val") -> bool:
    return el is not None and el.get(W + attr, "true") not in ("0", "false", "off")


class _Styles:
    def __init__(self, xml: Optional[bytes]):
        self.base_p: dict[str, Any] = {"size": 10.5}
        self.styles: dict[str, Any] = {}
        self.default_para: Optional[str] = None
        self.default_table: Optional[str] = None
        if not xml:
            return
        root = etree.fromstring(xml, _P)
        dd = root.find(f"{W}docDefaults")
        if dd is not None:
            self._rpr(dd.find(f"{W}rPrDefault/{W}rPr"), self.base_p)
            self._ppr(dd.find(f"{W}pPrDefault/{W}pPr"), self.base_p)
        for st in root.findall(f"{W}style"):
            sid = st.get(W + "styleId")
            self.styles[sid] = st
            if st.get(W + "default") in ("1", "true"):
                if st.get(W + "type") == "paragraph":
                    self.default_para = sid
                elif st.get(W + "type") == "table":
                    self.default_table = sid

    @staticmethod
    def _ppr(ppr, d):
        if ppr is None:
            return
        jc = ppr.find(f"{W}jc")
        if jc is not None:
            d["jc"] = jc.get(W + "val")
        sp = ppr.find(f"{W}spacing")
        if sp is not None:
            for k in ("before", "after", "line", "lineRule"):
                if sp.get(W + k) is not None:
                    d[k] = sp.get(W + k)
        ind = ppr.find(f"{W}ind")
        if ind is not None:
            for k, key in (("left", "ind_left"), ("start", "ind_left"), ("firstLine", "ind_first"), ("hanging", "ind_hanging")):
                if ind.get(W + k) is not None:
                    d[key] = int(ind.get(W + k))
        rpr = ppr.find(f"{W}rPr")
        if rpr is not None:
            d.setdefault("_mark", {})

    @staticmethod
    def _rpr(rpr, d):
        if rpr is None:
            return
        sz = rpr.find(f"{W}sz")
        if sz is not None and sz.get(W + "val"):
            d["size"] = int(sz.get(W + "val")) / 2
        b = rpr.find(f"{W}b")
        if b is not None:
            d["bold"] = _on(b)
        c = rpr.find(f"{W}color")
        if c is not None and c.get(W + "val") not in (None, "auto"):
            color = _safe_color(c.get(W + "val"), None)
            if color:
                d["color"] = color
        f = rpr.find(f"{W}rFonts")
        if f is not None:
            names = " ".join(v for k, v in f.attrib.items() if not k.endswith("Theme"))
            if names:
                d["sans"] = bool(re.search(r"Gothic|ゴシック|Meiryo|メイリオ|Sans|Arial|Calibri", names))

    def chain(self, sid: Optional[str]) -> list:
        out, seen = [], set()
        while sid and sid in self.styles and sid not in seen and len(out) < 10:
            seen.add(sid)
            st = self.styles[sid]
            out.insert(0, st)
            nxt = st.find(f"{W}basedOn")
            sid = nxt.get(W + "val") if nxt is not None else None
        return out

    def para(self, p) -> dict[str, Any]:
        d = dict(self.base_p)
        ppr = p.find(f"{W}pPr")
        ps = ppr.find(f"{W}pStyle") if ppr is not None else None
        for st in self.chain(ps.get(W + "val") if ps is not None else self.default_para):
            self._ppr(st.find(f"{W}pPr"), d)
            self._rpr(st.find(f"{W}rPr"), d)
        self._ppr(ppr, d)
        return d

    def run(self, r, pd: dict[str, Any]) -> dict[str, Any]:
        d = {k: pd[k] for k in ("size", "bold", "color", "sans") if k in pd}
        rpr = r.find(f"{W}rPr")
        if rpr is not None:
            rs = rpr.find(f"{W}rStyle")
            if rs is not None:
                for st in self.chain(rs.get(W + "val")):
                    self._rpr(st.find(f"{W}rPr"), d)
            self._rpr(rpr, d)
        return d

    def table_borders(self, tbl) -> dict[str, Any]:
        tblPr = tbl.find(f"{W}tblPr")
        ts = tblPr.find(f"{W}tblStyle") if tblPr is not None else None
        out: dict[str, Any] = {}
        for st in self.chain(ts.get(W + "val") if ts is not None else self.default_table):
            _border_map(st.find(f"{W}tblPr/{W}tblBorders"), out)
        if tblPr is not None:
            _border_map(tblPr.find(f"{W}tblBorders"), out)
        return out


def _border_map(el, out):
    if el is None:
        return
    for child in el:
        side = etree.QName(child).localname
        side = {"start": "left", "end": "right"}.get(side, side)
        out[side] = (child.get(W + "val", "single"), int(child.get(W + "sz", "4") or 4), child.get(W + "color", "000000"))


def _walk_text(p):
    """Yield (char, run_element) in the same order/with the same barriers as the scanner."""
    def walk(el, run):
        for ch in el:
            if not isinstance(ch.tag, str):
                continue
            name = etree.QName(ch)
            if name.namespace != W_NS:
                if name.localname in ("txbxContent",):
                    continue
                yield from walk(ch, run)
                continue
            ln = name.localname
            if ln in ("p", "txbxContent", "rt", "pPr", "rPr", "tblPr"):
                continue
            if ln == "r":
                yield from walk(ch, ch)
            elif ln == "t" and run is not None:
                for c in ch.text or "":
                    yield c, run
            elif ln in ("tab",) and run is not None:
                yield "\t", run
            elif ln in ("br", "cr") and run is not None:
                yield "\n", run
            elif ln in ("drawing", "pict", "object", "sym") and run is not None:
                yield "\ufffc", run
            else:
                yield from walk(ch, run)
    yield from walk(p, None)


@dataclass
class _Line:
    chars: list[tuple[int, str, dict, float]]  # (text index, char, style, width)
    height: float
    width: float


def _measure(ch: str, st: dict) -> float:
    em = st.get("size", 10.5) * PT_TO_PX
    if ch == "\t":
        return em * 2
    if ch == "\ufffc":
        return em
    return char_width_em(ch) * em


def _break_lines(items: list[tuple[int, str, dict]], width: float, first_indent: float, line_factor: float) -> list[_Line]:
    lines: list[_Line] = []
    cur: list[tuple[int, str, dict, float]] = []
    cur_w = 0.0
    avail = max(10.0, width - first_indent)

    def flush():
        nonlocal cur, cur_w, avail
        size = max((c[2].get("size", 10.5) for c in cur), default=items[0][2].get("size", 10.5) if items else 10.5)
        lines.append(_Line(cur, size * PT_TO_PX * line_factor, cur_w))
        cur, cur_w, avail = [], 0.0, max(10.0, width)

    for idx, ch, st in items:
        if ch == "\n":
            flush()
            continue
        w = _measure(ch, st)
        if cur and cur_w + w > avail and ch not in NO_LINE_START:
            carry: list = []
            if ch.isascii() and ch.isalnum():
                j = len(cur) - 1
                while j > 0 and cur[j][1].isascii() and cur[j][1].isalnum():
                    j -= 1
                if 0 < j < len(cur) - 1 and len(cur) - j < 24:
                    carry = cur[j + 1 :]
                    cur = cur[: j + 1]
            elif cur[-1][1] in NO_LINE_END:
                carry = [cur.pop()]
            cur_w = sum(c[3] for c in cur)
            flush()
            cur = carry
            cur_w = sum(c[3] for c in cur)
        cur.append((idx, ch, st, w))
        cur_w += w
    if cur or not lines:
        flush()
    return lines


class _DocxRenderer:
    def __init__(self, blob: bytes):
        self.zf = zipfile.ZipFile(io.BytesIO(blob))
        names = set(self.zf.namelist())
        self.styles = _Styles(self.zf.read("word/styles.xml") if "word/styles.xml" in names else None)
        self.root = etree.fromstring(self.zf.read("word/document.xml"), _P)
        self.rels = self._rels("word/_rels/document.xml.rels")
        self.para_idx = {p: i for i, p in enumerate(self.root.iter(f"{W}p"))}
        self.tbl_idx = {t: i for i, t in enumerate(self.root.iter(f"{W}tbl"))}
        sect = self.root.find(f"{W}body/{W}sectPr")
        if sect is None:
            sect = self.root.find(f".//{W}sectPr")
        pg = sect.find(f"{W}pgSz") if sect is not None else None
        mar = sect.find(f"{W}pgMar") if sect is not None else None
        tw = lambda el, k, d: int(el.get(W + k, d)) if el is not None and el.get(W + k) else d
        self.page_w, self.page_h = tw(pg, "w", 11906) * PX_PER_TWIP, tw(pg, "h", 16838) * PX_PER_TWIP
        self.m_top, self.m_bottom = tw(mar, "top", 1440) * PX_PER_TWIP, tw(mar, "bottom", 1440) * PX_PER_TWIP
        self.m_left, self.m_right = tw(mar, "left", 1440) * PX_PER_TWIP, tw(mar, "right", 1440) * PX_PER_TWIP
        self.m_header, self.m_footer = tw(mar, "header", 720) * PX_PER_TWIP, tw(mar, "footer", 720) * PX_PER_TWIP
        self.sect = sect
        self.pages: list[Page] = []
        self.boxes: dict[str, Box] = {}
        self.char_rects: dict[tuple[str, int], tuple[int, float, float, float, float]] = {}
        self.y = 0.0
        self._new_page()

    def _rels(self, name: str) -> dict[str, str]:
        if name not in self.zf.namelist():
            return {}
        root = etree.fromstring(self.zf.read(name), _P)
        return {r.get("Id"): r.get("Target", "") for r in root.findall(f"{{{PKG_REL_NS}}}Relationship")}

    def _new_page(self):
        self.pages.append(Page(self.page_w, self.page_h))
        self.y = self.m_top

    @property
    def page(self) -> int:
        return len(self.pages) - 1

    @property
    def bottom(self) -> float:
        return self.page_h - self.m_bottom

    # -- paragraphs -----------------------------------------------------------
    def para_metrics(self, p, width: float):
        pd = self.styles.para(p)
        items = [(i, ch, self.styles.run(r, pd)) for i, (ch, r) in enumerate(_walk_text(p))]
        line = int(pd.get("line", 240) or 240)
        rule = pd.get("lineRule", "auto")
        factor = 1.25 * (line / 240) if rule == "auto" else None
        ind_left = pd.get("ind_left", 0) * PX_PER_TWIP
        first = (pd.get("ind_first", 0) - pd.get("ind_hanging", 0)) * PX_PER_TWIP
        size = pd.get("size", 10.5)
        lines = _break_lines(items, (width - ind_left) * 0.985, first, factor or 1.25)
        if not items:
            lines = [_Line([], size * PT_TO_PX * (factor or 1.25), 0)]
        if factor is None:
            exact = line * PX_PER_TWIP
            for ln in lines:
                ln.height = max(exact, ln.height) if rule == "atLeast" else exact
        before = int(pd.get("before", 0) or 0) * PX_PER_TWIP
        after = int(pd.get("after", 0) or 0) * PX_PER_TWIP
        h = before + sum(ln.height for ln in lines) + after
        return pd, lines, before, after, ind_left, first, h

    def draw_para(self, p, x: float, y: float, width: float, page: int, box_prefix: str = "") -> float:
        pd, lines, before, after, ind_left, first, h = self.para_metrics(p, width)
        jc = pd.get("jc", "left")
        cy = y + before
        pid = f"{box_prefix}p{self.para_idx.get(p, -1)}" if not box_prefix else f"{box_prefix}p{self._local_idx(p)}"
        text = "".join(c[1] for ln in lines for c in ln.chars)
        for li, ln in enumerate(lines):
            lx = x + ind_left + (first if li == 0 else 0)
            avail = width - ind_left - (first if li == 0 else 0)
            if jc == "center":
                lx += max(0.0, (avail - ln.width) / 2)
            elif jc in ("right", "end"):
                lx += max(0.0, avail - ln.width)
            base = cy + ln.height * 0.78
            cx = lx
            seg: list = []
            seg_x = cx
            for idx, ch, st, w in ln.chars:
                key = (st.get("size"), st.get("bold"), st.get("color"), st.get("sans"))
                if seg and (seg[0] != key or ch in "\t\ufffc"):
                    self._emit(seg, seg_x, base, page)
                    seg = []
                if not seg:
                    seg = [key, ""]
                    seg_x = cx
                if ch not in "\t\ufffc":
                    seg[1] += ch
                self.char_rects[(pid, idx)] = (page, cx, cy, w, ln.height)
                cx += w
            if seg:
                self._emit(seg, seg_x, base, page)
            cy += ln.height
        self.boxes[pid] = Box(pid, page, x, y, width, h, "para", text)
        return h

    def _local_idx(self, p) -> int:
        return self._hf_idx.get(p, -1)

    def _emit(self, seg, x, base, page):
        (size, bold, color, sans), text = seg
        if text.strip():
            self.pages[page].prims.append(Prim("text", x, base, text=text, size=(size or 10.5) * PT_TO_PX,
                                               bold=bool(bold), color=color or "#111", sans=bool(sans)))

    # -- tables ---------------------------------------------------------------
    def draw_table(self, tbl, x0: float, width: float):
        grid = [int(g.get(W + "w", "0") or 0) * PX_PER_TWIP for g in tbl.findall(f"{W}tblGrid/{W}gridCol")]
        if not grid:
            return
        tblPr = tbl.find(f"{W}tblPr")
        jc = tblPr.find(f"{W}jc") if tblPr is not None else None
        ind = tblPr.find(f"{W}tblInd") if tblPr is not None else None
        total = sum(grid)
        x = x0
        if jc is not None and jc.get(W + "val") in ("right", "end"):
            x = x0 + width - total
        elif jc is not None and jc.get(W + "val") == "center":
            x = x0 + (width - total) / 2
        if ind is not None and ind.get(W + "w"):
            x += int(ind.get(W + "w")) * PX_PER_TWIP
        xs = [x]
        for g in grid:
            xs.append(xs[-1] + g)
        tb = self.styles.table_borders(tbl)
        ti = self.tbl_idx.get(tbl, -1)
        rows = tbl.findall(f"{W}tr")
        cells: list[list[dict]] = []
        for ri, tr in enumerate(rows):
            col, row_cells = 0, []
            trPr = tr.find(f"{W}trPr")
            gb = trPr.find(f"{W}gridBefore") if trPr is not None else None
            if gb is not None:
                col += int(gb.get(W + "val", "0"))
            for tc in tr.findall(f"{W}tc"):
                tcPr = tc.find(f"{W}tcPr")
                gs = tcPr.find(f"{W}gridSpan") if tcPr is not None else None
                span = int(gs.get(W + "val", "1")) if gs is not None else 1
                vm = tcPr.find(f"{W}vMerge") if tcPr is not None else None
                vmerge = (vm.get(W + "val") or "continue") if vm is not None else None
                own: dict[str, Any] = {}
                _border_map(tcPr.find(f"{W}tcBorders") if tcPr is not None else None, own)
                shd = tcPr.find(f"{W}shd") if tcPr is not None else None
                fill = _safe_color(shd.get(W + "fill"), None) if shd is not None and shd.get(W + "fill") not in (None, "auto") else None
                va = tcPr.find(f"{W}vAlign") if tcPr is not None else None
                row_cells.append({"tc": tc, "row": ri, "col": col, "span": span, "vmerge": vmerge, "borders": own,
                                  "fill": fill, "valign": va.get(W + "val") if va is not None else "top", "rows": 1})
                col += span
            cells.append(row_cells)
        # resolve vertical merges
        for ri, row in enumerate(cells):
            for c in row:
                if c["vmerge"] == "restart":
                    n = 1
                    for rj in range(ri + 1, len(cells)):
                        nxt = next((d for d in cells[rj] if d["col"] == c["col"]), None)
                        if nxt is None or nxt["vmerge"] != "continue":
                            break
                        nxt["covered"] = True
                        n += 1
                    c["rows"] = n
        pad_x = 108 * PX_PER_TWIP
        heights = []
        for ri, tr in enumerate(rows):
            trPr = tr.find(f"{W}trPr")
            th = trPr.find(f"{W}trHeight") if trPr is not None else None
            min_h = int(th.get(W + "val", "0")) * PX_PER_TWIP if th is not None else 0
            rule = th.get(W + "hRule", "atLeast") if th is not None else "atLeast"
            need = 0.0
            for c in cells[ri]:
                if c.get("covered") or c["rows"] > 1:
                    continue
                inner = xs[c["col"] + c["span"]] - xs[c["col"]] - 2 * pad_x
                need = max(need, sum(self.para_metrics(p, inner)[-1] for p in c["tc"].findall(f"{W}p")) + 4)
            heights.append(min_h if rule == "exact" else max(min_h, need, 12))
        for ri, row in enumerate(cells):
            for c in row:
                if c["rows"] > 1:
                    inner = xs[c["col"] + c["span"]] - xs[c["col"]] - 2 * pad_x
                    need = sum(self.para_metrics(p, inner)[-1] for p in c["tc"].findall(f"{W}p")) + 4
                    have = sum(heights[ri : ri + c["rows"]])
                    if need > have:
                        heights[ri + c["rows"] - 1] += need - have
        n_rows, n_grid = len(rows), len(grid)
        row_top: list[tuple[int, float]] = []
        for ri in range(n_rows):
            if self.y + heights[ri] > self.bottom and self.y > self.m_top + 1:
                self._new_page()
            row_top.append((self.page, self.y))
            self.y += heights[ri]
        for ri, row in enumerate(cells):
            for c in row:
                if c.get("covered"):
                    continue
                page, top = row_top[ri]
                h = sum(heights[ri : ri + c["rows"]])
                cx0, cx1 = xs[c["col"]], xs[c["col"] + c["span"]]
                prims = self.pages[page].prims
                if c["fill"]:
                    prims.append(Prim("rect", cx0, top, cx1 - cx0, h, fill=c["fill"]))
                last_row = ri + c["rows"] - 1 == n_rows - 1
                edges = {
                    "top": c["borders"].get("top", tb.get("top") if ri == 0 else tb.get("insideH")),
                    "bottom": c["borders"].get("bottom", tb.get("bottom") if last_row else tb.get("insideH")),
                    "left": c["borders"].get("left", tb.get("left") if c["col"] == 0 else tb.get("insideV")),
                    "right": c["borders"].get("right", tb.get("right") if c["col"] + c["span"] == n_grid else tb.get("insideV")),
                }
                for side, spec in edges.items():
                    self._border(prims, side, spec, cx0, top, cx1, top + h)
                paras = c["tc"].findall(f"{W}p")
                inner = cx1 - cx0 - 2 * pad_x
                content = sum(self.para_metrics(p, inner)[-1] for p in paras)
                cy = top + 2
                if c["valign"] == "center":
                    cy = top + max(2.0, (h - content) / 2)
                elif c["valign"] == "bottom":
                    cy = top + max(2.0, h - content - 2)
                for p in paras:
                    cy += self.draw_para(p, cx0 + pad_x, cy, inner, page)
                bid = f"t{ti}.r{ri}.c{c['col']}"
                text = "\n".join(self.boxes[f"p{self.para_idx.get(p, -1)}"].text for p in paras
                                 if f"p{self.para_idx.get(p, -1)}" in self.boxes)
                self.boxes[bid] = Box(bid, page, cx0, top, cx1 - cx0, h, "cell", text)
        self.boxes[f"t{ti}"] = Box(f"t{ti}", row_top[0][0], xs[0], row_top[0][1], total, sum(heights), "table")

    @staticmethod
    def _border(prims, side, spec, x0, y0, x1, y1):
        if not spec:
            return
        val, sz, color = spec
        if val in ("nil", "none"):
            return
        w = max(0.75, sz / 8 * PT_TO_PX)
        col = "#000000" if color in (None, "auto") else _safe_color(color, "#000000")
        dash = {"dashed": "4 3", "dotted": "1 2", "dotDash": "4 2 1 2"}.get(val)
        coords = {"top": (x0, y0, x1, y0), "bottom": (x0, y1, x1, y1), "left": (x0, y0, x0, y1), "right": (x1, y0, x1, y1)}[side]
        if val == "double":
            off = w + 0.6
            dx, dy = (0, off) if side in ("top", "bottom") else (off, 0)
            sign = 1 if side in ("top", "left") else -1
            prims.append(Prim("line", coords[0], coords[1], x2=coords[2], y2=coords[3], stroke=col, stroke_w=w * 0.6))
            prims.append(Prim("line", coords[0] + dx * sign, coords[1] + dy * sign, x2=coords[2] + dx * sign,
                              y2=coords[3] + dy * sign, stroke=col, stroke_w=w * 0.6))
            return
        prims.append(Prim("line", coords[0], coords[1], x2=coords[2], y2=coords[3], stroke=col, stroke_w=w, dash=dash))

    # -- header / footer ----------------------------------------------------
    def draw_header_footer(self):
        if self.sect is None:
            return
        width = self.page_w - self.m_left - self.m_right
        for kind in ("headerReference", "footerReference"):
            ref = next((r for r in self.sect.findall(f"{W}{kind}") if r.get(W + "type", "default") == "default"), None)
            if ref is None:
                continue
            target = self.rels.get(ref.get(f"{{{R_NS}}}id"), "")
            part = "word/" + target.lstrip("/").removeprefix("word/")
            if part not in self.zf.namelist():
                continue
            root = etree.fromstring(self.zf.read(part), _P)
            self._hf_idx = {p: i for i, p in enumerate(root.iter(f"{W}p"))}
            stem = part.rsplit("/", 1)[-1].removesuffix(".xml")
            paras = list(root.iter(f"{W}p"))
            hs = [self.para_metrics(p, width)[-1] for p in paras]
            for pg in range(len(self.pages)):
                y = self.m_header if kind == "headerReference" else self.page_h - self.m_footer - sum(hs)
                for p, h in zip(paras, hs):
                    self.draw_para(p, self.m_left, y, width, pg, box_prefix=f"{stem}:")
                    y += h

    def render(self) -> Layout:
        body = self.root.find(f"{W}body")
        width = self.page_w - self.m_left - self.m_right
        for el in body if body is not None else []:
            tag = etree.QName(el).localname if isinstance(el.tag, str) else ""
            if tag == "p":
                h = self.para_metrics(el, width)[-1]
                if self.y + h > self.bottom and self.y > self.m_top + 1:
                    self._new_page()
                self.draw_para(el, self.m_left, self.y, width, self.page)
                self.y += h
            elif tag == "tbl":
                self.draw_table(el, self.m_left, width)
        self._hf_idx = {}
        self.draw_header_footer()
        meta = {"page_mm": (round(self.page_w * 25.4 / 96, 1), round(self.page_h * 25.4 / 96, 1)),
                "margins_mm": tuple(round(v * 25.4 / 96, 1) for v in (self.m_top, self.m_right, self.m_bottom, self.m_left)),
                "pages": len(self.pages)}
        return Layout(self.pages, self.boxes, meta=meta)


def layout_docx(src: Union[str, Path, bytes]) -> tuple[Layout, _DocxRenderer]:
    blob = src if isinstance(src, bytes) else Path(src).read_bytes()
    r = _DocxRenderer(blob)
    return r.render(), r


# ===================================================================== XLSX


def _col_px(width: Optional[float]) -> float:
    return round((width if width else 8.43) * 7 + 5)


def _rgb(color) -> Optional[str]:
    try:
        v = color.rgb
    except AttributeError:
        return None
    return _safe_color(v, None)


_COUNTIF = re.compile(r'^=COUNTIF\(\$?([A-Z]+)\$?(\d+):\$?([A-Z]+)\$?(\d+),"([^"]*)"\)$', re.I)


def _format_number(v, fmt: str) -> str:
    if isinstance(v, (int, float)):
        m = re.fullmatch(r'0"([^"]*)"', fmt or "")
        if m:
            return f"{int(round(v))}{m.group(1)}"
        if fmt and "#,##0" in fmt:
            return f"{v:,.0f}"
        return f"{v:g}" if isinstance(v, float) else str(v)
    return str(v)


def layout_xlsx(src: Union[str, Path, bytes], sheet_indices: Optional[Sequence[int]] = None) -> Layout:
    """Render every worksheet (or ``sheet_indices``, in workbook order) as one page each.

    Box IDs are ``s{sheet}!{cell}`` — the same IDs the scanner gives slot occurrences — so overlays,
    drift and overflow checks work on every sheet, not just the first.
    """
    import openpyxl

    blob = src if isinstance(src, bytes) else Path(src).read_bytes()
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        check_xlsx_limits(z)
    wb = openpyxl.load_workbook(io.BytesIO(blob), data_only=False)
    order = list(wb.sheetnames)  # workbook order, as in xl/workbook.xml (the scanner's sheet index)
    wanted = [ws for ws in wb.worksheets if sheet_indices is None or order.index(ws.title) in set(sheet_indices)]
    pages: list[Page] = []
    boxes: dict[str, Box] = {}
    sheets_meta = []
    for ws in wanted:
        page, meta = _layout_sheet(ws, order.index(ws.title), len(pages), boxes)
        pages.append(page)
        sheets_meta.append(meta)
    if not pages:
        pages.append(Page(200.0, 100.0))
    first = sheets_meta[0] if sheets_meta else {"sheet": "", "range": "", "page_px": (200, 100)}
    meta = {"sheet": first["sheet"], "range": first["range"], "pages": len(pages), "page_px": first["page_px"],
            "sheets": sheets_meta}
    return Layout(pages, boxes, meta=meta)


def _layout_sheet(ws, sheet_index: int, page_no: int, boxes: dict[str, Box]) -> tuple[Page, dict[str, Any]]:
    from openpyxl.utils import column_index_from_string, get_column_letter
    from openpyxl.utils.cell import range_boundaries

    used_r, used_c = max(ws.max_row, 1), max(ws.max_column, 1)
    min_c, min_r, max_c, max_r = 1, 1, used_c, used_r
    area = ws.print_area
    if area:
        ref = str(area).split("!")[-1].replace("$", "").split(",")[0]
        try:
            b = range_boundaries(ref)
        except (TypeError, ValueError):
            b = (None, None, None, None)
        # whole-column / whole-row print areas ("A:H") come back with None bounds: use the used range
        min_c, min_r = b[0] or 1, b[1] or 1
        max_c, max_r = b[2] or used_c, b[3] or used_r
    max_r, max_c = min(max_r, MAX_XLSX_ROW), min(max_c, MAX_XLSX_COL)
    min_r, min_c = min(min_r, max_r), min(min_c, max_c)
    margin = 28.0
    xs = [margin]
    for c in range(min_c, max_c + 1):
        xs.append(xs[-1] + _col_px(ws.column_dimensions[get_column_letter(c)].width))
    ys = [margin]
    for r in range(min_r, max_r + 1):
        h = ws.row_dimensions[r].height
        ys.append(ys[-1] + (h if h else 15) * PT_TO_PX)
    page = Page(xs[-1] + margin, ys[-1] + margin)
    prims = page.prims
    merged = {}
    covered = set()

    from openpyxl.cell.cell import Cell

    def cell_at(r: int, c: int):
        return ws._cells.get((r, c)) or Cell(ws, row=r, column=c)

    for rng in ws.merged_cells.ranges:
        merged[(rng.min_row, rng.min_col)] = (rng.max_row, rng.max_col)
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                if (r, c) != (rng.min_row, rng.min_col):
                    covered.add((r, c))

    def value_of(cell) -> str:
        v = cell.value
        if v is None:
            return ""
        if isinstance(v, str) and v.startswith("="):
            m = _COUNTIF.match(v)
            if m:
                c0, r0, c1, r1, crit = m.groups()
                n = 0
                for rr in range(int(r0), min(int(r1), MAX_XLSX_ROW) + 1):
                    for cc in range(column_index_from_string(c0), min(column_index_from_string(c1), MAX_XLSX_COL) + 1):
                        hit = ws._cells.get((rr, cc))
                        if hit is not None and str(hit.value or "") == crit:
                            n += 1
                return _format_number(n, cell.number_format)
            return v
        return _format_number(v, cell.number_format)

    for r in range(min_r, max_r + 1):
        for c in range(min_c, max_c + 1):
            x0, y0 = xs[c - min_c], ys[r - min_r]
            prims.append(Prim("rect", x0, y0, xs[c - min_c + 1] - x0, ys[r - min_r + 1] - y0, stroke="#e3e6ea",
                              stroke_w=0.6, dash="2 2"))
    for r in range(min_r, max_r + 1):
        for c in range(min_c, max_c + 1):
            if (r, c) in covered:
                continue
            r1, c1 = merged.get((r, c), (r, c))
            r1, c1 = min(r1, max_r), min(c1, max_c)
            x0, y0 = xs[c - min_c], ys[r - min_r]
            x1, y1 = xs[c1 - min_c + 1], ys[r1 - min_r + 1]
            cell = cell_at(r, c)
            fill = _rgb(cell.fill.fgColor) if cell.fill is not None and cell.fill.fill_type == "solid" else None
            if fill:
                prims.append(Prim("rect", x0, y0, x1 - x0, y1 - y0, fill=fill))
            text = value_of(cell)
            overflow = (0.0, 0.0)
            if text:
                font = cell.font
                size = (font.sz or 11) * PT_TO_PX
                al = cell.alignment
                st = {"size": (font.sz or 11), "bold": bool(font.b), "sans": True}
                inner = max(8.0, (x1 - x0 - 6) * 0.96)
                items = [(i, ch, st) for i, ch in enumerate(text)]
                lines = _break_lines(items, inner, 0, 1.2) if al.wrap_text else [
                    _Line([(i, ch, st, _measure(ch, st)) for i, ch, _ in items], size * 1.2,
                          sum(_measure(ch, st) for _, ch, _ in items))]
                total_h = sum(ln.height for ln in lines)
                max_line_w = max((ln.width for ln in lines), default=0.0)
                overflow = (max(0.0, max_line_w - (x1 - x0 - 4)), max(0.0, total_h - (y1 - y0 - 2)))
                if al.vertical == "top":
                    ty = y0 + 3
                elif al.vertical == "center":
                    ty = y0 + (y1 - y0 - total_h) / 2
                else:
                    ty = y1 - total_h - 2
                horiz = al.horizontal or ("right" if isinstance(cell.value, (int, float)) else "left")
                color = _rgb(font.color) if font.color is not None else None
                for ln in lines:
                    s = "".join(ch for _, ch, _, _ in ln.chars)
                    lx = x0 + 3
                    if horiz in ("center", "centerContinuous"):
                        lx = x0 + (x1 - x0 - ln.width) / 2
                    elif horiz == "right":
                        lx = x1 - ln.width - 3
                    prims.append(Prim("text", lx, ty + ln.height * 0.8, text=s, size=size, bold=bool(font.b),
                                      color=color or "#111", sans=True))
                    ty += ln.height
            bid = f"s{sheet_index}!{get_column_letter(c)}{r}"
            ox, oy = overflow if text else (0.0, 0.0)
            boxes[bid] = Box(bid, page_no, x0, y0, x1 - x0, y1 - y0, "cell", text, ox, oy)
    # borders drawn last so they sit on top of fills; Excel never draws edges inside a merged range
    owner = {}
    for (tr, tc), (br, bc) in merged.items():
        for r in range(tr, br + 1):
            for c in range(tc, bc + 1):
                owner[(r, c)] = (tr, tc, br, bc)
    for r in range(min_r, max_r + 1):
        for c in range(min_c, max_c + 1):
            b = cell_at(r, c).border
            x0, y0, x1, y1 = xs[c - min_c], ys[r - min_r], xs[c - min_c + 1], ys[r - min_r + 1]
            tr, tc, br, bc = owner.get((r, c), (r, c, r, c))
            outline = {"left": c == tc, "right": c == bc, "top": r == tr, "bottom": r == br}
            for side, coords in (("left", (x0, y0, x0, y1)), ("right", (x1, y0, x1, y1)),
                                 ("top", (x0, y0, x1, y0)), ("bottom", (x0, y1, x1, y1))):
                s = getattr(b, side)
                if s is None or not s.style or not outline[side]:
                    continue
                w = {"thin": 1.0, "medium": 2.0, "thick": 3.0, "double": 2.5}.get(s.style, 1.0)
                dash = {"dashed": "4 3", "dotted": "1 2", "hair": "1 1"}.get(s.style)
                prims.append(Prim("line", coords[0], coords[1], x2=coords[2], y2=coords[3],
                                  stroke=(_rgb(s.color) if s.color is not None else None) or "#000000", stroke_w=w,
                                  dash=dash))
    meta = {"sheet": ws.title, "sheet_index": sheet_index,
            "range": f"{get_column_letter(min_c)}{min_r}:{get_column_letter(max_c)}{max_r}",
            "page_px": (round(page.width), round(page.height))}
    return page, meta


# ================================================================ rendering


def render_layout(src, file_type: str) -> tuple[Layout, Any]:
    if file_type == "docx":
        return layout_docx(src)
    return layout_xlsx(src), None


def char_range_rects(renderer: _DocxRenderer, box_id: str, start: int, end: int) -> list[tuple[int, float, float, float, float]]:
    """Merge per-character rectangles of [start, end) into one rectangle per line."""
    rects: list[list[float]] = []
    for i in range(start, end):
        r = renderer.char_rects.get((box_id, i))
        if r is None:
            continue
        page, x, y, w, h = r
        if rects and rects[-1][0] == page and abs(rects[-1][2] - y) < 0.5 and abs(rects[-1][1] + rects[-1][3] - x) < 2:
            rects[-1][3] = x + w - rects[-1][1]
        else:
            rects.append([page, x, y, w, h])
    return [tuple(r) for r in rects]  # type: ignore[misc]


def svg_document(layout: Layout, overlays: list[Overlay], title: str = "", banner: str = "",
                 banner_cls: str = "ok", scale: float = 1.0) -> str:
    gap = 24.0
    width = max(p.width for p in layout.pages)
    offsets, y = [], (36.0 if banner else 8.0)
    for p in layout.pages:
        offsets.append(y)
        y += p.height + gap
    height = y
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width + 16:.0f} {height:.0f}" '
           f'width="{(width + 16) * scale:.0f}" height="{height * scale:.0f}" role="img" aria-label="{html.escape(title)}" '
           f'class="fg-preview">',
           f"<title>{html.escape(title)}</title>",
           "<style>.fg-t{white-space:pre}.fg-slot{fill:rgba(12,163,12,.16);stroke:#0a8a0a;stroke-width:1.6}"
           ".fg-slot-cell{fill:none;stroke:#0ca30c;stroke-width:1;stroke-dasharray:4 3}"
           ".fg-marker{fill:rgba(37,99,235,.14);stroke:#2563eb;stroke-width:1.2}"
           ".fg-pending{fill:rgba(250,178,25,.28);stroke:#b97a00;stroke-width:1.6;stroke-dasharray:5 3}"
           ".fg-drift{fill:rgba(236,131,90,.18);stroke:#c4562b;stroke-width:2}"
           ".fg-locked{fill:rgba(100,116,139,.13);stroke:#64748b;stroke-width:1.2;stroke-dasharray:3 3}"
           ".nv-alter{fill:rgba(208,59,59,.13);stroke:#d03b3b;stroke-width:1.6}"
           ".nv-border{fill:none;stroke:#d03b3b;stroke-width:2;stroke-dasharray:7 4}"
           ".nv-invent{fill:rgba(208,59,59,.24);stroke:#a32525;stroke-width:2}"
           ".fg-banner-ok{fill:#0a6e0a}.fg-banner-bad{fill:#a32525}.fg-banner-info{fill:#1e3a8a}"
           "[data-slot-id]{cursor:pointer}</style>",
           '<defs><filter id="fgshadow" x="-5%" y="-5%" width="110%" height="110%"><feDropShadow dx="0" dy="1.5" '
           'stdDeviation="2" flood-opacity=".18"/></filter></defs>']
    if banner:
        out.append(f'<rect x="8" y="6" width="{width:.0f}" height="24" rx="5" class="fg-banner-{_attr(banner_cls)}"/>'
                   f'<text x="18" y="22" font-size="12.5" fill="#fff" font-family="{SANS}" font-weight="700">'
                   f"{html.escape(banner)}</text>")
    for pi, p in enumerate(layout.pages):
        oy = offsets[pi]
        out.append(f'<g transform="translate(8,{oy:.1f})"><rect x="0" y="0" width="{p.width:.1f}" height="{p.height:.1f}" '
                   'fill="#fff" stroke="#d6d9de" filter="url(#fgshadow)"/>')
        for pr in p.prims:
            out.append(_svg_prim(pr))
        for ov in overlays:
            if ov.page != pi:
                continue
            attrs = f' data-slot-id="{_attr(ov.slot_id)}"' if ov.slot_id else ""
            attrs += f' data-box-id="{_attr(ov.box_id)}"' if ov.box_id else ""
            out.append(f'<rect x="{ov.x - 1.5:.1f}" y="{ov.y - 1:.1f}" width="{ov.w + 3:.1f}" height="{ov.h + 2:.1f}" rx="2.5" '
                       f'class="{_attr(ov.cls)}"{attrs}><title>{html.escape(ov.title)}</title></rect>')
        out.append("</g>")
    out.append("</svg>")
    return "".join(out)


def _svg_prim(p: Prim) -> str:
    if p.kind == "rect":
        st = f' fill="{_attr(p.fill)}"' if p.fill else ' fill="none"'
        if p.stroke:
            st += f' stroke="{_attr(p.stroke)}" stroke-width="{p.stroke_w:.2f}"'
        if p.dash:
            st += f' stroke-dasharray="{_attr(p.dash)}"'
        return f'<rect x="{p.x:.1f}" y="{p.y:.1f}" width="{p.w:.1f}" height="{p.h:.1f}"{st}/>'
    if p.kind == "line":
        d = f' stroke-dasharray="{_attr(p.dash)}"' if p.dash else ""
        return (f'<line x1="{p.x:.1f}" y1="{p.y:.1f}" x2="{p.x2:.1f}" y2="{p.y2:.1f}" stroke="{_attr(p.stroke)}" '
                f'stroke-width="{p.stroke_w:.2f}" stroke-linecap="square"{d}/>')
    fam = SANS if p.sans else SERIF
    weight = ' font-weight="700"' if p.bold else ""
    xpos = (" ".join(f"{x:.1f}" for x in p.xs) if p.xs and len(p.xs) == len(p.text) else f"{p.x:.1f}")
    return (f'<text x="{xpos}" y="{p.y:.1f}" font-size="{p.size:.1f}" font-family="{fam}"{weight} '
            f'fill="{_attr(p.color)}" class="fg-t" xml:space="preserve">{html.escape(p.text)}</text>')


# ---------------------------------------------------------------- PNG output


_FONT_CANDIDATES = [
    "C:/Windows/Fonts/YuGothM.ttc", "C:/Windows/Fonts/yugothm.ttc", "C:/Windows/Fonts/meiryo.ttc",
    "C:/Windows/Fonts/msgothic.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc", "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
]


def png_available() -> Optional[str]:
    import importlib.util

    if importlib.util.find_spec("PIL") is None:
        return None
    return next((f for f in _FONT_CANDIDATES if Path(f).exists()), None)


def render_png(layout: Layout, overlays: list[Overlay], out_path: Union[str, Path], scale: float = 1.5) -> Optional[Path]:
    """Raster version of the preview (needs Pillow and a CJK font); returns None if unavailable."""
    img = render_image(layout, overlays, scale)
    if img is None:
        return None
    out_path = Path(out_path)
    img.save(out_path)
    return out_path


def render_image(layout: Layout, overlays: list[Overlay], scale: float = 1.5):
    """The preview as a Pillow image (None without Pillow or a CJK font) — also the rasteriser of the PDF pixel diff."""
    font_path = png_available()
    if not font_path:
        return None
    from PIL import Image, ImageDraw, ImageFont

    gap = 24
    width = int((max(p.width for p in layout.pages) + 16) * scale)
    height = int((sum(p.height + gap for p in layout.pages) + 8) * scale)
    img = Image.new("RGB", (width, height), "#eef0f3")
    draw = ImageDraw.Draw(img, "RGBA")
    fonts: dict[int, Any] = {}

    def font(px: float):
        k = max(6, int(px * scale))
        if k not in fonts:
            fonts[k] = ImageFont.truetype(font_path, k)
        return fonts[k]

    oy = 8.0
    colors = {"fg-slot": ((12, 163, 12, 45), (10, 138, 10, 255)), "fg-pending": ((250, 178, 25, 75), (185, 122, 0, 255)),
              "fg-marker": ((37, 99, 235, 40), (37, 99, 235, 255)), "nv-alter": ((208, 59, 59, 38), (208, 59, 59, 255)),
              "nv-invent": ((208, 59, 59, 65), (163, 37, 37, 255)), "nv-border": ((0, 0, 0, 0), (208, 59, 59, 255)),
              "fg-drift": ((236, 131, 90, 50), (196, 86, 43, 255)), "fg-slot-cell": ((0, 0, 0, 0), (12, 163, 12, 255)),
              "fg-locked": ((100, 116, 139, 34), (100, 116, 139, 255))}
    for pi, p in enumerate(layout.pages):
        S = lambda v: v * scale
        ox = 8.0
        draw.rectangle([S(ox), S(oy), S(ox + p.width), S(oy + p.height)], fill="#ffffff", outline="#d6d9de")
        for pr in p.prims:
            if pr.kind == "rect":
                draw.rectangle([S(ox + pr.x), S(oy + pr.y), S(ox + pr.x + pr.w), S(oy + pr.y + pr.h)],
                               fill=pr.fill, outline=pr.stroke if pr.stroke and not pr.dash else None)
            elif pr.kind == "line":
                draw.line([S(ox + pr.x), S(oy + pr.y), S(ox + pr.x2), S(oy + pr.y2)], fill=pr.stroke or "#000",
                          width=max(1, int(pr.stroke_w * scale)))
            elif pr.xs and len(pr.xs) == len(pr.text):  # glyph by glyph, at the positions the PDF uses
                for ch, gx in zip(pr.text, pr.xs):
                    draw.text((S(ox + gx), S(oy + pr.y - pr.size * 0.86)), ch, fill=pr.color, font=font(pr.size))
            else:
                draw.text((S(ox + pr.x), S(oy + pr.y - pr.size * 0.86)), pr.text, fill=pr.color, font=font(pr.size))
        for ov in overlays:
            if ov.page != pi:
                continue
            fill, outline = colors.get(ov.cls, ((0, 0, 0, 0), (0, 0, 0, 255)))
            draw.rectangle([S(ox + ov.x - 1.5), S(oy + ov.y - 1), S(ox + ov.x + ov.w + 1.5), S(oy + ov.y + ov.h + 1)],
                           fill=fill, outline=outline, width=max(1, int(1.5 * scale)))
        oy += p.height + gap
    return img


# ---------------------------------------------------------------- comparison


OVERFLOW_X_PX = 6.0
OVERFLOW_Y_PX = 4.0


def compare_layouts(blank: Layout, filled: Layout, slot_boxes: set[str], tolerance: float = 0.6) -> dict[str, Any]:
    """Geometry of every non-slot box must be unchanged, and no filled slot may spill out of its cell.

    Excel cells never grow, so a value that is too long does not move anything — it runs past the
    cell edge (or below it when wrapped). That overflow is reported as drift as well.
    """
    drift, compared, max_shift, overflows = [], 0, 0.0, 0
    for bid, b in blank.boxes.items():
        f = filled.boxes.get(bid)
        if f is None:
            drift.append({"box": bid, "issue": "missing"})
            continue
        if bid in slot_boxes:
            ox, oy = f.overflow_x, f.overflow_y
            if (ox > OVERFLOW_X_PX and ox > b.overflow_x + 0.5) or (oy > OVERFLOW_Y_PX and oy > b.overflow_y + 0.5):
                overflows += 1
                drift.append({"box": bid, "issue": f"cell text overflow ({ox:.0f}px wide / {oy:.0f}px tall)",
                              "text": f.text[:40]})
            continue
        if b.kind == "table":
            continue
        compared += 1
        shift = max(abs(b.x - f.x), abs(b.y - f.y), abs(b.w - f.w), abs(b.h - f.h))
        max_shift = max(max_shift, shift)
        if shift > tolerance or b.page != f.page:
            drift.append({"box": bid, "issue": f"moved {shift:.1f}px", "text": b.text[:40]})
    return {
        "pages_blank": len(blank.pages), "pages_filled": len(filled.pages),
        "boxes_compared": compared, "slot_boxes": len(slot_boxes),
        "drift_outside_slots": drift[:20], "drift_count": len(drift), "overflow_count": overflows,
        "max_shift_px": round(max_shift, 2), "identical_outside_slots": not drift and len(blank.pages) == len(filled.pages),
        "page": blank.meta.get("page_mm") or blank.meta.get("page_px"),
    }
