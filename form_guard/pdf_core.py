"""Minimal, deterministic PDF machinery for fixed PDF forms — Python standard library only.

Form-Guard never re-serialises a blank PDF. It reads the file with this small parser, and a filled copy is the
original bytes followed by one *incremental update* (new objects + a new cross-reference section), exactly how a PDF
viewer saves an edit incrementally. Everything here is fail-closed: a file this module cannot read with certainty is
refused with a :class:`PDFError` instead of being guessed at.

* Parser: classic cross-reference tables, ``/Prev`` chains of incremental updates, literal / hex strings, names
  with ``#xx`` escapes, indirect stream lengths, FlateDecode streams. Cross-reference *streams*, object streams and
  hybrid files (PDF 1.5 compression) are refused, as are encrypted files.
* Safety gate (:func:`check_pdf`): size and object-count limits, a decompression cap per stream and in total (PDF
  bomb), and no active content — JavaScript, Launch / ImportData actions, embedded files, RichMedia and XFA forms
  are refused before anything else looks at the file.
* Builder (:class:`PDFBuilder`): writes small PDFs with a classic cross-reference table, byte-for-byte reproducible
  (the sample generator and the tests use it).
* Content interpreter (:func:`interpret_page`): paths (lines, rectangles, fills), text with the exact origin and
  advance of every glyph (CTM × text matrix, ``/W`` widths, Tc/Tw/Tz/TJ), images and marked-content tags.
"""

from __future__ import annotations

import codecs
import hashlib
import re
import zlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Union

# ------------------------------------------------------------------ limits (fail closed)

MAX_PDF_BYTES = 30 * 1024 * 1024        # a form, not a book
MAX_OBJECTS = 20_000
MAX_STREAM_DECODED = 20 * 1024 * 1024    # one decoded stream
MAX_TOTAL_DECODED = 100 * 1024 * 1024    # all decoded streams together (PDF bomb)
MAX_NESTING = 64                         # arrays / dictionaries inside each other
MAX_XREF_SECTIONS = 64
MAX_FORM_XOBJECT_DEPTH = 6

ACTIVE_ACTIONS = frozenset({
    "JavaScript", "Launch", "ImportData", "RichMedia", "Rendition", "GoToE",
    "SubmitForm", "ResetForm", "URI", "GoToR", "Sound", "Movie", "Hide", "Thread", "SetOCGState",
})
ACTIVE_KEYS = frozenset({
    "JS", "JavaScript", "EmbeddedFiles", "EmbeddedFile", "XFA", "RichMediaContent",
    "OpenAction", "AA",
})


class PDFError(ValueError):
    """A PDF that is malformed, or uses a feature Form-Guard deliberately refuses (fail-closed)."""


# ------------------------------------------------------------------------ object model


@dataclass(frozen=True)
class Ref:
    """An indirect reference ``num gen R``."""

    num: int
    gen: int = 0

    def __str__(self) -> str:
        return f"{self.num} {self.gen} R"


class Name(str):
    """A PDF name object (``/Type`` → ``Name("Type")``)."""

    __slots__ = ()


class Keyword(str):
    """A bare keyword token (``obj``, ``R``, an operator in a content stream …)."""

    __slots__ = ()


@dataclass
class Stream:
    dict: dict
    raw: bytes                # the encoded bytes between ``stream``+EOL and the EOL before ``endstream``
    data_start: int = -1      # absolute file offset of raw[0] (-1: built in memory)

    def decoded(self, limit: int = MAX_STREAM_DECODED) -> bytes:
        return decode_stream(self.dict, self.raw, limit)


@dataclass
class IndirectObject:
    num: int
    gen: int
    value: Any
    span: tuple[int, int]                      # ``N G obj`` … ``endobj`` in the file
    dict_spans: dict[str, tuple[int, int]]     # top-level dictionary entries: key -> value byte span
    dict_span: Optional[tuple[int, int]] = None  # ``<<`` … ``>>`` of the top-level dictionary


# ------------------------------------------------------------------------------ lexer

WHITESPACE = b"\x00\t\n\x0c\r "
_WS = frozenset(WHITESPACE)
_DELIM = frozenset(b"()<>[]{}/%")
_REGULAR_RE = re.compile(rb"[^\x00\t\n\x0c\r ()<>\[\]{}/%]+")
_NUMBER_RE = re.compile(rb"^[+-]?(?:\d+\.?\d*|\.\d+)$")
_INT_RE = re.compile(rb"^[+-]?\d+$")
_ESCAPES = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b", ord("f"): b"\f",
            ord("("): b"(", ord(")"): b")", ord("\\"): b"\\"}


class _Tok:
    __slots__ = ("kind", "value", "start", "end")

    def __init__(self, kind: str, value: Any, start: int, end: int):
        self.kind, self.value, self.start, self.end = kind, value, start, end


class Lexer:
    """Tokens of PDF syntax (objects and content streams). Comments count as whitespace."""

    def __init__(self, data: bytes, pos: int = 0, end: Optional[int] = None):
        self.data = data
        self.pos = pos
        self.end = len(data) if end is None else end

    def skip_ws(self) -> None:
        d, n = self.data, self.end
        while self.pos < n:
            c = d[self.pos]
            if c in _WS:
                self.pos += 1
            elif c == 0x25:  # % comment to end of line
                while self.pos < n and d[self.pos] not in (0x0A, 0x0D):
                    self.pos += 1
            else:
                break

    def next(self) -> Optional[_Tok]:
        self.skip_ws()
        d, i = self.data, self.pos
        if i >= self.end:
            return None
        c = d[i]
        if c == 0x2F:  # /Name
            m = _REGULAR_RE.match(d, i + 1, self.end)
            raw = m.group(0) if m else b""
            self.pos = i + 1 + len(raw)
            return _Tok("name", Name(_decode_name(raw)), i, self.pos)
        if c == 0x28:  # (literal string)
            s, self.pos = _literal_string(d, i, self.end)
            return _Tok("string", s, i, self.pos)
        if c == 0x3C:  # < hex string or << dict
            if i + 1 < self.end and d[i + 1] == 0x3C:
                self.pos = i + 2
                return _Tok("<<", None, i, self.pos)
            j = d.find(b">", i + 1, self.end)
            if j < 0:
                raise PDFError(f"unterminated hex string at byte {i}")
            hexdigits = bytes(b for b in d[i + 1 : j] if b not in _WS)
            if re.search(rb"[^0-9A-Fa-f]", hexdigits):
                raise PDFError(f"invalid hex string at byte {i}")
            if len(hexdigits) % 2:
                hexdigits += b"0"
            self.pos = j + 1
            return _Tok("string", bytes.fromhex(hexdigits.decode("ascii")), i, self.pos)
        if c == 0x3E:
            if i + 1 < self.end and d[i + 1] == 0x3E:
                self.pos = i + 2
                return _Tok(">>", None, i, self.pos)
            raise PDFError(f"stray '>' at byte {i}")
        if c in (0x5B, 0x5D, 0x7B, 0x7D):
            self.pos = i + 1
            return _Tok(chr(c), None, i, self.pos)
        m = _REGULAR_RE.match(d, i, self.end)
        if not m:
            raise PDFError(f"unexpected byte {c:#x} at {i}")
        raw = m.group(0)
        self.pos = m.end()
        if _NUMBER_RE.match(raw):
            return _Tok("int" if _INT_RE.match(raw) else "real",
                        int(raw) if _INT_RE.match(raw) else float(raw), i, self.pos)
        return _Tok("keyword", Keyword(raw.decode("latin-1")), i, self.pos)


