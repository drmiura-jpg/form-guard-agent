"""Strict source grounding: every value must be backed by verbatim memo excerpts.

The checker is deliberately independent of whichever engine proposed the value
(Claude or the offline demo engine): it re-locates each quote in the memo itself,
recomputes line numbers, and checks that every number / date the value asserts
actually appears in the quoted evidence. Names and IDs must appear in the quotes
(after width / whitespace / honorific normalisation), and a selected option must be
the one the quote affirmatively states.
"""

from __future__ import annotations

import bisect
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional, Union

from .models import FormSlot, SourceSpan, ValidationIssue
from .textnorm import clean_person_name, find_dates, normalize_for_match, quantities, to_halfwidth_id, value_quantities

LEXICAL_WARN_THRESHOLD = 0.5
# GRD-02 covers numbers and dates; every other error-severity grounding issue fails GRD-01.
NUMBER_DATE_CODES = ("number_unsupported", "date_unsupported", "weekday_mismatch")


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


def name_supported(value: str, evidence: str) -> bool:
    """The name (honorifics / bracketed notes removed, width and spacing normalised) appears in the quotes."""
    v = normalize_for_match(clean_person_name(value))
    return bool(v) and v in normalize_for_match(evidence)


def id_supported(value: str, evidence: str, digits_only: bool = False) -> bool:
    """The ID appears in the quotes as a whole token (ＪＲ－２０２６－０４７ supports JR-2026-047)."""
    v, ev = to_halfwidth_id(value), to_halfwidth_id(evidence)
    if digits_only:
        v = re.sub(r"\D", "", v)
        return bool(v) and v in {re.sub(r"\D", "", t) for t in re.findall(r"[0-9](?:[0-9]|-(?=[0-9]))*", ev)}
    if not v:
        return False
    for m in re.finditer(re.escape(v), ev):
        before = ev[m.start() - 1] if m.start() else ""
        after = ev[m.end()] if m.end() < len(ev) else ""
        if not (before.isascii() and before.isalnum()) and not (after.isascii() and after.isalnum()):
            return True
    return False


_NEGATED_AFTER = re.compile(r"\s*(?:以外|(?:で|では|じゃ)?(?:は)?(?:なく|ない|無く|無い|ありません|ございません))")
_JUDGEMENT_BEFORE = re.compile(r"(?:判定|結果|評価|区分|選択|該当)\s*(?:は|を|が|[:：])?\s*$")


def affirmed_options(allowed: list[str], evidence: str) -> tuple[set[str], set[str]]:
    """Options the quote states affirmatively, and every option it mentions without negation.

    Longest options match first (so 「不可」 is not read as 「可」). An option followed by
    「ではなく」「以外」… is negated. If any option is introduced by 判定／結果／評価, only those
    count as affirmed (「良好だが判定は要是正」 affirms 要是正, not 良).
    """
    ev = unicodedata.normalize("NFKC", evidence)
    mentions: list[tuple[int, int, str, bool, bool]] = []
    for opt in sorted(allowed, key=len, reverse=True):
        o = unicodedata.normalize("NFKC", opt)
        if not o:
            continue
        for m in re.finditer(re.escape(o), ev):
            if any(s < m.end() and m.start() < e for s, e, *_ in mentions):
                continue
            negated = bool(_NEGATED_AFTER.match(ev, m.end()))
            cued = bool(_JUDGEMENT_BEFORE.search(ev[max(0, m.start() - 8) : m.start()]))
            mentions.append((m.start(), m.end(), opt, negated, cued))
    positive = [x for x in mentions if not x[3]]
    cued = [x for x in positive if x[4]]
    return {x[2] for x in (cued or positive)}, {x[2] for x in positive}


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
        same = [e for e in md if d.year is None or e.year == d.year]
        if same:
            if d.weekday and not any(e.weekday in (None, d.weekday) for e in same):
                res.issues.append(ValidationIssue(
                    code="weekday_mismatch",
                    message=f"{d.month}月{d.day}日 is （{d.weekday}） in the value but （{same[0].weekday}） in the memo "
                            f"/ 曜日が引用と一致しません",
                ))
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
    if fmt == "person_name" and not name_supported(value, evidence):
        res.issues.append(ValidationIssue(
            code="value_unsupported",
            message=f"name «{value}» does not appear in the quoted evidence / 氏名が引用と一致しません",
        ))
    elif fmt in ("pattern", "digits") and not id_supported(value, evidence, digits_only=fmt == "digits"):
        res.issues.append(ValidationIssue(
            code="value_unsupported",
            message=f"«{value}» does not appear in the quoted evidence (width / dashes / spaces normalised) "
                    f"/ 番号が引用と一致しません",
        ))
    elif fmt == "choice" and slot is not None:
        from .constraints import split_choice

        selected = split_choice(value, slot)
        affirmed, mentioned = affirmed_options(slot.constraints.get("allowed_values") or [], evidence)
        missing = [s for s in selected if s not in affirmed]
        if missing:
            stated = "／".join(sorted(affirmed)) or "none of the options"
            res.issues.append(ValidationIssue(
                code="choice_unsupported",
                message=f"«{'、'.join(missing)}» is not what the quote states (it states {stated}) "
                        f"/ 選択肢が引用の記載と一致しません",
            ))
        elif not slot.constraints.get("multi_select") and len(mentioned) > 1:
            res.issues.append(ValidationIssue(
                code="choice_ambiguous", severity="warning",
                message=f"the quote also mentions {'／'.join(sorted(mentioned - set(selected)))} — confirm the option "
                        f"/ 引用に他の選択肢も含まれます",
            ))
    if fmt == "text":
        ratio = lexical_support(value, located)
        if ratio < LEXICAL_WARN_THRESHOLD:
            res.issues.append(ValidationIssue(
                code="low_lexical_support", severity="warning",
                message=f"only {ratio:.0%} of the wording overlaps the quotes — check the paraphrase / 言い換えを確認",
            ))
    return res
