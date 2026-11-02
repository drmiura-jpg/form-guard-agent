"""Raw ZIP surgery for OOXML packages, behind an archive safety gate.

``rewrite_zip`` copies every untouched entry *as raw bytes* — local header,
compressed stream and data descriptor exactly as they were — and rebuilds only
the replaced parts, keeping their ZipInfo metadata (name, timestamps, flags,
compression method, attributes, extra fields). Central-directory offsets are
patched accordingly. Nothing is re-compressed unless it was actually edited.

``check_archive`` is the gate every package passes before anything reads it
(scanner, writer, verifier, web upload): it refuses path traversal in entry
names (``..`` segments, a leading ``/`` or ``\\``, drive letters), duplicate
names, more than 2,000 entries, more than 100 MB declared uncompressed size or
an extreme compression ratio (ZIP bombs), and XML parts that carry a DTD or
an entity declaration (XXE / "billion laughs") — in UTF-8, UTF-16 or UTF-32,
with or without a byte-order mark.
"""

from __future__ import annotations

import hashlib
import io
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Union

LFH_SIG = b"PK\x03\x04"
CDH_SIG = b"PK\x01\x02"
EOCD_SIG = b"PK\x05\x06"
DD_SIG = b"PK\x07\x08"
ZIP64_LOC_SIG = b"PK\x06\x07"

MAX_ARCHIVE_ENTRIES = 2_000
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200          # per entry, for entries that expand beyond 1 MiB
_RATIO_FLOOR = 1024 * 1024
XML_PART_SUFFIXES = (".xml", ".rels", ".vml")
_DTD_RE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.I)


class ZipSurgeryError(ValueError):
    pass


class UnsafeArchiveError(ZipSurgeryError):
    """A package Form-Guard refuses to open: path traversal, ZIP bomb, too many entries, DTD / entity declarations."""


def unsafe_entry_name(name: str) -> str:
    """Why an entry name is unsafe (path traversal, absolute path, drive letter, control characters), or ""."""
    if not name:
        return "empty entry name"
    if "\x00" in name or any(ord(ch) < 0x20 for ch in name):
        return "control character in entry name"
    if name[0] in "/\\":
        return "absolute entry name (leading / or \\)"
    if re.match(r"^[A-Za-z]:", name):
        return "entry name with a drive letter"
    if any(seg == ".." for seg in re.split(r"[\\/]", name)):
        return "path traversal (.. segment) in entry name"
    return ""


# byte-order marks, longest first: FF FE 00 00 is UTF-32-LE, not UTF-16-LE followed by U+0000
_BOMS = ((b"\x00\x00\xfe\xff", "utf-32-be"), (b"\xff\xfe\x00\x00", "utf-32-le"), (b"\xef\xbb\xbf", "utf-8"),
         (b"\xfe\xff", "utf-16-be"), (b"\xff\xfe", "utf-16-le"))
_WIDE_ENCODINGS = ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")
_DECLARED_ENCODING_RE = re.compile(rb"""^\s*<\?xml[^>]*?\sencoding\s*=\s*["']([A-Za-z0-9._-]+)["']""")


def xml_text_variants(data: bytes) -> list[bytes]:
    """The XML part as UTF-8 in every encoding a parser could read it in: the one its byte-order mark names (UTF-8,
    UTF-16 or UTF-32, either byte order); BOM-less UTF-16 / UTF-32 in both byte orders whenever the part holds a NUL
    byte (UTF-8 XML never does); and a declared UTF-7 or an EBCDIC ``<?xml`` start."""
    out = []
    for bom, enc in _BOMS:
        if data.startswith(bom):
            out.append(data[len(bom):].decode(enc, "replace").encode("utf-8"))
            break
    else:
        out.append(data)
    if b"\x00" in data:
        out += [data.decode(enc, "replace").encode("utf-8") for enc in _WIDE_ENCODINGS]
    declared = _DECLARED_ENCODING_RE.match(data)
    if declared and declared.group(1).lower().replace(b"_", b"-") in (b"utf-7", b"utf7", b"unicode-1-1-utf-7"):
        out.append(data.decode("utf-7", "replace").encode("utf-8"))
    if data.startswith(b"\x4c\x6f\xa7\x94"):  # "<?xm" in EBCDIC
        out.append(data.decode("cp037", "replace").encode("utf-8"))
    return out