def _decode_name(raw: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] == 0x23 and i + 2 < len(raw) and re.fullmatch(rb"[0-9A-Fa-f]{2}", raw[i + 1 : i + 3]):
            out.append(int(raw[i + 1 : i + 3], 16))
            i += 3
        else:
            out.append(raw[i])
            i += 1
    return out.decode("utf-8", "replace")


def _literal_string(d: bytes, i: int, end: int) -> tuple[bytes, int]:
    depth, j, out = 1, i + 1, bytearray()
    while j < end:
        c = d[j]
        if c == 0x5C:  # backslash
            j += 1
            if j >= end:
                break
            e = d[j]
            if e in _ESCAPES:
                out += _ESCAPES[e]
                j += 1
            elif 0x30 <= e <= 0x37:
                k = j
                while k < end and k < j + 3 and 0x30 <= d[k] <= 0x37:
                    k += 1
                out.append(int(d[j:k], 8) & 0xFF)
                j = k
            elif e == 0x0D:  # line continuation
                j += 2 if d[j + 1 : j + 2] == b"\n" else 1
            elif e == 0x0A:
                j += 1
            else:
                out.append(e)
                j += 1
            continue
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                return bytes(out), j + 1
        if c == 0x0D:  # an EOL inside a string reads as \n
            out += b"\n"
            j += 2 if d[j + 1 : j + 2] == b"\n" else 1
            continue
        out.append(c)
        j += 1
    raise PDFError(f"unterminated literal string at byte {i}")


# ----------------------------------------------------------------------------- parser


def _parse_value(lex: Lexer, tok: Optional[_Tok] = None, depth: int = 0, spans: Optional[dict] = None) -> Any:
    """One PDF object. ``spans`` (top level only) collects key -> value byte span of a dictionary."""
    if depth > MAX_NESTING:
        raise PDFError("objects nested too deeply")
    tok = tok or lex.next()
    if tok is None:
        raise PDFError("unexpected end of data")
    k = tok.kind
    if k == "int":
        save = lex.pos
        t2 = lex.next()
        if t2 is not None and t2.kind == "int" and t2.value >= 0 and tok.value >= 0:
            t3 = lex.next()
            if t3 is not None and t3.kind == "keyword" and t3.value == "R":
                return Ref(tok.value, t2.value)
        lex.pos = save
        return tok.value
    if k in ("real", "string", "name"):
        return tok.value
    if k == "<<":
        out: dict[str, Any] = {}
        while True:
            t = lex.next()
            if t is None:
                raise PDFError("unterminated dictionary")
            if t.kind == ">>":
                return out
            if t.kind != "name":
                raise PDFError(f"dictionary key expected at byte {t.start}")
            vt = lex.next()
            if vt is None:
                raise PDFError("unterminated dictionary")
            v = _parse_value(lex, vt, depth + 1)
            if spans is not None:
                spans[str(t.value)] = (vt.start, lex.pos)
            out[str(t.value)] = v
    if k == "[":
        arr = []
        while True:
            t = lex.next()
            if t is None:
                raise PDFError("unterminated array")
            if t.kind == "]":
                return arr
            arr.append(_parse_value(lex, t, depth + 1))
    if k == "keyword":
        if tok.value == "true":
            return True
        if tok.value == "false":
            return False
        if tok.value == "null":
            return None
        return tok.value
    raise PDFError(f"unexpected token {k!r} at byte {tok.start}")


_OBJ_HEAD_RE = re.compile(rb"(\d+)[\x00\t\n\x0c\r ]+(\d+)[\x00\t\n\x0c\r ]+obj(?![^\x00\t\n\x0c\r ()<>\[\]{}/%])")


def parse_indirect_at(data: bytes, offset: int, resolve_length=None, expect_num: Optional[int] = None,
                      expect_gen: Optional[int] = None) -> IndirectObject:
    """The indirect object ``N G obj … endobj`` that starts at ``offset`` (stream included)."""
    m = _OBJ_HEAD_RE.match(data, offset)
    if not m:
        raise PDFError(f"no 'N G obj' at byte {offset}")
    num, gen = int(m.group(1)), int(m.group(2))
    if expect_num is not None and num != expect_num:
        raise PDFError(f"the cross-reference entry of object {expect_num} points at object {num}")
    if expect_gen is not None and gen != expect_gen:
        raise PDFError(f"the cross-reference entry of object {num} has generation {expect_gen}, but the object header is generation {gen}")
    lex = Lexer(data, m.end())
    first = lex.next()
    if first is None:
        raise PDFError(f"object {num}: empty")
    spans: dict[str, tuple[int, int]] = {}
    value = _parse_value(lex, first, 0, spans if first.kind == "<<" else None)
    dict_span = (first.start, lex.pos) if first.kind == "<<" else None
    t = lex.next()
    if t is not None and t.kind == "keyword" and t.value == "stream":
        if not isinstance(value, dict):
            raise PDFError(f"object {num}: 'stream' after a non-dictionary")
        p = t.end
        if data[p : p + 2] == b"\r\n":
            p += 2
        elif data[p : p + 1] in (b"\n", b"\r"):
            p += 1
        else:
            raise PDFError(f"object {num}: no end-of-line after 'stream'")
        length = value.get("Length")
        if isinstance(length, Ref):
            length = resolve_length(length) if resolve_length else None
        if not isinstance(length, int) or length < 0 or p + length > len(data):
            raise PDFError(f"object {num}: missing or impossible /Length")
        raw = data[p : p + length]
        q = p + length
        tail = Lexer(data, q)
        t2 = tail.next()
        if t2 is None or t2.kind != "keyword" or t2.value != "endstream":
            raise PDFError(f"object {num}: /Length does not end at 'endstream' (stream length mismatch)")
        value = Stream(value, raw, p)
        lex.pos = tail.pos
        t = lex.next()
    if t is None or t.kind != "keyword" or t.value != "endobj":
        raise PDFError(f"object {num}: 'endobj' expected")
    return IndirectObject(num, gen, value, (offset, lex.pos), spans, dict_span)


# -------------------------------------------------------------------- stream decoding


def decode_stream(d: dict, raw: bytes, limit: int = MAX_STREAM_DECODED) -> bytes:
    filters = d.get("Filter")
    if filters is None:
        if len(raw) > limit:
            raise PDFError(f"stream larger than {limit:,} bytes")
        return raw
    if isinstance(filters, Name):
        filters = [filters]
    if not isinstance(filters, list) or len(filters) > 1:
        raise PDFError("only a single /FlateDecode filter is supported")
    if filters[0] != "FlateDecode":
        raise PDFError(f"unsupported stream filter /{filters[0]} (only /FlateDecode)")
    parms = d.get("DecodeParms")
    if isinstance(parms, list):
        parms = parms[0] if parms else None
    if isinstance(parms, dict) and parms.get("Predictor", 1) not in (1, None):
        raise PDFError("FlateDecode predictors are not supported")
    dec = zlib.decompressobj()
    try:
        out = dec.decompress(raw, limit + 1)
    except zlib.error as exc:
        raise PDFError(f"corrupt FlateDecode stream: {exc}") from exc
    if len(out) > limit or dec.unconsumed_tail:
        raise PDFError(f"a stream decompresses to more than {limit:,} bytes (PDF bomb guard)")
    return out


# ------------------------------------------------------------------ cross-references


@dataclass
class XrefEntry:
    offset: int
    gen: int
    in_use: bool


