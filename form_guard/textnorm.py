"""Deterministic normalisers for Japanese administrative text.

These power the self-correction loop: converting Western dates to 和暦, turning
``483万6千円`` into ``4,836,000円``, fixing full-width IDs, and condensing long text
*extractively* (deleting whole sentences / asides, never inventing words).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Optional

# ----------------------------------------------------------------------- dates

ERAS = [("令和", date(2019, 5, 1)), ("平成", date(1989, 1, 8)), ("昭和", date(1926, 12, 25))]
WEEKDAYS_JA = "月火水木金土日"

_D = "[0-9０-９]"
_WAREKI_RE = re.compile(rf"(令和|平成|昭和)\s*(元|{_D}{{1,2}})\s*年\s*({_D}{{1,2}})\s*月\s*({_D}{{1,2}})\s*日")
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


def _n(s: str) -> int:
    return int(unicodedata.normalize("NFKC", s))


def era_to_year(era: str, y: str) -> int:
    start = dict(ERAS)[era]
    return start.year + (1 if y == "元" else _n(y)) - 1


def find_dates(text: str) -> list[DateMatch]:
    """Find date expressions (和暦, 2026年11月2日, 2026/11/2, 11月2日, 11/2（月）)."""
    found: list[DateMatch] = []
    taken: list[tuple[int, int]] = []

    def free(a: int, b: int) -> bool:
        return all(b <= s or a >= e for s, e in taken)

    def add(m, year, month, day, kind):
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
        found.append(DateMatch(m.start(), end, year, month, day, wd, kind))

    for m in _WAREKI_RE.finditer(text):
        add(m, era_to_year(m.group(1), m.group(2)), _n(m.group(3)), _n(m.group(4)), "wareki")
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


def to_wareki(d: date) -> str:
    for name, start in ERAS:
        if d >= start:
            y = d.year - start.year + 1
            return f"{name}{'元' if y == 1 else y}年{d.month}月{d.day}日"
    raise ValueError(f"date before supported eras: {d}")


def weekday_ja(d: date) -> str:
    return WEEKDAYS_JA[d.weekday()]


WAREKI_FULL_RE = re.compile(r"^(令和|平成|昭和)(元|[0-9]{1,2})年([0-9]{1,2})月([0-9]{1,2})日$")


def parse_wareki_exact(s: str) -> Optional[date]:
    m = WAREKI_FULL_RE.match(s)
    if not m:
        return None
    try:
        return date(era_to_year(m.group(1), m.group(2)), int(m.group(3)), int(m.group(4)))
    except ValueError:
        return None


# --------------------------------------------------------------------- numbers

_NUM = "[0-9０-９][0-9０-９,，]*(?:[.．][0-9０-９]+)?"
_KANJI_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_KANJI_SMALL = {"十": 10, "百": 100, "千": 1000}
_KANJI_BIG = {"万": 10_000, "億": 100_000_000, "兆": 1_000_000_000_000}
_UNIT_MULT = {"千": 1_000, "万": 10_000, "億": 100_000_000}

_AMOUNT_RE = re.compile(
    rf"(?P<approx>約|およそ|おおよそ|概ね)?\s*(?P<num>(?:{_NUM}\s*[億万千]\s*)+(?:{_NUM})?|{_NUM})\s*(?P<k>千)?円"
)
_KANJI_AMOUNT_RE = re.compile(r"(?P<approx>約|およそ)?(?P<num>[〇一二三四五六七八九十百千万億]+)円")
_PLAIN_NUM_RE = re.compile(r"[0-9][0-9,]*(?:\.[0-9]+)?")


def _to_float(s: str) -> float:
    s = unicodedata.normalize("NFKC", s).replace(",", "")
    return float(s)


def parse_compound(num: str) -> int:
    """Parse '483万6千' / '1,612,000' / '1億2,000万' into an integer."""
    num = unicodedata.normalize("NFKC", num).replace(",", "").replace(" ", "")
    total = 0
    cur = ""
    for ch in num:
        if ch.isdigit() or ch == ".":
            cur += ch
        elif ch in _UNIT_MULT:
            total += round(float(cur or "1") * _UNIT_MULT[ch])
            cur = ""
    if cur:
        total += round(float(cur))
    return int(total)


def kanji_to_int(s: str) -> Optional[int]:
    total, section, digit = 0, 0, None
    for ch in s:
        if ch in _KANJI_DIGITS:
            digit = _KANJI_DIGITS[ch]
        elif ch in _KANJI_SMALL:
            section += (digit if digit is not None else 1) * _KANJI_SMALL[ch]
            digit = None
        elif ch in _KANJI_BIG:
            section += digit or 0
            total += (section or 1) * _KANJI_BIG[ch]
            section, digit = 0, None
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
    for m in _AMOUNT_RE.finditer(text):
        try:
            v = parse_compound(m.group("num"))
        except ValueError:
            continue
        if m.group("k"):
            v *= 1000
        start = m.start("approx") if m.group("approx") else m.start("num")
        out.append(AmountMatch(start, m.end(), v, bool(m.group("approx")), text[start : m.end()]))
    for m in _KANJI_AMOUNT_RE.finditer(text):
        v = kanji_to_int(m.group("num"))
        if v is None or any(a.start <= m.start() < a.end for a in out):
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
    """All numeric quantities in a text, with 万/千/億 compounds expanded (dates excluded)."""
    t = unicodedata.normalize("NFKC", text)
    vals: set[float] = set()
    for d in sorted(find_dates(t), key=lambda d: -d.start):
        vals.update(float(x) for x in (d.year, d.month, d.day) if x is not None)
        t = t[: d.start] + " " + t[d.end :]
    for a in find_amounts(t):
        vals.add(float(a.value))
    for m in re.finditer(rf"(?:{_NUM}\s*[億万千]\s*)+(?:{_NUM})?", t):
        try:
            vals.add(float(parse_compound(m.group(0))))
        except ValueError:
            pass
    for m in _PLAIN_NUM_RE.finditer(t):
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
    for m in re.finditer(rf"(?:{_NUM}\s*[億万千]\s*)+(?:{_NUM})?", t):
        if any(s <= m.start() < e for s, e in covered):
            continue
        out.append((m.group(0), float(parse_compound(m.group(0)))))
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


def normalize_for_match(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", "", s)


_HONORIFIC_TAIL = re.compile(r"(?:様|殿|氏|さん|先生)$")


def clean_person_name(s: str) -> str:
    s = re.sub(r"[（(][^（）()]*[）)]", "", s).strip()
    s = _HONORIFIC_TAIL.sub("", s).strip()
    return re.sub(r"[ \t]+", " ", s)


def strip_trailing_note(s: str) -> str:
    """Drop trailing asides such as '（※…）' or '(note …)' that annotate a value."""
    return re.sub(r"\s*[（(]\s*(?:※|注|メモ|note)[^（）()]*[）)]\s*$", "", s).strip()


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
_ACTION_WORDS = re.compile(
    r"交換|依頼|実施|提出|確認|是正|修理|変更|追加|削減|増額|減額|合意|中止|延期|予定|必要|補う|賄う|導入|申請|対応|改修"
)
_PAREN_RE = re.compile(r"[（(][^（）()]*[）)]")


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


def _sentence_score(sentence: str, first: bool) -> float:
    score = 0.0
    if re.search(r"[0-9０-９]", sentence):
        score += 3
    if _ACTION_WORDS.search(sentence):
        score += 2
    if first:
        score += 2
    if _FILLER_START.match(sentence):
        score -= 3
    if "（" in sentence or "(" in sentence:
        score -= 1
    return score


@dataclass
class Condensed:
    text: str
    fragments: list[str]
    removed: list[str]


def condense_extractive(text: str, max_chars: int, joiner: str = "") -> Optional[Condensed]:
    """Shorten text to <= max_chars by deleting whole sentences / long asides only.

    Every fragment of the result is a verbatim substring of the input, so the
    result stays 100% grounded. Returns None if it cannot fit without rewriting.
    """
    if len(text) <= max_chars:
        return Condensed(text, [text], [])
    spans = split_sentences(text)
    if not spans:
        return None
    items = [{"span": sp, "text": text[sp[0] : sp[1]], "score": _sentence_score(text[sp[0] : sp[1]], i == 0),
              "pieces": [text[sp[0] : sp[1]]], "keep": True} for i, sp in enumerate(spans)]
    removed: list[str] = []

    def total() -> int:
        kept = [it for it in items if it["keep"]]
        return sum(len("".join(it["pieces"])) for it in kept) + len(joiner) * max(0, len(kept) - 1)

    # 1) drop filler sentences ("なお、…")
    for it in sorted(items, key=lambda it: (it["score"], -it["span"][0])):
        if total() <= max_chars:
            break
        if it["score"] <= 0 and sum(1 for x in items if x["keep"]) > 1:
            it["keep"] = False
            removed.append(it["text"])
    # 2) remove long parenthetical asides (short ones such as IDs are kept)
    while total() > max_chars:
        best = None
        for it in items:
            if not it["keep"]:
                continue
            for pi, piece in enumerate(it["pieces"]):
                for m in _PAREN_RE.finditer(piece):
                    if len(m.group(0)) > 10 and (best is None or len(m.group(0)) > best[3]):
                        best = (it, pi, m, len(m.group(0)))
        if best is None:
            break
        it, pi, m, _ = best
        piece = it["pieces"][pi]
        left, right = piece[: m.start()], piece[m.end() :]
        removed.append(m.group(0))
        it["pieces"][pi : pi + 1] = [p for p in (left, right) if p]
    # 3) drop lowest-value sentences, penalising redundancy with the rest
    while total() > max_chars:
        kept = [it for it in items if it["keep"]]
        if len(kept) <= 1:
            return None
        def adjusted(it):
            others = set().union(*(_bigrams("".join(o["pieces"])) for o in kept if o is not it))
            mine = _bigrams("".join(it["pieces"]))
            overlap = len(mine & others) / max(1, len(mine))
            return it["score"] - 4 * overlap
        victim = min((it for it in kept[1:]), key=lambda it: (adjusted(it), -it["span"][0]))
        victim["keep"] = False
        removed.append("".join(victim["pieces"]))
    kept = [it for it in items if it["keep"]]
    fragments = [p for it in kept for p in it["pieces"] if re.search(r"[^\s、。，．,.！？!?（）()「」『』]", p)]
    out = joiner.join("".join(it["pieces"]) for it in kept)
    if len(out) > max_chars:
        return None
    return Condensed(out, fragments, removed)
