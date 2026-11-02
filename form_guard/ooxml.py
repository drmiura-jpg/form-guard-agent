"""Byte-exact indexing of OOXML parts (WordprocessingML and SpreadsheetML).

The surgical writer may change *only* the characters of a slot marker, so it needs
the exact byte range of every text node in the original part. The stdlib expat
parser reports the byte offset of each parse event; combined with a quote-aware
tag regex we recover the start-tag / content / end-tag span of every element we
care about, without ever re-serialising the document.

Japanese-specific details handled here:
  * Word ruby text (``w:rt``) and Excel phonetic runs (``rPh``) are excluded from
    the visible text, so furigana never corrupts placeholder matching.
  * ``w:tab`` inside ``w:tabs`` (tab-stop definitions) is not a text barrier.
  * Character references such as ``&#28857;`` (openpyxl writes all non-ASCII text
    this way) are mapped back to byte offsets exactly.
"""

from __future__ import annotations

import re
import xml.parsers.expat as expat
import zipfile
from dataclasses import dataclass, field
from typing import Optional

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XML_NS = "http://www.w3.org/XML/1998/namespace"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

BARRIER_OBJECT = "￼"

_TAG_RE = re.compile(rb'<(?:[^>"\']|"[^"]*"|\'[^\']*\')*>')
_QNAME_RE = re.compile(rb"</?([^\s/>]+)")
_REF_RE = re.compile(rb"&(?:#[xX][0-9A-Fa-f]+|#[0-9]+|[A-Za-z_][A-Za-z0-9._-]*);")
_XMLSPACE_RE = re.compile(rb"""\s+xml:space\s*=\s*(?:"[^"]*"|'[^']*')""")
_XMLNS_RE = re.compile(rb"""\sxmlns(?::([A-Za-z_][\w.\-]*))?\s*=\s*("[^"]*"|'[^']*')""")
_INVALID_XML_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")