@dataclass
class XrefSection:
    offset: int                       # byte offset of the ``xref`` keyword
    entries: dict[int, XrefEntry]
    trailer: dict
    end: int                          # byte offset just after the trailer dictionary
    trailer_spans: dict[str, tuple[int, int]] = field(default_factory=dict)


_XREF_ENTRY_RE = re.compile(rb"(\d{10})[ ](\d{5})[ ]([nf])")
_SUBSECTION_RE = re.compile(rb"(\d+)[ \t]+(\d+)[ \t]*(?:\r\n|\r|\n)")


def _parse_xref_section(data: bytes, offset: int) -> XrefSection:
    if data[offset : offset + 4] != b"xref":
        if _OBJ_HEAD_RE.match(data, offset):
            raise PDFError("cross-reference streams (PDF 1.5 compressed cross-references) are not supported — refused")
        raise PDFError(f"startxref / Prev offset {offset} does not point at a cross-reference table")
    pos = offset + 4
    while pos < len(data) and data[pos] in _WS:
        pos += 1
    entries: dict[int, XrefEntry] = {}
    while True:
        m = _SUBSECTION_RE.match(data, pos)
        if not m:
            break
        start, count = int(m.group(1)), int(m.group(2))
        if count > MAX_OBJECTS or start + count > MAX_OBJECTS * 4:
            raise PDFError("cross-reference table larger than the form limit")
        pos = m.end()
        for k in range(count):
            e = _XREF_ENTRY_RE.match(data, pos)
            if not e:
                raise PDFError(f"malformed cross-reference entry at byte {pos}")
            entries[start + k] = XrefEntry(int(e.group(1)), int(e.group(2)), e.group(3) == b"n")
            pos = e.end()
            while pos < len(data) and data[pos] in b" \r\n":
                pos += 1
    lex = Lexer(data, pos)
    t = lex.next()
    if t is None or t.kind != "keyword" or t.value != "trailer":
        raise PDFError(f"'trailer' expected after the cross-reference table at byte {pos}")
    t2 = lex.next()
    if t2 is None or t2.kind != "<<":
        raise PDFError("trailer dictionary expected")
    spans: dict[str, tuple[int, int]] = {}
    trailer = _parse_value(lex, t2, 0, spans)
    if not isinstance(trailer, dict):
        raise PDFError("trailer is not a dictionary")
    return XrefSection(offset, entries, trailer, lex.pos, spans)


def find_startxref(data: bytes) -> int:
    tail = data[-2048:]
    i = tail.rfind(b"startxref")
    if i < 0:
        raise PDFError("no startxref — not a complete PDF")
    m = re.match(rb"startxref[\x00\t\n\x0c\r ]+(\d+)", tail[i:])
    if not m:
        raise PDFError("unreadable startxref")
    off = int(m.group(1))
    if off <= 0 or off >= len(data):
        raise PDFError("startxref points outside the file")
    return off


# ---------------------------------------------------------------------------- document


@dataclass
class PageRef:
    index: int
    num: int                      # object number of the page dictionary
    dict: dict
    mediabox: tuple[float, float, float, float]
    cropbox: tuple[float, float, float, float]
    rotate: int
    resources: dict
    resources_ref: Any            # the /Resources value as written (a Ref, or an inline / inherited dictionary)

    @property
    def width(self) -> float:
        return self.mediabox[2] - self.mediabox[0]

    @property
    def height(self) -> float:
        return self.mediabox[3] - self.mediabox[1]


class PDFDocument:
    """A parsed PDF. ``objects`` reflects the latest revision; ``sections`` lists every cross-reference section, oldest
    first (one per incremental update)."""

    def __init__(self, data: bytes):
        if len(data) > MAX_PDF_BYTES:
            raise PDFError(f"PDF larger than {MAX_PDF_BYTES // (1024 * 1024)} MB")
        if not re.match(rb"%PDF-(?:1\.[0-7]|2\.0)", data[:8]):
            raise PDFError("not a PDF file (no %PDF-1.x header)")
        if b"%%EOF" not in data[-1024:]:
            raise PDFError("no %%EOF marker at the end — truncated PDF")
        self.data = data
        self.version = data[5:8].decode("ascii")
        self.startxref = find_startxref(data)
        chain, off, seen = [], self.startxref, set()
        while off is not None:
            if off in seen:
                raise PDFError("cross-reference /Prev chain loops")
            if len(chain) >= MAX_XREF_SECTIONS:
                raise PDFError("too many incremental updates")
            seen.add(off)
            sec = _parse_xref_section(data, off)
            if "XRefStm" in sec.trailer:
                raise PDFError("hybrid cross-reference files (/XRefStm) are not supported — refused")
            chain.append(sec)
            prev = sec.trailer.get("Prev")
            if prev is not None and (not isinstance(prev, int) or prev <= 0 or prev >= len(data)):
                raise PDFError("trailer /Prev points outside the file")
            off = prev
        self.sections: list[XrefSection] = list(reversed(chain))
        self.xref: dict[int, XrefEntry] = {}
        for sec in self.sections:
            self.xref.update(sec.entries)
        if sum(1 for e in self.xref.values() if e.in_use) > MAX_OBJECTS:
            raise PDFError(f"more than {MAX_OBJECTS:,} objects")
        self.trailer: dict = dict(self.sections[-1].trailer)
        if "Encrypt" in self.trailer or any("Encrypt" in s.trailer for s in self.sections):
            raise PDFError("encrypted PDF — refused (Form-Guard only reads unencrypted forms)")
        self._cache: dict[int, IndirectObject] = {}
        self._loading: set[int] = set()

    # ---------------------------------------------------------------- objects
    def indirect(self, num: int) -> Optional[IndirectObject]:
        if num in self._cache:
            return self._cache[num]
        e = self.xref.get(num)
        if e is None or not e.in_use:
            return None
        if num in self._loading:
            raise PDFError(f"object {num} refers to itself while loading")
        self._loading.add(num)
        try:
            if not 0 < e.offset < len(self.data):
                raise PDFError(f"object {num}: offset outside the file")
            obj = parse_indirect_at(self.data, e.offset, self._length, expect_num=num, expect_gen=e.gen)
        finally:
            self._loading.discard(num)
        self._cache[num] = obj
        return obj

    def _length(self, ref: Ref) -> Optional[int]:
        e = self.xref.get(ref.num)
        if e is not None and e.in_use and e.gen != ref.gen:
            raise PDFError(f"reference {ref} disagrees with cross-reference generation {e.gen}")
        obj = self.indirect(ref.num)
        return obj.value if obj is not None and isinstance(obj.value, int) else None

    def get(self, value: Any, depth: int = 0) -> Any:
        """Resolve indirect references (a missing object reads as null, as the PDF specification says)."""
        while isinstance(value, Ref):
            if depth > 32:
                raise PDFError("reference chain too long")
            e = self.xref.get(value.num)
            if e is not None and e.in_use and e.gen != value.gen:
                raise PDFError(f"reference {value} disagrees with cross-reference generation {e.gen}")
            obj = self.indirect(value.num)
            value = None if obj is None else obj.value
            depth += 1
        return value

    def in_use(self) -> list[int]:
        return sorted(n for n, e in self.xref.items() if e.in_use and n > 0)

    def load_all(self) -> list[IndirectObject]:
        return [o for o in (self.indirect(n) for n in self.in_use()) if o is not None]

    # ------------------------------------------------------------------- pages
    @property
    def root(self) -> dict:
        root = self.get(self.trailer.get("Root"))
        if not isinstance(root, dict) or root.get("Type") not in (None, "Catalog"):
            raise PDFError("trailer /Root is not a catalog dictionary")
        return root

    def pages(self) -> list[PageRef]:
        out: list[PageRef] = []
        start = self.root.get("Pages")
        seen: set[int] = set()

        def walk(ref: Any, inherited: dict[str, Any], depth: int) -> None:
            if depth > 32 or len(out) > 500:
                raise PDFError("page tree too deep or too large")
            if not isinstance(ref, Ref):
                raise PDFError("page tree nodes must be indirect objects")
            if ref.num in seen:
                raise PDFError("page tree loops")
            seen.add(ref.num)
            node = self.get(ref)
            if not isinstance(node, dict):
                raise PDFError(f"page tree node {ref.num} is not a dictionary")
            inh = dict(inherited)
            for key in ("MediaBox", "CropBox", "Resources", "Rotate"):
                if key in node:
                    inh[key] = node[key]
            if node.get("Type") == "Pages" or ("Kids" in node and node.get("Type") != "Page"):
                for kid in self.get(node.get("Kids")) or []:
                    walk(kid, inh, depth + 1)
                return
            mb = _box(self.get(inh.get("MediaBox")))
            if mb is None:
                raise PDFError(f"page {len(out) + 1}: no /MediaBox")
            cb = _box(self.get(inh.get("CropBox"))) or mb
            res = self.get(inh.get("Resources"))
            rot = self.get(inh.get("Rotate")) or 0
            out.append(PageRef(len(out), ref.num, node, mb, cb, int(rot) if isinstance(rot, (int, float)) else 0,
                               res if isinstance(res, dict) else {}, inh.get("Resources")))

        walk(start, {}, 0)
        if not out:
            raise PDFError("the PDF has no pages")
        return out

    def page_streams(self, page: PageRef) -> list[tuple[Optional[Ref], Stream]]:
        """The content streams of a page, in painting order, with the reference each one was reached by."""
        c = page.dict.get("Contents")
        refs: list[Any] = []
        if isinstance(c, Ref):
            target = self.get(c)
            if isinstance(target, list):
                refs = list(target)
            else:
                refs = [c]
        elif isinstance(c, list):
            refs = list(c)
        elif c is not None:
            raise PDFError("page /Contents must be a stream or an array of streams")
        out = []
        for r in refs:
            s = self.get(r)
            if not isinstance(s, Stream):
                raise PDFError("page /Contents entry is not a stream")
            out.append((r if isinstance(r, Ref) else None, s))
        return out


