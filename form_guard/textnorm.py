"""Deterministic normalisers for Japanese administrative text.

These power the self-correction loop: converting Western dates to 和暦, turning
``483万6千円`` into ``4,836,000円``, fixing full-width IDs, and condensing long text
*extractively* (deleting whole sentences / asides, never inventing words).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

# ----------------------------------------------------------------------- dates

ERAS = [("令和", date(2019, 5, 1)), ("平成", date(1989, 1, 8)), ("昭和", date(1926, 12, 25))]
# First and last day of each era (令和 is open-ended).
ERA_BOUNDS: dict[str, tuple[date, Optional[date]]] = {
    "令和": (date(2019, 5, 1), None),
    "平成": (date(1989, 1, 8), date(2019, 4, 30)),
    "昭和": (date(1926, 12, 25), date(1989, 1, 7)),
}
WEEKDAYS_JA = "月火水木金土日"

_D = "[0-9０-９]"
_WAREKI_RE = re.compile(rf"(令和|平成|昭和)\s*(元|{_D}{{1,2}})\s*年\s*({_D}{{1,2}})\s*月\s*({_D}{{1,2}})\s*日")
# Abbreviated era dates as written in administrative notes: R8.12.15, H31/4/30, Ｒ８．１２．１５, 令和8.12.15
_ERA_LETTERS = {"R": "令和", "H": "平成", "S": "昭和"}
_WAREKI_ABBR_RE = re.compile(
    rf"(?<![A-Za-zＡ-Ｚａ-ｚ0-9０-９])([RHSＲＨＳ]|令和|平成|昭和)\s*(元|{_D}{{1,2}})\s*([./／．])\s*({_D}{{1,2}})\s*\3\s*({_D}{{1,2}})"
    rf"(?![0-9０-９]|\s*[./／．]\s*[0-9０-９])")
_WESTERN_KANJI_RE = re.compile(rf"({_D}{{4}})\s*年\s*({_D}{{1,2}})\s*月\s*({_D}{{1,2}})\s*日")
_WESTERN_SEP_RE = re.compile(rf"(?<![0-9０-９])({_D}{{4}})\s*[/／\-－.．]\s*({_D}{{1,2}})\s*[/／\-－.．]\s*({_D}{{1,2}})(?![0-9０-９])")
_MD_KANJI_RE = re.compile(rf"(?<![年0-9０-９])({_D}{{1,2}})\s*月\s*({_D}{{1,2}})\s*日")
_MD_SLASH_RE = re.compile(rf"(?<![0-9０-９/／])({_D}{{1,2}})\s*[/／]\s*({_D}{{1,2}})(?=\s*[（(][月火水木金土日])")
_WEEKDAY_RE = re.compile(r"\s*[（(]\s*([月火水木金土日])(?:曜日?)?\s*[）)]")


@dataclass
class DateMatch:
    start: int
    end: int
    year: Optional[int]
    month: int
    day: int
    weekday: Optional[str] = None
    kind: str = "western"
    era: Optional[str] = None
    era_year: Optional[int] = None

    @property
    def as_date(self) -> Optional[date]:
        if self.year is None:
            return None
        try:
            return date(self.year, self.month, self.day)
        except ValueError:
            return None

    def key(self) -> tuple[Optional[int], int, int]:
        return (self.year, self.month, self.day)

    def problem(self) -> Optional[tuple[str, str]]:
        """``(code, message)`` if this is not a real date: impossible day, outside its era, or wrong weekday."""
        if self.year is None:
            try:
                date(2000, self.month, self.day)  # a leap year: only days that never exist are rejected
            except ValueError:
                return "date_invalid", f"{self.month}月{self.day}日 does not exist / 存在しない日付です"
            return None
        d = self.as_date
        if d is None:
            return "date_invalid", f"{self.year}年{self.month}月{self.day}日 is not a real calendar date / 存在しない日付です"
        if self.era:
            err = era_problem(self.era, self.era_year, d)
            if err:
                return "date_invalid", err
        if self.weekday and weekday_ja(d) != self.weekday:
            return "weekday_mismatch", (f"{d.isoformat()} is a {weekday_ja(d)}曜日, not （{self.weekday}） "
                                        f"/ 曜日が暦と一致しません（{weekday_ja(d)}曜日）")
        return None


def _n(s: str) -> int:
    return int(unicodedata.normalize("NFKC", s))


def _era_number(y: str) -> int:
    return 1 if y == "元" else _n(y)


def era_problem(era: str, era_year: Optional[int], d: Optional[date] = None) -> Optional[str]:
    """Why an era year / era date is impossible (year 0, before the era began, after it ended), or None."""
    start, end = ERA_BOUNDS[era]
    if era_year is not None and era_year < 1:
        return f"{era}{era_year}年 does not exist (era years start at 元年) / 存在しない年です"
    if era_year is not None and end is not None and start.year + era_year - 1 > end.year:
        return f"{era}{era_year}年 is after the {era} era ended ({end.isoformat()}) / 元号の期間外です"
    if d is not None and (d < start or (end is not None and d > end)):
        span = f"{start.isoformat()}～{end.isoformat() if end else ''}"
        return f"{d.isoformat()} is outside the {era} era ({span}) / 元号の期間外の日付です"
    return None


def era_to_year(era: str, y: str) -> int:
    """Western year of an era year; raises ValueError for 0年 or a year after the era ended."""
    n = _era_number(y)
    err = era_problem(era, n)
    if err:
        raise ValueError(err)
    return ERA_BOUNDS[era][0].year + n - 1


def find_dates(text: str) -> list[DateMatch]:
    """Find date expressions (和暦, R8.12.15, 2026年11月2日, 2026/11/2, 11月2日, 11/2（月）)."""
    found: list[DateMatch] = []
    taken: list[tuple[int, int]] = []

    def free(a: int, b: int) -> bool:
        return all(b <= s or a >= e for s, e in taken)

    def add(m, year, month, day, kind, era=None, era_year=None):
        if not free(m.start(), m.end()):
            return
        end = m.end()
        wd = None
        w = _WEEKDAY_RE.match(text, end)
        if w:
            wd = w.group(1)
            end = w.end()
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return
        taken.append((m.start(), end))
        found.append(DateMatch(m.start(), end, year, month, day, wd, kind, era, era_year))

    for m in _WAREKI_RE.finditer(text):
        n = _era_number(m.group(2))  # the year is kept even if the era year is impossible; problem() reports it
        add(m, ERA_BOUNDS[m.group(1)][0].year + n - 1, _n(m.group(3)), _n(m.group(4)), "wareki", m.group(1), n)
    for m in _WAREKI_ABBR_RE.finditer(text):
        era = _ERA_LETTERS.get(unicodedata.normalize("NFKC", m.group(1)), m.group(1))
        n = _era_number(m.group(2))
        add(m, ERA_BOUNDS[era][0].year + n - 1, _n(m.group(4)), _n(m.group(5)), "wareki", era, n)
    for m in _WESTERN_KANJI_RE.finditer(text):
        add(m, _n(m.group(1)), _n(m.group(2)), _n(m.group(3)), "western")
    for m in _WESTERN_SEP_RE.finditer(text):
        add(m, _n(m.group(1)), _n(m.group(2)), _n(m.group(3)), "western")
    for m in _MD_KANJI_RE.finditer(text):
        add(m, None, _n(m.group(1)), _n(m.group(2)), "month_day")
    for m in _MD_SLASH_RE.finditer(text):
        add(m, None, _n(m.group(1)), _n(m.group(2)), "month_day")
    found.sort(key=lambda d: d.start)
    return found


def find_invalid_dates(text: str) -> list[tuple[DateMatch, str, str]]:
    """Date expressions that cannot be real: 2026年2月30日, 2026/02/29, 令和元年1月1日, 平成31年5月1日, wrong （曜）."""
    out = []
    for d in find_dates(text):
        p = d.problem()
        if p:
            out.append((d, p[0], p[1]))
    return out


def weekday_ja(d: date) -> str:
    return WEEKDAYS_JA[d.weekday()]


def to_wareki(d: date, include_weekday: bool = True) -> str:
    """令和8年11月2日（月） — human-facing dates carry the weekday unless ``include_weekday`` is False.

    Raises ValueError for a date before 昭和 (1926-12-25), the first era this tool writes (``pre_era_problem``)."""
    for name, start in ERAS:
        if d >= start:
            y = d.year - start.year + 1
            out = f"{name}{'元' if y == 1 else y}年{d.month}月{d.day}日"
            return out + (f"（{weekday_ja(d)}）" if include_weekday else "")
    raise ValueError(f"date before supported eras: {d}")


def pre_era_problem(d: Optional[date]) -> Optional[str]:
    """Why ``d`` cannot be written as 和暦 here (it is before 昭和元年12月25日), or None."""
    first = ERAS[-1][1]
    if d is None or d >= first:
        return None
    return (f"{d.isoformat()} is before the supported eras (昭和 began on {first.isoformat()}) — it cannot be written as "
            f"a 和暦 date / 昭和より前の日付は和暦で記入できません")


def to_western(d: date, include_weekday: bool = True) -> str:
    return f"{d.year}年{d.month}月{d.day}日" + (f"（{weekday_ja(d)}）" if include_weekday else "")


_WD_SUFFIX = r"(?:[（(]([月火水木金土日])[）)])?"
WAREKI_FULL_RE = re.compile(rf"^(令和|平成|昭和)(元|[0-9]{{1,2}})年([0-9]{{1,2}})月([0-9]{{1,2}})日{_WD_SUFFIX}$")
WESTERN_FULL_RE = re.compile(rf"^([0-9]{{4}})(?:年([0-9]{{1,2}})月([0-9]{{1,2}})日|[/\-.]([0-9]{{1,2}})[/\-.]([0-9]{{1,2}})){_WD_SUFFIX}$")


def check_wareki(s: str) -> tuple[Optional[date], Optional[tuple[str, str]]]:
    """Strict 和暦 date (optional （曜）): ``(date, None)`` if valid, ``(None, None)`` if it is not
    和暦 syntax at all, ``(None, (code, message))`` if it is 和暦 but impossible (2月30日, outside the
    era, 令和0年, weekday that does not match the calendar)."""
    m = WAREKI_FULL_RE.match(s.strip())
    if not m:
        return None, None
    era, y, mo, dd, wd = m.groups()
    n = _era_number(y)
    dm = DateMatch(0, len(s), ERA_BOUNDS[era][0].year + n - 1, int(mo), int(dd), wd, "wareki", era, n)
    p = dm.problem()
    return (None, p) if p else (dm.as_date, None)


def check_full_date(s: str) -> tuple[Optional[date], Optional[tuple[str, str]]]:
    """Like ``check_wareki`` but also accepts Western dates (2026年11月2日（月）, 2026/11/02)."""
    d, p = check_wareki(s)
    if d is not None or p is not None:
        return d, p
    m = WESTERN_FULL_RE.match(s.strip())
    if not m:
        return None, None
    y, mo1, d1, mo2, d2, wd = m.groups()
    dm = DateMatch(0, len(s), int(y), int(mo1 or mo2), int(d1 or d2), wd, "western")
    p = dm.problem()
    return (None, p) if p else (dm.as_date, None)


def parse_wareki_exact(s: str) -> Optional[date]:
    return check_wareki(s)[0]


# --------------------------------------------------------------------- numbers

_NUM = "[0-9０-９][0-9０-９,，]*(?:[.．][0-9０-９]+)?"
_KANJI_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_KANJI_SMALL = {"十": 10, "百": 100, "千": 1000}
_KANJI_BIG = {"万": 10_000, "億": 100_000_000, "兆": 1_000_000_000_000}
_UNIT_MULT = {**_KANJI_SMALL, **_KANJI_BIG}
_COMPOUND_NUM_PAT = rf"(?:{_NUM}\s*[兆億万千百十]+)+(?:{_NUM})?"

_AMOUNT_RE = re.compile(
    rf"(?P<approx>約|およそ|おおよそ|概ね)?\s*(?P<num>{_COMPOUND_NUM_PAT}|{_NUM})\s*(?P<k>千)?円"
)
_KANJI_AMOUNT_RE = re.compile(r"(?P<approx>約|およそ)?(?P<num>[〇一二三四五六七八九十百千万億兆]+)円")
_PLAIN_NUM_RE = re.compile(r"[0-9][0-9,]*(?:\.[0-9]+)?")


def _to_float(s: str) -> float:
    s = unicodedata.normalize("NFKC", s).replace(",", "")
    return float(s)


# Comma grouping must be groups of three digits: 1,612,000 — never 52,00 / 1,,000 / 1,00,000.
_GROUPED_RE = re.compile(r"^[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?$")


def _decimal_term(tok: str) -> Decimal:
    if "," in tok and not _GROUPED_RE.match(tok):
        raise ValueError(f"malformed digit grouping {tok!r} (commas must separate groups of three digits)")
    try:
        return Decimal(tok.replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {tok!r}") from exc


def parse_compound(num: str) -> int:
    """Parse '483万6千' / '1,612,000' / '1億2,000万' / '2千万' / '3百万' / '1億2千万' / '1.5万' / '0万5千' into an integer.

    十 / 百 / 千 build a section that the next 万 / 億 / 兆 multiplies (2千万 = 2,000 × 10,000; 1千2百万 = 1,200万).
    Exact decimal arithmetic: malformed comma grouping (52,00 / 1,,000) and a total that is not a whole number
    (1.5 / 0.5) raise ValueError instead of being read as another number or rounded. An explicit 0 before a unit
    stays 0 (0万 = 0); only a unit with nothing before it counts as one (万 = 10,000)."""
    num = unicodedata.normalize("NFKC", num).replace(" ", "")
    total = section = Decimal(0)
    has_section = False  # the section states a number (0万 = 0, 万 = 1万)
    cur = ""
    for ch in num:
        if ch.isdigit() or ch in ".,":
            cur += ch
        elif ch in _KANJI_SMALL:
            section += (_decimal_term(cur) if cur else Decimal(1)) * _KANJI_SMALL[ch]
            has_section, cur = True, ""
        elif ch in _KANJI_BIG:
            if cur:
                section += _decimal_term(cur)
                has_section = True
            total += (section if has_section else Decimal(1)) * _KANJI_BIG[ch]
            section, has_section, cur = Decimal(0), False, ""
    if cur:
        section += _decimal_term(cur)
    total += section
    if total != int(total):
        raise ValueError(f"{num!r} is not a whole number")
    return int(total)


def kanji_to_int(s: str) -> Optional[int]:
    total, section, digit, seen = 0, 0, None, False  # seen: the section states a number (〇万 = 0, 万 = 1万)
    for ch in s:
        if ch in _KANJI_DIGITS:
            digit, seen = _KANJI_DIGITS[ch], True
        elif ch in _KANJI_SMALL:
            section += (digit if digit is not None else 1) * _KANJI_SMALL[ch]
            digit, seen = None, True
        elif ch in _KANJI_BIG:
            section += digit or 0
            total += (section if seen else 1) * _KANJI_BIG[ch]
            section, digit, seen = 0, None, False
        else:
            return None
    return total + section + (digit or 0)


@dataclass
class AmountMatch:
    start: int
    end: int
    value: int
    approx: bool
    text: str


def find_amounts(text: str) -> list[AmountMatch]:
    out: list[AmountMatch] = []
    refused: list[tuple[int, int]] = []  # 52,00万円 / 1.5円: not an amount, and nothing inside them is read as one
    for m in _AMOUNT_RE.finditer(text):
        try:
            v = parse_compound(m.group("num"))
        except ValueError:
            refused.append((m.start(), m.end()))
            continue
        if m.group("k"):
            v *= 1000
        start = m.start("approx") if m.group("approx") else m.start("num")
        out.append(AmountMatch(start, m.end(), v, bool(m.group("approx")), text[start : m.end()]))
    for m in _KANJI_AMOUNT_RE.finditer(text):
        v = kanji_to_int(m.group("num"))
        s = m.start("num")
        # the 万円 / 千円 of a number that is no amount (52,00万円, 1.23456万円) or of 数万円 / 何千円 is never one either
        if (v is None or any(a.start <= s < a.end for a in out) or any(rs <= s < re_ for rs, re_ in refused)
                or (s > 0 and text[s - 1] in "0123456789０１２３４５６７８９,，.．数何")):
            continue
        start = m.start("approx") if m.group("approx") else m.start("num")
        out.append(AmountMatch(start, m.end(), v, bool(m.group("approx")), text[start : m.end()]))
    out.sort(key=lambda a: a.start)
    return out


def format_yen(v: int) -> str:
    return f"{v:,}円"


YEN_RE = re.compile(r"^[0-9]{1,3}(?:,[0-9]{3})*円$")


def parse_yen_exact(s: str) -> Optional[int]:
    if not YEN_RE.match(s):
        return None
    return int(s[:-1].replace(",", ""))


def quantities(text: str) -> set[float]:
    """All numeric quantities in a text, with 万/千/億 compounds expanded (dates excluded).

    A compound such as ``483万6千円`` contributes only its total (4,836,000): its digit groups
    ``483`` and ``6`` are not quantities the text states, so they are never exposed on their own
    (otherwise a value of ``6円`` would look supported by ``483万6千円``).
    """
    t = unicodedata.normalize("NFKC", text)
    vals: set[float] = set()
    for d in sorted(find_dates(t), key=lambda d: -d.start):
        vals.update(float(x) for x in (d.year, d.month, d.day) if x is not None)
        t = t[: d.start] + " " * (d.end - d.start) + t[d.end :]
    covered: list[tuple[int, int]] = []
    for a in find_amounts(t):
        vals.add(float(a.value))
        covered.append((a.start, a.end))
    for m in re.finditer(_COMPOUND_NUM_PAT, t):
        try:
            vals.add(float(parse_compound(m.group(0))))
        except ValueError:  # 52,00万 / 1.23456万: states no number — its digits do not support anything either
            pass
        covered.append((m.start(), m.end()))
    for m in _PLAIN_NUM_RE.finditer(t):
        if any(s <= m.start() < e for s, e in covered):
            continue
        try:
            vals.add(_to_float(m.group(0)))
        except ValueError:
            pass
    return vals


def value_quantities(text: str) -> list[tuple[str, float]]:
    """Quantities a value asserts: compounds/amounts first, then leftover plain numbers."""
    t = unicodedata.normalize("NFKC", text)
    for d in sorted(find_dates(t), key=lambda d: -d.start):
        t = t[: d.start] + " " * (d.end - d.start) + t[d.end :]
    out: list[tuple[str, float]] = []
    covered: list[tuple[int, int]] = []
    for a in find_amounts(t):
        out.append((a.text, float(a.value)))
        covered.append((a.start, a.end))
    for m in re.finditer(_COMPOUND_NUM_PAT, t):
        if any(s <= m.start() < e for s, e in covered):
            continue
        try:
            out.append((m.group(0), float(parse_compound(m.group(0)))))
        except ValueError:  # 52,00万 / 1.23456万: asserts no number (the memo side reads it the same way)
            pass
        covered.append((m.start(), m.end()))
    for m in _PLAIN_NUM_RE.finditer(t):
        if any(s <= m.start() < e for s, e in covered):
            continue
        out.append((m.group(0), _to_float(m.group(0))))
    return out


# ------------------------------------------------------------------- widths/ids

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−ー－"), "-")


def to_halfwidth_id(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).translate(_DASHES)
    return re.sub(r"\s+", "", s)


# Invisible characters pasted from web pages, PDFs and chat tools (zero-width space, word joiner, BOM, direction
# marks, soft hyphen, Arabic letter mark and the bidirectional embedding / override / isolate controls U+202A–U+202E,
# U+2066–U+2069). They would end up invisible inside a filled form, split IDs and numbers apart, or — like U+202E
# RIGHT-TO-LEFT OVERRIDE — make the reviewer read a value in a different order than it is stored.
_INVISIBLE = dict.fromkeys([0x200B, 0x2060, 0xFEFF, 0x200E, 0x200F, 0x00AD, 0x061C, *range(0x202A, 0x202F),
                            *range(0x2066, 0x206A)])
# A kana followed by a combining (semi-)voiced sound mark — NFD text, e.g. from macOS: フ + U+309A instead of プ.
_KANA_COMBINING_RE = re.compile("[ぁ-ゖゝゞァ-ヺヽヾ][゙゚]")


def clean_text(text: str) -> tuple[str, list[str]]:
    """Remove invisible characters and compose kana + combining sound marks (NFD → NFC for those pairs only).

    Nothing visible changes: CJK compatibility ideographs and other NFC / NFKC mappings are deliberately left alone
    (a person's 異体字 must stay as written). Returns the text and a note per kind of change made.
    """
    notes: list[str] = []
    out = text.translate(_INVISIBLE)
    if len(out) != len(text):
        notes.append(f"{len(text) - len(out)} invisible character(s) removed (zero-width space, BOM, soft hyphen …)")
    n = 0

    def compose(m: re.Match) -> str:
        nonlocal n
        c = unicodedata.normalize("NFC", m.group(0))
        n += c != m.group(0)
        return c

    composed = _KANA_COMBINING_RE.sub(compose, out)
    if n:
        notes.append(f"{n} kana + combining sound mark pair(s) composed (NFD → NFC, e.g. フ+゜ → プ)")
    return composed, notes


def normalize_for_match(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", "", s)


_HONORIFIC_TAIL = re.compile(r"(?:様|殿|氏|さん|先生)$")


def clean_person_name(s: str) -> str:
    s = re.sub(r"[（(][^（）()]*[）)]", "", s).strip()
    s = _HONORIFIC_TAIL.sub("", s).strip()
    return re.sub(r"[ \t]+", " ", s)


_TRAILING_NOTE_RE = re.compile(r"\s*[（(]\s*(?:※|注|メモ|note)[^（）()]*[）)]\s*$")


def strip_trailing_note(s: str) -> str:
    """Drop a trailing aside such as '（※…）' or '(note …)' that annotates a value.

    An aside that states a condition, restriction, interim measure or scope limit (「（※利用は平日に限る）」
    「（※利用は平日のみ）」, ``protection > 0``) is part of the value and is kept: shortening such a value goes
    through ``condense_extractive``, which never deletes it without handing the field to a human."""
    m = _TRAILING_NOTE_RE.search(s)
    if m is None or protection(m.group(0)) > 0:
        return s.strip()
    return s[: m.start()].strip()


def char_width_em(ch: str) -> float:
    if ch in "\t":
        return 1.0
    eaw = unicodedata.east_asian_width(ch)
    if eaw in ("F", "W", "A"):
        return 1.0
    if ch == " ":
        return 0.3
    if ch.isupper():
        return 0.66
    if ch.isdigit():
        return 0.56
    if ch in "il.,:;'|!()[]{}":
        return 0.32
    return 0.53


# --------------------------------------------------------- extractive condenser

_FILLER_START = re.compile(r"^(?:なお|ちなみに|参考までに|余談|念のため|補足)")
_STATUS_REMARK = re.compile(r"(?:異常|異状|問題)(?:は)?(?:なし|無し|ありません)")
_ACTION_WORDS = re.compile(
    r"交換|依頼|実施|提出|確認|是正|修理|変更|追加|削減|増額|減額|合意|中止|延期|予定|必要|補う|賄う|導入|申請|対応|改修"
)
_PROVISIONAL_WORDS = re.compile(r"短縮|補う|完了まで|増額|追加負担金")
# Conditions / restrictions: deleting text that contains one of these can change what the value permits
# (「…に限る」「…に限定」「…以外は」「…は対象外」「…できない」「…してはならない」「…しないこと」 included).
RESTRICTIVE_WORDS = re.compile(r"ただし|但し|場合に限り|限り|限る|限ら|限定|禁止|除く|除き|不可|条件|なければ|以外|対象外|"
                               r"できない|できません|してはならない|してはいけない|しないこと")
# Interim measures / scope limits: kept ahead of anything else, but deletable as a last resort.
_SAFETY_WORDS = re.compile(r"完了まで|短縮|のみ")
# The core decision / remedy of the text (「そこで…こととした」「…で補う」「…に変更する」): what is done about the
# problem it describes. Without it the text jumps from the problem straight to the next topic.
_DECISION_WORDS = re.compile(r"^そこで|こととした|ことにした|こととする|ことにする|で補う|で代替|に変更する|に変更した|"
                             r"に切り替え")
_PAREN_RE = re.compile(r"[（(][^（）()]*[）)]")
WEEKDAY_NOTE_RE = re.compile(r"[（(][月火水木金土日](?:曜日?)?[）)]")


def split_sentences(text: str) -> list[tuple[int, int]]:
    """Return (start, end) spans of sentences; never splits inside （…）."""
    spans: list[tuple[int, int]] = []
    depth, start = 0, 0
    for i, ch in enumerate(text):
        if ch in "（(「『":
            depth += 1
        elif ch in "）)」』":
            depth = max(0, depth - 1)
        elif depth == 0 and ch in "。！？!?\n":
            end = i + 1
            if text[start:end].strip():
                spans.append((start, end))
            start = end
    if text[start:].strip():
        spans.append((start, len(text)))
    out = []
    for s, e in spans:
        while s < e and text[s] in " \t\n　":
            s += 1
        while e > s and text[e - 1] in " \t\n":
            e -= 1
        if s < e:
            out.append((s, e))
    return out


def _bigrams(s: str) -> set[str]:
    s = normalize_for_match(s)
    return {s[i : i + 2] for i in range(len(s) - 1)}


def is_remark(sentence: str) -> bool:
    """Supplementary or status remarks: 「なお、…」「念のため…」「…異常なし」."""
    return bool(_FILLER_START.match(sentence.lstrip()) or _STATUS_REMARK.search(sentence))


def protection(piece: str) -> int:
    """2 = states a condition / restriction, 1 = interim measure, scope limit or the core decision / remedy
    (「そこで…こととした」), 0 = none of these. Deleting a piece with protection > 0 needs a human."""
    if RESTRICTIVE_WORDS.search(piece):
        return 2
    if _SAFETY_WORDS.search(piece):
        if is_remark(piece) and not re.search(r"完了まで|短縮", piece):
            return 0  # 「なお、…清掃のみで異常なし」: 'のみ' in a status remark is not a condition
        return 1
    if _DECISION_WORDS.search(piece.lstrip()) and not is_remark(piece):
        return 1
    return 0


def _long_aside(sentence: str) -> bool:
    return any(len(m.group(0)) > 10 for m in _PAREN_RE.finditer(sentence))


def _sentence_score(sentence: str, first: bool) -> float:
    score = 0.0
    if re.search(r"[0-9０-９]", sentence):
        score += 3
    if _ACTION_WORDS.search(sentence):
        score += 2
    if _PROVISIONAL_WORDS.search(sentence):
        score += 2
    if first:
        score += 2
    if is_remark(sentence):
        score -= 3
    if _long_aside(sentence):
        score -= 1
    return score


@dataclass
class Condensed:
    text: str
    fragments: list[str]
    removed: list[str]
    # A deleted piece stated a condition, restriction, interim measure, scope limit or the core decision
    # (protection > 0), or a date lost its weekday: a human must confirm the wording.
    unsafe: bool = False
    held: list[str] = field(default_factory=list)  # the deleted pieces that made it unsafe, in reading order


def condense_extractive(text: str, max_chars: int, joiner: str = "") -> Optional[Condensed]:
    """Shorten text to <= max_chars by deleting whole sentences / asides only.

    Every fragment of the result is a verbatim substring of the input, so the result stays
    grounded. Deletion order, least important first:

      1. supplementary or status remarks (「なお、…」「…異常なし」) and content-free sentences;
      2. long parenthetical asides that state no condition;
      3. other sentences without conditions, interim measures or the core decision (redundancy penalised);
      4. last resort: the weekday notes after dates (「11月6日（金）」→「11月6日」, the dates stay), then interim
         measures, scope limits and the core decision / remedy (完了まで, 短縮, のみ, 「そこで…こととした」,
         「…で補う」, 「…に変更する」), then conditions / restrictions (ただし, 場合に限り, に限る, 禁止, 除く,
         不可, 以外, 対象外, できない, 条件 …).

    Deleting anything from step 4 (a weekday, or a piece with ``protection > 0``) marks the result ``unsafe``: it is
    only offered as a suggestion, never written automatically, and ``held`` names the pieces a human has to decide
    about.
    Anything deleted that fits back afterwards is restored. ``removed`` lists every deleted piece
    in reading order. Returns None if the text cannot fit without rewriting.
    """
    if len(text) <= max_chars:
        return Condensed(text, [text], [])
    spans = split_sentences(text)
    if not spans:
        return None
    items = []
    for i, (s, e) in enumerate(spans):
        sent, units, cur = text[s:e], [], 0
        for m in _PAREN_RE.finditer(sent):
            if m.start() > cur:
                units.append({"kind": "body", "start": s + cur, "end": s + m.start(), "kept": True})
            weekday = bool(WEEKDAY_NOTE_RE.fullmatch(m.group(0))) and m.start() > 0 and sent[m.start() - 1] == "日"
            units.append({"kind": "weekday" if weekday else "paren", "start": s + m.start(), "end": s + m.end(),
                          "kept": True})
            cur = m.end()
        if cur < len(sent):
            units.append({"kind": "body", "start": s + cur, "end": e, "kept": True})
        items.append({"start": s, "end": e, "first": i == 0, "score": _sentence_score(sent, i == 0), "units": units,
                      "kept": True})
    ops: list[tuple[str, Any]] = []

    def sent_text(it) -> str:
        return "".join(text[u["start"] : u["end"]] for u in it["units"] if u["kept"])

    def unit_text(u) -> str:
        return text[u["start"] : u["end"]]

    def kept_items():
        return [it for it in items if it["kept"]]

    def total() -> int:
        kept = kept_items()
        return sum(len(sent_text(it)) for it in kept) + len(joiner) * max(0, len(kept) - 1)

    def drop_sentence(it) -> None:
        it["kept"] = False
        ops.append(("sentence", it))

    def drop_units(us) -> None:
        for u in us:
            u["kept"] = False
        ops.append(("units", us))

    def adjusted(it, kept) -> float:
        others = set().union(*(_bigrams(sent_text(o)) for o in kept if o is not it))
        mine = _bigrams(sent_text(it))
        return it["score"] - 4 * len(mine & others) / max(1, len(mine))

    def drop_sentences(level: int) -> None:
        while total() > max_chars:
            kept = kept_items()
            cands = [it for it in kept if not it["first"] and protection(sent_text(it)) == level]
            if not cands:
                return
            drop_sentence(min(cands, key=lambda it: (adjusted(it, kept), -it["start"])))

    def drop_asides(level: int, min_len: int) -> None:
        while total() > max_chars:
            cands = [u for it in kept_items() for u in it["units"] if u["kept"] and u["kind"] == "paren"
                     and u["end"] - u["start"] > min_len and protection(unit_text(u)) == level]
            if not cands:
                return
            drop_units([max(cands, key=lambda u: (u["end"] - u["start"], -u["start"]))])

    # 1) supplementary / status remarks and content-free sentences
    for it in sorted(items, key=lambda it: (it["score"], -it["start"])):
        if total() <= max_chars:
            break
        if it["first"] or len(kept_items()) <= 1:
            continue
        t = sent_text(it)
        if (is_remark(t) or it["score"] <= 0) and protection(t) == 0:
            drop_sentence(it)
    # 2) long parenthetical asides that state no condition (short ones such as IDs are kept)
    drop_asides(0, 10)
    # 3) sentences without conditions, interim measures or the core decision
    drop_sentences(0)
    # 4) last resort: the weekday notes after dates (the dates stay, but a human decides), then interim measures /
    #    scope limits / the core decision, then conditions / restrictions
    if total() > max_chars:
        notes = [u for it in kept_items() for u in it["units"] if u["kept"] and u["kind"] == "weekday"]
        if notes:
            drop_units(notes)
    for level in (1, 2):
        drop_asides(level, 0)
        drop_sentences(level)
    if total() > max_chars:
        return None
    # restore anything that fits again, most valuable (= removed last) first
    for kind, obj in reversed(ops):
        if kind == "sentence":
            obj["kept"] = True
            if total() > max_chars:
                obj["kept"] = False
        else:
            for u in obj:
                u["kept"] = True
            if total() > max_chars:
                for u in obj:
                    u["kept"] = False

    removed: list[str] = []
    held: list[str] = []
    fragments: list[str] = []
    for it in items:
        if not it["kept"]:
            removed.append(text[it["start"] : it["end"]])
            if protection(removed[-1]) > 0:
                held.append(removed[-1])
            continue
        run: list[str] = []
        for u in it["units"]:
            if u["kept"]:
                run.append(unit_text(u))
                continue
            removed.append(unit_text(u))
            if u["kind"] == "weekday" or protection(removed[-1]) > 0:  # a date without its weekday, or a condition
                held.append(removed[-1])
            if run:
                fragments.append("".join(run))
            run = []
        if run:
            fragments.append("".join(run))
    fragments = [f for f in fragments if re.search(r"[^\s、。，．,.！？!?（）()「」『』]", f)]
    out = joiner.join(sent_text(it) for it in kept_items())
    if len(out) > max_chars:
        return None
    return Condensed(out, fragments, removed, unsafe=bool(held), held=held)
