"""Strict source grounding: every value must be backed by verbatim memo excerpts.

The checker is deliberately independent of whichever engine proposed the value
(Claude or the offline demo engine): it re-locates each quote in the memo itself,
recomputes line numbers, and checks that every number / date the value asserts
actually appears in the quoted evidence.
"""

from __future__ import annotations

import bisect
import unicodedata
from dataclasses import dataclass, field
from typing import Optional, Union

from .models import FormSlot, SourceSpan, ValidationIssue
from .textnorm import find_dates, quantities, value_quantities

LEXICAL_WARN_THRESHOLD = 0.5


def line_starts(memo: str) -> list[int]:
    starts = [0]
    for i, ch in enumerate(memo):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def offset_to_line(starts: list[int], offset: int) -> int:
    return bisect.bisect_right(starts, offset)


def numbered_memo(memo: str) -> str:
    return "\n".join(f"L{i:03d}| {line}" for i, line in enumerate(memo.split("\n"), start=1))


def _norm_map(s: str, nfkc: bool) -> tuple[str, list[int]]:
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(s):
        if ch.isspace() or ch == "　":
            continue
        piece = unicodedata.normalize("NFKC", ch) if nfkc else ch
        for p in piece:
            if p.isspace():
                continue
            out.append(p)
            idx.append(i)
    return "".join(out), idx


@dataclass
class Located:
    start: int
    end: int
    how: str  # exact | whitespace | width


def _pick(starts: list[int], near: Optional[int]) -> int:
    """Choose the occurrence the engine pointed at: nearest at/after ``near``, else nearest before."""
    if near is None or len(starts) == 1:
        return starts[0]
    return min(starts, key=lambda s: (s - near) if s >= near else (near - s) + 1_000_000)


def _all(hay: str, needle: str) -> list[int]:
    out, i = [], hay.find(needle)
    while i >= 0:
        out.append(i)
        i = hay.find(needle, i + 1)
    return out


def locate_quote(memo: str, quote: str, near: Optional[int] = None) -> Optional[Located]:
    q = quote.strip()
    if not q:
        return None
    hits = _all(memo, q)
    if hits:
        i = _pick(hits, near)
        return Located(i, i + len(q), "exact")
    for nfkc, how in ((False, "whitespace"), (True, "width")):
        nm, im = _norm_map(memo, nfkc)
        nq, _ = _norm_map(q, nfkc)
        if not nq:
            continue
        hits = _all(nm, nq)
        if hits:
            j = _pick([im[h] for h in hits], near)
            k = hits[[im[h] for h in hits].index(j)]
            return Located(im[k], im[k + len(nq) - 1] + 1, how)
    return None


def make_span(memo: str, start: int, end: int, starts: Optional[list[int]] = None) -> SourceSpan:
    starts = starts or line_starts(memo)
    return SourceSpan(
        text=memo[start:end], start_char=start, end_char=end,
        line_start=offset_to_line(starts, start), line_end=offset_to_line(starts, max(start, end - 1)),
    )


def _bigrams(s: str) -> set[str]:
    t, _ = _norm_map(s, True)
    t = "".join(ch for ch in t if not unicodedata.category(ch).startswith("P"))
    return {t[i : i + 2] for i in range(len(t) - 1)}


def lexical_support(value: str, quotes: list[str]) -> float:
    vb = _bigrams(value)
    if not vb:
        return 1.0
    qb: set[str] = set()
    for q in quotes:
        qb |= _bigrams(q)
    return len(vb & qb) / len(vb)


@dataclass
class GroundingResult:
    spans: list[SourceSpan] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ungrounded(self) -> bool:
        return any(i.code == "ungrounded_claim" for i in self.issues)


def ground_value(value: str, quotes: list[str], memo: str, slot: Optional[FormSlot] = None,
                 near: Union[int, list, None] = None) -> GroundingResult:
    """Independently re-locate every quote (preferring the position the engine reported)."""
    res = GroundingResult()
    starts = line_starts(memo)
    if not value.strip():
        return res
    if not [q for q in quotes if q.strip()]:
        res.issues.append(ValidationIssue(code="ungrounded_claim", message="no verbatim source quote supports this value / 根拠となる引用がありません"))
        return res
    located: list[str] = []
    cursor = near if isinstance(near, int) else None
    for qi, q in enumerate(quotes):
        if not q.strip():
            continue
        hint = near[qi] if isinstance(near, list) and qi < len(near) else cursor
        loc = locate_quote(memo, q, hint)
        if loc is not None and isinstance(near, int):
            cursor = loc.end
        if loc is None:
            res.issues.append(ValidationIssue(
                code="ungrounded_claim",
                message=f"quote not found verbatim in the memo: 「{q[:40]}{'…' if len(q) > 40 else ''}」 / 原文に存在しない引用",
            ))
            continue
        if loc.how != "exact":
            res.notes.append(f"quote re-anchored ({loc.how}-insensitive match) to the memo's exact text")
        span = make_span(memo, loc.start, loc.end, starts)
        res.spans.append(span)
        located.append(span.text)
    if res.ungrounded:
        return res

    # Every number the value asserts must appear in the evidence.
    evidence = "".join(located)
    have = quantities(evidence)
    missing = [txt for txt, val in value_quantities(value) if val not in have]
    if missing:
        res.issues.append(ValidationIssue(
            code="number_unsupported",
            message=f"number(s) {', '.join(missing)} not present in the quoted evidence / 引用に無い数値",
        ))
    # Every date the value asserts must appear in the evidence (year may be inferred).
    ev_dates = find_dates(evidence)
    for d in find_dates(value):
        md = [e for e in ev_dates if (e.month, e.day) == (d.month, d.day)]
        if md and (d.year is None or any(e.year == d.year for e in md)):
            continue
        if md and all(e.year is None for e in md):
            res.issues.append(ValidationIssue(
                code="year_inferred", severity="warning",
                message=f"year of {d.month}月{d.day}日 is not written in the memo — inferred, please confirm / 年は推定",
            ))
        else:
            res.issues.append(ValidationIssue(
                code="date_unsupported", message=f"date {d.month}月{d.day}日 not supported by the quoted evidence / 引用に無い日付",
            ))
    fmt = slot.required_format if slot else None
    if fmt in ("text", "person_name"):
        ratio = lexical_support(value, located)
        if ratio < LEXICAL_WARN_THRESHOLD:
            res.issues.append(ValidationIssue(
                code="low_lexical_support", severity="warning",
                message=f"only {ratio:.0%} of the wording overlaps the quotes — check the paraphrase / 言い換えを確認",
            ))
    return res