def _box(v: Any) -> Optional[tuple[float, float, float, float]]:
    if isinstance(v, list) and len(v) == 4 and all(isinstance(x, (int, float)) for x in v):
        x0, y0, x1, y1 = (float(x) for x in v)
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    return None


# ------------------------------------------------------------------------ safety gate


def _active_content(value: Any, depth: int = 0) -> Optional[str]:
    if depth > MAX_NESTING:
        return None
    if isinstance(value, Stream):
        value = value.dict
    if isinstance(value, dict):
        for k, v in value.items():
            if k in ACTIVE_KEYS:
                return f"/{k}"
            if k == "S" and isinstance(v, Name) and v in ACTIVE_ACTIONS:
                return f"/S /{v}"
            if k == "Type" and isinstance(v, Name) and v in ("EmbeddedFile", "Filespec") and "EF" in value:
                return "/EmbeddedFile"
            hit = _active_content(v, depth + 1)
            if hit:
                return hit
    elif isinstance(value, list):
        for v in value:
            hit = _active_content(v, depth + 1)
            if hit:
                return hit
    return None


def check_pdf(source: Union[bytes, str, Any]) -> PDFDocument:
    """The PDF gate (the counterpart of the ZIP archive gate): parse every object, decode every stream under the
    size caps, and refuse encryption and active content. Returns the parsed document."""
    from pathlib import Path

    data = source if isinstance(source, (bytes, bytearray)) else Path(source).read_bytes()
    doc = PDFDocument(bytes(data))
    total = 0
    for obj in doc.load_all():
        hit = _active_content(obj.value)
        if hit:
            raise PDFError(f"active content ({hit}) in object {obj.num} — PDFs with JavaScript, launch actions, "
                           f"embedded files or XFA are refused")
        if isinstance(obj.value, Stream):
            if obj.value.dict.get("Type") in ("XRef", "ObjStm"):
                raise PDFError("object / cross-reference streams (PDF 1.5 compression) are not supported — refused")
            try:
                total += len(obj.value.decoded())
            except PDFError as exc:
                if obj.value.dict.get("Subtype") == "Image" and str(exc).startswith("unsupported stream filter "):
                    # Opaque image codecs (DCTDecode, CCITTFaxDecode …) are not Flate-inflated; count raw bytes against caps
                    if len(obj.value.raw) > MAX_STREAM_DECODED:
                        raise PDFError(f"image stream larger than {MAX_STREAM_DECODED:,} bytes") from exc
                    total += len(obj.value.raw)
                else:
                    raise
            if total > MAX_TOTAL_DECODED:
                raise PDFError(f"streams decompress to more than {MAX_TOTAL_DECODED // (1024 * 1024)} MB (PDF bomb guard)")
    doc.root  # noqa: B018 - raises if the catalog is unusable
    doc.pages()
    return doc


def is_pdf_bytes(head: bytes) -> bool:
    return head[:5] == b"%PDF-"


# ---------------------------------------------------------------------- text strings


def pdf_text(value: Any) -> str:
    """A PDF text string (UTF-16BE with BOM, UTF-8 with BOM, or PDFDocEncoding ≈ Latin-1) as str."""
    if isinstance(value, str):
        return str(value)
    if not isinstance(value, (bytes, bytearray)):
        return ""
    b = bytes(value)
    if b[:2] == codecs.BOM_UTF16_BE:
        return b[2:].decode("utf-16-be", "replace")
    if b[:3] == codecs.BOM_UTF8:
        return b[3:].decode("utf-8", "replace")
    return b.decode("latin-1")


def text_string(s: str) -> bytes:
    """Serialise a Python string as a PDF text string token: ASCII as a literal, anything else UTF-16BE hex."""
    if all(32 <= ord(ch) < 127 for ch in s):
        return b"(" + s.encode("ascii").replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"
    return b"<FEFF" + s.encode("utf-16-be").hex().upper().encode("ascii") + b">"


# ---------------------------------------------------------------------- serialisation


def fmt_num(v: float) -> bytes:
    if isinstance(v, bool):
        raise TypeError("bool is not a PDF number")
    if isinstance(v, int):
        return str(v).encode("ascii")
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return (s if s not in ("-0", "") else "0").encode("ascii")


def _name_bytes(n: str) -> bytes:
    out = bytearray(b"/")
    for b in n.encode("utf-8"):
        if b < 0x21 or b > 0x7E or b in b"#()<>[]{}/%":
            out += b"#%02X" % b
        else:
            out.append(b)
    return bytes(out)


def serialize(v: Any) -> bytes:
    if isinstance(v, Ref):
        return f"{v.num} {v.gen} R".encode("ascii")
    if isinstance(v, Name):
        return _name_bytes(v)
    if isinstance(v, bool):
        return b"true" if v else b"false"
    if v is None:
        return b"null"
    if isinstance(v, (int, float)):
        return fmt_num(v)
    if isinstance(v, (bytes, bytearray)):
        return b"(" + bytes(v).replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"
    if isinstance(v, str):
        return text_string(v)
    if isinstance(v, list):
        return b"[" + b" ".join(serialize(x) for x in v) + b"]"
    if isinstance(v, dict):
        return b"<< " + b" ".join(_name_bytes(k) + b" " + serialize(x) for k, x in v.items()) + b" >>"
    if isinstance(v, Raw):
        return v.data
    raise TypeError(f"cannot serialise {type(v).__name__}")