def xml_has_dtd(data: bytes) -> bool:
    """True if an XML part declares a DTD or an entity (``<!DOCTYPE`` / ``<!ENTITY``) in whatever encoding it is
    written — UTF-8, UTF-16 or UTF-32, with or without a byte-order mark. Escaped text such as ``&lt;!DOCTYPE``
    inside an element is content, not a declaration, and is not flagged."""
    return any(_DTD_RE.search(v) for v in xml_text_variants(data))


def check_archive(source: Union[bytes, str, Path, zipfile.ZipFile], *, scan_xml: bool = True,
                  max_entries: int = MAX_ARCHIVE_ENTRIES,
                  max_uncompressed: int = MAX_UNCOMPRESSED_BYTES) -> list[zipfile.ZipInfo]:
    """Refuse an unsafe package before anything else reads it; returns its entries.

    Raises ``UnsafeArchiveError`` (a ``ValueError``) with the reason. With ``scan_xml`` every XML part is read —
    bounded by the size limit just checked — and refused if it declares a DTD or an entity.
    """
    if isinstance(source, zipfile.ZipFile):
        zf, own = source, False
    else:
        try:
            zf = zipfile.ZipFile(io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else Path(source))
        except zipfile.BadZipFile as exc:
            raise ZipSurgeryError(f"not a ZIP archive: {exc}") from exc
        own = True
    try:
        infos = zf.infolist()
        if len(infos) > max_entries:
            raise UnsafeArchiveError(f"{len(infos):,} entries in the package (limit {max_entries:,}) — refused")
        seen: set[str] = set()
        total = 0
        for info in infos:
            why = unsafe_entry_name(info.filename)
            if why:
                raise UnsafeArchiveError(f"{why}: {info.filename[:80]!r} — refused")
            key = info.filename.replace("\\", "/").lower()
            if key in seen:
                raise UnsafeArchiveError(f"duplicate entry name {info.filename[:80]!r} — refused")
            seen.add(key)
            total += info.file_size
            if total > max_uncompressed:
                limit = (f"{max_uncompressed // (1024 * 1024)} MB" if max_uncompressed >= 1024 * 1024
                         else f"{max_uncompressed:,} bytes")
                raise UnsafeArchiveError(f"package expands to more than {limit} (ZIP bomb guard) — refused")
            if info.file_size > _RATIO_FLOOR and info.file_size > MAX_COMPRESSION_RATIO * max(1, info.compress_size):
                raise UnsafeArchiveError(f"{info.filename[:80]}: compression ratio above {MAX_COMPRESSION_RATIO}:1 "
                                         "(ZIP bomb guard) — refused")
        if scan_xml:
            for info in infos:
                if info.filename.lower().endswith(XML_PART_SUFFIXES):
                    try:
                        data = zf.read(info.filename)
                    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, zlib.error) as exc:
                        raise ZipSurgeryError(f"{info.filename}: unreadable entry ({exc})") from exc
                    if xml_has_dtd(data):
                        raise UnsafeArchiveError(f"{info.filename}: DTD / entity declaration in an XML part "
                                                 "(XXE / entity-expansion guard) — refused")
        return infos
    finally:
        if own:
            zf.close()


@dataclass
class RawEntry:
    name: str
    info: zipfile.ZipInfo
    local_header: bytes
    data: bytes
    descriptor: bytes
    central: bytes

    @property
    def raw(self) -> bytes:
        return self.local_header + self.data + self.descriptor

    @property
    def raw_sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()


@dataclass
class RawZip:
    entries: list[RawEntry]
    eocd: bytes

    def entry(self, name: str) -> RawEntry:
        for e in self.entries:
            if e.name == name:
                return e
        raise KeyError(name)