WORD_STORY_PARTS = re.compile(r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$")


class UnsafeXMLError(ValueError):
    """XML we refuse to process (DTDs, entity declarations)."""


class UnsupportedXMLError(ValueError):
    """Valid-but-unusual XML that the byte-exact writer deliberately does not handle."""


# --------------------------------------------------------------------------- data


@dataclass
class TextNode:
    idx: int
    qname: str
    start_tag: tuple[int, int]
    content: tuple[int, int]
    end_tag: Optional[tuple[int, int]]
    empty: bool
    text: str
    preserve: bool
    container: int
    run: int = -1

    @property
    def span(self) -> tuple[int, int]:
        end = self.end_tag[1] if self.end_tag else self.start_tag[1]
        return (self.start_tag[0], end)


@dataclass
class CellInfo:
    table_idx: int
    row_idx: int
    col_idx: int
    cell_idx: int
    grid_span: int = 1
    vmerge: Optional[str] = None
    depth: int = 0
    containers: list[int] = field(default_factory=list)


@dataclass
class TableInfo:
    table_idx: int
    depth: int
    rows: list[list[CellInfo]] = field(default_factory=list)
    grid_col: int = 0


@dataclass
class Container:
    """A text container: a Word paragraph (w:p), a shared string (si) or inline string (is)."""

    idx: int
    kind: str
    qname: str
    start_tag: tuple[int, int]
    end_tag: Optional[tuple[int, int]]
    empty: bool
    segments: list[tuple[str, object]] = field(default_factory=list)
    para_index: Optional[int] = None
    cell: Optional[CellInfo] = None
    cell_ref: Optional[str] = None
    si_index: Optional[int] = None
    ppr_rpr: Optional[bytes] = None
    runs: list[tuple[int, int]] = field(default_factory=list)

    @property
    def span(self) -> tuple[int, int]:
        end = self.end_tag[1] if self.end_tag else self.start_tag[1]
        return (self.start_tag[0], end)


@dataclass
class SheetCell:
    """A worksheet ``<c>`` element (profile ``sheet``), located by byte offsets."""

    ref: str
    row: int
    col: int
    qname: str
    start_tag: tuple[int, int]
    end_tag: Optional[tuple[int, int]]
    empty: bool
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[str] = field(default_factory=list)  # local names of the direct children (v, f, is, …)
    is_container: Optional[int] = None  # index of its <is> inline-string container, if any

    @property
    def span(self) -> tuple[int, int]:
        end = self.end_tag[1] if self.end_tag else self.start_tag[1]
        return (self.start_tag[0], end)


@dataclass
class SheetRow:
    r: int
    qname: str
    start_tag: tuple[int, int]
    end_tag: Optional[tuple[int, int]]
    empty: bool
    attrs: dict[str, str] = field(default_factory=dict)
    cells: list[str] = field(default_factory=list)  # cell refs in document order

    @property
    def span(self) -> tuple[int, int]:
        end = self.end_tag[1] if self.end_tag else self.start_tag[1]
        return (self.start_tag[0], end)


@dataclass
class ElementSpan:
    qname: str
    start_tag: tuple[int, int]
    end_tag: Optional[tuple[int, int]]
    empty: bool

    @property
    def span(self) -> tuple[int, int]:
        end = self.end_tag[1] if self.end_tag else self.start_tag[1]
        return (self.start_tag[0], end)


@dataclass
class PartIndex:
    part_name: str
    data: bytes
    profile: str
    nodes: list[TextNode]
    containers: list[Container]
    tables: list[TableInfo]
    # worksheet structure (profile "sheet" only): every <c> / <row>, <sheetData>, column styles
    cells: dict[str, SheetCell] = field(default_factory=dict)
    rows: dict[int, SheetRow] = field(default_factory=dict)
    sheet_data: Optional[ElementSpan] = None
    col_styles: list[tuple[int, int, str]] = field(default_factory=list)  # (min, max, style id)
    implicit_refs: bool = False  # a <row> or <c> without its r attribute (cells cannot be addressed)

    def container_text(self, c: Container) -> str:
        out = []
        for kind, ref in c.segments:
            out.append(self.nodes[ref].text if kind == "n" else ref)  # type: ignore[index]
        return "".join(out)

    def container_nodes(self, c: Container) -> list[TextNode]:
        """The text nodes of a container, in document order (one per <w:t> / <t>)."""
        return [self.nodes[ref] for kind, ref in c.segments if kind == "n"]  # type: ignore[index]

    def node_layout(self, c: Container) -> list[tuple[int, int, int]]:
        """Return (node_idx, start, end) offsets of each text node inside the container text."""
        pos = 0
        layout = []
        for kind, ref in c.segments:
            if kind == "n":
                n = self.nodes[ref]  # type: ignore[index]
                layout.append((n.idx, pos, pos + len(n.text)))
                pos += len(n.text)
            else:
                pos += len(ref)  # type: ignore[arg-type]
        return layout


@dataclass
class _Frame:
    uri: str
    local: str
    start: tuple[int, int]
    empty: bool
    role: str = ""
    ref: object = None


# ------------------------------------------------------------------- primitives


def ensure_safe_xml(data: bytes, part_name: str = "") -> None:
    from .ziputil import xml_has_dtd  # every encoding: UTF-8, UTF-16 / UTF-32 with or without a byte-order mark

    if xml_has_dtd(data):
        raise UnsafeXMLError(f"{part_name}: DTD / entity declarations are not allowed in OOXML parts")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or data[:4] == b"\x00\x00\xfe\xff" or b"\x00" in data:
        raise UnsupportedXMLError(f"{part_name}: UTF-16 / UTF-32 XML parts are not supported")


def tag_span(data: bytes, pos: int) -> tuple[int, int]:
    m = _TAG_RE.match(data, pos)
    if m is None:
        raise UnsupportedXMLError(f"could not delimit tag at byte {pos}")
    return (pos, m.end())


def qname_of(tag: bytes) -> str:
    m = _QNAME_RE.match(tag)
    return m.group(1).decode("utf-8") if m else ""


def raw_char_offsets(raw: bytes) -> list[int]:
    """Map decoded character index -> byte offset within a raw text-node payload.

    Returns len(decoded) + 1 offsets. Handles UTF-8, entity / character references
    and CR/CRLF normalisation. Markup inside text (CDATA, comments) is rejected.
    """
    if b"<" in raw:
        raise UnsupportedXMLError("markup inside a text node (CDATA/comment) is not supported")
    offs: list[int] = []
    i, n = 0, len(raw)
    while i < n:
        offs.append(i)
        b = raw[i]
        if b == 0x26:
            m = _REF_RE.match(raw, i)
            if m is None:
                raise UnsupportedXMLError("malformed entity reference in text node")
            i = m.end()
        elif b == 0x0D:
            i += 2 if raw[i + 1 : i + 2] == b"\n" else 1
        elif b < 0x80:
            i += 1
        elif b >= 0xF0:
            i += 4
        elif b >= 0xE0:
            i += 3
        else:
            i += 2
    offs.append(n)
    return offs


def sanitize_xml_text(s: str) -> str:
    return _INVALID_XML_CHARS.sub("", s.replace("\r\n", "\n").replace("\r", "\n"))


def escape_text(s: str) -> bytes:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").encode("utf-8")


def needs_preserve(s: str) -> bool:
    return bool(s) and (s[0] in " \t\n" or s[-1] in " \t\n" or "  " in s)


def add_preserve_attr(start_tag: bytes) -> bytes:
    """Set xml:space="preserve" on a start tag (works for '>' and '/>' endings).

    An existing ``xml:space="default"`` / ``xml:space='default'`` is replaced in place —
    appending a second attribute would make the part malformed (``Attribute xml:space redefined``).
    """
    if _XMLSPACE_RE.search(start_tag):
        return _XMLSPACE_RE.sub(b' xml:space="preserve"', start_tag, count=1)
    if start_tag.endswith(b"/>"):
        return start_tag[:-2] + b' xml:space="preserve"/>'
    return start_tag[:-1] + b' xml:space="preserve">'


def canonical_text_node_token(start_tag: bytes) -> bytes:
    """Skeleton token for a text node: start tag without xml:space, empty, explicit end tag."""
    tag = _XMLSPACE_RE.sub(b"", start_tag)
    qn = _QNAME_RE.match(tag)
    name = qn.group(1) if qn else b""
    if tag.endswith(b"/>"):
        tag = tag[:-2].rstrip() + b">"
    return tag + b"</" + name + b">"


def xmlns_declarations(tag: bytes) -> list[tuple[str, str]]:
    out = []
    for prefix, uri in _XMLNS_RE.findall(tag):
        out.append(((prefix or b"").decode(), uri[1:-1].decode("utf-8")))
    return out


def iter_start_tags(data: bytes):
    for m in _TAG_RE.finditer(data):
        tag = m.group(0)
        if tag.startswith((b"</", b"<?", b"<!")):
            continue
        yield m.start(), tag


# ---------------------------------------------------------------------- indexer


_PROFILES = {
    # profile: (container (ns, local), text (ns, local), skip-subtree (ns, local) set)
    "word": ((W_NS, "p"), (W_NS, "t"), {(W_NS, "rt")}),
    "sst": ((S_NS, "si"), (S_NS, "t"), {(S_NS, "rPh")}),
    "sheet": ((S_NS, "is"), (S_NS, "t"), {(S_NS, "rPh")}),
}

_WORD_BARRIERS = {"tab": "\t", "br": "\n", "cr": "\n", "drawing": BARRIER_OBJECT,
                  "pict": BARRIER_OBJECT, "object": BARRIER_OBJECT, "sym": BARRIER_OBJECT}


def index_part(part_name: str, data: bytes, profile: str) -> PartIndex:
    """Index all text containers / text nodes of an OOXML part with byte offsets."""
    if profile not in _PROFILES:
        raise ValueError(f"unknown profile {profile!r}")
    ensure_safe_xml(data, part_name)
    container_key, text_key, skip_keys = _PROFILES[profile]

    nodes: list[TextNode] = []
    containers: list[Container] = []
    tables: list[TableInfo] = []
    stack: list[_Frame] = []
    cont_stack: list[int] = []
    run_stack: list[int] = []
    tbl_stack: list[TableInfo] = []
    cell_stack: list[CellInfo] = []
    counters = {"para": 0, "si": 0, "run": 0}
    state = {"skip": 0, "cell_ref": None, "text": None}
    sheet_cells: dict[str, SheetCell] = {}
    sheet_rows: dict[int, SheetRow] = {}
    sheet_meta: dict = {"sheet_data": None, "cols": [], "implicit": False}

    parser = expat.ParserCreate(namespace_separator=" ")
    parser.buffer_text = False

    def _reject(*_a):
        raise UnsafeXMLError(f"{part_name}: DTD / entity declarations are not allowed")

    parser.StartDoctypeDeclHandler = _reject
    parser.EntityDeclHandler = _reject

    def start(name: str, attrs: dict):
        pos = parser.CurrentByteIndex
        span = tag_span(data, pos)
        empty = data[span[1] - 2 : span[1]] == b"/>"
        uri, _, local = name.rpartition(" ")
        frame = _Frame(uri, local, span, empty)
        parent = stack[-1] if stack else None
        key = (uri, local)
        if profile == "sheet" and parent is not None and parent.role == "cell":
            parent.ref.children.append(local)  # type: ignore[union-attr]

        if key in skip_keys:
            state["skip"] += 1
            frame.role = "skip"
        elif key == container_key:
            c = Container(
                idx=len(containers), kind=local, qname=qname_of(data[span[0] : span[1]]),
                start_tag=span, end_tag=None, empty=empty,
            )
            if profile == "word":
                c.para_index = counters["para"]
                counters["para"] += 1
                if cell_stack:
                    c.cell = cell_stack[-1]
                    cell_stack[-1].containers.append(c.idx)
            elif profile == "sst":
                c.si_index = counters["si"]
                counters["si"] += 1
            else:
                c.cell_ref = state["cell_ref"]
                if parent is not None and parent.role == "cell":
                    parent.ref.is_container = c.idx  # type: ignore[union-attr]
            containers.append(c)
            cont_stack.append(c.idx)
            frame.role, frame.ref = "container", c.idx
        elif key == text_key and cont_stack and state["skip"] == 0:
            in_run = profile != "word" or (parent is not None and parent.role == "run")
            if in_run:
                content_start = span[1]
                n = TextNode(
                    idx=len(nodes), qname=qname_of(data[span[0] : span[1]]), start_tag=span,
                    content=(content_start, content_start), end_tag=None, empty=empty, text="",
                    preserve=attrs.get(f"{XML_NS} space") == "preserve", container=cont_stack[-1],
                    run=run_stack[-1] if run_stack else -1,
                )
                nodes.append(n)
                containers[cont_stack[-1]].segments.append(("n", n.idx))
                frame.role, frame.ref = "text", n.idx
                state["text"] = [] if not empty else None
        elif uri == W_NS and profile == "word":
            if local == "r" and cont_stack:
                run_stack.append(counters["run"])
                counters["run"] += 1
                frame.role = "run"
            elif local in _WORD_BARRIERS and parent is not None and parent.role == "run" and cont_stack \
                    and state["skip"] == 0:
                containers[cont_stack[-1]].segments.append(("b", _WORD_BARRIERS[local]))
            elif local == "tbl":
                t = TableInfo(table_idx=len(tables), depth=len(tbl_stack))
                tables.append(t)
                tbl_stack.append(t)
                frame.role = "tbl"
            elif local == "tr" and tbl_stack:
                tbl_stack[-1].rows.append([])
                tbl_stack[-1].grid_col = 0
                frame.role = "tr"
            elif local == "gridBefore" and parent is not None and parent.local == "trPr" and tbl_stack:
                tbl_stack[-1].grid_col += _int(attrs.get(f"{W_NS} val"), 0)
            elif local == "tc" and tbl_stack and tbl_stack[-1].rows:
                t = tbl_stack[-1]
                cell = CellInfo(
                    table_idx=t.table_idx, row_idx=len(t.rows) - 1, col_idx=t.grid_col,
                    cell_idx=len(t.rows[-1]), depth=t.depth,
                )
                t.rows[-1].append(cell)
                cell_stack.append(cell)
                frame.role, frame.ref = "tc", cell
            elif local == "gridSpan" and parent is not None and parent.local == "tcPr" and cell_stack:
                cell_stack[-1].grid_span = max(1, _int(attrs.get(f"{W_NS} val"), 1))
            elif local == "vMerge" and parent is not None and parent.local == "tcPr" and cell_stack:
                cell_stack[-1].vmerge = attrs.get(f"{W_NS} val") or "continue"
            elif local == "rPr" and parent is not None and parent.local == "pPr":
                frame.role = "ppr_rpr"
        elif profile == "sst" and key == (S_NS, "r") and cont_stack:
            run_stack.append(counters["run"])
            counters["run"] += 1
            frame.role = "run"
        elif profile == "sheet" and key == (S_NS, "c"):
            state["cell_ref"] = attrs.get("r")
            if parent is not None and parent.role == "row":
                ref = (attrs.get("r") or "").replace("$", "").upper()
                rc = split_cell_ref(ref) if ref else None
                if rc is None:
                    sheet_meta["implicit"] = True
                else:
                    cell = SheetCell(ref=ref, row=rc[0], col=rc[1], qname=qname_of(data[span[0] : span[1]]),
                                     start_tag=span, end_tag=None, empty=empty, attrs=dict(attrs))
                    sheet_cells.setdefault(ref, cell)
                    parent.ref.cells.append(ref)  # type: ignore[union-attr]
                    frame.role, frame.ref = "cell", cell
        elif profile == "sheet" and key == (S_NS, "r") and cont_stack:
            run_stack.append(counters["run"])
            counters["run"] += 1
            frame.role = "run"
        elif profile == "sheet" and key == (S_NS, "row") and parent is not None and parent.role == "sheetData":
            r = _int(attrs.get("r"), 0)
            if r <= 0:
                sheet_meta["implicit"] = True
            else:
                row = SheetRow(r=r, qname=qname_of(data[span[0] : span[1]]), start_tag=span, end_tag=None,
                               empty=empty, attrs=dict(attrs))
                sheet_rows.setdefault(r, row)
                frame.role, frame.ref = "row", row
        elif profile == "sheet" and key == (S_NS, "sheetData"):
            sheet_meta["sheet_data"] = ElementSpan(qname_of(data[span[0] : span[1]]), span, None, empty)
            frame.role = "sheetData"
        elif profile == "sheet" and key == (S_NS, "col"):
            lo, hi = _int(attrs.get("min"), 0), _int(attrs.get("max"), 0)
            if lo and hi and attrs.get("style"):
                sheet_meta["cols"].append((lo, hi, attrs["style"]))
        stack.append(frame)

    def end(name: str):
        frame = stack.pop()
        if frame.empty:
            end_span = None
        else:
            end_span = tag_span(data, parser.CurrentByteIndex)
        role = frame.role
        if role == "skip":
            state["skip"] -= 1
        elif role == "container":
            c = containers[frame.ref]  # type: ignore[index]
            c.end_tag = end_span
            cont_stack.pop()
        elif role == "text":
            n = nodes[frame.ref]  # type: ignore[index]
            if end_span is not None:
                n.content = (frame.start[1], end_span[0])
                n.end_tag = end_span
                n.text = "".join(state["text"] or [])
            state["text"] = None
        elif role == "run":
            run_stack.pop()
            if cont_stack:
                full_end = end_span[1] if end_span else frame.start[1]
                containers[cont_stack[-1]].runs.append((frame.start[0], full_end))
        elif role == "tbl":
            tbl_stack.pop()
        elif role == "tc":
            cell = cell_stack.pop()
            if tbl_stack:
                tbl_stack[-1].grid_col += cell.grid_span
        elif role == "ppr_rpr" and cont_stack:
            full_end = end_span[1] if end_span else frame.start[1]
            containers[cont_stack[-1]].ppr_rpr = data[frame.start[0] : full_end]
        elif role in ("cell", "row"):
            frame.ref.end_tag = end_span  # type: ignore[union-attr]
        elif role == "sheetData" and sheet_meta["sheet_data"] is not None:
            sheet_meta["sheet_data"].end_tag = end_span

    def chars(text: str):
        if state["text"] is not None and stack and stack[-1].role == "text":
            state["text"].append(text)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:  # pragma: no cover - surfaced to the verifier
        raise UnsupportedXMLError(f"{part_name}: XML parse error: {exc}") from exc
    return PartIndex(part_name, data, profile, nodes, containers, tables, cells=sheet_cells, rows=sheet_rows,
                     sheet_data=sheet_meta["sheet_data"], col_styles=sheet_meta["cols"],
                     implicit_refs=sheet_meta["implicit"])


def _int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


_A1_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?([0-9]{1,7})$")


def split_cell_ref(ref: str) -> Optional[tuple[int, int]]:
    """'C5' -> (5, 3) as (row, column); None if ``ref`` is not an A1-style cell reference."""
    m = _A1_RE.match(ref.strip())
    if not m or int(m.group(2)) < 1:
        return None
    return int(m.group(2)), column_number(m.group(1))


def column_letters(n: int) -> str:
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def inherited_cell_style(idx: PartIndex, row: int, col: int) -> Optional[str]:
    """Style id Excel applies to a cell that does not exist yet: the row's (customFormat) or the column's."""
    r = idx.rows.get(row)
    if r is not None and r.attrs.get("customFormat") in ("1", "true") and r.attrs.get("s"):
        return r.attrs["s"]
    for lo, hi, style in idx.col_styles:
        if lo <= col <= hi:
            return style
    return None


# --------------------------------------------------------------- package helpers


def docx_story_parts(zf: zipfile.ZipFile) -> list[str]:
    names = [n for n in zf.namelist() if WORD_STORY_PARTS.match(n)]
    names.sort(key=lambda n: (0 if n == "word/document.xml" else 1, n))
    return names


def xlsx_sheet_parts(zf: zipfile.ZipFile) -> list[tuple[int, str, str]]:
    """Return [(sheet_index, sheet_name, part_name)] in workbook order."""
    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    wb = etree.fromstring(zf.read("xl/workbook.xml"), parser)
    rels = etree.fromstring(zf.read("xl/_rels/workbook.xml.rels"), parser)
    targets = {}
    for rel in rels.findall(f"{{{PKG_REL_NS}}}Relationship"):
        tgt = rel.get("Target", "")
        if tgt.startswith("/"):
            part = tgt.lstrip("/")
        else:
            part = "xl/" + tgt
        targets[rel.get("Id")] = _normpath(part)
    out = []
    for i, sh in enumerate(wb.iter(f"{{{S_NS}}}sheet")):
        rid = sh.get(f"{{{R_NS}}}id")
        if rid in targets:
            out.append((i, sh.get("name", f"Sheet{i + 1}"), targets[rid]))
    return out


# ------------------------------------------------------------ xlsx size guard

# A real form fits easily inside these bounds; a 10 KB workbook with one cell at XFD1048576 or a
# merge over a whole column would otherwise make openpyxl-style rectangle loops run for hours.
MAX_XLSX_ROW = 2000
MAX_XLSX_COL = 200
MAX_XLSX_CELLS = 100_000

_PFX = rb"(?:[A-Za-z_][\w.\-]*:)?"
_CELL_TAG_RE = re.compile(rb"<" + _PFX + rb"c[\s/>]")
_CELL_REF_RE = re.compile(rb"<" + _PFX + rb"c\s(?:[^>]*?\s)?r\s*=\s*[\"']\$?([A-Za-z]{1,3})\$?([0-9]{1,7})[\"']")
_MERGE_REF_RE = re.compile(rb"<" + _PFX + rb"mergeCell\s(?:[^>]*?\s)?ref\s*=\s*[\"']([^\"']*)[\"']")
_RANGE_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?([0-9]{1,7})(?::\$?([A-Za-z]{1,3})\$?([0-9]{1,7}))?$")


def column_number(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def check_xlsx_limits(zf: zipfile.ZipFile, sheets: Optional[list[tuple[int, str, str]]] = None) -> None:
    """Refuse worksheets whose cells or merged ranges exceed safe form limits (read from the raw XML,
    before any library expands a range). Raises UnsupportedXMLError."""
    names = set(zf.namelist())
    for _, sname, part in sheets if sheets is not None else xlsx_sheet_parts(zf):
        if part not in names:
            continue
        data = zf.read(part)
        if len(_CELL_TAG_RE.findall(data)) > MAX_XLSX_CELLS:
            raise UnsupportedXMLError(f"Worksheet dimensions exceed safe form limits: sheet '{sname}' has more than "
                                      f"{MAX_XLSX_CELLS:,} cells")
        for m in _CELL_REF_RE.finditer(data):
            col, row = column_number(m.group(1).decode()), int(m.group(2))
            if row > MAX_XLSX_ROW or col > MAX_XLSX_COL:
                raise UnsupportedXMLError(f"Worksheet dimensions exceed safe form limits: sheet '{sname}' has a cell at "
                                          f"{m.group(1).decode()}{row} (limit: {MAX_XLSX_ROW} rows × {MAX_XLSX_COL} columns)")
        area = 0
        for m in _MERGE_REF_RE.finditer(data):
            r = _RANGE_RE.match(m.group(1).decode("ascii", "replace").strip())
            if not r:
                raise UnsupportedXMLError(f"Worksheet dimensions exceed safe form limits: unreadable merged range "
                                          f"{m.group(1)[:40]!r} in sheet '{sname}'")
            c0, r0 = column_number(r.group(1)), int(r.group(2))
            c1, r1 = (column_number(r.group(3)), int(r.group(4))) if r.group(3) else (c0, r0)
            if max(r0, r1) > MAX_XLSX_ROW or max(c0, c1) > MAX_XLSX_COL:
                raise UnsupportedXMLError(f"Worksheet dimensions exceed safe form limits: merged range {r.group(0)} in "
                                          f"sheet '{sname}' (limit: {MAX_XLSX_ROW} rows × {MAX_XLSX_COL} columns)")
            area += (abs(r1 - r0) + 1) * (abs(c1 - c0) + 1)
        if area > MAX_XLSX_CELLS:
            raise UnsupportedXMLError(f"Worksheet dimensions exceed safe form limits: merged ranges in sheet '{sname}' "
                                      f"cover {area:,} cells")


def _normpath(p: str) -> str:
    parts: list[str] = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)