@dataclass
class Raw:
    """Bytes written verbatim (a pre-serialised token)."""

    data: bytes


class PDFBuilder:
    """Write a small PDF with a classic cross-reference table — byte-for-byte reproducible."""

    def __init__(self, version: str = "1.7"):
        self.version = version
        self.objects: list[Optional[bytes]] = [None]

    def reserve(self) -> Ref:
        self.objects.append(None)
        return Ref(len(self.objects) - 1, 0)

    def set(self, ref: Ref, value: Any, stream: Optional[bytes] = None, compress: bool = False) -> Ref:
        self.objects[ref.num] = self._body(value, stream, compress)
        return ref

    def add(self, value: Any, stream: Optional[bytes] = None, compress: bool = False) -> Ref:
        return self.set(self.reserve(), value, stream, compress)

    @staticmethod
    def _body(value: Any, stream: Optional[bytes], compress: bool) -> bytes:
        if stream is None:
            return serialize(value)
        d = dict(value or {})
        data = stream
        if compress:
            data = zlib.compress(stream, 9)
            d["Filter"] = Name("FlateDecode")
        d["Length"] = len(data)
        return serialize(d) + b"\nstream\n" + data + b"\nendstream"

    def build(self, root: Ref, info: Optional[Ref] = None, file_id: Optional[bytes] = None) -> bytes:
        out = bytearray(f"%PDF-{self.version}\n".encode("ascii") + b"%\xe2\xe3\xcf\xd3\n")
        offsets: list[int] = [0]
        for num in range(1, len(self.objects)):
            body = self.objects[num]
            if body is None:
                raise PDFError(f"object {num} was reserved but never written")
            offsets.append(len(out))
            out += f"{num} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(self.objects)}\n".encode("ascii") + b"0000000000 65535 f \n"
        for off in offsets[1:]:
            out += f"{off:010d} 00000 n \n".encode("ascii")
        fid = file_id or hashlib.md5(bytes(out)).digest()
        trailer: dict[str, Any] = {"Size": len(self.objects), "Root": root}
        if info is not None:
            trailer["Info"] = info
        trailer["ID"] = [Raw(b"<" + fid.hex().upper().encode() + b">"), Raw(b"<" + fid.hex().upper().encode() + b">")]
        out += b"trailer\n" + serialize(trailer) + f"\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
        return bytes(out)


def incremental_update(original: bytes, doc: PDFDocument, objects: dict[int, bytes], new_id: Optional[bytes] = None
                       ) -> tuple[bytes, dict[int, tuple[int, int]]]:
    """``original`` + one incremental update holding ``objects`` (object number -> serialised body).

    Returns (new file bytes, object number -> byte span of ``N 0 obj … endobj`` in the new file). The original bytes
    are kept as an exact prefix; a missing final end-of-line is the only thing appended before the update."""
    out = bytearray(original)
    if not out.endswith((b"\n", b"\r")):
        out += b"\n"
    spans: dict[int, tuple[int, int]] = {}
    gens: dict[int, int] = {}
    for num in sorted(objects):
        old_entry = doc.xref.get(num)
        gen = old_entry.gen if (old_entry is not None and old_entry.in_use) else 0
        gens[num] = gen
        start = len(out)
        out += f"{num} {gen} obj\n".encode("ascii") + objects[num] + b"\nendobj\n"
        spans[num] = (start, len(out))
    xref = len(out)
    out += b"xref\n0 1\n0000000000 65535 f \n"
    nums = sorted(objects)
    i = 0
    while i < len(nums):  # contiguous subsections
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out += f"{nums[i]} {j - i + 1}\n".encode("ascii")
        for n in nums[i : j + 1]:
            out += f"{spans[n][0]:010d} {gens[n]:05d} n \n".encode("ascii")
        i = j + 1
    size = max([doc.trailer.get("Size", 0) or 0] + [n + 1 for n in nums])
    trailer: dict[str, Any] = {"Size": size}
    last = doc.sections[-1]
    for key in ("Root", "Info"):
        if key in last.trailer_spans:  # the value exactly as the original trailer wrote it
            a, b = last.trailer_spans[key]
            trailer[key] = Raw(doc.data[a:b])
    old_id = doc.trailer.get("ID")
    first = old_id[0] if isinstance(old_id, list) and old_id and isinstance(old_id[0], bytes) else None
    nid = new_id or hashlib.md5(bytes(out)).digest()
    first = first or nid
    trailer["ID"] = [Raw(b"<" + first.hex().upper().encode() + b">"), Raw(b"<" + nid.hex().upper().encode() + b">")]
    trailer["Prev"] = doc.startxref
    out += b"trailer\n" + serialize(trailer) + f"\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    return bytes(out), spans


# ------------------------------------------------------------------------------- fonts

UCS2_CMAPS = frozenset({"UniJIS-UCS2-H", "UniJIS-UCS2-HW-H", "UniJIS-UTF16-H", "UniGB-UCS2-H", "UniGB-UTF16-H",
                        "UniCNS-UCS2-H", "UniCNS-UTF16-H", "UniKS-UCS2-H", "UniKS-UTF16-H"})
ASCII_WIDTH = 500   # declared /W width of the Latin characters of the form font (half-width)


def ascii_cid(cp: int) -> Optional[int]:
    """Adobe-Japan1 CID that UniJIS-UCS2-H maps a printable ASCII code point to (proportional Roman, CIDs 1–95)."""
    if cp == 0x5C:
        return 97
    if cp == 0x7E:
        return 100
    if 0x20 <= cp <= 0x7D:
        return cp - 31
    return None


def _parse_widths(w: Any, doc: Optional[PDFDocument]) -> dict[int, float]:
    out: dict[int, float] = {}
    g = (lambda v: doc.get(v)) if doc is not None else (lambda v: v)
    w = g(w)
    if not isinstance(w, list):
        return out
    i = 0
    while i < len(w):
        first = g(w[i])
        nxt = g(w[i + 1]) if i + 1 < len(w) else None
        if isinstance(first, int) and isinstance(nxt, list):
            for k, x in enumerate(nxt):
                x = g(x)
                if isinstance(x, (int, float)):
                    out[first + k] = float(x)
            i += 2
        elif isinstance(first, int) and isinstance(nxt, int) and i + 2 < len(w):
            last, width = nxt, g(w[i + 2])
            if isinstance(width, (int, float)) and last - first < 70_000:
                for c in range(first, last + 1):
                    out[c] = float(width)
            i += 3
        else:
            break
    return out


_BF_RE = re.compile(rb"begin(bfchar|bfrange)(.*?)end\1", re.S)