def read_raw_zip(blob: bytes) -> RawZip:
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise ZipSurgeryError(f"not a ZIP archive: {exc}") from exc
    infos = check_archive(zf, scan_xml=False)  # names, entry count and sizes; XML parts are checked at the gate
    eocd_pos = blob.rfind(EOCD_SIG)
    if eocd_pos < 0 or len(blob) - eocd_pos < 22:
        raise ZipSurgeryError("end-of-central-directory record not found")
    if blob.rfind(ZIP64_LOC_SIG, max(0, eocd_pos - 20), eocd_pos) >= 0:
        raise ZipSurgeryError("ZIP64 archives are not supported")
    _sig, _d, _cd, _nd, n_total, cd_size, cd_offset, clen = struct.unpack("<4sHHHHIIH", blob[eocd_pos : eocd_pos + 22])
    if n_total == 0xFFFF or cd_offset == 0xFFFFFFFF:
        raise ZipSurgeryError("ZIP64 archives are not supported")
    if n_total != len(infos):
        raise ZipSurgeryError("central directory entry count mismatch")
    eocd = blob[eocd_pos : eocd_pos + 22 + clen]
    pos = cd_offset
    centrals = []
    for _ in range(n_total):
        if blob[pos : pos + 4] != CDH_SIG:
            raise ZipSurgeryError("corrupt central directory")
        n, m, k = struct.unpack("<HHH", blob[pos + 28 : pos + 34])
        rec_len = 46 + n + m + k
        centrals.append(blob[pos : pos + rec_len])
        pos += rec_len
    entries = []
    for info, central in zip(infos, centrals):
        off = info.header_offset
        if blob[off : off + 4] != LFH_SIG:
            raise ZipSurgeryError(f"{info.filename}: local header signature missing")
        n, m = struct.unpack("<HH", blob[off + 26 : off + 30])
        lh_len = 30 + n + m
        data_start = off + lh_len
        data = blob[data_start : data_start + info.compress_size]
        desc = b""
        if info.flag_bits & 0x08:
            p = data_start + info.compress_size
            desc = blob[p : p + 16] if blob[p : p + 4] == DD_SIG else blob[p : p + 12]
        entries.append(RawEntry(info.filename, info, blob[off:data_start], data, desc, central))
    return RawZip(entries, eocd)


def _compress(data: bytes, method: int) -> bytes:
    if method == zipfile.ZIP_STORED:
        return data
    if method == zipfile.ZIP_DEFLATED:
        c = zlib.compressobj(6, zlib.DEFLATED, -15)
        return c.compress(data) + c.flush()
    raise ZipSurgeryError(f"unsupported compression method {method}")


def rewrite_zip(raw: RawZip, replacements: dict[str, bytes]) -> bytes:
    """Rebuild the archive: untouched entries copied raw, replaced entries re-encoded."""
    unknown = set(replacements) - {e.name for e in raw.entries}
    if unknown:
        raise ZipSurgeryError(f"cannot replace missing entries: {sorted(unknown)}")
    out = bytearray()
    centrals: list[bytes] = []
    for e in raw.entries:
        offset = len(out)
        central = bytearray(e.central)
        if e.name in replacements:
            payload = replacements[e.name]
            cdata = _compress(payload, e.info.compress_type)
            crc = zlib.crc32(payload) & 0xFFFFFFFF
            if len(cdata) >= 0xFFFFFFFF or len(payload) >= 0xFFFFFFFF:
                raise ZipSurgeryError("entry too large")
            lh = bytearray(e.local_header)
            if e.info.flag_bits & 0x08:
                head = DD_SIG if e.descriptor.startswith(DD_SIG) else b""
                desc = head + struct.pack("<III", crc, len(cdata), len(payload))
            else:
                struct.pack_into("<III", lh, 14, crc, len(cdata), len(payload))
                desc = b""
            out += lh + cdata + desc
            struct.pack_into("<III", central, 16, crc, len(cdata), len(payload))
        else:
            out += e.raw
        struct.pack_into("<I", central, 42, offset)
        centrals.append(bytes(central))
    cd_offset = len(out)
    for c in centrals:
        out += c
    cd_size = len(out) - cd_offset
    eocd = bytearray(raw.eocd)
    struct.pack_into("<II", eocd, 12, cd_size, cd_offset)
    out += eocd
    return bytes(out)


FIXED_DATE_TIME = (2026, 11, 2, 0, 0, 0)


def rebuild_deterministic(blob: bytes, drop: set[str] = frozenset(), date_time=FIXED_DATE_TIME,
                          transform=None) -> bytes:
    """Re-pack an archive with fixed metadata (used only to make generated samples reproducible)."""
    src = zipfile.ZipFile(io.BytesIO(blob))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            if info.filename in drop:
                continue
            data = src.read(info.filename)
            if transform is not None:
                data = transform(info.filename, data)
            zi = zipfile.ZipInfo(info.filename, date_time=date_time)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.create_system = 0
            zi.external_attr = 0
            dst.writestr(zi, data)
    return buf.getvalue()
