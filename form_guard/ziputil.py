"""Raw ZIP surgery for OOXML packages.

``rewrite_zip`` copies every untouched entry *as raw bytes* — local header,
compressed stream and data descriptor exactly as they were — and rebuilds only
the replaced parts, keeping their ZipInfo metadata (name, timestamps, flags,
compression method, attributes, extra fields). Central-directory offsets are
patched accordingly. Nothing is re-compressed unless it was actually edited.
"""

from __future__ import annotations

import hashlib
import io
import struct
import zipfile
import zlib
from dataclasses import dataclass

LFH_SIG = b"PK\x03\x04"
CDH_SIG = b"PK\x01\x02"
EOCD_SIG = b"PK\x05\x06"
DD_SIG = b"PK\x07\x08"
ZIP64_LOC_SIG = b"PK\x06\x07"


class ZipSurgeryError(ValueError):
    pass


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
    infos = zf.infolist()
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