def parse_tounicode(data: bytes) -> dict[int, str]:
    out: dict[int, str] = {}

    def u(h: bytes) -> str:
        try:
            return bytes.fromhex(h.decode("ascii")).decode("utf-16-be", "replace")
        except ValueError:
            return ""

    for kind, body in _BF_RE.findall(data):
        toks = re.findall(rb"<([0-9A-Fa-f]*)>|(\[)|(\])", body)
        flat: list[Any] = []
        arr: Optional[list] = None
        for h, lb, rb in toks:
            if lb:
                arr = []
            elif rb:
                flat.append(arr)
                arr = None
            elif arr is not None:
                arr.append(h)
            else:
                flat.append(h)
        if kind == b"bfchar":
            for i in range(0, len(flat) - 1, 2):
                if isinstance(flat[i], bytes) and isinstance(flat[i + 1], bytes):
                    out[int(flat[i] or b"0", 16)] = u(flat[i + 1])
        else:
            for i in range(0, len(flat) - 2, 3):
                lo, hi, dst = flat[i], flat[i + 1], flat[i + 2]
                if not (isinstance(lo, bytes) and isinstance(hi, bytes)):
                    continue
                a, b = int(lo or b"0", 16), int(hi or b"0", 16)
                if b - a > 70_000:
                    continue
                if isinstance(dst, list):
                    for k, h in enumerate(dst):
                        out[a + k] = u(h)
                elif isinstance(dst, bytes):
                    base = u(dst)
                    for k in range(b - a + 1):
                        out[a + k] = base[:-1] + chr(ord(base[-1]) + k) if base else ""
    return out


class FontInfo:
    """How to decode a font's character codes into Unicode text and advance widths."""

    def __init__(self, name: str, fdict: Any, doc: Optional[PDFDocument]):
        g = (lambda v: doc.get(v)) if doc is not None else (lambda v: v)
        self.name = name
        self.dict = fdict if isinstance(fdict, dict) else {}
        self.subtype = str(self.dict.get("Subtype") or "")
        enc = g(self.dict.get("Encoding"))
        self.cmap = str(enc) if isinstance(enc, Name) else ""
        self.two_byte = self.subtype == "Type0"
        self.dw, self.widths = 1000.0, {}
        self.first_char, self.simple_widths = 0, []
        self.base_font = str(self.dict.get("BaseFont") or "")
        if self.two_byte:
            desc = g(self.dict.get("DescendantFonts"))
            d0 = g(desc[0]) if isinstance(desc, list) and desc else {}
            if isinstance(d0, dict):
                dw = g(d0.get("DW"))
                self.dw = float(dw) if isinstance(dw, (int, float)) else 1000.0
                self.widths = _parse_widths(d0.get("W"), doc)
        else:
            fc = g(self.dict.get("FirstChar"))
            self.first_char = int(fc) if isinstance(fc, int) else 0
            ws = g(self.dict.get("Widths"))
            self.simple_widths = [float(g(x)) if isinstance(g(x), (int, float)) else 500.0 for x in ws] \
                if isinstance(ws, list) else []
        self.tounicode: dict[int, str] = {}
        tu = g(self.dict.get("ToUnicode"))
        if isinstance(tu, Stream):
            try:
                self.tounicode = parse_tounicode(tu.decoded())
            except PDFError:
                self.tounicode = {}
        self.simple_codec = "cp1252"
        if isinstance(enc, Name) and enc == "MacRomanEncoding":
            self.simple_codec = "mac_roman"

    @property
    def unicode_encoding(self) -> bool:
        """Codes are Unicode code units: any (BMP) text can be shown with this font."""
        return self.two_byte and self.cmap in UCS2_CMAPS

    def width_of_unicode(self, ch: str) -> float:
        """Advance width (1/1000 em) of ``ch`` when shown with this font (Unicode CMap fonts)."""
        cid = ascii_cid(ord(ch))
        if cid is not None and cid in self.widths:
            return self.widths[cid]
        return self.dw

    def encode(self, text: str) -> bytes:
        if not self.unicode_encoding:
            raise PDFError(f"font /{self.name} has no Unicode encoding the overlay can use")
        out = bytearray()
        for ch in text:
            cp = ord(ch)
            if cp > 0xFFFF:
                if "UTF16" not in self.cmap:
                    raise PDFError(f"character U+{cp:X} is outside the UCS-2 range of font /{self.name}")
                out += ch.encode("utf-16-be")
            else:
                out += cp.to_bytes(2, "big")
        return bytes(out)

    def decode(self, s: bytes) -> list[tuple[bytes, str, float]]:
        """[(code bytes, unicode text, advance width in 1/1000 em)] for a shown string."""
        out: list[tuple[bytes, str, float]] = []
        if self.two_byte:
            i = 0
            while i + 1 < len(s):
                code = s[i : i + 2]
                cp = int.from_bytes(code, "big")
                if self.unicode_encoding:
                    if "UTF16" in self.cmap and 0xD800 <= cp < 0xDC00 and i + 3 < len(s):
                        code = s[i : i + 4]
                        ch = code.decode("utf-16-be", "replace")
                        i += 4
                        out.append((code, ch, self.dw))
                        continue
                    ch = self.tounicode.get(cp) or chr(cp)
                    out.append((code, ch, self.width_of_unicode(chr(cp))))
                else:  # Identity-H (or another CID CMap): the code is the CID
                    ch = self.tounicode.get(cp, "�")
                    out.append((code, ch, self.widths.get(cp, self.dw)))
                i += 2
            return out
        for b in s:
            ch = self.tounicode.get(b) or bytes([b]).decode(self.simple_codec, "replace")
            k = b - self.first_char
            w = self.simple_widths[k] if 0 <= k < len(self.simple_widths) else 500.0
            out.append((bytes([b]), ch, w))
        return out


def font_resources(doc: Optional[PDFDocument], resources: dict) -> dict[str, FontInfo]:
    g = (lambda v: doc.get(v)) if doc is not None else (lambda v: v)
    fonts = g(resources.get("Font")) if isinstance(resources, dict) else None
    out: dict[str, FontInfo] = {}
    if isinstance(fonts, dict):
        for name, ref in fonts.items():
            out[name] = FontInfo(name, g(ref), doc)
    return out


# ------------------------------------------------------------------ content streams


def content_tokens(data: bytes) -> Iterable[tuple[str, list[Any], tuple[int, int]]]:
    """(operator, operands, byte span of operands+operator) for a content stream; inline images are skipped."""
    lex = Lexer(data)
    operands: list[Any] = []
    op_start: Optional[int] = None
    while True:
        tok = lex.next()
        if tok is None:
            if operands:
                raise PDFError("content stream ends with dangling operands")
            return
        if op_start is None:
            op_start = tok.start
        if tok.kind == "keyword":
            op = str(tok.value)
            if op in ("true", "false", "null"):
                operands.append({"true": True, "false": False, "null": None}[op])
                continue
            if op == "BI":  # inline image: skip to EI after the binary data
                j = data.find(b"ID", tok.end)
                if j < 0:
                    raise PDFError("inline image without ID")
                k = j + 3
                m = re.search(rb"[\x00\t\n\x0c\r ]EI(?=[\x00\t\n\x0c\r ]|$)", data[k:])
                if not m:
                    raise PDFError("inline image without EI")
                lex.pos = k + m.end()
                yield "BI", [], (op_start, lex.pos)
                operands, op_start = [], None
                continue
            yield op, operands, (op_start, tok.end)
            operands, op_start = [], None
            continue
        operands.append(_parse_value(lex, tok, 1))


def _mul(a: tuple, b: tuple) -> tuple:
    """Matrix product a × b for [a b c d e f] affine matrices (PDF row-vector convention)."""
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5])


def _apply(m: tuple, x: float, y: float) -> tuple[float, float]:
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


@dataclass
class Glyph:
    ch: str
    x: float          # origin, user space (pt)
    y: float          # baseline, user space (pt)
    adv: float        # advance width, user space (pt)
    size: float       # font size in user space (pt)
    font: str
    code: bytes


