"""Constraint discovery (read from the form itself) and deterministic value validation.

Japanese forms state their rules in the printed text — ``（200字以内）``,
``法人番号（13桁）``, ``例：JR-2026-001``, ``上限1,000,000円``, ``千円未満切捨て``,
``研究期間／研究分担者／…のうち`` — so the scanner reads them instead of asking
the user to configure anything. Excel data-validation lists are used as well.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Optional

from .models import FormSlot, ValidationIssue
from .textnorm import check_full_date, check_wareki, find_invalid_dates, parse_compound, parse_yen_exact

FORMAT_GUIDE = {
    "wareki_date": "和暦の年月日（曜日つき）— e.g. 令和8年11月2日（月） (2026 = 令和8年, 2019-05-01 = 令和元年)",
    "wareki_period": "和暦の期間（曜日つき）— e.g. 令和8年12月1日（火）～令和9年2月26日（金）",
    "date": "日付（曜日つき）— e.g. 2026年11月2日（月）",
    "yen": "円金額 — digits with comma grouping + 円, e.g. 4,836,000円",
    "pattern": "must match the regular expression in constraints.pattern",
    "digits": "digits only (half-width), exact length in constraints.digits",
    "person_name": "氏名 (no honorifics such as 様/殿)",
    "choice": "one of constraints.allowed_values (several joined by 、 if multi_select)",
    "text": "free text copied or condensed from the memo",
}

_MAXCHARS_RE = re.compile(r"([0-9０-９]+)\s*(?:字|文字)以内")
_DIGITS_RE = re.compile(r"([0-9０-９]+)\s*桁")
_EXAMPLE_RE = re.compile(r"例\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9\-_/.]*[A-Za-z0-9])")
_CAP_RE = re.compile(r"上限\s*(?:額)?\s*[:：]?\s*((?:[0-9０-９][0-9０-９,，]*\s*[万千億]?\s*)+)円")
_OPTIONS_SLASH_RE = re.compile(
    r"([^\s※（）()「」:：、。／/]+(?:[／/][^\s※（）()「」／/:：、。]+)+)\s*(?:のうち|から|より|のいずれか)"
)
_OPTIONS_QUOTED_RE = re.compile(r"((?:「[^」]+」[、・]?){2,})\s*(?:のいずれか|から|のうち)")


def pattern_from_example(example: str) -> str:
    """Derive a regex from a printed example: 'JR-2026-001' -> '^[A-Z]{2}-[0-9]{4}-[0-9]{3}$'."""
    out = []
    for m in re.finditer(r"[A-Z]+|[a-z]+|[0-9]+|.", example):
        tok = m.group(0)
        if tok.isdigit():
            out.append(f"[0-9]{{{len(tok)}}}")
        elif tok.isalpha() and tok.isupper():
            out.append(f"[A-Z]{{{len(tok)}}}")
        elif tok.isalpha():
            out.append(f"[a-z]{{{len(tok)}}}")
        else:
            out.append(re.escape(tok))
    return "^" + "".join(out) + "$"


def parse_options(texts: Iterable[str]) -> tuple[list[str], bool]:
    for t in texts:
        m = _OPTIONS_QUOTED_RE.search(t)
        if m:
            return re.findall(r"「([^」]+)」", m.group(1)), "複数" in t
        m = _OPTIONS_SLASH_RE.search(t)
        if m:
            opts = [o.strip() for o in re.split(r"[／/]", m.group(1)) if o.strip()]
            return opts, "複数" in t
    return [], False


def infer_slot_constraints(
    slot_id: str,
    label_texts: list[str],
    hint_texts: list[str],
    doc_text: str,
    allowed_values: Optional[list[str]] = None,
    xlsx_max_len: Optional[int] = None,
) -> dict[str, Any]:
    """Infer required_format / max_chars / constraints / importance for one slot."""
    own = [slot_id] + [t for t in label_texts if t]
    hints = [t for t in hint_texts if t]
    blob = " ".join(own + hints)
    constraints: dict[str, Any] = {}

    max_chars: Optional[int] = None
    for t in own + hints:
        m = _MAXCHARS_RE.search(t)
        if m:
            max_chars = int(unicodedata.normalize("NFKC", m.group(1)))
            break
    if xlsx_max_len and (max_chars is None or xlsx_max_len < max_chars):
        max_chars = xlsx_max_len

    fmt = "text"
    name = slot_id.replace("_", "")
    wareki = "和暦" in doc_text or "令和" in doc_text
    options, multi = (allowed_values, False) if allowed_values else parse_options(hints + own)
    if options:
        fmt = "choice"
        constraints["allowed_values"] = options
        constraints["multi_select"] = multi
        if multi:
            constraints["separator"] = "、"
    elif "期間" in name:
        fmt = "wareki_period" if wareki else "date"
    elif name.endswith(("日", "日付")) or "年月日" in blob:
        fmt = "wareki_date" if wareki else "date"
    elif "金額" in name or name.endswith("額") or re.search(r"単位\s*[:：]\s*円", blob):
        fmt = "yen"
        if "千円未満切捨" in blob or "千円未満切り捨" in blob:
            constraints["multiple_of"] = 1000
        cap = _CAP_RE.search(blob)
        if cap:
            constraints["max_value"] = parse_compound(cap.group(1))
    else:
        dm = _DIGITS_RE.search(blob)
        ex = _EXAMPLE_RE.search(blob)
        if dm and not ex:
            fmt = "digits"
            constraints["digits"] = int(unicodedata.normalize("NFKC", dm.group(1)))
            constraints["pattern"] = f"^[0-9]{{{constraints['digits']}}}$"
        elif ex:
            fmt = "pattern"
            constraints["example"] = ex.group(1)
            constraints["pattern"] = pattern_from_example(ex.group(1))
            if "半角" in blob:
                constraints["halfwidth"] = True
        elif name.endswith("氏名"):
            fmt = "person_name"

    importance = "normal" if fmt == "text" else "high"
    return {"required_format": fmt, "max_chars": max_chars, "constraints": constraints, "importance": importance}


def _issue(code: str, message: str, severity: str = "error") -> ValidationIssue:
    return ValidationIssue(code=code, message=message, severity=severity)  # type: ignore[arg-type]


def split_choice(value: str, slot: FormSlot) -> list[str]:
    if slot.constraints.get("multi_select"):
        return [p.strip() for p in re.split(r"[、,，・／/]", value) if p.strip()]
    return [value.strip()]


_PERIOD_SEP = re.compile(r"\s*[～〜~]\s*")
DATE_CODES = ("format", "date_invalid", "weekday_mismatch", "period_order")


def _date_issues(parts: list[str], check, kind: str) -> list[ValidationIssue]:
    """Validate the endpoint(s) of a date / period: real calendar date, inside its era, matching （曜）, in order."""
    issues: list[ValidationIssue] = []
    dates = []
    for p in parts:
        d, problem = check(p)
        if problem:
            issues.append(_issue(problem[0], f"«{p}»: {problem[1]}"))
        elif d is None:
            # wrong syntax (e.g. a Western date in a 和暦 field): say what is wrong with the date itself first
            # (2026年2月30日, wrong （曜）), then "format" so the repair step can still convert it
            issues += [_issue(code, f"«{p}»: {msg}") for _, code, msg in find_invalid_dates(p)]
            if not any(i.code == "format" for i in issues):  # one format complaint per value, not per endpoint
                issues.append(_issue("format", kind))
        dates.append(d)
    if not issues and len(dates) == 2 and dates[0] > dates[1]:  # type: ignore[operator]
        issues.append(_issue("period_order", "period ends before it starts / 期間の終了が開始より前です"))
    return issues


def validate_value(slot: FormSlot, value: str) -> list[ValidationIssue]:
    """Check a candidate value against the slot's discovered constraints."""
    v = value.strip()
    if not v:
        return [_issue("empty", "value is empty / 値が空です")]
    issues: list[ValidationIssue] = []
    if slot.max_chars and len(v) > slot.max_chars:
        issues.append(_issue("max_chars", f"{len(v)} chars exceeds the {slot.max_chars}-char limit / {slot.max_chars}字以内の制限を超過"))
    fmt = slot.required_format
    c = slot.constraints
    if fmt == "wareki_date":
        issues += _date_issues([v], check_wareki, "not a 和暦 date such as 令和8年11月2日（月） / 和暦の年月日ではありません")
    elif fmt == "wareki_period":
        parts = _PERIOD_SEP.split(v)
        if len(parts) != 2:
            issues.append(_issue("format", "not a 和暦 period such as 令和8年12月1日（火）～令和9年2月26日（金） / 和暦の期間ではありません"))
        else:
            issues += _date_issues(parts, check_wareki, "not a 和暦 period such as 令和8年12月1日（火）～令和9年2月26日（金） "
                                                        "/ 和暦の期間ではありません")
    elif fmt == "date":
        parts = _PERIOD_SEP.split(v)
        if len(parts) > 2:
            issues.append(_issue("format", "not a date such as 2026年11月2日（月） / 日付ではありません"))
        else:
            issues += _date_issues(parts, check_full_date, "not a full date such as 2026年11月2日（月） / 年月日がそろった日付ではありません")
    elif fmt == "yen":
        n = parse_yen_exact(v)
        if n is None:
            issues.append(_issue("format", "not a yen amount such as 4,836,000円 / 円金額の形式ではありません"))
        else:
            if c.get("multiple_of") and n % int(c["multiple_of"]):
                issues.append(_issue("multiple_of", f"must be a multiple of {c['multiple_of']:,}円 (千円未満切捨て)"))
            if c.get("max_value") is not None and n > int(c["max_value"]):
                issues.append(_issue("max_value", f"{n:,}円 exceeds the printed cap {int(c['max_value']):,}円 / 上限超過"))
    elif fmt in ("pattern", "digits"):
        pat = c.get("pattern")
        if pat and not re.fullmatch(pat, v) and fmt == "digits" and c.get("digits"):
            # say how many digits were given: 「123456789012」 is one digit short of a 法人番号, not just "invalid"
            n = len(re.sub(r"\D", "", unicodedata.normalize("NFKC", v)))
            other = "" if re.fullmatch(r"[0-9]*", v) else " (half-width digits only, no letters or symbols)"
            issues.append(_issue("format", f"{c['digits']} digits required, {n} given{other} / "
                                           f"{c['digits']}桁の数字が必要です（入力は{n}桁）"))
        elif pat and not re.fullmatch(pat, v):
            hint = c.get("example") or (f"{c.get('digits')} digits" if c.get("digits") else pat)
            issues.append(_issue("format", f"does not match the required pattern ({hint}) / 書式不一致"))
    elif fmt == "choice":
        allowed = c.get("allowed_values") or []
        items = split_choice(v, slot)
        bad = [i for i in items if i not in allowed]
        if bad:
            issues.append(_issue("allowed_values", f"{'、'.join(bad)} is not an allowed option ({'／'.join(allowed)})"))
        if not c.get("multi_select") and len(items) > 1:
            issues.append(_issue("allowed_values", "only one option may be selected / 単一選択です"))
        if len(set(items)) != len(items):
            issues.append(_issue("allowed_values", "duplicate options / 選択肢が重複しています"))
    elif fmt == "person_name":
        if re.search(r"[（()）]", v):
            issues.append(_issue("format", "a name should not contain annotations in brackets", "warning"))
    if fmt not in ("wareki_date", "wareki_period", "date"):
        # free text copied from the memo may carry an impossible date — surface it to the reviewer
        issues += [_issue(code, msg, "warning") for _, code, msg in find_invalid_dates(v)]
    return issues