@dataclass
class TextRun:
    glyphs: list[Glyph]
    font: str
    size: float
    stream: int                     # index of the content stream on the page (or of the top-level stream)
    tags: tuple                     # marked-content stack: ((tag, properties dict), …)
    span: tuple[int, int]           # bytes of the show operator in the decoded stream
    color: tuple[float, float, float] = (0.0, 0.0, 0.0)
    invisible: bool = False
    rotated: bool = False

    @property
    def text(self) -> str:
        return "".join(g.ch for g in self.glyphs)

    def bbox(self) -> tuple[float, float, float, float]:
        """Glyph boxes (em box: 12 % below the baseline to 88 % above) united."""
        x0 = min(g.x for g in self.glyphs)
        x1 = max(g.x + g.adv for g in self.glyphs)
        y0 = min(g.y - 0.12 * g.size for g in self.glyphs)
        y1 = max(g.y + 0.88 * g.size for g in self.glyphs)
        return (x0, y0, x1, y1)


@dataclass
class PathPaint:
    op: str                                              # S s f F f* B B* b b*
    segments: list[tuple[float, float, float, float]]    # (x0, y0, x1, y1) user space
    rects: list[tuple[float, float, float, float]]       # axis-aligned rectangles from ``re`` (x0, y0, x1, y1)
    line_width: float
    stroke: tuple[float, float, float]
    fill: tuple[float, float, float]
    stream: int
    tags: tuple
    span: tuple[int, int]

    @property
    def stroked(self) -> bool:
        return self.op in ("S", "s", "B", "B*", "b", "b*")

    @property
    def filled(self) -> bool:
        return self.op in ("f", "F", "f*", "B", "B*", "b", "b*")


@dataclass
class PageGraphics:
    mediabox: tuple[float, float, float, float]
    runs: list[TextRun] = field(default_factory=list)
    paths: list[PathPaint] = field(default_factory=list)
    images: list[tuple[float, float, float, float, int]] = field(default_factory=list)   # bbox + stream index
    operators: list[tuple[int, str]] = field(default_factory=list)                      # (stream index, op)
    notes: list[str] = field(default_factory=list)

    @property
    def width(self) -> float:
        return self.mediabox[2] - self.mediabox[0]

    @property
    def height(self) -> float:
        return self.mediabox[3] - self.mediabox[1]


def _color(ops: list[Any]) -> tuple[float, float, float]:
    nums = [float(x) for x in ops if isinstance(x, (int, float))]
    if len(nums) == 1:
        return (nums[0], nums[0], nums[0])
    if len(nums) == 3:
        return (nums[0], nums[1], nums[2])
    if len(nums) == 4:
        c, m, y, k = nums
        return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    return (0.0, 0.0, 0.0)


def interpret(streams: list[bytes], resources: dict, doc: Optional[PDFDocument],
              mediabox: tuple[float, float, float, float], stream_offset: int = 0,
              out: Optional[PageGraphics] = None, ctm0: tuple = IDENTITY, depth: int = 0,
              props: Optional[dict] = None) -> PageGraphics:
    """Run the content streams of one page (each stream is decoded bytes) and collect what they paint."""
    pg = out or PageGraphics(mediabox)
    g = (lambda v: doc.get(v)) if doc is not None else (lambda v: v)
    fonts = font_resources(doc, resources)
    xobjects = g(resources.get("XObject")) if isinstance(resources, dict) else None
    xobjects = xobjects if isinstance(xobjects, dict) else {}
    properties = g(resources.get("Properties")) if isinstance(resources, dict) else None
    properties = properties if isinstance(properties, dict) else {}
    gs: dict[str, Any] = {"ctm": ctm0, "lw": 1.0, "stroke": (0.0, 0.0, 0.0), "fill": (0.0, 0.0, 0.0),
                          "Tc": 0.0, "Tw": 0.0, "Th": 1.0, "TL": 0.0, "font": None, "fs": 0.0, "Ts": 0.0, "Tr": 0}
    stack: list[dict[str, Any]] = []
    tags: list[tuple[str, dict]] = []
    tm = tlm = IDENTITY
    path_segs: list[tuple[float, float, float, float]] = []
    path_rects: list[tuple[float, float, float, float]] = []
    cur: Optional[tuple[float, float]] = None
    start_pt: Optional[tuple[float, float]] = None
    in_text = False
    for si, data in enumerate(streams):
        sidx = stream_offset + si
        for op, args, span in content_tokens(data):
            pg.operators.append((sidx, op))
            nums = [a for a in args if isinstance(a, (int, float)) and not isinstance(a, bool)]
            if op == "q":
                stack.append(dict(gs))
            elif op == "Q":
                if stack:
                    gs = stack.pop()
            elif op == "cm" and len(nums) == 6:
                gs["ctm"] = _mul(tuple(float(n) for n in nums), gs["ctm"])
            elif op == "w" and nums:
                gs["lw"] = float(nums[0])
            elif op in ("g", "rg", "k", "sc", "scn"):
                gs["fill"] = _color(args)
            elif op in ("G", "RG", "K", "SC", "SCN"):
                gs["stroke"] = _color(args)
            elif op == "m" and len(nums) >= 2:
                cur = start_pt = _apply(gs["ctm"], float(nums[0]), float(nums[1]))
            elif op == "l" and len(nums) >= 2 and cur is not None:
                p = _apply(gs["ctm"], float(nums[0]), float(nums[1]))
                path_segs.append((cur[0], cur[1], p[0], p[1]))
                cur = p
            elif op in ("c", "v", "y") and len(nums) >= 4 and cur is not None:
                p = _apply(gs["ctm"], float(nums[-2]), float(nums[-1]))
                path_segs.append((cur[0], cur[1], p[0], p[1]))  # a curve is reduced to its chord
                cur = p
            elif op == "h" and cur is not None and start_pt is not None:
                if cur != start_pt:
                    path_segs.append((cur[0], cur[1], start_pt[0], start_pt[1]))
                cur = start_pt
            elif op == "re" and len(nums) >= 4:
                x, y, w, h = (float(n) for n in nums[:4])
                pts = [_apply(gs["ctm"], px, py) for px, py in ((x, y), (x + w, y), (x + w, y + h), (x, y + h))]
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                path_rects.append((min(xs), min(ys), max(xs), max(ys)))
                for a, b in zip(pts, pts[1:] + pts[:1]):
                    path_segs.append((a[0], a[1], b[0], b[1]))
                cur = start_pt = pts[0]
            elif op in ("S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"):
                if op in ("s", "b", "b*") and cur is not None and start_pt is not None and cur != start_pt:
                    path_segs.append((cur[0], cur[1], start_pt[0], start_pt[1]))
                if op != "n" and (path_segs or path_rects):
                    scale = (abs(gs["ctm"][0]) + abs(gs["ctm"][3])) / 2 or 1.0
                    pg.paths.append(PathPaint(op, path_segs, path_rects, gs["lw"] * scale, gs["stroke"], gs["fill"],
                                              sidx, tuple(tags), span))
                path_segs, path_rects, cur, start_pt = [], [], None, None
            elif op in ("W", "W*", "d", "J", "j", "M", "ri", "i", "gs", "cs", "CS", "sh", "d0", "d1", "BX", "EX",
                        "MP", "DP"):
                if op == "sh":
                    pg.notes.append("shading")
                elif op in ("W", "W*"):
                    pg.notes.append("clipping")
            elif op == "BT":
                in_text = True
                tm = tlm = IDENTITY
            elif op == "ET":
                in_text = False
            elif op == "Tc" and nums:
                gs["Tc"] = float(nums[0])
            elif op == "Tw" and nums:
                gs["Tw"] = float(nums[0])
            elif op == "Tz" and nums:
                gs["Th"] = float(nums[0]) / 100.0
            elif op == "TL" and nums:
                gs["TL"] = float(nums[0])
            elif op == "Ts" and nums:
                gs["Ts"] = float(nums[0])
            elif op == "Tr" and nums:
                gs["Tr"] = int(nums[0])
            elif op == "Tf" and len(args) >= 2:
                gs["font"] = str(args[0]) if isinstance(args[0], Name) else None
                gs["fs"] = float(args[1]) if isinstance(args[1], (int, float)) else 0.0
            elif op in ("Td", "TD") and len(nums) >= 2:
                if op == "TD":
                    gs["TL"] = -float(nums[1])
                tlm = _mul((1.0, 0.0, 0.0, 1.0, float(nums[0]), float(nums[1])), tlm)
                tm = tlm
            elif op == "Tm" and len(nums) >= 6:
                tlm = tm = tuple(float(n) for n in nums[:6])
            elif op == "T*":
                tlm = _mul((1.0, 0.0, 0.0, 1.0, 0.0, -gs["TL"]), tlm)
                tm = tlm
            elif op in ("Tj", "TJ", "'", '"'):
                if not in_text:
                    pg.notes.append("text shown outside BT/ET")
                if op in ("'", '"'):
                    if op == '"' and len(nums) >= 2:
                        gs["Tw"], gs["Tc"] = float(nums[0]), float(nums[1])
                    tlm = _mul((1.0, 0.0, 0.0, 1.0, 0.0, -gs["TL"]), tlm)
                    tm = tlm
                items = args[-1] if op == "TJ" and args and isinstance(args[-1], list) else \
                    [a for a in args if isinstance(a, bytes)][-1:]
                font = fonts.get(gs["font"] or "")
                glyphs: list[Glyph] = []
                fs, th = gs["fs"], gs["Th"]
                for item in items:
                    if isinstance(item, (int, float)) and not isinstance(item, bool):
                        tm = _mul((1.0, 0.0, 0.0, 1.0, -float(item) / 1000.0 * fs * th, 0.0), tm)
                        continue
                    if not isinstance(item, bytes):
                        continue
                    decoded = font.decode(item) if font else [(bytes([b]), "�", 500.0) for b in item]
                    for code, ch, w in decoded:
                        trm = _mul((fs * th, 0.0, 0.0, fs, 0.0, gs["Ts"]), _mul(tm, gs["ctm"]))
                        ox, oy = trm[4], trm[5]
                        word = gs["Tw"] if code == b" " else 0.0
                        tx = ((w / 1000.0) * fs + gs["Tc"] + word) * th
                        end = _apply(_mul(_mul((1.0, 0.0, 0.0, 1.0, tx, 0.0), tm), gs["ctm"]), 0.0, 0.0)
                        size = (trm[2] ** 2 + trm[3] ** 2) ** 0.5
                        glyphs.append(Glyph(ch, ox, oy, end[0] - ox, size, gs["font"] or "", code))
                        tm = _mul((1.0, 0.0, 0.0, 1.0, tx, 0.0), tm)
                if glyphs:
                    m = _mul(tm, gs["ctm"])
                    rotated = abs(m[1]) > 1e-6 or abs(m[2]) > 1e-6
                    pg.runs.append(TextRun(glyphs, gs["font"] or "", glyphs[0].size, sidx, tuple(tags), span,
                                           gs["fill"], gs["Tr"] in (3, 7), rotated))
            elif op in ("BMC", "BDC"):
                tag = str(args[0]) if args and isinstance(args[0], Name) else ""
                p: dict = {}
                if op == "BDC" and len(args) >= 2:
                    p = args[1] if isinstance(args[1], dict) else (g(properties.get(str(args[1]))) or {})
                tags.append((tag, p if isinstance(p, dict) else {}))
            elif op == "EMC":
                if tags:
                    tags.pop()
            elif op == "Do" and args and isinstance(args[0], Name):
                xo = g(xobjects.get(str(args[0])))
                if isinstance(xo, Stream) and xo.dict.get("Subtype") == "Image":
                    pts = [_apply(gs["ctm"], px, py) for px, py in ((0, 0), (1, 0), (1, 1), (0, 1))]
                    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                    pg.images.append((min(xs), min(ys), max(xs), max(ys), sidx))
                elif isinstance(xo, Stream) and xo.dict.get("Subtype") == "Form" and depth < MAX_FORM_XOBJECT_DEPTH:
                    mtx = g(xo.dict.get("Matrix"))
                    mt = tuple(float(x) for x in mtx) if isinstance(mtx, list) and len(mtx) == 6 else IDENTITY
                    res = g(xo.dict.get("Resources"))
                    interpret([xo.decoded()], res if isinstance(res, dict) else resources, doc, mediabox, sidx, pg,
                              _mul(mt, gs["ctm"]), depth + 1)
                else:
                    pg.notes.append(f"XObject /{args[0]} not drawn")
            elif op == "BI":
                pts = [_apply(gs["ctm"], px, py) for px, py in ((0, 0), (1, 0), (1, 1), (0, 1))]
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                pg.images.append((min(xs), min(ys), max(xs), max(ys), sidx))
            else:
                pg.notes.append(f"operator {op} ignored")
    return pg


def interpret_page(doc: PDFDocument, page: PageRef) -> PageGraphics:
    streams = [s.decoded() for _, s in doc.page_streams(page)]
    return interpret(streams, page.resources, doc, page.mediabox)


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Canvas:
    """Write content-stream operators (the sample generator and the overlay writer use the same few)."""

    def __init__(self) -> None:
        self.ops: list[bytes] = []

    def raw(self, line: Union[str, bytes]) -> "Canvas":
        self.ops.append(line.encode("latin-1") if isinstance(line, str) else line)
        return self

    def rect(self, x0: float, y0: float, x1: float, y1: float, width: float = 0.6, fill: Optional[float] = None,
             stroke: bool = True) -> "Canvas":
        body = b" ".join(fmt_num(round(v, 2)) for v in (x0, y0, x1 - x0, y1 - y0)) + b" re"
        if fill is not None:
            self.ops.append(b"q " + fmt_num(fill) + b" g " + body + b" f Q")
        if stroke:
            self.ops.append(fmt_num(width) + b" w " + body + b" S")
        return self

    def line(self, x0: float, y0: float, x1: float, y1: float, width: float = 0.6) -> "Canvas":
        self.ops.append(fmt_num(width) + b" w " + b" ".join(fmt_num(round(v, 2)) for v in (x0, y0)) + b" m "
                        + b" ".join(fmt_num(round(v, 2)) for v in (x1, y1)) + b" l S")
        return self

    def text(self, font: str, size: float, x: float, y: float, encoded: bytes, gray: float = 0.0) -> "Canvas":
        self.ops.append(b"BT " + _name_bytes(font) + b" " + fmt_num(size) + b" Tf " + fmt_num(gray) + b" g 1 0 0 1 "
                        + fmt_num(round(x, 3)) + b" " + fmt_num(round(y, 3)) + b" Tm <" + encoded.hex().upper().encode()
                        + b"> Tj ET")
        return self

    def data(self) -> bytes:
        return b"\n".join(self.ops) + b"\n"
