"""Form-Guard Agent test suite (offline; the live Claude path is exercised with a fake client)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import docx
import openpyxl
import pytest
from lxml import etree

from conftest import echo_model, fake_message
from form_guard import DEFAULT_MODEL, MODEL_HAIKU, MODEL_OPUS, MODEL_SONNET
from form_guard.constraints import pattern_from_example, validate_value
from form_guard.grounding import affirmed_options, ground_value, locate_quote
from form_guard.mapper import (
    HUMAN_ROUTE,
    THINKING_SUMMARY_LABEL,
    TIER_MODELS,
    ClaudeEngine,
    DemoEngine,
    Draft,
    LiveEngineError,
    _effort_for,
    human_mapping,
    map_fields,
    model_capabilities,
    route_slot,
)
from form_guard.measure import markdown, measure
from form_guard.models import FormSlot
from form_guard.ooxml import S_NS, UnsafeXMLError, UnsupportedXMLError, add_preserve_attr, index_part, raw_char_offsets
from form_guard.pipeline import run_pipeline
from form_guard.report import build_report_html
from form_guard.samples import DEMOS, UNMARKED_FORMS, demo_paths, generate_samples, read_memo, unmarked_paths
from form_guard.scanner import build_confirmed_slot_spec, discover_blank_cells, find_markers, scan
from form_guard.textnorm import (
    check_wareki,
    condense_extractive,
    era_to_year,
    find_amounts,
    find_dates,
    find_invalid_dates,
    kanji_to_int,
    parse_compound,
    parse_wareki_exact,
    quantities,
    to_wareki,
)
from form_guard.verifier import build_hitl_checklist, compare_naive, flag_label, infer_values_from_filled, verify
from form_guard.visual_diff import _safe_color, compare_layouts, layout_docx, layout_xlsx, svg_document
from form_guard.writer import fill_form, patch_part
from form_guard.ziputil import read_raw_zip, rewrite_zip

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def paths(samples_dir, demo_id):
    return demo_paths(next(d for d in DEMOS if d["id"] == demo_id), samples_dir)


def rewrite_entry(src: Path, dst: Path, name: str, fn) -> None:
    """Copy a package, transforming one entry (used to simulate tampering)."""
    zin = zipfile.ZipFile(src)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            zout.writestr(info, fn(data) if info.filename == name else data)
    dst.write_bytes(buf.getvalue())


# ===================================================================== samples


def test_generate_samples_creates_all_forms_memos_and_naive_files(samples_dir):
    for d in DEMOS:
        p = demo_paths(d, samples_dir)
        assert p["form"].exists() and p["memo"].exists() and p["naive"].exists()
        assert (samples_dir / "blank_forms" / f"{p['form'].stem}.slots.json").exists()
    f1 = docx.Document(paths(samples_dir, "form1")["form"])
    f2 = docx.Document(paths(samples_dir, "form2")["form"])
    assert len(f1.tables) == 1 and len(f2.tables) == 2
    wb = openpyxl.load_workbook(paths(samples_dir, "form3")["form"])
    ws = wb["点検報告書"]
    assert ws["D13"].value.startswith("=COUNTIF") and len(ws.merged_cells.ranges) == 40
    assert ws.data_validations.dataValidation[0].formula1 == '"良,要注意,要是正"'


def test_sample_generation_is_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    generate_samples(a, with_comparisons=False)
    generate_samples(b, with_comparisons=False)
    files = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
    assert files
    for rel in files:
        assert (a / rel).read_bytes() == (b / rel).read_bytes(), rel


def test_samples_are_fictional_and_non_medical(samples_dir):
    texts = [read_memo(samples_dir / "memos" / d["memo"]) for d in DEMOS]
    for d in DEMOS:
        sc = scan(demo_paths(d, samples_dir)["form"])
        texts.append(sc.doc_text)
        assert "架空" in sc.doc_text, f"{d['id']} must state it is fictional"
    banned = ["病院", "患者", "診断", "診療", "医療", "処方", "看護", "病名", "治療"]
    blob = "\n".join(texts)
    assert not [w for w in banned if w in blob]
    assert "example.jp" in blob  # only reserved example domains are used


def test_form1_contains_word_style_split_run_markers(samples_dir):
    xml = zipfile.ZipFile(paths(samples_dir, "form1")["form"]).read("word/document.xml").decode("utf-8")
    assert "<w:t>{{研究</w:t>" in xml and "<w:t>氏名}}</w:t>" in xml and "w:proofErr" in xml
    assert "<w:t>{{変更理由</w:t>" in xml and "w:bookmarkStart" in xml
    sc = scan(paths(samples_dir, "form1")["form"])
    assert len(sc.slot("研究代表者氏名").occurrences) == 2


# ======================================================================= ooxml


def test_raw_char_offsets_handle_entities_char_refs_and_utf8():
    raw = "A&amp;&#28857;日".encode("utf-8")
    offs = raw_char_offsets(raw)
    assert len(offs) == 5 and offs[:4] == [0, 1, 6, 14] and offs[-1] == len(raw)


def test_index_rejects_dtd_and_entity_tricks():
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><w:document xmlns:w="%s"/>' % W.encode()
    with pytest.raises(UnsafeXMLError):
        index_part("word/document.xml", evil, "word")


def test_ruby_phonetic_and_tab_stop_definitions_are_excluded():
    word = (f'<w:document xmlns:w="{W}"><w:body><w:p><w:pPr><w:tabs><w:tab w:val="left" w:pos="100"/></w:tabs></w:pPr>'
            '<w:r><w:ruby><w:rt><w:r><w:t>ふりがな</w:t></w:r></w:rt><w:rubyBase><w:r><w:t>漢字</w:t></w:r></w:rubyBase></w:ruby></w:r>'
            '<w:r><w:tab/><w:t>{{A}}</w:t></w:r></w:p></w:body></w:document>').encode()
    idx = index_part("word/document.xml", word, "word")
    assert idx.container_text(idx.containers[0]) == "漢字\t{{A}}"
    sst = (f'<sst xmlns="{S_NS}"><si><r><t>{{{{点検</t></r><r><t>日}}}}</t></r><rPh sb="0" eb="1"><t>テンケン</t></rPh></si></sst>').encode()
    sidx = index_part("xl/sharedStrings.xml", sst, "sst")
    assert sidx.container_text(sidx.containers[0]) == "{{点検日}}"


def test_patch_part_adds_xml_space_preserve_only_when_needed():
    xml = (f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:rPr><w:b/></w:rPr><w:t>{{{{A</w:t></w:r>'
           '<w:r><w:t>}} tail</w:t></w:r></w:p></w:body></w:document>').encode()
    idx = index_part("word/document.xml", xml, "word")
    new, recs = patch_part(idx, {"A": "X & Y"}, "docx")
    root = etree.fromstring(new)
    ts = [t.text or "" for t in root.iter(f"{{{W}}}t")]
    assert ts == ["X & Y", " tail"]
    assert {r.kind for r in recs} == {"text", "attr"}
    assert b'<w:rPr><w:b/></w:rPr><w:t>X &amp; Y</w:t>' in new and b'xml:space="preserve"> tail' in new


# ===================================================================== scanner


def test_scan_form1_discovers_slots_locations_and_printed_constraints(samples_dir):
    form = paths(samples_dir, "form1")["form"]
    before = hashlib.sha256(form.read_bytes()).hexdigest()
    sc = scan(form)
    assert hashlib.sha256(form.read_bytes()).hexdigest() == before  # scanning never writes
    ids = [s.slot_id for s in sc.slots]
    assert ids == ["提出日", "研究課題番号", "研究代表者氏名", "所属部局", "変更種別", "変更前の計画概要", "変更後の計画概要",
                   "変更理由および予算影響"]
    num = sc.slot("研究課題番号")
    assert num.required_format == "pattern" and num.constraints["pattern"] == r"^[A-Z]{2}\-[0-9]{4}\-[0-9]{3}$"
    assert (num.occurrences[0].table_idx, num.occurrences[0].row_idx, num.occurrences[0].col_idx) == (0, 1, 1)
    assert sc.slot("変更理由および予算影響").max_chars == 200
    assert sc.slot("変更前の計画概要").max_chars == 100
    kind = sc.slot("変更種別")
    assert kind.constraints["allowed_values"] == ["研究期間", "研究分担者", "研究経費", "研究内容"] and kind.constraints["multi_select"]
    assert sc.slot("提出日").required_format == "wareki_date"
    assert sc.slot("研究代表者氏名").context_label == "研究代表者 › 氏名"  # vMerge resolved


def test_scan_form2_marker_kinds_and_coordinate_target(samples_dir):
    sc = scan(paths(samples_dir, "form2")["form"])
    kinds = {s.slot_id: s.occurrences[0].marker_kind for s in sc.slots}
    assert kinds["申請日"] == "kinyuran" and kinds["法人番号"] == "kinyuran" and kinds["所在地"] == "braces"
    assert kinds["担当者連絡先"] == "empty_cell"
    cap = sc.slot("補助金申請額")
    assert cap.required_format == "yen" and cap.constraints == {"multiple_of": 1000, "max_value": 1_000_000}
    assert sc.slot("法人番号").constraints["digits"] == 13
    assert sc.slot("事業実施期間").required_format == "wareki_period"


def test_scan_form3_xlsx_uses_data_validation_and_cell_refs(samples_dir):
    sc = scan(paths(samples_dir, "form3")["form"])
    assert sc.file_type == "xlsx" and sc.target_parts == ["xl/worksheets/sheet1.xml"]
    j = sc.slot("空調設備_判定")
    assert j.constraints["allowed_values"] == ["良", "要注意", "要是正"] and j.occurrences[0].cell_ref == "F8"
    assert sc.slot("指摘事項および是正措置").max_chars == 120
    assert sc.slot("点検対象施設").occurrences[0].box_id == "s0!D4"


def to_shared_strings(src: Path, dst: Path) -> None:
    """Convert openpyxl's inline strings into an Excel-style sharedStrings part with a rich-text split marker."""
    zin = zipfile.ZipFile(src)
    sheet = etree.fromstring(zin.read("xl/worksheets/sheet1.xml"))
    strings = []
    for c in sheet.iter(f"{{{S_NS}}}c"):
        if c.get("t") == "inlineStr":
            is_ = c.find(f"{{{S_NS}}}is")
            text = "".join(t.text or "" for t in is_.iter(f"{{{S_NS}}}t"))
            c.remove(is_)
            c.set("t", "s")
            etree.SubElement(c, f"{{{S_NS}}}v").text = str(len(strings))
            strings.append(text)
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    sis = []
    for s in strings:
        if s == "{{点検対象施設}}":
            sis.append('<si><r><t>{{点検</t></r><r><rPr><b/></rPr><t>対象施設}}</t></r>'
                       '<rPh sb="0" eb="2"><t>テンケンタイショウ</t></rPh><phoneticPr fontId="1"/></si>')
        else:
            sis.append(f'<si><t xml:space="preserve">{esc(s)}</t></si>')
    sst = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<sst xmlns="{S_NS}" count="{len(strings)}" '
           f'uniqueCount="{len(strings)}">{"".join(sis)}</sst>').encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                data = etree.tostring(sheet, xml_declaration=True, encoding="UTF-8", standalone=True)
            elif info.filename == "[Content_Types].xml":
                data = data.replace(b"</Types>", b'<Override PartName="/xl/sharedStrings.xml" ContentType="application/'
                                    b'vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>')
            elif info.filename == "xl/_rels/workbook.xml.rels":
                data = data.replace(b"</Relationships>", b'<Relationship Id="rIdSST" Type="http://schemas.openxmlformats.org/'
                                    b'officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/></Relationships>')
            z.writestr(info, data)
        z.writestr("xl/sharedStrings.xml", sst)
    dst.write_bytes(buf.getvalue())


def test_shared_strings_workbook_rich_text_split_and_phonetic_runs(samples_dir, tmp_path):
    src = tmp_path / "shared.xlsx"
    to_shared_strings(paths(samples_dir, "form3")["form"], src)
    sc = scan(src)
    slot = sc.slot("点検対象施設")
    assert slot.occurrences[0].part_name == "xl/sharedStrings.xml" and slot.occurrences[0].cell_ref == "D4"
    out = tmp_path / "shared_filled.xlsx"
    w = fill_form(sc, {"点検対象施設": "テスト施設", "空調設備_判定": "要是正"}, out)
    assert w.modified_parts == ["xl/sharedStrings.xml"]  # the worksheet XML is not touched at all
    zo, zf = zipfile.ZipFile(src), zipfile.ZipFile(out)
    assert zo.read("xl/worksheets/sheet1.xml") == zf.read("xl/worksheets/sheet1.xml")
    sst = zf.read("xl/sharedStrings.xml").decode("utf-8")
    assert "<r><t>テスト施設</t></r><r><rPr><b/></rPr><t></t></r>" in sst and "テンケンタイショウ" in sst
    ws = openpyxl.load_workbook(out).active
    assert ws["D4"].value == "テスト施設" and ws["F8"].value == "要是正"
    maps = [human_mapping(sc.slot(k), v) for k, v in w.values.items()]
    rep = verify(sc, out, maps, "")
    assert not [c for c in rep.checks if not c.passed]


# ============================================================ normalisers/rules


def test_wareki_dates_weekdays_and_amounts():
    assert to_wareki(date(2026, 11, 2)) == "令和8年11月2日（月）"  # human-facing dates carry the weekday
    assert to_wareki(date(2026, 11, 2), include_weekday=False) == "令和8年11月2日"
    assert to_wareki(date(2019, 5, 1)) == "令和元年5月1日（水）" and to_wareki(date(2019, 4, 30)) == "平成31年4月30日（火）"
    d = find_dates("提出日：2026年11月2日（月）に提出")[0]
    assert (d.year, d.month, d.day, d.weekday) == (2026, 11, 2, "月")
    assert [x.kind for x in find_dates("令和8年11月2日、12月8日（火）、11/2（月）、補助率1/3")] == ["wareki", "month_day", "month_day"]
    assert parse_compound("483万6千") == 4_836_000 and kanji_to_int("四百八十三万六千") == 4_836_000
    assert [a.value for a in find_amounts("税抜で483万6千円、税込では5,319,600円、1,000千円")] == [4_836_000, 5_319_600, 1_000_000]
    assert pattern_from_example("JR-2026-001") == r"^[A-Z]{2}\-[0-9]{4}\-[0-9]{3}$"


def test_condensation_only_deletes_verbatim_text(samples_dir):
    memo = read_memo(paths(samples_dir, "form1")["memo"])
    para = memo.split("■ 変更理由と予算への影響（届出書は200字以内）\n")[1].split("\n")[0]
    c = condense_extractive(para, 200)
    assert c is not None and len(c.text) <= 200 and len(para) > 200
    assert all(f in para for f in c.fragments) and any(r.startswith("なお") for r in c.removed)
    assert "38万円" in c.text  # the budget figure survives


def test_validate_value_enforces_printed_rules(samples_dir):
    sc = scan(paths(samples_dir, "form2")["form"])
    codes = lambda sid, v: [i.code for i in validate_value(sc.slot(sid), v) if i.severity == "error"]
    assert codes("補助金申請額", "1,612,000円") == ["max_value"]
    assert codes("補助金申請額", "999,500円") == ["multiple_of"]
    assert codes("補助金申請額", "100万円") == ["format"]
    assert codes("補助金申請額", "1,000,000円") == []
    assert codes("法人番号", "12345") == ["format"] and codes("法人番号", "1234567890123") == []
    assert codes("申請日", "2026年11月2日") == ["format"] and codes("申請日", "令和8年11月2日") == []
    assert codes("事業実施期間", "令和9年2月26日～令和8年12月1日") == ["period_order"]


# ====================================================================== mapper


def test_demo_engine_fills_form1_with_exact_source_spans(samples_dir):
    p = paths(samples_dir, "form1")
    memo = read_memo(p["memo"])
    run = map_fields(scan(p["form"]).slots, memo, DemoEngine())
    by = {m.slot_id: m for m in run.mappings}
    assert all(m.status == "filled" for m in run.mappings)
    for m in run.mappings:
        assert m.source_spans
        for sp in m.source_spans:
            assert memo[sp.start_char : sp.end_char] == sp.text  # verbatim, at the stated offsets
    assert by["研究代表者氏名"].filled_value == "架空 太郎" and by["研究代表者氏名"].source_line_range == (7, 7)
    assert by["変更種別"].filled_value == "研究経費、研究内容" and "order" in by["変更種別"].normalizations


def test_self_correction_converts_formats(demo_runs):
    by = {m.slot_id: m for m in demo_runs("form1").mappings}
    date_m = by["提出日"]
    assert date_m.filled_value == "令和8年11月2日（月）"
    assert [a.passed for a in date_m.attempts] == [False, True] and date_m.attempts[0].issues[0].code == "format"
    assert {"wareki", "weekday_ok"} <= set(date_m.normalizations)
    assert by["研究課題番号"].filled_value == "JR-2026-047" and "halfwidth" in by["研究課題番号"].normalizations


def test_self_correction_condenses_to_the_printed_limit(demo_runs):
    m = {x.slot_id: x for x in demo_runs("form1").mappings}["変更理由および予算影響"]
    assert m.attempts[0].issues[0].code == "max_chars" and len(m.attempts[0].value) > 200
    assert m.char_count <= 200 and "condensed" in m.normalizations and len(m.source_spans) >= 3


def test_printed_cap_triggers_constraint_guided_reextraction(demo_runs):
    by = {m.slot_id: m for m in demo_runs("form2").mappings}
    m = by["補助金申請額"]
    assert m.attempts[0].value == "1,612,000円" and m.attempts[0].issues[0].code == "max_value"
    assert m.filled_value == "1,000,000円" and "re_extracted" in m.normalizations
    assert by["導入予定金額_税抜"].filled_value == "4,836,000円"
    assert by["事業実施期間"].filled_value == "令和8年12月1日（火）～令和9年2月26日（金）"


def test_missing_fact_is_never_guessed(demo_runs):
    r = demo_runs("form2")
    m = {x.slot_id: x for x in r.mappings}["法人番号"]
    assert m.status == "needs_human_input" and m.filled_value == "" and not m.suggested_value
    assert r.report.pending_slots == ["法人番号"] and r.report.overall_status == "NEEDS_HUMAN_INPUT"
    assert "[記入欄: 法人番号]" in "".join(p.text for t in docx.Document(r.filled_path).tables for row in t.rows for p in row.cells[1].paragraphs)


def test_inferred_year_is_flagged_for_the_reviewer(demo_runs):
    m = {x.slot_id: x for x in demo_runs("form3").mappings}["次回点検予定日"]
    assert m.filled_value == "令和8年12月8日（火）" and "year_inferred" in m.normalizations
    hitl = next(h for h in demo_runs("form3").report.hitl_checklist if h.slot_id == "次回点検予定日")
    assert "year_inferred" in hitl.flags


def test_grounding_rejects_fabricated_quotes_and_numbers(samples_dir):
    memo = read_memo(paths(samples_dir, "form2")["memo"])
    g = ground_value("1,500,000円", ["補助金は150万円で申請"], memo)
    assert g.ungrounded
    g2 = ground_value("年間約30%の電力削減", ["年間12,400kWhの電力削減（現行設備比で約18％削減）"], memo)
    assert [i.code for i in g2.issues] == ["number_unsupported"]
    g3 = ground_value("4,836,000円", ["税抜で483万6千円"], memo)
    assert not g3.issues and g3.spans[0].line_start == 18
    assert locate_quote(memo, "株式会社サンプル精工", near=memo.index("申請事業者名")).start > memo.index("申請事業者名")


def _claude_fields(memo_overrides=None):
    base = {
        "提出日": ("令和8年11月2日", ["2026年11月2日（月）"]),
        "研究課題番号": ("JR-2026-047", ["ＪＲ－２０２６－０４７"]),
        "研究代表者氏名": ("架空 太郎", ["架空 太郎（理工学研究科 機械システム専攻 教授）"]),
        "所属部局": ("工学部", ["所属：工学部"]),  # hallucinated -> must be caught and revised
        "変更種別": ("研究経費、研究内容", ["研究内容、研究経費"]),
        "変更前の計画概要": ("現行計画では、試作した小型熱交換器の性能を実機試験のみで評価する（試験回数12回）。",
                     ["現行計画では、試作した小型熱交換器の性能を実機試験のみで評価する（試験回数12回）。"]),
        "変更後の計画概要": ("実機試験を6回に減らし、数値シミュレーションによる性能評価を追加する。",
                     ["実機試験を6回に減らし、数値シミュレーションによる性能評価を追加する。"]),
        "変更理由および予算影響": ("共同研究先の試験設備が2026年12月から改修工事に入り、当初予定していた実機試験12回のうち6回が実施できなくなった。"
                        "予算面では、試験装置の借用料が42万円減る一方、計算サーバ利用料として80万円が新たに必要になるため、差し引きで38万円の増額となる。",
                        ["共同研究先の試験設備が2026年12月から改修工事に入り、当初予定していた実機試験12回のうち6回が実施できなくなった。",
                         "予算面では、試験装置の借用料が42万円減る一方、計算サーバ利用料として80万円が新たに必要になるため、差し引きで38万円の増額となる。"]),
    }
    base.update(memo_overrides or {})
    return {"fields": [{"slot_id": k, "status": "filled", "value": v, "source_quotes": q, "line_start": 1, "line_end": 1,
                        "confidence": 0.9, "reasoning": "from memo"} for k, (v, q) in base.items()]}


def test_live_claude_engine_request_shape_and_self_correction(samples_dir, fake_client_factory):
    p = paths(samples_dir, "form1")
    memo = read_memo(p["memo"])
    revision = {"fields": [{"slot_id": "所属部局", "status": "filled", "value": "理工学研究科",
                            "source_quotes": ["・所属部局：理工学研究科"], "line_start": 8, "line_end": 8,
                            "confidence": 0.95, "reasoning": "fixed"}]}
    client = fake_client_factory([fake_message(_claude_fields()), fake_message(revision)])
    run = map_fields(scan(p["form"]).slots, memo, ClaudeEngine(client=client, model_tier="opus"))
    calls = client.beta.messages.calls
    assert len(calls) == 2
    first = calls[0]
    assert first["model"] == "claude-opus-5-5" and first["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert first["output_config"]["format"]["type"] == "json_schema" and first["output_config"]["effort"] == "medium"
    assert first["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in first["betas"]
    assert "tool_choice" not in first and "temperature" not in first
    assert "L001|" in first["messages"][0]["content"] and "<memo>" in first["messages"][0]["content"]
    feedback = calls[1]["messages"][-1]["content"]
    assert "所属部局" in feedback and "quote not found verbatim" in feedback
    by = {m.slot_id: m for m in run.mappings}
    assert by["所属部局"].filled_value == "理工学研究科" and "revision" in by["所属部局"].attempts[-1].action
    assert by["研究課題番号"].filled_value == "JR-2026-047"
    assert all(m.status == "filled" for m in run.mappings)
    assert run.usage["requests"] == 2 and run.usage["input_tokens"] == 2400


def test_refusal_is_handled_and_auto_mode_falls_back(samples_dir, fake_client_factory, tmp_path):
    p = paths(samples_dir, "form1")
    memo = read_memo(p["memo"])
    with pytest.raises(LiveEngineError, match="declined"):
        map_fields(scan(p["form"]).slots, memo, ClaudeEngine(client=fake_client_factory([fake_message({}, "refusal", "cyber")])))
    r = run_pipeline(p["form"], memo, tmp_path / "out", mode="auto", client=fake_client_factory([fake_message({}, "refusal")]))
    assert r.engine["mode"] == "demo" and "declined" in r.engine["fallback_reason"]
    assert r.report.overall_status == "PASS"


# ====================================================================== writer


def test_writer_copies_untouched_entries_as_raw_bytes(demo_runs):
    r = demo_runs("form1")
    orig = read_raw_zip(r.form_path.read_bytes())
    filled = read_raw_zip(r.filled_path.read_bytes())
    assert [e.name for e in orig.entries] == [e.name for e in filled.entries]
    for eo, ef in zip(orig.entries, filled.entries):
        if eo.name == "word/document.xml":
            assert eo.raw != ef.raw
        else:
            assert eo.raw == ef.raw, eo.name  # compressed stream + local header byte-identical
            assert eo.info.date_time == ef.info.date_time and eo.info.CRC == ef.info.CRC
    assert r.write.modified_parts == ["word/document.xml"]


def test_split_run_marker_is_coalesced_into_first_run(demo_runs):
    r = demo_runs("form1")
    zo, zf = zipfile.ZipFile(r.form_path), zipfile.ZipFile(r.filled_path)
    po = etree.fromstring(zo.read("word/document.xml"))
    pf = etree.fromstring(zf.read("word/document.xml"))
    runs_o = [x for x in po.iter(f"{{{W}}}r")]
    runs_f = [x for x in pf.iter(f"{{{W}}}r")]
    assert len(runs_o) == len(runs_f)  # no run added or removed
    i = next(k for k, x in enumerate(runs_o) if "".join(t.text or "" for t in x.iter(f"{{{W}}}t")) == "{{研究")
    texts = ["".join(t.text or "" for t in x.iter(f"{{{W}}}t")) for x in runs_f[i : i + 3]]
    assert texts == ["架空 太郎", "", ""]
    dump = lambda el: etree.tostring(el) if el is not None else b""
    for a, b in zip(runs_o[i : i + 3], runs_f[i : i + 3]):
        assert dump(a.find(f"{{{W}}}rPr")) == dump(b.find(f"{{{W}}}rPr"))
        assert a.attrib == b.attrib and a.get(f"{{{W}}}rsidR")  # distinct rsids preserved


def test_xlsx_writer_changes_only_text_inside_the_sheet(demo_runs):
    r = demo_runs("form3")
    wo, wf = openpyxl.load_workbook(r.form_path), openpyxl.load_workbook(r.filled_path)
    so, sf = wo.active, wf.active
    assert sf["F8"].value == "要是正" and sf["D18"].value == "令和8年12月8日（火）"
    assert [str(x) for x in so.merged_cells.ranges] == [str(x) for x in sf.merged_cells.ranges]
    assert so["D13"].value == sf["D13"].value and so["H13"].number_format == sf["H13"].number_format
    assert str(sf.data_validations.dataValidation[0].sqref) == "F8:F12"


def test_empty_cell_designated_by_coordinates_gets_one_run(demo_runs):
    r = demo_runs("form2")
    d = docx.Document(r.filled_path)
    assert d.tables[1].cell(8, 1).text == "総務部 架空 次郎（jiro@example.jp）"
    assert any(p.kind in ("insert_run", "expand_empty") and p.slot_id == "担当者連絡先" for p in r.write.patches)


# ==================================================================== verifier


def test_verifier_passes_all_demo_outputs(demo_runs):
    for demo in ("form1", "form2", "form3"):
        rep = demo_runs(demo).report
        assert rep.failed_count == 0, [(c.check_id, c.details) for c in rep.checks if not c.passed]
        assert rep.zip_integrity_passed and rep.xml_schema_wellformed and rep.non_target_xml_unchanged
        assert rep.style_and_borders_unchanged and rep.ungrounded_claims_count == 0
        assert rep.visual_diff_summary["identical_outside_slots"]


def _failing(rep):
    return {c.check_id for c in rep.checks if not c.passed}


def test_verifier_catches_altered_boilerplate(demo_runs, tmp_path):
    r = demo_runs("form1")
    bad = tmp_path / "tampered.docx"
    rewrite_entry(r.filled_path, bad, "word/document.xml", lambda b: b.replace("30日前まで".encode(), "早めに".encode()))
    rep = verify(r.scan, bad, r.mappings, r.memo_text)
    assert rep.overall_status == "FAIL" and "TEXT-01" in _failing(rep)


def test_verifier_catches_removed_cell_border(demo_runs, tmp_path):
    r = demo_runs("form1")
    bad = tmp_path / "no_border.docx"
    rewrite_entry(r.filled_path, bad, "word/document.xml",
                  lambda b: re.sub(rb"<w:tcBorders>.*?</w:tcBorders>", b"", b, count=1, flags=re.S))
    rep = verify(r.scan, bad, r.mappings, r.memo_text)
    assert {"LAYOUT-02", "XML-03"} <= _failing(rep) and rep.overall_status == "FAIL"


def test_verifier_catches_unmerged_cells_and_style_edits(demo_runs, tmp_path):
    r = demo_runs("form1")
    bad = tmp_path / "unmerged.docx"
    rewrite_entry(r.filled_path, bad, "word/document.xml", lambda b: b.replace(b'<w:gridSpan w:val="3"/>', b"", 1))
    assert "LAYOUT-01" in _failing(verify(r.scan, bad, r.mappings, r.memo_text))
    bad2 = tmp_path / "styles.docx"
    rewrite_entry(r.filled_path, bad2, "word/styles.xml", lambda b: b.replace(b'w:val="21"', b'w:val="24"', 1))
    assert {"ZIP-04", "ZIP-05"} <= _failing(verify(r.scan, bad2, r.mappings, r.memo_text))


def test_verifier_catches_wrong_values_and_ungrounded_claims(demo_runs, tmp_path):
    r = demo_runs("form1")
    bad = tmp_path / "wrong_value.docx"
    rewrite_entry(r.filled_path, bad, "word/document.xml", lambda b: b.replace("理工学研究科".encode(), "工学部".encode(), 1))
    assert "TEXT-02" in _failing(verify(r.scan, bad, r.mappings, r.memo_text))
    forged = [m.model_copy(update={"source_spans": [], "source_quote": "所属は工学部"}) if m.slot_id == "所属部局" else m
              for m in r.mappings]
    rep = verify(r.scan, r.filled_path, forged, r.memo_text)
    assert "GRD-01" in _failing(rep) and rep.ungrounded_claims_count == 1


def test_verifier_catches_xlsx_formula_tampering(demo_runs, tmp_path):
    r = demo_runs("form3")
    bad = tmp_path / "formula.xlsx"
    rewrite_entry(r.filled_path, bad, "xl/worksheets/sheet1.xml", lambda b: b.replace(b"COUNTIF", b"COUNTA", 1))
    assert {"XML-03", "LAYOUT-04"} <= _failing(verify(r.scan, bad, r.mappings, r.memo_text))


def test_naive_regeneration_is_flagged(samples_dir):
    p = paths(samples_dir, "form1")
    rep = compare_naive(scan(p["form"]), p["naive"], read_memo(p["memo"]))
    failing = _failing(rep)
    assert {"LAYOUT-01", "LAYOUT-02", "LAYOUT-04", "TEXT-01", "GRD-02", "CON-02"} <= failing
    grd = next(c for c in rep.checks if c.check_id == "GRD-02")
    assert "約50万円" in grd.evidence["invented"]
    p3 = paths(samples_dir, "form3")
    rep3 = compare_naive(scan(p3["form"]), p3["naive"], read_memo(p3["memo"]))
    assert {"LAYOUT-01", "LAYOUT-04"} <= _failing(rep3)


def test_values_can_be_recovered_from_any_filled_copy(demo_runs):
    r = demo_runs("form2")
    values = infer_values_from_filled(r.scan, r.filled_path)
    assert values["補助金申請額"] == "1,000,000円" and values["担当者連絡先"].startswith("総務部")
    assert "法人番号" not in values


# ================================================================= visual diff


def test_previews_render_with_slot_overlays(demo_runs):
    for demo in ("form1", "form2", "form3"):
        r = demo_runs(demo)
        assert {"blank", "filled", "naive"} <= set(r.previews)
        assert r.previews["filled"].startswith("<svg") and 'data-slot-id="' in r.previews["filled"]
        assert r.visual["pages_blank"] == r.visual["pages_filled"] == 1
    assert 'class="fg-pending"' in demo_runs("form2").previews["filled"]
    assert 'class="nv-invent"' in demo_runs("form2").previews["naive"]


def test_layout_drift_is_reported_when_a_value_outgrows_its_cell(demo_runs, tmp_path):
    r = demo_runs("form2")
    long_value = "年間12,400kWhの電力削減" * 20
    out = tmp_path / "long.docx"
    fill_form(r.scan, {**r.write.values, "年間省エネ見込量": long_value}, out)
    blank, _ = layout_docx(r.form_path)
    filled, _ = layout_docx(out)
    slot_boxes = {o.box_id for s in r.scan.slots for o in s.occurrences}
    summary = compare_layouts(blank, filled, slot_boxes)
    assert not summary["identical_outside_slots"] and summary["drift_count"] > 0
    assert summary["max_shift_px"] > 10  # rows below the overgrown cell were pushed down
    same = compare_layouts(blank, layout_docx(r.filled_path)[0], slot_boxes)
    assert same["identical_outside_slots"] and same["max_shift_px"] == 0


# ============================================================ pipeline/CLI/API


def test_cli_all_demos_end_to_end(samples_dir, tmp_path):
    from form_guard.cli import main

    out = tmp_path / "output"
    assert main(["--all-demos", "--mode", "demo", "--out-dir", str(out), "--samples-dir", str(samples_dir)]) == 0
    assert (out / "index.html").exists() and (out / "summary.json").exists()
    for demo in ("form1", "form2", "form3"):
        d = out / demo
        assert (d / "report.html").exists() and (d / "audit_log.json").exists()
        assert (d / "preview_filled.svg").exists() and list(d.glob("filled_*"))
    html = (out / "form1" / "report.html").read_text(encoding="utf-8")
    assert "self-correction" in html and "<svg" in html and "根拠" in html


def test_cli_scan_set_and_verify(samples_dir, tmp_path, capsys):
    from form_guard.cli import main

    p = paths(samples_dir, "form2")
    assert main(["--scan", str(p["form"])]) == 0
    assert "法人番号" in capsys.readouterr().out
    out = tmp_path / "one"
    assert main(["--form", str(p["form"]), "--memo", str(p["memo"]), "--out-dir", str(out), "--mode", "demo",
                 "--set", "法人番号=1234567890123"]) == 0
    filled = next(out.glob("filled_*.docx"))
    assert main(["--verify", str(p["form"]), str(filled), "--memo", str(p["memo"])]) == 0
    assert main(["--verify", str(p["form"]), str(p["naive"])]) == 1


@pytest.fixture
def api(samples_dir, tmp_path):
    from fastapi.testclient import TestClient

    from form_guard.server import create_app

    return TestClient(create_app(run_root=tmp_path / "web_runs", samples_dir=samples_dir))


def test_api_hitl_gate_blocks_download_until_full_human_approval(api):
    run = api.post("/api/runs", json={"demo_id": "form2", "mode": "demo"}).json()
    rid = run["run_id"]
    assert run["report"]["pending_slots"] == ["法人番号"] and run["can_approve"] is False
    assert api.get(f"/api/runs/{rid}/download").status_code == 403
    assert api.post(f"/api/runs/{rid}/approve", json={"reviewer": "x", "checked": []}).status_code == 409
    bad = api.post(f"/api/runs/{rid}/overrides", json={"values": {"法人番号": "12-34"}}).json()
    assert bad["report"]["pending_slots"] == ["法人番号"]
    ok = api.post(f"/api/runs/{rid}/overrides", json={"values": {"法人番号": "1234567890123"}}).json()
    assert ok["report"]["overall_status"] == "PASS" and ok["can_approve"]
    required = [h["item_id"] for h in ok["report"]["hitl_checklist"] if h["required"]]
    partial = api.post(f"/api/runs/{rid}/approve", json={"reviewer": "架空 花子", "checked": required[:-1]})
    assert partial.status_code == 400 and partial.json()["detail"]["missing"] == required[-1:]
    assert api.post(f"/api/runs/{rid}/approve", json={"reviewer": " ", "checked": required}).status_code == 400
    approved = api.post(f"/api/runs/{rid}/approve", json={"reviewer": "架空 花子", "checked": required}).json()
    dl = api.get(f"/api/runs/{rid}/download")
    assert dl.status_code == 200 and hashlib.sha256(dl.content).hexdigest() == approved["filled_sha256"]
    assert dl.headers["x-form-guard-sha256"] == approved["filled_sha256"]
    assert docx.Document(io.BytesIO(dl.content)).tables[0].cell(3, 1).text == "1234567890123"
    assert api.get(f"/api/runs/{rid}/audit").json()["approval"]["reviewer"] == "架空 花子"


def test_api_upload_validation_and_static_ui(api, samples_dir):
    index = api.get("/")
    assert index.status_code == 200 and "Form-Guard Agent" in index.text
    assert "default-src 'self'" in index.headers["content-security-policy"]
    assert api.get("/static/app.js").status_code == 200
    assert api.get("/api/runs/not-a-run").status_code == 404
    assert api.post("/api/runs/upload", files={"file": ("x.docx", b"not a zip")}, data={"memo_text": "m"}).status_code == 400
    assert api.post("/api/runs/upload", files={"file": ("x.pdf", b"%PDF")}, data={"memo_text": "m"}).status_code == 400
    p = paths(samples_dir, "form3")
    up = api.post("/api/runs/upload", files={"file": ("点検 様式.xlsx", p["form"].read_bytes())},
                  data={"memo_text": read_memo(p["memo"]), "mode": "demo"})
    assert up.status_code == 200 and up.json()["report"]["overall_status"] == "PASS"
    assert "naive" not in up.json()["previews"]
    demos = api.get("/api/demos").json()
    assert [d["id"] for d in demos] == ["form1", "form2", "form3", "form4", "form5"] and "memo_text" in demos[0]
    assert [d["label"] for d in demos] == ["デモ1", "デモ2", "デモ3", "デモ4", "デモ5"]
    assert [d["unmarked"] for d in demos] == [False, False, False, True, True]
    assert [d["badge"] for d in demos] == ["", "", "", "空欄自動検出", "空欄自動検出"]


def test_live_engine_through_the_real_anthropic_sdk_with_a_mock_transport(samples_dir):
    """Wire-level check: the real SDK builds the request we expect and our code parses the SSE stream."""
    import json as _json

    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    p = paths(samples_dir, "form1")
    memo = read_memo(p["memo"])
    payload = _json.dumps(_claude_fields({"所属部局": ("理工学研究科", ["・所属部局：理工学研究科"])}), ensure_ascii=False)
    events = [
        ("message_start", {"type": "message_start", "message": {"id": "msg_test", "type": "message", "role": "assistant",
                                                                 "model": "claude-opus-5-5", "content": [], "stop_reason": None,
                                                                 "stop_sequence": None, "usage": {"input_tokens": 900, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": payload}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 321}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    sse = "".join(f"event: {e}\ndata: {_json.dumps(d, ensure_ascii=False)}\n\n" for e, d in events).encode("utf-8")
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["beta"] = request.headers.get("anthropic-beta", "")
        seen["body"] = _json.loads(request.content)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse)

    client = anthropic.Anthropic(**{'api' + '_key': 'mock'}, max_retries=0,
                                 http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    run = map_fields(scan(p["form"]).slots, memo, ClaudeEngine(client=client, model_tier="opus"))
    body = seen["body"]
    assert seen["url"].endswith("/v1/messages?beta=true") or "/v1/messages" in seen["url"]
    assert "server-side-fallback-2026-07-01" in seen["beta"]
    assert body["model"] == "claude-opus-5-5" and body["stream"] is True
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"} and body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "medium" and body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["required"] == ["fields"]
    assert body["cache_control"] == {"type": "ephemeral"} and "tool_choice" not in body and "temperature" not in body
    assert all(m.status == "filled" for m in run.mappings)
    assert run.usage["output_tokens"] == 321 and run.usage["models"] == ["claude-opus-5-5"]


# =============================================================== R1 audit fixes
# One block per audit item (R1-A1 … R1-A19, R1-O1 … R1-O7). Every test below fails on the
# pre-fix code and documents the exact failure mode it guards against.

ROOT = Path(__file__).resolve().parent.parent


def surgical_rewrite(src: Path, dst: Path, name: str, fn) -> None:
    """Change one ZIP entry and copy every other entry as raw bytes (so only that part differs)."""
    raw = read_raw_zip(Path(src).read_bytes())
    data = zipfile.ZipFile(src).read(name)
    Path(dst).write_bytes(rewrite_zip(raw, {name: fn(data)}))


def check(rep, check_id):
    return next(c for c in rep.checks if c.check_id == check_id)


class FixedEngine:
    """Proposes fixed drafts (as a mis-behaving engine might) and never revises."""

    name = "fixed"
    label = "fixed test engine"

    def __init__(self, drafts):
        self.drafts, self.usage = drafts, {}

    def propose(self, slots, memo, form_title=""):
        return {k: Draft(d.slot_id, d.status, d.value, list(d.quotes), d.confidence) for k, d in self.drafts.items()}

    def revise(self, failing, memo):
        return {}


def _text_slot(slot_id="記入欄", max_chars=None, fmt="text", **constraints):
    return FormSlot(slot_id=slot_id, label=slot_id, location_path="test", file_type="docx", current_text="",
                    max_chars=max_chars, required_format=fmt, constraints=constraints)


# ------------------------------------------------------------- #1 R1-A1 grounding


def test_r1_a1_grounding_rejects_wrong_names_ids_options_and_composite_parts(samples_dir):
    sc1, memo1 = scan(paths(samples_dir, "form1")["form"]), read_memo(paths(samples_dir, "form1")["memo"])
    sc3, memo3 = scan(paths(samples_dir, "form3")["form"]), read_memo(paths(samples_dir, "form3")["memo"])
    memo2 = read_memo(paths(samples_dir, "form2")["memo"])
    errors = lambda g: [i.code for i in g.issues if i.severity == "error"]
    name = sc1.slot("研究代表者氏名")
    # (a) quoting 架空 太郎 while filling 架空 花子 is an error, not a warning
    assert "value_unsupported" in errors(ground_value("架空 花子", ["架空 太郎"], memo1, name))
    assert errors(ground_value("架空 太郎", ["架空 太郎（理工学研究科 機械システム専攻 教授）"], memo1, name)) == []
    # IDs: width / dash normalised, but the ID itself must be the one quoted
    rid = sc1.slot("研究課題番号")
    assert errors(ground_value("JR-2026-047", ["ＪＲ－２０２６－０４７"], memo1, rid)) == []
    assert "value_unsupported" in errors(ground_value("JR-2099-999", ["ＪＲ－２０２６－０４７"], memo1, rid))
    digits = _text_slot("法人番号", fmt="digits", digits=13, pattern="^[0-9]{13}$")
    memo_d = "法人番号：１２３４５６７８９０１２３（確認済み）\n"
    assert errors(ground_value("1234567890123", ["１２３４５６７８９０１２３"], memo_d, digits)) == []
    assert "value_unsupported" in errors(ground_value("1234567890124", ["１２３４５６７８９０１２３"], memo_d, digits))
    # (b) choice: the option must be the one the quote affirmatively states
    ac = sc3.slot("空調設備_判定")
    assert "choice_unsupported" in errors(ground_value("良", ["判定は要是正"], memo3, ac))
    assert errors(ground_value("要是正", ["判定は要是正"], memo3, ac)) == []
    opts = ["良", "要注意", "要是正"]
    assert affirmed_options(opts, "良好だが判定は要是正")[0] == {"要是正"}  # 良 inside 良好 is not a judgement
    assert affirmed_options(opts, "要是正ではなく要注意")[0] == {"要注意"}  # negated option is not affirmed
    # (c) 483万6千円 states 4,836,000 only — never 483 or 6 on their own
    assert quantities("税抜で483万6千円") == {4_836_000.0}
    assert "number_unsupported" in errors(ground_value("6円", ["税抜で483万6千円"], memo2))
    assert "number_unsupported" in errors(ground_value("483円", ["税抜で483万6千円"], memo2))
    # (d) the legitimate conversions still pass
    assert errors(ground_value("4,836,000円", ["税抜で483万6千円"], memo2)) == []


def test_r1_a1_verifier_fails_a_value_that_contradicts_its_quote(demo_runs, tmp_path):
    r = demo_runs("form1")
    bad = tmp_path / "name.docx"
    surgical_rewrite(r.filled_path, bad, "word/document.xml", lambda b: b.replace("架空 太郎".encode(), "架空 花子".encode()))
    forged = [m.model_copy(update={"filled_value": "架空 花子"}) if m.slot_id == "研究代表者氏名" else m for m in r.mappings]
    rep = verify(r.scan, bad, forged, r.memo_text)
    assert check(rep, "TEXT-02").passed  # the file matches the forged mapping …
    assert not check(rep, "GRD-01").passed and rep.overall_status == "FAIL"  # … but the quote says 架空 太郎
    assert "研究代表者氏名" in check(rep, "GRD-01").evidence["ungrounded"]

    r3 = demo_runs("form3")
    bad3 = tmp_path / "choice.xlsx"
    surgical_rewrite(r3.filled_path, bad3, "xl/worksheets/sheet1.xml", lambda b: b.replace("要是正".encode(), "良".encode(), 1))
    assert openpyxl.load_workbook(bad3).active["F8"].value == "良"
    forged3 = [m.model_copy(update={"filled_value": "良"}) if m.slot_id == "空調設備_判定" else m for m in r3.mappings]
    rep3 = verify(r3.scan, bad3, forged3, r3.memo_text)
    assert check(rep3, "TEXT-02").passed and check(rep3, "CON-03").passed  # 良 is an allowed option …
    assert not check(rep3, "GRD-01").passed and rep3.overall_status == "FAIL"  # … but the quote says 要是正

    r2 = demo_runs("form2")
    forged2 = [m.model_copy(update={"source_spans": [], "source_quote": "税抜で483万6千円"}) if m.slot_id == "導入予定金額_税抜" else m
               for m in r2.mappings]
    forged2 = [m.model_copy(update={"filled_value": "6円"}) if m.slot_id == "導入予定金額_税抜" else m for m in forged2]
    assert "導入予定金額_税抜" in check(verify(r2.scan, r2.filled_path, forged2, r2.memo_text), "GRD-02").evidence["unsupported"]


def test_r1_a1_mapper_never_writes_values_that_fail_grounding(samples_dir):
    p = paths(samples_dir, "form1")
    memo = read_memo(p["memo"])
    slots = [s for s in scan(p["form"]).slots if s.slot_id in ("研究代表者氏名", "研究課題番号")]
    engine = FixedEngine({
        "研究代表者氏名": Draft("研究代表者氏名", value="架空 花子", quotes=["架空 太郎"], confidence=0.9),
        "研究課題番号": Draft("研究課題番号", value="JR-2099-999", quotes=["ＪＲ－２０２６－０４７"], confidence=0.9),
    })
    by = {m.slot_id: m for m in map_fields(slots, memo, engine).mappings}
    for sid, proposed in (("研究代表者氏名", "架空 花子"), ("研究課題番号", "JR-2099-999")):
        m = by[sid]
        assert m.status == "needs_human_input" and m.filled_value == "" and not m.is_written
        assert m.suggested_value == proposed and m.errors
    p3 = paths(samples_dir, "form3")
    slot = scan(p3["form"]).slot("空調設備_判定")
    m = map_fields([slot], read_memo(p3["memo"]), FixedEngine({
        "空調設備_判定": Draft("空調設備_判定", value="良", quotes=["判定は要是正"], confidence=0.9)})).mappings[0]
    assert m.status == "needs_human_input" and m.suggested_value == "良" and m.filled_value == ""


# ----------------------------------------------- #2 / #23 R1-A2, R1-O4 condensation


def test_r1_a2_condensation_never_silently_drops_a_condition():
    text = "この設備（安全性が第三者により確認された場合に限り）を使用する。"
    c = condense_extractive(text, 15)
    assert c is not None and c.text == "この設備を使用する。" and c.unsafe
    assert c.removed == ["（安全性が第三者により確認された場合に限り）"]
    # supplementary / status remarks go first; interim measures are kept
    c2 = condense_extractive("点検は完了した。なお、給湯室の換気扇は清掃のみで異常なし。交換完了までは運転時間を短縮する。", 30)
    assert c2.text == "点検は完了した。交換完了までは運転時間を短縮する。" and not c2.unsafe
    assert c2.removed == ["なお、給湯室の換気扇は清掃のみで異常なし。"]

    slot = _text_slot("使用条件", max_chars=15)
    m = map_fields([slot], text, FixedEngine({"使用条件": Draft("使用条件", value=text, quotes=[text], confidence=0.9)})).mappings[0]
    assert m.status == "needs_human_input" and m.filled_value == "" and not m.is_written  # NOT auto-filled
    assert m.suggested_value == "この設備を使用する。" and "unsafe_condensation" in m.normalizations
    assert any("場合に限り" in e for e in m.errors)
    assert "condensed — removed: （安全性が第三者により確認された場合に限り）" in m.warnings
    assert m.condensed_removed == ["（安全性が第三者により確認された場合に限り）"]


def test_r1_a2_demo3_keeps_the_interim_measure_and_hitl_shows_full_removed_text(demo_runs):
    r3 = demo_runs("form3")
    m = {x.slot_id: x for x in r3.mappings}["指摘事項および是正措置"]
    assert m.status == "filled" and len(m.filled_value) <= 120 and "condensed" in m.normalizations
    assert "交換完了までは同機の運転時間を短縮し、隣接区画の空調で補う。" in m.filled_value
    removed_sentence = "給湯室の排水不良については、清掃業者に高圧洗浄を依頼済み。"
    assert removed_sentence in m.condensed_removed
    assert any(w.startswith("condensed — removed: ") and removed_sentence in w for w in m.warnings)
    item = next(h for h in r3.report.hitl_checklist if h.slot_id == "指摘事項および是正措置")
    assert "字数制限のため削除した文：" in item.prompt and removed_sentence in item.prompt
    assert "フィルターの目詰まりも著しく、冷房効率の低下が見られる。" in item.prompt
    assert "必要な条件や措置が欠けていないか確認 / Condensed for char limit; confirm no essential condition was lost" in item.prompt
    # Demo 1: the long aside is disclosed in full (no 24-character truncation anywhere)
    m1 = {x.slot_id: x for x in demo_runs("form1").mappings}["変更理由および予算影響"]
    aside = "（直接経費総額に占める割合は約6％で、20％未満のため経費変更承認申請は不要）"
    assert aside in m1.condensed_removed and any(aside in w for w in m1.warnings)
    assert aside in m1.attempts[-1].action and "…»" not in m1.attempts[-1].action
    item1 = next(h for h in demo_runs("form1").report.hitl_checklist if h.slot_id == "変更理由および予算影響")
    assert aside in item1.prompt


def test_r1_a2_hitl_explains_a_suggestion_that_was_held_back(samples_dir):
    text = "この設備（安全性が第三者により確認された場合に限り）を使用する。"
    slot = _text_slot("使用条件", max_chars=15)
    m = map_fields([slot], text, FixedEngine({"使用条件": Draft("使用条件", value=text, quotes=[text], confidence=0.9)})).mappings[0]
    from form_guard.models import VerificationReport

    fake_scan = type("S", (), {"slots": [slot]})()
    items = build_hitl_checklist(fake_scan, [m], VerificationReport(file_type="docx", zip_integrity_passed=True,
                                 xml_schema_wellformed=True, non_target_xml_unchanged=True, style_and_borders_unchanged=True,
                                 all_slots_filled=False, ungrounded_claims_count=0))
    pending = items[0]
    assert pending.kind == "pending_input" and pending.value == "この設備を使用する。"
    assert "unsafe_condensation" in pending.flags and "（安全性が第三者により確認された場合に限り）" in pending.prompt


# --------------------------------------------- #3 R1-A3 xml:space / runs outside slots


def test_r1_a3_xml_space_change_on_a_non_slot_node_fails_xml03(demo_runs, tmp_path):
    r = demo_runs("form1")
    xml = zipfile.ZipFile(r.filled_path).read("word/document.xml")
    target = 'xml:space="preserve">　下記のとおり'.encode()
    assert xml.count(target) == 1  # a boilerplate paragraph, not a slot
    bad = tmp_path / "space.docx"
    surgical_rewrite(r.filled_path, bad, "word/document.xml",
                     lambda b: b.replace(target, 'xml:space="default">　下記のとおり'.encode()))
    rep = verify(r.scan, bad, r.mappings, r.memo_text)
    assert _failing(rep) == {"XML-03"} and rep.overall_status == "FAIL"
    assert check(rep, "ZIP-04").passed and check(rep, "TEXT-01").passed  # text and other parts untouched


def _bold_run_docx(path: Path) -> None:
    d = docx.Document()
    p = d.add_paragraph()
    r = p.add_run("注")
    r.bold = True
    p.add_run("意事項：架空の様式です")
    d.add_paragraph("{{A}}")
    d.save(path)


def test_r1_a3_moving_a_character_between_runs_fails_text01(tmp_path):
    src, out = tmp_path / "runs.docx", tmp_path / "runs_filled.docx"
    _bold_run_docx(src)
    sc = scan(src)
    fill_form(sc, {"A": "記入値"}, out)
    maps = [human_mapping(sc.slot("A"), "記入値")]
    assert not _failing(verify(sc, out, maps, ""))  # the untouched fill passes
    bad = tmp_path / "moved.docx"
    surgical_rewrite(out, bad, "word/document.xml", lambda b: b.replace("<w:t>注</w:t>".encode(), "<w:t>注意</w:t>".encode())
                     .replace("<w:t>意事項：".encode(), "<w:t>事項：".encode()))
    assert docx.Document(bad).paragraphs[0].text == "注意事項：架空の様式です"  # same paragraph text …
    rep = verify(sc, bad, maps, "")
    assert {"TEXT-01", "XML-03"} <= _failing(rep) and rep.overall_status == "FAIL"  # … but the bold run grew
    assert "moved between runs" in check(rep, "TEXT-01").details


def test_r1_a3_split_markers_and_preserve_on_slot_nodes_still_pass(demo_runs, tmp_path):
    rep = demo_runs("form1").report  # form1 has markers split over three runs ({{研究|代表者|氏名}})
    assert check(rep, "XML-03").passed and check(rep, "TEXT-01").passed and check(rep, "TEXT-02").passed
    src, out = tmp_path / "pres.docx", tmp_path / "pres_filled.docx"
    _bold_run_docx(src)
    sc = scan(src)
    w = fill_form(sc, {"A": "記入  値"}, out)  # a double space needs xml:space="preserve"
    assert any(p.kind == "attr" for p in w.patches)  # … which is added to the slot node only
    assert not _failing(verify(sc, out, [human_mapping(sc.slot("A"), "記入  値")], ""))


# ------------------------------------------------------------ #4 R1-A4 approval binding


def test_r1_a4_approval_is_bound_to_the_verified_bytes_and_revision(api):
    run = api.post("/api/runs", json={"demo_id": "form1", "mode": "demo"}).json()
    rid = run["run_id"]
    assert run["can_approve"] and run["revision"] == 0
    st = api.app.state.store.runs[rid]
    path = st.result.filled_path
    good = path.read_bytes()
    assert run["filled"]["sha256"] == hashlib.sha256(good).hexdigest() == st.verified_sha256 and st.verified_bytes == good
    required = [h["item_id"] for h in run["report"]["hitl_checklist"] if h["required"]]
    body = {"reviewer": "架空 花子", "checked": required}
    url = f"/api/runs/{rid}/approve"
    # (b) a stale view (other hash / other revision) is refused
    assert api.post(url, json={**body, "filled_sha256": "0" * 64}).status_code == 409
    assert api.post(url, json={**body, "revision": 7}).status_code == 409
    # (a) the file was changed on disk after verification, before approval
    path.write_bytes(good + b"tampered")
    assert api.post(url, json={**body, "filled_sha256": run["filled"]["sha256"], "revision": 0}).status_code == 409
    path.write_bytes(good)
    ok = api.post(url, json={**body, "filled_sha256": run["filled"]["sha256"], "revision": 0})
    assert ok.status_code == 200 and ok.json()["filled_sha256"] == st.verified_sha256 and ok.json()["revision"] == 0
    # (c) the file was changed after approval: download refuses
    path.write_bytes(good[:-8] + b"XXXXXXXX")
    assert api.get(f"/api/runs/{rid}/download").status_code == 409
    path.write_bytes(good)
    dl = api.get(f"/api/runs/{rid}/download")
    assert dl.status_code == 200 and dl.content == good  # the exact verified bytes, served from memory
    # an edit creates a new revision and clears the approval; the old view can no longer approve
    edited = api.post(f"/api/runs/{rid}/overrides", json={"values": {"所属部局": "理工学研究科"}}).json()
    assert edited["revision"] == 1 and edited["approval"] is None
    assert api.post(url, json={**body, "revision": 0}).status_code == 409


# ------------------------------------------------------------ #5 R1-A5 --verify


def test_r1_a5_cli_verify_grounds_values_against_the_memo(demo_runs, samples_dir, tmp_path, capsys):
    from form_guard.cli import main

    r = demo_runs("form1")
    p = paths(samples_dir, "form1")
    tampered = tmp_path / "tampered.docx"
    surgical_rewrite(r.filled_path, tampered, "word/document.xml", lambda b: b.replace(b"JR-2026-047", b"JR-2099-999"))
    assert main(["--verify", str(p["form"]), str(tampered), "--memo", str(p["memo"])]) == 1
    out = capsys.readouterr().out
    assert re.search(r"✗ GRD-01 ", out) and re.search(r"✗ GRD-02 ", out) and "JR-2099-999" in out
    assert "attested by reviewer" not in out
    clean = tmp_path / "clean" / r.filled_path.name
    clean.parent.mkdir()
    shutil.copy(r.filled_path, clean)
    assert main(["--verify", str(p["form"]), str(clean), "--memo", str(p["memo"])]) == 0
    assert re.search(r"✓ GRD-01 .*8 values grounded", capsys.readouterr().out)
    # no memo and no audit log: structure only, and the report says so
    assert main(["--verify", str(p["form"]), str(clean)]) == 0
    out = capsys.readouterr().out
    assert "attested by reviewer" not in out
    assert "structural verification only (no memo provided — grounding not verified)" in out
    assert "grounding NOT verified" in out
    # the audit log written next to the filled file is used when its SHA-256 matches
    assert main(["--verify", str(p["form"]), str(r.filled_path)]) == 0
    out = capsys.readouterr().out
    assert "audit_log.json (SHA-256 matches the filled file)" in out and "8 values grounded" in out


# ---------------------------------------------------------------- #6 R1-A6 SVG


def test_r1_a6_svg_previews_never_carry_attribute_injection(tmp_path):
    src = tmp_path / "colors.docx"
    d = docx.Document()
    d.add_paragraph().add_run("色付きの文字（架空）")
    t = d.add_table(rows=1, cols=1)
    t.cell(0, 0).text = "{{A}}"
    d.save(src)
    W_ = f"{{{W}}}"
    root = etree.fromstring(zipfile.ZipFile(src).read("word/document.xml"))
    rpr = etree.SubElement(next(root.iter(f"{W_}r")), f"{W_}rPr")
    next(root.iter(f"{W_}r")).insert(0, rpr)
    etree.SubElement(rpr, f"{W_}color").set(f"{W_}val", '000000" data-audit-probe="1')
    tcpr = next(root.iter(f"{W_}tc")).find(f"{W_}tcPr")
    etree.SubElement(tcpr, f"{W_}shd").set(f"{W_}fill", "<script>alert(1)</script>")
    borders = etree.SubElement(tcpr, f"{W_}tcBorders")
    top = etree.SubElement(borders, f"{W_}top")
    top.set(f"{W_}val", "single")
    top.set(f"{W_}color", '"/><script>alert(2)</script><x a="')
    bad = tmp_path / "colors_bad.docx"
    surgical_rewrite(src, bad, "word/document.xml", lambda _b: etree.tostring(root, xml_declaration=True, encoding="UTF-8"))
    svg = svg_document(layout_docx(bad)[0], [], "t", "banner", "ok")
    assert "data-audit-probe" not in svg and "<script" not in svg and "alert(" not in svg
    parsed = etree.fromstring(svg.encode("utf-8"))
    assert not [el for el in parsed.iter() if "data-audit-probe" in el.attrib or etree.QName(el).localname == "script"]
    fills = {el.get("fill") for el in parsed.iter() if el.get("fill")} | {el.get("stroke") for el in parsed.iter() if el.get("stroke")}
    assert all(re.fullmatch(r"#[0-9A-Fa-f]{3,6}|none|#fff|url\(#fgshadow\)", f) or f.startswith("rgba") for f in fills if f)
    assert _safe_color("ff00ff") == "#FF00FF" and _safe_color("#abc") == "#AABBCC" and _safe_color("FF102030") == "#102030"
    assert _safe_color('000000" onload="x', None) is None and _safe_color("<script>", "#000000") == "#000000"


# ------------------------------------------------------- #7 R1-A7 Excel overflow


def test_r1_a7_excel_cell_overflow_fails_vis01(demo_runs, samples_dir, tmp_path):
    vis = demo_runs("form3").visual
    assert vis["drift_count"] == 0 and vis["overflow_count"] == 0 and vis["identical_outside_slots"]
    p = paths(samples_dir, "form3")
    r = run_pipeline(p["form"], read_memo(p["memo"]), tmp_path / "overflow", mode="demo",
                     overrides={"点検対象施設": "架空" * 240})
    assert len({m.slot_id: m for m in r.mappings}["点検対象施設"].filled_value) == 480
    assert r.visual["overflow_count"] >= 1 and not r.visual["identical_outside_slots"]
    vis01 = check(r.report, "VIS-01")
    assert not vis01.passed and "overflow" in vis01.details and r.report.overall_status == "FAIL"
    assert any(d["box"] == "s0!D4" and "cell text overflow" in d["issue"] for d in vis01.evidence["drift"])


# ---------------------------------------------------------- #8 R1-A8 multi-sheet


def test_r1_a8_every_worksheet_is_rendered_highlighted_and_checked(tmp_path):
    src = tmp_path / "two_sheets.xlsx"
    wb = openpyxl.Workbook()
    wb.active.title = "表紙"
    wb.active["A1"] = "架空の申請書（表紙）"
    ws2 = wb.create_sheet("明細")
    ws2["A2"] = "項目"
    ws2["B2"] = "{{項目}}"
    ws2.column_dimensions["B"].width = 24
    wb.save(src)
    sc = scan(src)
    assert sc.slot("項目").occurrences[0].box_id == "s1!B2"
    lay = layout_xlsx(src)
    assert len(lay.pages) == 2 and lay.boxes["s1!B2"].page == 1 and lay.boxes["s0!A1"].page == 0
    r = run_pipeline(src, "項目：テスト値\n", tmp_path / "ms", mode="demo")
    assert {m.slot_id: m for m in r.mappings}["項目"].filled_value == "テスト値"
    filled_svg = r.previews["filled"]
    assert filled_svg.count('<g transform="translate(8,') == 2 and 'data-slot-id="項目"' in filled_svg
    assert r.visual["pages_filled"] == 2 and r.report.overall_status == "PASS"
    long = run_pipeline(src, "項目：テスト値\n", tmp_path / "ms_long", mode="demo", overrides={"項目": "長い記入値" * 60})
    assert any(d["box"] == "s1!B2" for d in check(long.report, "VIS-01").evidence["drift"])


# ------------------------------------------- #9 / #10 / #18 R1-A9, R1-A10, R1-O2 dates


def test_r1_a9_validate_value_rejects_impossible_dates_eras_periods_and_weekdays(samples_dir):
    sc = scan(paths(samples_dir, "form2")["form"])
    day, period = sc.slot("申請日"), sc.slot("事業実施期間")
    western = _text_slot("日付", fmt="date")
    errs = lambda slot, v: [i.code for i in validate_value(slot, v) if i.severity == "error"]
    for bad in ("2026年2月29日", "2026年2月30日", "令和元年1月1日", "平成31年5月1日", "令和0年5月1日", "令和8年2月29日",
                "令和8年11月2日（火）", "昭和64年1月8日"):
        assert errs(day, bad), bad
    assert errs(day, "令和元年1月1日") == ["date_invalid"] and errs(day, "令和8年11月2日（火）") == ["weekday_mismatch"]
    assert errs(day, "2026年2月30日") == ["date_invalid", "format"]  # says why first, not just "not 和暦"
    for good in ("令和8年11月2日（月）", "令和8年11月2日", "令和元年5月1日（水）", "平成31年4月30日（火）", "昭和64年1月7日"):
        assert errs(day, good) == [], good
    assert errs(period, "令和9年1月1日（金）～令和8年12月1日（火）") == ["period_order"]
    assert errs(period, "令和8年12月1日（火）～令和9年2月26日（金）") == []
    assert errs(period, "令和8年12月1日（水）～令和9年2月26日（金）") == ["weekday_mismatch"]
    for bad in ("2026年2月29日", "2026年2月30日", "2026/02/29", "2026年11月2日" + "（火）", "11月2日"):
        assert errs(western, bad), bad
    for good in ("2026年11月2日（月）", "2028年2月29日", "2026/11/02", "2026年12月1日（火）～2027年2月26日（金）"):
        assert errs(western, good) == [], good
    assert parse_wareki_exact("令和元年1月1日") is None and parse_wareki_exact("令和8年11月2日（月）") == date(2026, 11, 2)
    assert check_wareki("平成31年5月1日")[1][0] == "date_invalid"
    with pytest.raises(ValueError):
        era_to_year("令和", "0")
    with pytest.raises(ValueError):
        era_to_year("平成", "32")
    assert len(find_invalid_dates("2026年2月30日と2026/02/29と2月30日")) == 3
    assert to_wareki(date(2026, 11, 2)) == "令和8年11月2日（月）"


def test_r1_a10_weekday_or_calendar_error_in_the_memo_is_never_auto_filled(samples_dir):
    slots = [s for s in scan(paths(samples_dir, "form1")["form"]).slots if s.slot_id == "提出日"]
    m = map_fields(slots, "提出日：2026年11月2日" + "（火）に提出\n", DemoEngine()).mappings[0]
    assert m.status == "needs_human_input" and m.filled_value == "" and m.suggested_value == "令和8年11月2日（火）"
    assert any("曜日" in e or "weekday" in e for e in m.errors)
    m2 = map_fields(slots, "提出日：2026年2月30日に提出\n", DemoEngine()).mappings[0]
    assert m2.status == "needs_human_input" and any("存在しない" in e or "not a real" in e for e in m2.errors)
    ok = map_fields(slots, "提出日：2026年11月2日（月）に提出\n", DemoEngine()).mappings[0]
    assert ok.status == "filled" and ok.filled_value == "令和8年11月2日（月）" and "weekday_ok" in ok.normalizations


def test_r1_o2_memos_and_readme_write_dates_with_the_weekday(samples_dir):
    memos = {d["id"]: read_memo(paths(samples_dir, d["id"])["memo"]) for d in DEMOS}
    assert "2026年12月1日（火）から2027年2月26日（金）まで" in memos["form2"]
    assert "11月6日（金）までに" in memos["form3"] and "11月20日（金）までに" in memos["form3"]
    for memo in memos.values():
        assert not find_invalid_dates(memo)
        for d in find_dates(memo):
            assert d.weekday, memo[d.start : d.end]  # every date in the memos carries （曜）
            assert "月火水木金土日"[date(d.year or 2026, d.month, d.day).weekday()] == d.weekday
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "(development started Monday, 2026-11-02 / 2026-11-02（月）)" in readme.splitlines()[5]
    assert "2026-11-02)" not in readme.replace("2026-11-02（月）)", "")


def test_r1_a11_demo1_is_submitted_at_least_30_days_before_the_change(samples_dir):
    memo = read_memo(paths(samples_dir, "form1")["memo"])
    assert "2026年12月15日（火）" in memo and "2026年12月1日" not in memo
    line = memo.split("\n")[21]
    assert "変更予定日（2026年12月15日（火））の30日前までに提出する" in line
    submitted = next(d for d in find_dates(memo) if (d.month, d.day) == (11, 2) and d.year).as_date
    change = next(d for d in find_dates(memo) if (d.month, d.day) == (12, 15)).as_date
    assert submitted == date(2026, 11, 2) and change - submitted >= timedelta(days=30)


# ---------------------------------------------------- #11 R1-A12 xml:space duplicates


def test_r1_a12_preserve_never_duplicates_an_existing_xml_space(tmp_path):
    assert add_preserve_attr(b'<w:t xml:space="default">') == b'<w:t xml:space="preserve">'
    assert add_preserve_attr(b"<w:t xml:space='default'>") == b'<w:t xml:space="preserve">'
    assert add_preserve_attr(b"<w:t>") == b'<w:t xml:space="preserve">' and add_preserve_attr(b"<w:t/>") == b'<w:t xml:space="preserve"/>'
    src, mid, out = tmp_path / "sp.docx", tmp_path / "sp2.docx", tmp_path / "sp_filled.docx"
    d = docx.Document()
    for k in "ABCD":
        d.add_paragraph("{{" + k + "}}")
    d.save(src)
    surgical_rewrite(src, mid, "word/document.xml", lambda b: b
                     .replace(b"<w:t>{{B}}</w:t>", b'<w:t xml:space="default">{{B}}</w:t>')
                     .replace(b"<w:t>{{C}}</w:t>", b"<w:t xml:space='default'>{{C}}</w:t>")
                     .replace(b"<w:t>{{D}}</w:t>", b'<w:t xml:space="preserve">{{D}}</w:t>'))
    sc = scan(mid)
    values = {"A": "A  B", "B": "B  C", "C": "C  D", "D": "D  E"}  # every value needs xml:space="preserve"
    fill_form(sc, values, out)
    xml = zipfile.ZipFile(out).read("word/document.xml")
    root = etree.fromstring(xml)  # well-formed: no "Attribute xml:space redefined"
    texts = [t.text for t in root.iter(f"{{{W}}}t")]
    assert texts == ["A  B", "B  C", "C  D", "D  E"]
    for tag in re.findall(rb"<w:t\b[^>]*>", xml):
        assert tag.count(b"xml:space") == 1 and b'xml:space="preserve"' in tag
    assert not _failing(verify(sc, out, [human_mapping(sc.slot(k), v) for k, v in values.items()], ""))


# ------------------------------------------------------ #12 R1-A13 huge worksheets


def test_r1_a13_huge_cell_coordinates_and_merges_are_refused_quickly(tmp_path):
    far = tmp_path / "far.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = "{{項目}}"
    wb.active["XFD1048576"] = "x"
    wb.save(far)
    t = time.perf_counter()
    with pytest.raises(UnsupportedXMLError, match="safe form limits"):
        scan(far)
    assert time.perf_counter() - t < 0.5
    t = time.perf_counter()
    with pytest.raises(UnsupportedXMLError, match="safe form limits"):
        layout_xlsx(far)
    assert time.perf_counter() - t < 0.5
    small, merged = tmp_path / "small.xlsx", tmp_path / "merged.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = "{{項目}}"
    wb.save(small)
    surgical_rewrite(small, merged, "xl/worksheets/sheet1.xml", lambda b: b.replace(
        b"</sheetData>", b'</sheetData><mergeCells count="1"><mergeCell ref="B1:B1048576"/></mergeCells>', 1))
    t = time.perf_counter()
    with pytest.raises(UnsupportedXMLError, match="merged range"):
        scan(merged)
    assert time.perf_counter() - t < 0.5


def test_r1_a13_full_column_validation_is_not_expanded(tmp_path):
    from openpyxl.worksheet.datavalidation import DataValidation

    src = tmp_path / "column_rule.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["C3"], ws["D3"] = "判定", "{{判定}}"
    dv = DataValidation(type="list", formula1='"良,要注意,要是正"', allow_blank=True)
    ws.add_data_validation(dv)
    dv.add("D1:D1048576")
    wb.save(src)
    t = time.perf_counter()
    sc = scan(src)
    assert time.perf_counter() - t < 0.5
    assert sc.slot("判定").required_format == "choice" and sc.slot("判定").constraints["allowed_values"] == ["良", "要注意", "要是正"]
    t = time.perf_counter()
    lay = layout_xlsx(src)
    assert time.perf_counter() - t < 0.5 and "s0!D3" in lay.boxes


# ------------------------------------------------- #13 R1-A14 override length


def test_r1_a14_overrides_are_validated_not_truncated(api):
    run = api.post("/api/runs", json={"demo_id": "form2", "mode": "demo"}).json()
    rid = run["run_id"]
    st = api.app.state.store.runs[rid]
    ok = api.post(f"/api/runs/{rid}/overrides", json={"values": {"所在地": "架" * 1000}})
    assert ok.status_code == 200 and ok.json()["revision"] == 1
    assert {m["slot_id"]: m for m in ok.json()["mappings"]}["所在地"]["filled_value"] == "架" * 1000  # not cut
    before_result, before = st.result, (dict(st.overrides), st.approval, st.revision, st.verified_sha256)
    too_long = api.post(f"/api/runs/{rid}/overrides", json={"values": {"所在地": "架" * 1001}})
    assert too_long.status_code == 422 and "exceeds maximum length (1001 > 1000 chars)" in too_long.json()["detail"]
    over_slot = api.post(f"/api/runs/{rid}/overrides", json={"values": {"導入設備名称": "高" * 41}})
    assert over_slot.status_code == 422 and "導入設備名称 exceeds maximum length (41 > 40 chars)" == over_slot.json()["detail"]
    assert st.result is before_result and (st.overrides, st.approval, st.revision, st.verified_sha256) == before


# ---------------------------------------------------- #14 R1-A15 report footer


def test_r1_a15_report_only_calls_unmodified_builtin_demos_fictional(demo_runs, api, samples_dir, tmp_path):
    builtin = (demo_runs("form1").out_dir / "report.html").read_text(encoding="utf-8")
    assert demo_runs("form1").builtin_demo and "100% fictional" in builtin
    assert "このレポートは入力された様式・メモに基づきます" in builtin
    p = paths(samples_dir, "form3")
    up = api.post("/api/runs/upload", files={"file": ("my_form.xlsx", p["form"].read_bytes())},
                  data={"memo_text": read_memo(p["memo"]), "mode": "demo"}).json()
    custom = api.get(f"/api/runs/{up['run_id']}/report").text
    assert "100% fictional" not in custom and "generated from the supplied form and memo" in custom
    p1 = paths(samples_dir, "form1")
    edited = run_pipeline(p1["form"], read_memo(p1["memo"]) + "追記：担当者の私的なメモ\n", tmp_path / "edited",
                          mode="demo", demo_id="form1")
    assert not edited.builtin_demo and "100% fictional" not in (edited.out_dir / "report.html").read_text(encoding="utf-8")
    run = api.post("/api/runs", json={"demo_id": "form2", "mode": "demo"}).json()
    api.post(f"/api/runs/{run['run_id']}/overrides", json={"values": {"法人番号": "1234567890123"}})
    assert "100% fictional" not in api.get(f"/api/runs/{run['run_id']}/report").text


# ------------------------------------------------ #15 R1-A16 byte-identical tile


def test_r1_a16_report_tile_counts_only_byte_identical_parts(demo_runs, tmp_path):
    r = demo_runs("form1")
    bad = tmp_path / "core.docx"
    surgical_rewrite(r.filled_path, bad, "docProps/core.xml",
                     lambda b: b.replace(b"Form-Guard Agent sample generator", b"someone else", 1))
    rep = verify(r.scan, bad, r.mappings, r.memo_text)
    n = sum(1 for e in rep.entry_hashes if e.role == "untouched")
    d = r.to_dict()
    d["report"] = rep.model_dump()
    html = build_report_html(d)
    assert n == 17 and "<b>16/17 (changed: 1)</b><span>ZIP entries byte-identical" in html
    assert f"<b>{n}</b><span>ZIP entries byte-identical" in build_report_html(r.to_dict())


# ------------------------------------------------------- #16 R1-A17 zero slots


def test_r1_a17_a_form_without_slots_never_passes(tmp_path):
    src = tmp_path / "no_slots.docx"
    d = docx.Document()
    d.add_paragraph("この様式には記入欄がありません（架空）")
    d.save(src)
    r = run_pipeline(src, "メモ：特になし\n", tmp_path / "none", mode="demo")
    assert r.report.overall_status == "FAIL" and "TEXT-02" in _failing(r.report)
    assert "0 fillable slots found" in check(r.report, "TEXT-02").details


# ------------------------------------------------- #17 R1-A18 edit any field


def test_r1_a18_reviewer_can_correct_an_ai_filled_field(api):
    run = api.post("/api/runs", json={"demo_id": "form1", "mode": "demo"}).json()
    rid = run["run_id"]
    assert {m["slot_id"]: m for m in run["mappings"]}["所属部局"]["status"] == "filled"
    required = [h["item_id"] for h in run["report"]["hitl_checklist"] if h["required"]]
    assert api.post(f"/api/runs/{rid}/approve", json={"reviewer": "架空 花子", "checked": required}).status_code == 200
    edited = api.post(f"/api/runs/{rid}/overrides", json={"values": {"所属部局": "理工学研究科 機械システム専攻"}}).json()
    m = {x["slot_id"]: x for x in edited["mappings"]}["所属部局"]
    assert m["status"] == "human_entered" and m["filled_value"] == "理工学研究科 機械システム専攻"
    assert edited["approval"] is None and api.get(f"/api/runs/{rid}/download").status_code == 403
    js = (ROOT / "form_guard" / "static" / "app.js").read_text(encoding="utf-8")
    detail = js[js.index("function renderDetail") : js.index("function editControl")]
    assert "editControl(s, m)" in detail and "needs_human_input" not in detail.split("editControl(s, m)")[1][:40]
    assert "Save edit / 値を修正" in js and "editControl(slot, m)" in js  # field detail + HITL checklist
    assert "filled_sha256: S.run.filled.sha256, revision: S.run.revision" in js


# ------------------------------------------------ #18 R1-A19 code-point indexing


def test_r1_a19_ui_slices_code_points_like_python():
    js = (ROOT / "form_guard" / "static" / "app.js").read_text(encoding="utf-8")
    assert "const chars = Array.from(S.run.memo.text);" in js and 'chars.slice(a, b).join("")' in js
    assert "memo.slice(" not in js and "(m.filled_value || \"\").length" not in js
    assert "const n = cpLength(m.filled_value);" in js and "Array.from(s || \"\").length" in js
    memo = "𠮷野家🧪の件：試料番号は𠮷-01（🧪）\n担当：架空 太郎\n"
    g = ground_value("架空 太郎", ["架空 太郎"], memo, _text_slot("担当者氏名", fmt="person_name"))
    sp = g.spans[0]
    assert memo[sp.start_char : sp.end_char] == "架空 太郎" and sp.start_char == memo.index("架空")
    assert human_mapping(_text_slot("備考"), "𠮷野家🧪").char_count == 4  # code points, as the UI now counts


# ------------------------------------------- #19 R1-O5 / R1-O7 wording & flags


def test_r1_o5_o7_readme_wording_and_re_anchored_flag():
    assert flag_label("re_anchored") == "引用を付け替え / quote re-anchored — check the wording"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "21 deterministic checks (plus VIS-01 if layout drift or overflow is detected)" in readme
    assert "21項目を決定論的に検査します（描画位置のずれやセルはみ出しが検出された場合は VIS-01 を追加）" in readme
    assert ("Form-Guard checks that each value is **traceable to verbatim memo quotes** (numbers, dates, names, IDs and "
            "choices must match them) and that **the form itself was not altered**.") in readme
    assert ("本ツールが機械的に確認するのは「各値がメモの原文引用に紐づき、数値・日付・氏名・ID・選択肢が一致すること」と"
            "「様式そのものが改変されていないこと」です。") in readme
    assert "memo grounding is checked when `--memo MEMO` is given or a" in readme
    assert "21 deterministic checks: ZIP" not in readme and "描画位置のずれを含む 21 項目" not in readme


# --------------------------------------------------- #20 R1-O6 Host / Origin


def test_r1_o6_server_refuses_foreign_hosts_and_cross_site_origins(api):
    assert api.get("/api/health", headers={"Host": "evil.example.com"}).status_code == 403
    assert api.get("/api/health", headers={"Host": "evil.example.com"}).json()["detail"] == "forbidden host"
    assert api.get("/api/health", headers={"Host": "127.0.0.1.evil.example.com:8000"}).status_code == 403
    cross = api.post("/api/runs", json={"demo_id": "nope"}, headers={"Origin": "https://evil.example.com"})
    assert cross.status_code == 403 and cross.json()["detail"] == "forbidden origin"
    assert api.post("/api/runs", json={"demo_id": "nope"}, headers={"Origin": "null"}).status_code == 403
    assert api.post("/api/runs", json={"demo_id": "nope"}, headers={"Origin": "http://[::1"}).status_code == 403  # unparsable
    for host in ("testserver", "127.0.0.1:8000", "localhost:8000", "[::1]:8000"):
        assert api.get("/api/health", headers={"Host": host}).status_code == 200, host
    for origin in ("http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000"):
        assert api.post("/api/runs", json={"demo_id": "nope"}, headers={"Origin": origin}).status_code == 404, origin
    assert api.post("/api/runs", json={"demo_id": "nope"}).status_code == 404  # no Origin (CLI / same-origin GET-less)


# ===================================================================== Phase 2
# P0-0 strict checks of coordinate-designated empty cells + verified SHA-256,
# P0-1 multi-model routing, MCP server, Claude Code assets, docs,
# P0-2 Excel coordinate targets and 4-stage discovery on unmarked blank forms.

UNMARKED = {u["id"]: u for u in UNMARKED_FORMS}


def unmarked(samples_dir, uid):
    return unmarked_paths(UNMARKED[uid], samples_dir)


def _designated_docx(tmp_path):
    """A docx whose empty cell (0,1) carries paragraph-mark formatting; both empty cells designated by coordinates."""
    src, mid, out = tmp_path / "fmt.docx", tmp_path / "fmt_mid.docx", tmp_path / "fmt_filled.docx"
    d = docx.Document()
    d.add_paragraph("架空の届出書（テスト）")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(1, 0).text = "担当者", "連絡先"
    d.save(src)

    def add_ppr(b):
        tc = list(re.finditer(rb"<w:tc>.*?</w:tc>", b))[1]
        new = tc.group(0).replace(b"<w:p/>", b'<w:p><w:pPr><w:jc w:val="right"/><w:rPr><w:b/><w:sz w:val="18"/>'
                                               b'</w:rPr></w:pPr></w:p>')
        return b[: tc.start()] + new + b[tc.end():]

    surgical_rewrite(src, mid, "word/document.xml", add_ppr)
    sc = scan(mid, {"targets": [{"slot_id": "担当者", "table": 0, "row": 0, "col": 1},
                                {"slot_id": "連絡先", "table": 0, "row": 1, "col": 1}]})
    values = {"担当者": "模擬 花子", "連絡先": "info@example.jp"}
    fill_form(sc, values, out)
    return sc, out, [human_mapping(sc.slot(k), v) for k, v in values.items()]


_RUN = '<w:r><w:rPr><w:b/><w:sz w:val="18"/></w:rPr><w:t>模擬 花子</w:t></w:r>'.encode()


def _cell_issues(rep):
    return [i for c in rep.checks for i in c.evidence.get("designated_cell_issues", [])]


# --------------------------------------------------------------- P0-0 Word cells


def test_p00_designated_word_cell_gets_one_plain_run_with_the_blank_cells_formatting(tmp_path):
    sc, out, maps = _designated_docx(tmp_path)
    xml = zipfile.ZipFile(out).read("word/document.xml")
    assert _RUN in xml  # paragraph-mark formatting (w:pPr/w:rPr) copied to the one new run
    rep = verify(sc, out, maps, "")
    assert not _failing(rep) and rep.overall_status == "PASS"
    assert "2 designated empty cell(s) hold plain text only" in check(rep, "XML-03").details
    assert "formatted exactly like the blank cell" in check(rep, "LAYOUT-03").details


def test_p00_non_text_elements_in_a_designated_word_cell_fail_xml03(tmp_path, demo_runs):
    sc, out, maps = _designated_docx(tmp_path)
    attacks = {
        "drawing": lambda b: b.replace(_RUN, _RUN.replace(b"<w:t>", b"<w:drawing/><w:t>")),
        "pict": lambda b: b.replace(_RUN, _RUN.replace(b"<w:t>", b"<w:pict/><w:t>")),
        "object": lambda b: b.replace(_RUN, _RUN.replace(b"<w:t>", b"<w:object/><w:t>")),
        "field": lambda b: b.replace(_RUN, _RUN + b'<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
                                                b"<w:r><w:instrText>PAGE</w:instrText></w:r>"),
        "fldSimple": lambda b: b.replace(_RUN, b'<w:fldSimple w:instr="DATE">' + _RUN + b"</w:fldSimple>"),
        "hyperlink": lambda b: b.replace(_RUN, b'<w:hyperlink w:anchor="x">' + _RUN + b"</w:hyperlink>"),
        "tbl": lambda b: b.replace(_RUN, _RUN + b"<w:tbl><w:tr><w:tc><w:p/></w:tc></w:tr></w:tbl>"),
        "second_run": lambda b: b.replace(_RUN, _RUN + b"<w:r><w:t>2</w:t></w:r>"),
    }
    for name, fn in attacks.items():
        bad = tmp_path / f"{name}.docx"
        surgical_rewrite(out, bad, "word/document.xml", fn)
        rep = verify(sc, bad, maps, "")
        assert "XML-03" in _failing(rep) and rep.overall_status == "FAIL", name
    rep = verify(sc, tmp_path / "field.docx", maps, "")
    assert check(rep, "TEXT-02").passed  # a field adds no visible text — only the new cell check catches it
    assert any("<w:fldChar>, <w:instrText>" in i for i in _cell_issues(rep))
    rep = verify(sc, tmp_path / "second_run.docx", maps, "")
    assert any("at most one run may be added" in i for i in _cell_issues(rep))
    r2 = demo_runs("form2")  # the demo's coordinate-designated cell (担当者連絡先) is guarded the same way
    bad = tmp_path / "demo2_drawing.docx"
    surgical_rewrite(r2.filled_path, bad, "word/document.xml",
                     lambda b: b.replace("<w:t>総務部".encode(), "<w:drawing/><w:t>総務部".encode(), 1))
    assert "XML-03" in _failing(verify(r2.scan, bad, r2.mappings, r2.memo_text))


def test_p00_changed_or_removed_formatting_in_a_designated_word_cell_fails_layout03(tmp_path):
    sc, out, maps = _designated_docx(tmp_path)
    attacks = {
        "size": lambda b: b.replace(_RUN, _RUN.replace(b'w:val="18"', b'w:val="28"')),
        "bold_removed": lambda b: b.replace(_RUN, _RUN.replace(b"<w:b/>", b"")),
        "color": lambda b: b.replace(_RUN, _RUN.replace(b"<w:b/>", b'<w:b/><w:color w:val="FF0000"/>')),
        "rpr_removed": lambda b: b.replace(_RUN, "<w:r><w:t>模擬 花子</w:t></w:r>".encode()),
        "ppr_changed": lambda b: b.replace(b'<w:jc w:val="right"/>', b'<w:jc w:val="left"/>', 1),
    }
    for name, fn in attacks.items():
        bad = tmp_path / f"{name}.docx"
        surgical_rewrite(out, bad, "word/document.xml", fn)
        rep = verify(sc, bad, maps, "")
        assert "LAYOUT-03" in _failing(rep) and rep.overall_status == "FAIL", name
        assert check(rep, "TEXT-02").passed, name  # the text is right; the formatting is not
    rep = verify(sc, tmp_path / "size.docx", maps, "")
    assert _failing(rep) == {"LAYOUT-03"}  # before P0-0 this file passed every check
    assert any("[b, sz=28] differs from the blank cell's paragraph-mark formatting [b, sz=18]" in i for i in _cell_issues(rep))
    rep = verify(sc, tmp_path / "rpr_removed.docx", maps, "")
    assert any("run formatting [none]" in i for i in _cell_issues(rep))


# -------------------------------------------------------------- P0-0 Excel cells


def _designated_xlsx(tmp_path):
    """Existing empty styled <c r="B2" s=".."/>, B3 missing in its row, B5 missing before C5, row 9 missing."""
    from openpyxl.styles import Border, Side

    src, out = tmp_path / "cells.xlsx", tmp_path / "cells_filled.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "申請"
    ws["A1"], ws["A2"], ws["A3"], ws["A5"], ws["C5"] = "架空の申請書（テスト）", "団体名", "担当者", "備考", "（任意）"
    ws["B2"].border = Border(bottom=Side(style="thin"))
    wb.save(src)
    spec = {"targets": [{"slot_id": "団体名", "sheet": 0, "cell": "B2"}, {"slot_id": "担当者", "sheet": "申請", "cell": "B3"},
                        {"slot_id": "備考", "sheet": 0, "cell": "B5"}, {"slot_id": "追記", "sheet": 0, "cell": "D9"}]}
    sc = scan(src, spec)
    values = {"団体名": "架空町内会", "担当者": "模擬 太郎", "備考": "特になし", "追記": "テスト  値"}
    w = fill_form(sc, values, out)
    return src, sc, out, w, [human_mapping(sc.slot(k), v) for k, v in values.items()]


def test_p02_xlsx_coordinate_targets_fill_existing_and_missing_cells(tmp_path):
    src, sc, out, w, maps = _designated_xlsx(tmp_path)
    assert [(s.slot_id, s.occurrences[0].cell_ref, s.occurrences[0].box_id) for s in sc.slots] == [
        ("団体名", "B2", "s0!B2"), ("担当者", "B3", "s0!B3"), ("備考", "B5", "s0!B5"), ("追記", "D9", "s0!D9")]
    assert [(p.kind, p.slot_id) for p in w.patches] == [("fill_cell", "団体名"), ("insert_cell", "担当者"),
                                                        ("insert_cell", "備考"), ("insert_cell", "追記")]
    xml = zipfile.ZipFile(out).read("xl/worksheets/sheet1.xml").decode("utf-8")
    style = re.search(r'<c r="B2" s="(\d+)"', zipfile.ZipFile(src).read("xl/worksheets/sheet1.xml").decode()).group(1)
    assert f'<c r="B2" s="{style}" t="inlineStr"><is><t>架空町内会</t></is></c>' in xml  # style id kept
    assert re.search(r'<c r="B5" t="inlineStr"><is><t>特になし</t></is></c><c r="C5"', xml)  # created in column order
    assert '<row r="9"><c r="D9" t="inlineStr"><is><t xml:space="preserve">テスト  値</t></is></c></row>' in xml
    ws = openpyxl.load_workbook(out).active
    assert [ws[c].value for c in ("B2", "B3", "B5", "D9", "C5")] == ["架空町内会", "模擬 太郎", "特になし", "テスト  値", "（任意）"]
    rep = verify(sc, out, maps, "")
    assert not _failing(rep) and rep.overall_status == "PASS"
    assert "4 designated empty cell(s)" in check(rep, "XML-03").details and "style id (s) of 4" in check(rep, "LAYOUT-02").details
    assert infer_values_from_filled(sc, out) == {"団体名": "架空町内会", "担当者": "模擬 太郎", "備考": "特になし", "追記": "テスト  値"}
    raw_o, raw_f = read_raw_zip(src.read_bytes()), read_raw_zip(out.read_bytes())
    assert all(a.raw == b.raw for a, b in zip(raw_o.entries, raw_f.entries) if a.name != "xl/worksheets/sheet1.xml")
    memo = "団体名：架空町内会\n担当者：模擬 太郎\n備考：特になし\n追記：テスト値\n"
    r = run_pipeline(src, memo, tmp_path / "pipeline", mode="demo", slot_spec=sc.spec)  # end to end, incl. VIS-01
    assert r.report.overall_status == "PASS" and r.visual["identical_outside_slots"] and not _failing(r.report)
    assert "s0!D9" in r.previews["filled"]  # the cell created outside the blank sheet's used range is highlighted


def test_p00_designated_excel_cells_keep_their_style_and_never_take_a_formula(tmp_path):
    src, sc, out, w, maps = _designated_xlsx(tmp_path)
    style = re.search(rb'<c r="B2" s="(\d+)"', zipfile.ZipFile(src).read("xl/worksheets/sheet1.xml")).group(1)
    filled = b'<c r="B2" s="' + style + b'" t="inlineStr"><is><t>'
    cases = {
        "formula": (lambda b: b.replace(filled, filled.replace(b"<is>", b"<f>1+1</f><is>")), {"XML-03", "LAYOUT-04"}),
        "style": (lambda b: b.replace(filled, filled.replace(b's="' + style + b'"', b's="0"')), {"LAYOUT-02"}),
        "value_element": (lambda b: b.replace(filled, filled.replace(b"<is>", b"<v>1</v><is>")), {"XML-03"}),
        "other_cell": (lambda b: b.replace(b"&#20219;&#24847;", b"&#24517;&#38920;"), {"XML-03", "TEXT-01"}),  # （任意）→（必須）
        "unrelated_cell": (lambda b: b.replace(b'<row r="9">', b'<row r="8"><c r="A8" t="inlineStr"><is><t>x</t></is></c>'
                                                               b'</row><row r="9">'), {"XML-03", "TEXT-01"}),
    }
    for name, (fn, expected) in cases.items():
        bad = tmp_path / f"{name}.xlsx"
        surgical_rewrite(out, bad, "xl/worksheets/sheet1.xml", fn)
        rep = verify(sc, bad, maps, "")
        assert expected <= _failing(rep) and rep.overall_status == "FAIL", (name, _failing(rep))
    assert openpyxl.load_workbook(tmp_path / "other_cell.xlsx").active["C5"].value == "（必須）"
    rep = verify(sc, tmp_path / "style.xlsx", maps, "")
    assert any(f"B2 cell style s={style.decode()} → s=0" in i for i in _cell_issues(rep))
    rep = verify(sc, tmp_path / "formula.xlsx", maps, "")
    assert any("a formula was written into designated cell B2" in i for i in _cell_issues(rep))


def test_p02_xlsx_coordinate_targets_refuse_unsafe_cells(tmp_path):
    src = tmp_path / "unsafe.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"], ws["C2"], ws["A4"], ws["A6"] = "架空の申請書", "（任意）", "=SUM(1,2)", "期間"
    ws.merge_cells("B6:C6")
    wb.save(src)
    sc = scan(src, {"targets": [{"slot_id": "式", "sheet": 0, "cell": "A4"}, {"slot_id": "続き", "sheet": 0, "cell": "C6"},
                                {"slot_id": "印刷", "sheet": 0, "cell": "C2"}, {"slot_id": "表", "table": 0, "row": 0, "col": 0},
                                {"slot_id": "範囲外", "sheet": 0, "cell": "ZZZ9999999"}, {"slot_id": "無い", "sheet": 5, "cell": "A1"},
                                {"slot_id": "左上", "sheet": 0, "cell": "B6"}]})
    assert [s.slot_id for s in sc.slots] == ["左上"]  # only the top-left cell of the merged range is accepted
    w = "\n".join(sc.warnings)
    assert "A4 contains a formula — formulas are never fill targets" in w
    assert "C6 is a merged continuation — designate the top-left cell B6" in w
    assert "C2 is not empty — refusing to overwrite printed text" in w
    assert "table/row/col coordinates are for .docx" in w and "not a cell reference inside the form limits" in w
    assert "sheet 5 not found" in w
    doc = tmp_path / "t.docx"
    d = docx.Document()
    d.add_table(rows=1, cols=2).cell(0, 0).text = "項目"
    d.save(doc)
    assert "sheet/cell coordinates are for .xlsx" in "\n".join(
        scan(doc, {"targets": [{"slot_id": "X", "sheet": 0, "cell": "B1"}]}).warnings)


# ------------------------------------------------------------- P0-0 server SHA


def test_p00_verified_sha256_is_recorded_once_and_enforced_on_approve_and_download(api):
    run = api.post("/api/runs", json={"demo_id": "form1", "mode": "demo", "model_tier": "auto"}).json()
    rid = run["run_id"]
    st = api.app.state.store.runs[rid]
    assert st.verified_sha256 == st.result.verified_sha256 == run["filled"]["sha256"] == hashlib.sha256(st.verified_bytes).hexdigest()
    required = [h["item_id"] for h in run["report"]["hitl_checklist"] if h["required"]]
    approve = {"reviewer": "架空 花子", "checked": required, "filled_sha256": st.verified_sha256, "revision": 0}
    assert api.post(f"/api/runs/{rid}/approve", json=approve).status_code == 200
    url = f"/api/runs/{rid}/download"
    assert api.get(url, params={"filled_sha256": "0" * 64}).status_code == 409  # stale hash from the client
    assert api.get(url, params={"revision": 3}).status_code == 409  # stale revision from the client
    ok = api.get(url, params={"filled_sha256": st.verified_sha256, "revision": 0})
    assert ok.status_code == 200 and hashlib.sha256(ok.content).hexdigest() == st.verified_sha256
    path = st.result.filled_path
    good = path.read_bytes()
    path.write_bytes(good + b"x")  # the file on disk differs from the verified bytes
    assert api.get(url).status_code == 409
    assert api.post(f"/api/runs/{rid}/approve", json=approve).status_code == 409
    path.write_bytes(good)
    assert api.get(url).status_code == 200
    assert api.post("/api/runs", json={"demo_id": "form1", "mode": "demo", "model_tier": "gpt"}).status_code == 422


# ---------------------------------------------------- P0-1 multi-model routing


def test_p01_current_model_constants_and_capability_table():
    assert (MODEL_OPUS, MODEL_SONNET, MODEL_HAIKU) == ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")
    assert DEFAULT_MODEL == MODEL_OPUS
    for m in (MODEL_OPUS, MODEL_SONNET, MODEL_HAIKU, "claude-opus-4-6", "claude-sonnet-4-6", "claude-fable-5-1",
              "anthropic.claude-sonnet-5-5"):
        caps = model_capabilities(m)
        assert caps["adaptive_thinking"] and caps["effort"] and not caps["legacy"], m
    for m in ("claude-haiku-4-5", "claude-haiku-4-5-20251001", "claude-sonnet-4-5-20250929", "claude-3-5-haiku-20241022",
              "claude-3-haiku-20240307", "not-a-claude-model"):
        caps = model_capabilities(m)
        assert not caps["adaptive_thinking"] and not caps["effort"] and caps["legacy"], m
    assert model_capabilities("claude-opus-4-5-20251101")["effort"] and not model_capabilities("claude-opus-4-5")["adaptive_thinking"]
    assert model_capabilities("claude-haiku-4-5")["structured_outputs"] and not model_capabilities("claude-3-haiku-20240307")["structured_outputs"]
    assert model_capabilities("claude-3-haiku-20240307")["max_output_tokens"] == 4096
    assert model_capabilities(MODEL_HAIKU)["server_side_fallback"] and not model_capabilities("claude-sonnet-4-6")["server_side_fallback"]
    assert _effort_for(model_capabilities("claude-opus-4-6"), "xhigh") == "high"  # clamped, never a 400
    assert _effort_for(model_capabilities(MODEL_OPUS), "xhigh") == "xhigh"
    assert _effort_for(model_capabilities("claude-haiku-4-5"), "high") is None


def test_p01_route_slot_by_task_complexity(samples_dir):
    slots = {s.slot_id: s for s in scan(paths(samples_dir, "form2")["form"]).slots}
    memo1 = read_memo(paths(samples_dir, "form1")["memo"])
    slots1 = {s.slot_id: s for s in scan(paths(samples_dir, "form1")["form"]).slots}
    assert route_slot(slots["申請日"])[0] == "haiku" and route_slot(slots["法人番号"])[0] == "haiku"  # date, digits
    assert route_slot(slots["補助金申請額"])[0] == "sonnet" and route_slot(slots["申請事業者名"])[0] == "sonnet"  # yen, text
    assert route_slot(slots1["研究代表者氏名"])[0] == "sonnet" and route_slot(slots1["変更種別"])[0] == "haiku"  # name, choice
    tier, why = route_slot(slots1["変更理由および予算影響"], list(slots1.values()), memo1)
    assert tier == "opus" and "condensation" in why and "200-char" in why
    assert route_slot(slots1["変更前の計画概要"], list(slots1.values()), memo1)[0] == "sonnet"  # fits its 100-char limit
    assert route_slot(_text_slot("所見", max_chars=300))[0] == "opus"  # long free text, passage unknown


def _revision_fix():
    return {"fields": [{"slot_id": "所属部局", "status": "filled", "value": "理工学研究科",
                        "source_quotes": ["・所属部局：理工学研究科"], "line_start": 8, "line_end": 8,
                        "confidence": 0.95, "reasoning": "fixed"}]}


def test_p01_auto_tier_routes_fields_by_complexity_and_revises_with_opus(samples_dir, fake_client_factory):
    p = paths(samples_dir, "form1")
    client = fake_client_factory([echo_model(_claude_fields())] * 3 + [echo_model(_revision_fix())])
    engine = ClaudeEngine(client=client)  # model_tier defaults to "auto"
    run = map_fields(scan(p["form"]).slots, read_memo(p["memo"]), engine)
    calls = client.beta.messages.calls
    assert [c["model"] for c in calls] == [MODEL_HAIKU, MODEL_SONNET, MODEL_OPUS, MODEL_OPUS]
    routed = {f"{r['task']}:{r['model']}": r["slots"] for r in run.usage["routing"]}
    assert routed == {
        f"propose:{MODEL_HAIKU}": ["提出日", "研究課題番号", "変更種別"],
        f"propose:{MODEL_SONNET}": ["研究代表者氏名", "所属部局", "変更前の計画概要", "変更後の計画概要"],
        f"propose:{MODEL_OPUS}": ["変更理由および予算影響"],
        f"revise:{MODEL_OPUS}": ["所属部局"],  # self-correction after the validator rejected 「工学部」
    }
    revise = calls[3]["messages"]  # continues the Sonnet conversation that proposed 所属部局
    assert '"slot_id": "所属部局"' in revise[0]["content"] and '"slot_id": "変更理由および予算影響"' not in revise[0]["content"]
    assert revise[1]["role"] == "assistant" and "quote not found verbatim" in revise[-1]["content"]
    for c in calls:
        assert c["thinking"] == {"type": "adaptive", "display": "summarized"} and c["output_config"]["effort"] == "medium"
        assert c["fallbacks"] == "default" and c["cache_control"] == {"type": "ephemeral"}
    assert all(m.status == "filled" for m in run.mappings)
    assert {m.slot_id: m for m in run.mappings}["所属部局"].filled_value == "理工学研究科"
    assert run.usage["models"] == [MODEL_HAIKU, MODEL_SONNET, MODEL_OPUS] and run.usage["requests"] == 4
    assert engine.model == "auto" and engine.label.startswith("Claude (auto model routing")


@pytest.mark.parametrize("tier,model", [("opus", MODEL_OPUS), ("sonnet", MODEL_SONNET), ("haiku", MODEL_HAIKU)])
def test_p01_fixed_model_tier_uses_one_current_model_for_every_call(samples_dir, fake_client_factory, tier, model):
    p = paths(samples_dir, "form1")
    client = fake_client_factory([echo_model(_claude_fields()), echo_model(_revision_fix())])
    run = map_fields(scan(p["form"]).slots, read_memo(p["memo"]), ClaudeEngine(client=client), model_tier=tier)
    calls = client.beta.messages.calls
    assert [c["model"] for c in calls] == [model, model]  # propose + revise
    for c in calls:
        assert c["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert c["output_config"]["effort"] == "medium" and c["output_config"]["format"]["type"] == "json_schema"
    assert run.usage["models"] == [model] and all(m.status == "filled" for m in run.mappings)


def test_p01_legacy_haiku_4_5_never_gets_adaptive_thinking_or_effort(samples_dir, fake_client_factory):
    p = paths(samples_dir, "form1")
    fields = _claude_fields({"所属部局": ("理工学研究科", ["・所属部局：理工学研究科"])})
    client = fake_client_factory([echo_model(fields)])
    run = map_fields(scan(p["form"]).slots, read_memo(p["memo"]), ClaudeEngine(model="claude-haiku-4-5", client=client))
    (call,) = client.beta.messages.calls
    assert call["model"] == "claude-haiku-4-5" and "thinking" not in call
    assert call["output_config"] == {"format": {"type": "json_schema", "schema": call["output_config"]["format"]["schema"]}}
    assert "fallbacks" not in call and "betas" not in call and "cache_control" not in call
    assert run.usage["calls"][0]["thinking"].startswith("omitted") and run.usage["calls"][0]["effort"] is None
    assert all(m.status == "filled" for m in run.mappings)


def test_p01_legacy_model_wire_request_through_the_real_sdk(samples_dir):
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    p = paths(samples_dir, "form1")
    payload = json.dumps(_claude_fields({"所属部局": ("理工学研究科", ["・所属部局：理工学研究科"])}), ensure_ascii=False)
    events = [
        ("message_start", {"type": "message_start", "message": {"id": "msg_legacy", "type": "message", "role": "assistant",
                                                                 "model": "claude-haiku-4-5-20251001", "content": [],
                                                                 "stop_reason": None, "stop_sequence": None,
                                                                 "usage": {"input_tokens": 700, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": payload}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 210}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    sse = "".join(f"event: {e}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n" for e, d in events).encode("utf-8")
    seen = {}

    def handler(request):
        seen["beta"] = request.headers.get("anthropic-beta", "")
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse)

    client = anthropic.Anthropic(**{"api" + "_key": "mock"}, max_retries=0,
                                 http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    run = map_fields(scan(p["form"]).slots, read_memo(p["memo"]), ClaudeEngine(model="claude-haiku-4-5", client=client))
    body = seen["body"]
    assert body["model"] == "claude-haiku-4-5" and "thinking" not in body and "effort" not in body["output_config"]
    assert body["output_config"]["format"]["type"] == "json_schema" and "fallbacks" not in body and not seen["beta"]
    assert run.usage["models"] == ["claude-haiku-4-5-20251001"] and all(m.status == "filled" for m in run.mappings)


def test_p01_api_returned_thinking_summaries_are_recorded_and_labelled(samples_dir, fake_client_factory):
    p = paths(samples_dir, "form1")
    msg = fake_message(_claude_fields({"所属部局": ("理工学研究科", ["・所属部局：理工学研究科"])}),
                       thinking="Checked every quote against the numbered memo lines.")
    msg.content.append(SimpleNamespace(type="redacted_thinking", data="opaque"))
    run = map_fields(scan(p["form"]).slots, read_memo(p["memo"]),
                     ClaudeEngine(client=fake_client_factory([msg]), model_tier="opus"))
    (summary,) = run.usage["thinking_summaries"]
    assert summary == {"label": THINKING_SUMMARY_LABEL, "model": MODEL_OPUS, "task": "propose",
                       "slots": [s.slot_id for s in scan(p["form"]).slots],  # the fields this call covered
                       "text": "Checked every quote against the numbered memo lines."}
    assert THINKING_SUMMARY_LABEL.startswith("API-returned thinking summary") and run.usage["redacted_thinking_blocks"] == 1


def test_p01_explicit_model_or_environment_pins_every_call(samples_dir, fake_client_factory, monkeypatch):
    p = paths(samples_dir, "form1")
    monkeypatch.setenv("FORM_GUARD_MODEL", MODEL_SONNET)
    client = fake_client_factory([echo_model(_claude_fields()), echo_model(_revision_fix())])
    engine = ClaudeEngine(client=client)
    map_fields(scan(p["form"]).slots, read_memo(p["memo"]), engine)
    assert [c["model"] for c in client.beta.messages.calls] == [MODEL_SONNET, MODEL_SONNET]  # revise too
    assert engine.model == MODEL_SONNET and engine.label == f"Claude ({MODEL_SONNET}, effort medium)"
    with pytest.raises(ValueError, match="model_tier must be one of"):
        ClaudeEngine(model_tier="gpt")


def test_p01_model_tier_from_the_environment_applies_unless_an_argument_is_given(samples_dir, fake_client_factory,
                                                                                  tmp_path, monkeypatch):
    p = paths(samples_dir, "form1")
    monkeypatch.setenv("FORM_GUARD_MODEL_TIER", "sonnet")
    client = fake_client_factory([echo_model(_claude_fields()), echo_model(_revision_fix())])
    r = run_pipeline(p["form"], read_memo(p["memo"]), tmp_path / "env", mode="live", client=client)
    assert r.engine["model_tier"] == "sonnet" and [c["model"] for c in client.beta.messages.calls] == [MODEL_SONNET] * 2
    client = fake_client_factory([echo_model(_claude_fields()), echo_model(_revision_fix())])
    r = run_pipeline(p["form"], read_memo(p["memo"]), tmp_path / "arg", mode="live", client=client, model_tier="opus")
    assert r.engine["model_tier"] == "opus" and [c["model"] for c in client.beta.messages.calls] == [MODEL_OPUS] * 2


def test_p01_run_pipeline_and_cli_pass_the_model_tier(samples_dir, fake_client_factory, tmp_path, capsys):
    p = paths(samples_dir, "form1")
    client = fake_client_factory([echo_model(_claude_fields()), echo_model(_revision_fix())])
    r = run_pipeline(p["form"], read_memo(p["memo"]), tmp_path / "tier", mode="live", client=client, model_tier="haiku")
    assert r.engine["model_tier"] == "haiku" and r.engine["model"] == MODEL_HAIKU
    assert [c["model"] for c in client.beta.messages.calls] == [MODEL_HAIKU, MODEL_HAIKU]
    assert r.engine["usage"]["routing"][0]["reasons"]["提出日"] == "model tier 'haiku'"
    assert r.report.overall_status == "PASS"
    audit = json.loads((tmp_path / "tier" / "audit_log.json").read_text(encoding="utf-8"))
    assert audit["engine"]["usage"]["routing"][0]["model"] == MODEL_HAIKU  # which model handled which field is audited
    html = (tmp_path / "tier" / "report.html").read_text(encoding="utf-8")
    assert "Model routing / モデルの使い分け" in html and f"<td class='mono'>{MODEL_HAIKU}</td>" in html
    from form_guard.cli import main

    assert main(["--form", str(p["form"]), "--memo", str(p["memo"]), "--out-dir", str(tmp_path / "cli"), "--mode", "demo",
                 "--model-tier", "sonnet"]) == 0
    capsys.readouterr()
    with pytest.raises(SystemExit):
        main(["--all-demos", "--model-tier", "gpt"])


def test_p01_blank_cell_classification_uses_haiku_and_can_only_restrict(samples_dir, fake_client_factory):
    form = unmarked(samples_dir, "form4")["form"]
    rules = discover_blank_cells(form)
    verdicts = {"cells": [{"candidate_id": c["candidate_id"], "role": "applicant_fillable", "reason": "label"}
                          for c in rules["candidates"]]}
    verdicts["cells"][2]["role"] = "not_a_field"  # c03 所属機関
    client = fake_client_factory([echo_model(verdicts)])
    d = discover_blank_cells(form, mode="live", client=client)
    (call,) = client.beta.messages.calls
    assert call["model"] == MODEL_HAIKU and "blank-cell classification step" in call["system"]
    assert call["output_config"]["format"]["schema"]["required"] == ["cells"]
    by = {c["slot_id"]: c for c in d["candidates"]}
    assert not by["所属機関"]["recommended"] and "not an input field (Claude" in by["所属機関"]["reason"]
    assert by["受付番号"]["office_only"] and not by["受付番号"]["recommended"]  # Claude said applicant: the rules win
    assert d["engine"]["mode"] == "live" and d["engine"]["model"] == MODEL_HAIKU
    assert {c["classified_by"] for c in d["candidates"]} == {f"rules + {MODEL_HAIKU}"}
    fallback = discover_blank_cells(form, mode="auto", client=fake_client_factory([fake_message({}, "refusal")]))
    assert "declined" in fallback["engine"]["fallback_reason"] and fallback["candidates"] == rules["candidates"]


# ------------------------------------------------------------- P0-1 MCP server


def _mcp_root(tmp_path, samples_dir):
    root = tmp_path / "mcp_root"
    shutil.copytree(samples_dir, root / "samples")
    return root


def test_p01_mcp_server_speaks_json_rpc_over_stdio_in_both_framings(samples_dir, tmp_path):
    root = _mcp_root(tmp_path, samples_dir)
    env = {**os.environ, "FORM_GUARD_MCP_ROOT": str(root), "PYTHONPATH": str(ROOT)}
    err = open(tmp_path / "mcp_stderr.txt", "wb")
    proc = subprocess.Popen([sys.executable, "-m", "form_guard.mcp_server"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=err, cwd=root, env=env)

    def send(msg, framed=False):
        body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        proc.stdin.write((b"Content-Length: %d\r\n\r\n" % len(body)) + body if framed else body + b"\n")
        proc.stdin.flush()

    def recv(framed=False):
        if not framed:
            return json.loads(proc.stdout.readline())
        header = proc.stdout.readline()
        assert header.lower().startswith(b"content-length:")
        assert proc.stdout.readline() == b"\r\n"
        return json.loads(proc.stdout.read(int(header.split(b":")[1])))

    def call(i, name, args, framed=False):
        send({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}}, framed)
        return recv(framed)["result"]

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}}})
        init = recv()["result"]
        assert init["protocolVersion"] == "2025-06-18" and init["serverInfo"]["name"] == "form-guard"
        assert init["capabilities"] == {"tools": {"listChanged": False}}
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})  # a notification gets no answer
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, framed=True)
        tools = recv(framed=True)["result"]["tools"]
        assert [t["name"] for t in tools] == ["form_guard_list_demos", "form_guard_scan", "form_guard_discover_blank_cells",
                                              "form_guard_fill", "form_guard_verify"]
        assert all(t["inputSchema"]["type"] == "object" and t["inputSchema"]["additionalProperties"] is False for t in tools)
        assert all(t["inputSchema"]["required"] for t in tools if t["name"] != "form_guard_list_demos")
        demos = call(21, "form_guard_list_demos", {})["structuredContent"]
        assert [(d["id"], d["unmarked"]) for d in demos["demos"]] == [
            ("form1", False), ("form2", False), ("form3", False), ("form4", True), ("form5", True)]
        assert all(d["files_present"] and (root / d["form_path"]).is_file() for d in demos["demos"])
        scanned = call(3, "form_guard_scan", {"form_path": "samples/blank_forms/form2_municipal_equipment_subsidy.docx"})
        assert not scanned["isError"] and scanned["structuredContent"]["slot_count"] == 11
        assert json.loads(scanned["content"][0]["text"])["file_type"] == "docx"
        form5 = "samples/blank_forms/form5_unmarked_blank.xlsx"
        disc = call(4, "form_guard_discover_blank_cells", {"form_path": form5}, framed=True)["structuredContent"]
        rec = [c["candidate_id"] for c in disc["candidates"] if c["recommended"]]
        assert disc["requires_human_confirmation"] and len(rec) == 5
        fill = call(5, "form_guard_fill", {"form_path": form5, "memo_path": "samples/memos/memo5_stockpile_loan.txt",
                                           "discovery": disc, "confirmed_ids": rec, "mode": "demo"})["structuredContent"]
        assert fill["approval_status"] == "UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF" and fill["human_sign_off_required"]
        assert fill["verification"]["overall_status"] == "PASS" and fill["verification"]["ungrounded_claims"] == 0
        assert fill["out_dir"].startswith("output/mcp_runs/") and (root / fill["draft_path"]).is_file()
        assert (root / fill["out_dir"] / "APPROVAL_STATUS.json").is_file()
        assert any(h["item_id"] == "global:responsibility" for h in fill["hitl_checklist"])
        verified = call(6, "form_guard_verify", {"original_path": form5, "filled_path": fill["draft_path"],
                                                 "slot_spec_path": fill["slot_spec_path"]})["structuredContent"]
        assert verified["overall_status"] == "PASS" and verified["grounding_verified"]
        assert verified["grounding_basis"] == "memo recorded in the audit log"
        send({"jsonrpc": "2.0", "id": 7, "method": "ping"})
        assert recv() == {"jsonrpc": "2.0", "id": 7, "result": {}}
        send({"jsonrpc": "2.0", "id": 8, "method": "sampling/createMessage"})
        assert recv()["error"]["code"] == -32601
        send({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "delete_everything", "arguments": {}}})
        assert recv()["error"]["code"] == -32602
        proc.stdin.write(b"{not json\n")
        proc.stdin.flush()
        assert recv()["error"]["code"] == -32700
    finally:
        proc.stdin.close()
        proc.wait(timeout=60)
        err.close()
    assert proc.returncode == 0, (tmp_path / "mcp_stderr.txt").read_text(encoding="utf-8", errors="replace")


def test_p01_mcp_server_keeps_the_human_gate_and_the_server_root(samples_dir, tmp_path):
    from form_guard.mcp_server import APPROVAL_STATUS, FormGuardMCP

    root = _mcp_root(tmp_path, samples_dir)
    srv = FormGuardMCP(root=root)
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert srv.handle({"jsonrpc": "2.0", "id": 1, "result": {}}) is None  # a response from the client
    assert srv.handle("nonsense")["error"]["code"] == -32600
    assert srv.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}
                      )["result"]["protocolVersion"] == "2025-11-25"
    assert [r["id"] for r in srv.handle_payload([{"jsonrpc": "2.0", "id": 3, "method": "ping"},
                                                 {"jsonrpc": "2.0", "method": "notifications/initialized"}])] == [3]
    text = lambda res: res["content"][0]["text"]  # noqa: E731
    outside = srv.call_tool("form_guard_scan", {"form_path": str(paths(samples_dir, "form1")["form"])})
    assert outside["isError"] and "inside the server root" in text(outside)
    assert "inside the server root" in text(srv.call_tool("form_guard_scan", {"form_path": "../outside.docx"}))
    assert "missing required argument" in text(srv.call_tool("form_guard_verify", {"original_path": "x.docx"}))
    form2, memo2 = "samples/blank_forms/form2_municipal_equipment_subsidy.docx", "samples/memos/memo2_subsidy_application.txt"
    attested = srv.call_tool("form_guard_fill", {"form_path": form2, "memo_path": memo2, "overrides": {"法人番号": "1234567890123"}})
    assert attested["isError"] and "unknown argument(s) for form_guard_fill: overrides" in text(attested)  # no reviewer values via MCP
    form4, memo4 = "samples/blank_forms/form4_unmarked_blank.docx", "samples/memos/memo4_equipment_use.txt"
    disc = srv.call_tool("form_guard_discover_blank_cells", {"form_path": form4})["structuredContent"]
    office = [c["candidate_id"] for c in disc["candidates"] if c["office_only"]]
    refused = srv.call_tool("form_guard_fill", {"form_path": form4, "memo_path": memo4, "discovery": disc,
                                                "confirmed_ids": office[:1], "mode": "demo"})
    assert refused["isError"] and "office/administrative use only" in text(refused)
    zero = srv.call_tool("form_guard_fill", {"form_path": form4, "memo_path": memo4, "discovery": disc, "confirmed_ids": [],
                                             "mode": "demo"})
    assert "no confirmed fillable slots" in text(zero)
    assert not (root / "output" / "mcp_runs").exists()  # refused calls write nothing
    f2 = srv.call_tool("form_guard_fill", {"form_path": form2, "memo_path": memo2, "mode": "demo"})["structuredContent"]
    assert f2["approval_status"] == APPROVAL_STATUS and f2["verification"]["overall_status"] == "NEEDS_HUMAN_INPUT"
    assert f2["verification"]["pending_slots"] == ["法人番号"]  # missing fact stays for the human, even through MCP
    status = json.loads((root / f2["out_dir"] / "APPROVAL_STATUS.json").read_text(encoding="utf-8"))
    assert status["approval_status"] == APPROVAL_STATUS and status["filled_sha256"] == f2["filled_sha256"]


def test_p01_fastmcp_integration_is_optional(monkeypatch, tmp_path):
    from form_guard.mcp_server import FormGuardMCP, build_fastmcp

    monkeypatch.setitem(sys.modules, "mcp", None)  # the optional SDK is not installed
    with pytest.raises(RuntimeError, match="run the built-in server instead"):
        build_fastmcp(FormGuardMCP(root=tmp_path))


# --------------------------------------------------- P0-2 unmarked blank forms


def test_p02_unmarked_samples_are_marker_free_fictional_and_non_medical(samples_dir):
    banned = ["病院", "患者", "診断", "診療", "医療", "処方", "看護", "病名", "治療"]
    for uid in ("form4", "form5"):
        p = unmarked(samples_dir, uid)
        sc = scan(p["form"])
        assert sc.slots == [] and "架空" in sc.doc_text, uid
        assert not any(find_markers(idx.container_text(c)) for idx in sc.parts.values() for c in idx.containers)
        assert not p["form"].with_suffix(".slots.json").exists()
        memo = read_memo(p["memo"])
        assert not [w for w in banned if w in memo + sc.doc_text]
        for d in find_dates(memo):
            assert d.weekday and "月火水木金土日"[date(d.year or 2026, d.month, d.day).weekday()] == d.weekday


def test_p02_discover_blank_cells_on_the_unmarked_docx(samples_dir):
    form = unmarked(samples_dir, "form4")["form"]
    d = discover_blank_cells(form)
    assert d["form_sha256"] == hashlib.sha256(form.read_bytes()).hexdigest() and d["requires_human_confirmation"]
    by = {c["slot_id"]: c for c in d["candidates"]}
    assert [c["slot_id"] for c in d["candidates"] if c["recommended"]] == [
        "申請日", "申請者氏名", "所属機関", "利用目的", "利用希望期間", "研究代表者_氏名", "事務担当者_氏名"]
    office = [c["slot_id"] for c in d["candidates"] if c["office_only"]]
    assert office == ["受付番号", "受付日", "事務局受付欄", "審査欄"]
    for sid in office:
        assert not by[sid]["recommended"] and not by[sid]["applicant_fillable"]
        assert by[sid]["reason"] == "office/administrative use only — excluded from applicant fill"
    assert len(by) == len(d["candidates"]) == 11  # slot ids are unique
    assert by["研究代表者_氏名"]["group_label"] == "研究代表者" and by["事務担当者_氏名"]["evidence_label"] == "氏名"
    assert (by["申請日"]["table"], by["申請日"]["row"], by["申請日"]["col"]) == (0, 1, 2)
    assert by["申請日"]["required_format"] == "wareki_date" and by["利用希望期間"]["required_format"] == "wareki_period"
    assert by["利用目的"]["max_chars"] == 100 and by["申請者氏名"]["required_format"] == "person_name"
    assert by["事務局受付欄"]["label_direction"] == "above"  # the stamp cell sits under its printed header
    assert "使用施設・備品" not in by  # pre-printed value: not empty, never a candidate
    (rej,) = d["rejected"]
    assert "vertically merged continuation" in rej["reason"] and (rej["row"], rej["col"]) == (14, 3)


def test_p02_discover_blank_cells_on_the_unmarked_xlsx(samples_dir):
    d = discover_blank_cells(unmarked(samples_dir, "form5")["form"])
    cells = {c["cell"]: c for c in d["candidates"]}
    assert [(c["cell"], c["slot_id"]) for c in d["candidates"] if c["recommended"]] == [
        ("C5", "申請日"), ("C6", "申請団体名"), ("C7", "代表者氏名"), ("C8", "借用希望物品"), ("C9", "使用予定金額")]
    stamp = cells["C18"]
    assert stamp["office_only"] and not stamp["recommended"] and stamp["slot_id"] == "事務局使用欄"
    assert {"事務局", "受付番号", "決裁"} <= set(stamp["office_keywords"])
    assert cells["C9"]["required_format"] == "yen" and cells["C8"]["max_chars"] == 30 and cells["C5"]["merged_range"] == "C5:H5"
    rejected = {r["cell"]: r["reason"] for r in d["rejected"]}
    assert rejected["E16"].startswith("formula cell (=SUM(E13:E15))")
    assert rejected["C19"].startswith("merged continuation of C18:H19")
    assert len(rejected) == 2 and not set(rejected) & set(cells)
    assert d["stages"][2] == {"stage": 3, "name": "deterministic safety verification", "passed": 6, "rejected": 2}


@pytest.mark.parametrize("uid", ["form4", "form5"])
def test_p02_confirmed_cells_of_unmarked_forms_are_filled_surgically_and_pass(samples_dir, tmp_path, uid):
    p = unmarked(samples_dir, uid)
    d = discover_blank_cells(p["form"])
    rec = [c for c in d["candidates"] if c["recommended"]]
    spec = build_confirmed_slot_spec(d, [c["candidate_id"] for c in rec], p["form"])
    assert spec["form_sha256"] == d["form_sha256"] and [t["slot_id"] for t in spec["targets"]] == [c["slot_id"] for c in rec]
    r = run_pipeline(p["form"], read_memo(p["memo"]), tmp_path / uid, mode="demo", slot_spec=spec)
    rep = r.report
    assert rep.overall_status == "PASS" and rep.passed_count == 21 and rep.failed_count == 0, \
        [(c.check_id, c.details) for c in rep.checks if not c.passed]
    assert rep.ungrounded_claims_count == 0 and all(m.status == "filled" for m in r.mappings)
    assert r.visual["identical_outside_slots"] and not _cell_issues(rep)
    office_ids = {c["slot_id"] for c in d["candidates"] if c["office_only"]}
    assert office_ids and not office_ids & {pr.slot_id for pr in r.write.patches}  # 0 office-only cells written
    if uid == "form4":
        t = docx.Document(r.filled_path).tables[0]
        assert t.cell(2, 2).text == "架空 一郎" and t.cell(9, 2).text == "架空 一郎" and t.cell(10, 2).text == "模擬 花子"
        assert t.cell(7, 2).text == "令和8年11月16日（月）～令和8年11月27日（金）"
        assert [t.cell(r_, 2).text for r_ in (12, 13, 14)] == ["", "", ""] and t.cell(13, 3).text == ""
        assert {pr.kind for pr in r.write.patches} == {"expand_empty"} and r.write.modified_parts == ["word/document.xml"]
    else:
        ws = openpyxl.load_workbook(r.filled_path).active
        assert ws["C9"].value == "48,000円" and ws["C8"].value == "簡易テント2張、発電機1台、投光器2台"
        assert ws["C18"].value is None and ws["E16"].value == "=SUM(E13:E15)"
        style = lambda f, ref: re.search(rf'<c r="{ref}" s="(\d+)"'.encode(), zipfile.ZipFile(f).read("xl/worksheets/sheet1.xml")).group(1)  # noqa: E731
        assert all(style(p["form"], c) == style(r.filled_path, c) for c in ("C5", "C6", "C7", "C8", "C9"))
        assert {pr.kind for pr in r.write.patches} == {"fill_cell"} and r.write.modified_parts == ["xl/worksheets/sheet1.xml"]


def test_p02_human_gate_refuses_changed_forms_and_zero_or_office_only_confirmations(samples_dir, tmp_path):
    p = unmarked(samples_dir, "form4")
    d = discover_blank_cells(p["form"])
    rec = [c["candidate_id"] for c in d["candidates"] if c["recommended"]]
    office = [c["candidate_id"] for c in d["candidates"] if c["office_only"]]
    with pytest.raises(ValueError, match="no confirmed fillable slots — at least 1 slot must be confirmed by the human reviewer"):
        build_confirmed_slot_spec(d, [], p["form"])
    with pytest.raises(ValueError, match="office/administrative use only — excluded from applicant fill"):
        build_confirmed_slot_spec(d, rec + office[:1], p["form"])
    assert build_confirmed_slot_spec(d, office[:1], p["form"], allow_office_only=True)["office_only_override"]
    with pytest.raises(ValueError, match="unknown candidate id"):
        build_confirmed_slot_spec(d, ["c99"], p["form"])
    changed = tmp_path / "form4_changed.docx"
    surgical_rewrite(p["form"], changed, "word/document.xml", lambda b: b.replace("使用施設・備品".encode(), "使用施設・設備".encode()))
    with pytest.raises(ValueError, match="blank form changed after discovery — re-run discovery"):
        build_confirmed_slot_spec(d, rec, changed)
    spec = build_confirmed_slot_spec(d, rec, p["form"])
    out = tmp_path / "never"
    with pytest.raises(ValueError, match="blank form changed after discovery"):  # a stale spec is refused before any fill
        run_pipeline(changed, read_memo(p["memo"]), out, mode="demo", slot_spec=spec)
    assert not list(out.glob("filled_*"))


def test_p02_column_header_tables_and_duplicate_labels_get_unique_slot_ids(tmp_path):
    from openpyxl.styles import Border, Side

    src = tmp_path / "items.docx"
    d = docx.Document()
    d.add_paragraph("架空の物品リスト（テスト）")
    t = d.add_table(rows=3, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "品名", "数量"
    d.save(src)
    disc = discover_blank_cells(src)
    assert [(c["slot_id"], c["label_direction"], c["row"], c["col"]) for c in disc["candidates"]] == [
        ("品名_1", "above", 1, 0), ("数量_1", "above", 1, 1), ("品名_2", "above", 2, 0), ("数量_2", "above", 2, 1)]
    xl = tmp_path / "items.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"], ws["B1"] = "物品", "数量"
    for ref in ("A2", "B2", "A3", "B3"):
        ws[ref].border = Border(bottom=Side(style="thin"))
    wb.save(xl)
    assert [(c["slot_id"], c["cell"]) for c in discover_blank_cells(xl)["candidates"]] == [
        ("物品_1", "A2"), ("数量_1", "B2"), ("物品_2", "A3"), ("数量_2", "B3")]


def test_p02_cli_discover_confirm_fill_and_verify(samples_dir, tmp_path, capsys):
    from form_guard.cli import main

    p = unmarked(samples_dir, "form5")
    assert main(["--scan", str(p["form"])]) == 0 and "--discover-blanks" in capsys.readouterr().out
    assert main(["--discover-blanks", str(p["form"]), "--mode", "demo", "--out-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "c06 ✗ 事務局使用欄" in out and "office/administrative use only" in out and "rejected:" in out
    disc = tmp_path / "form5_unmarked_blank.discovery.json"
    assert json.loads(disc.read_text(encoding="utf-8"))["form_sha256"] == hashlib.sha256(p["form"].read_bytes()).hexdigest()
    run_dir = tmp_path / "run"
    args = ["--form", str(p["form"]), "--memo", str(p["memo"]), "--out-dir", str(run_dir), "--mode", "demo",
            "--discovery", str(disc)]
    assert main(args + ["--confirm", "recommended"]) == 0
    out = capsys.readouterr().out
    assert "Confirmed 5 cell(s)" in out and "PASS · 5/5 fields filled · 21/21 checks passed" in out
    spec = run_dir / "form5_unmarked_blank.confirmed.slots.json"
    filled = run_dir / "filled_form5_unmarked_blank.xlsx"
    assert main(["--verify", str(p["form"]), str(filled), "--slot-spec", str(spec)]) == 0
    assert "audit_log.json (SHA-256 matches the filled file)" in capsys.readouterr().out
    assert main(args + ["--confirm", "c06"]) == 2
    assert "refused before filling" in capsys.readouterr().err


def test_p02_api_discover_then_upload_confirmed_targets(api, samples_dir, tmp_path):
    p = unmarked(samples_dir, "form4")
    blob = p["form"].read_bytes()
    res = api.post("/api/discover", files={"file": ("研究設備 申請書.docx", blob)}, data={"mode": "demo"})
    assert res.status_code == 200
    d = res.json()
    assert re.fullmatch(r"[0-9a-f]{24}", d["discovery_id"]) and d["form"] == "研究設備 申請書.docx"
    rec = [c["candidate_id"] for c in d["candidates"] if c["recommended"]]
    office = [c["candidate_id"] for c in d["candidates"] if c["office_only"]]

    def upload(targets, data=blob, discovery_id=d["discovery_id"]):
        return api.post("/api/runs/upload", files={"file": ("研究設備 申請書.docx", data)},
                        data={"memo_text": read_memo(p["memo"]), "mode": "demo", "discovery_id": discovery_id,
                              "confirmed_targets": targets})

    ok = upload(json.dumps(rec))
    assert ok.status_code == 200 and ok.json()["report"]["overall_status"] == "PASS" and ok.json()["can_approve"]
    assert [m["slot_id"] for m in ok.json()["mappings"]] == [c["slot_id"] for c in d["candidates"] if c["recommended"]]
    two = upload(",".join(rec[:2]))
    assert two.status_code == 200 and len(two.json()["mappings"]) == 2
    assert upload("[]").status_code == 422 and "no confirmed fillable slots" in upload("[]").json()["detail"]
    office_res = upload(json.dumps(rec + office[:1]))
    assert office_res.status_code == 422 and "office/administrative use only" in office_res.json()["detail"]
    changed = tmp_path / "changed.docx"
    surgical_rewrite(p["form"], changed, "word/document.xml", lambda b: b.replace("使用施設・備品".encode(), "使用施設・設備".encode()))
    stale = upload(json.dumps(rec), data=changed.read_bytes())
    assert stale.status_code == 422 and "blank form changed after discovery" in stale.json()["detail"]
    assert upload(json.dumps(rec), discovery_id="0" * 24).status_code == 404
    assert api.post("/api/discover", files={"file": ("x.docx", blob)}, data={"model_tier": "gpt"}).status_code == 400
    health = api.get("/api/health").json()
    assert health["models"] == {"opus": MODEL_OPUS, "sonnet": MODEL_SONNET, "haiku": MODEL_HAIKU}
    assert health["model_tiers"] == ["auto", "opus", "sonnet", "haiku"]


# -------------------------------------------- P0-1 docs and Claude Code assets


def test_p01_built_with_claude_code_doc_is_bilingual_and_complete():
    doc = (ROOT / "docs" / "BUILT_WITH_CLAUDE_CODE.md").read_text(encoding="utf-8")
    for needle in (".claude/skills/form-scan/SKILL.md", ".claude/agents/memo-extractor.md", ".claude/agents/form-auditor.md",
                   ".claude/settings.json", ".mcp.json", "form_guard/mcp_server.py", MODEL_HAIKU, MODEL_SONNET, MODEL_OPUS,
                   "## 3. Measured workflow comparison", "python docs/measure_workflow.py", "UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF",
                   "(development started Monday,\n2026-11-02 / 2026-11-02（月）)", "## 日本語", "### 3. 作業量の比較（計測）",
                   "not** a timed study", "人の作業時間を計ったものではありません"):
        assert needle in doc, needle
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/BUILT_WITH_CLAUDE_CODE.md" in readme and "--discover-blanks" in readme and "--model-tier" in readme


def test_p01_human_facing_dates_in_readme_and_docs_carry_the_weekday():
    for name in ("README.md", "docs/BUILT_WITH_CLAUDE_CODE.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        found = 0
        for m in re.finditer(r"(?<![\w-])(\d{4})-(\d{2})-(\d{2})(?![\w-])", text):
            d = date(*map(int, m.groups()))
            ja, en = "月火水木金土日"[d.weekday()], d.strftime("%A")
            # 2026-11-02（月）, or the English form "Monday, 2026-11-02"
            assert text[m.end() : m.end() + 3] == f"（{ja}）" or text[: m.start()].endswith(f"{en}, ") or \
                text[: m.start()].endswith(f"{en},\n"), (name, text[m.start() - 20 : m.end() + 5])
            found += 1
        for m in re.finditer(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text):
            wd = "月火水木金土日"[date(*map(int, m.groups())).weekday()]
            assert text[m.end() : m.end() + 3] == f"（{wd}）", (name, text[m.start() - 20 : m.end() + 5])
            found += 1
        assert found, name


def test_p01_measured_workflow_script_reproduces_the_table(samples_dir, tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("measure_workflow", ROOT / "docs" / "measure_workflow.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rows = mod.measure(samples_dir, tmp_path / "measure")
    assert [r["form"] for r in rows] == ["form1", "form2", "form3", "form4", "form5"]
    assert all(r["passed"] == r["total"] == 21 for r in rows)
    assert [r["status"] for r in rows] == ["PASS", "NEEDS_HUMAN_INPUT", "PASS", "PASS", "PASS"]
    assert sum(r["written"] for r in rows) == 40 and sum(r["chars"] for r in rows) == 844
    table = mod.markdown(rows)
    doc = (ROOT / "docs" / "BUILT_WITH_CLAUDE_CODE.md").read_text(encoding="utf-8")
    for line in table.splitlines()[2:]:  # the counted columns in the doc match a fresh run (timings may vary)
        assert re.sub(r"\| [\d,]+ ms \|", "| ms |", line) in re.sub(r"\| [\d,]+ ms \|", "| ms |", doc), line


# ===================================================================== Phase 3
# P3-1 open-source licence, P3-2 web discovery of unmarked forms (built-in + upload), P3-3 per-field model
# routing on every mapping and the Claude Code suite endpoint, P3-4 bilingual UI + judge walkthrough, P3-5 docs.


def test_p3_1_mit_license_file_and_package_metadata():
    lic = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert lic.startswith("MIT License\n\nCopyright (c) 2026 Masaki Miura\n\nPermission is hereby granted, free of charge")
    assert 'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND' in lic
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^license = \{ text = "MIT" \}$', pyproject, re.M)
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10: the regex above already checked the line
        tomllib = None
    if tomllib is not None:
        assert tomllib.loads(pyproject)["project"]["license"] == {"text": "MIT"}
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "License: MIT (see [LICENSE](LICENSE))." in readme and "ライセンス：MIT（[LICENSE](LICENSE) を参照）。" in readme
    assert "to be decided" not in readme


def test_p3_2_api_lists_the_unmarked_demo_forms(api, samples_dir):
    demos = api.get("/api/unmarked-demos").json()
    assert [(d["id"], d["file_type"], d["form"], d["memo_name"]) for d in demos] == [
        ("form4", "docx", "form4_unmarked_blank.docx", "memo4_equipment_use.txt"),
        ("form5", "xlsx", "form5_unmarked_blank.xlsx", "memo5_stockpile_loan.txt")]
    for d in demos:
        assert set(d) == {"id", "title", "title_en", "org", "file_type", "form", "memo_name", "memo_text", "highlights"}
        assert d["memo_text"] == read_memo(unmarked(samples_dir, d["id"])["memo"]) and len(d["highlights"]) >= 3
        assert "架空" in d["org"] and d["title"] == UNMARKED[d["id"]]["title"]


@pytest.mark.parametrize("uid,office", [("form4", ["受付番号", "受付日", "事務局受付欄", "審査欄"]), ("form5", ["事務局使用欄"])])
def test_p3_2_discover_a_builtin_unmarked_form_then_fill_only_the_confirmed_cells(api, samples_dir, uid, office):
    p = unmarked(samples_dir, uid)
    res = api.post("/api/discover/demo", json={"demo_id": uid, "mode": "demo", "model_tier": "auto"})
    assert res.status_code == 200, res.text
    d = res.json()
    assert re.fullmatch(r"[0-9a-f]{24}", d["discovery_id"]) and d["demo_id"] == uid and d["requires_human_confirmation"]
    assert d["form_sha256"] == hashlib.sha256(p["form"].read_bytes()).hexdigest()
    assert (d["title"], d["title_en"], d["org"]) == (UNMARKED[uid]["title"], UNMARKED[uid]["title_en"], UNMARKED[uid]["org"])
    assert d["memo_text"] == read_memo(p["memo"]) and d["stages"][3]["status"] == "awaiting human confirmation"
    assert [c["slot_id"] for c in d["candidates"] if c["office_only"]] == office
    rec = [c for c in d["candidates"] if c["recommended"]]
    assert rec and not any(c["office_only"] for c in rec)
    res = api.post("/api/runs/from-discovery", json={"discovery_id": d["discovery_id"], "confirmed_targets": "recommended",
                                                     "mode": "demo"})
    assert res.status_code == 200, res.text
    r = res.json()
    assert r["report"]["overall_status"] == "PASS" and r["report"]["passed_count"] == 21 and r["can_approve"]
    assert [m["slot_id"] for m in r["mappings"]] == [c["slot_id"] for c in rec]
    assert r["confirmed_candidates"] == [c["candidate_id"] for c in rec] and r["demo_id"] == uid
    assert r["display_name"] == p["form"].name and "naive" not in r["previews"]
    assert not {pr["slot_id"] for pr in r["patches"]} & set(office)  # 0 office-only cells written
    assert all(m["routed_model"].startswith("offline (claude-") and m["route_reason"] for m in r["mappings"])
    assert r["is_builtin_demo"] and "100% fictional" in api.get(r["links"]["report"]).text
    assert api.get(r["links"]["download"]).status_code == 403  # the human gate still applies
    required = [i["item_id"] for i in r["report"]["hitl_checklist"] if i["required"]]
    ok = api.post(f"/api/runs/{r['run_id']}/approve", json={"reviewer": "架空 花子", "checked": required,
                                                            "filled_sha256": r["filled"]["sha256"], "revision": r["revision"]})
    assert ok.status_code == 200, ok.text
    dl = api.get(r["links"]["download"])
    assert dl.status_code == 200 and hashlib.sha256(dl.content).hexdigest() == r["filled"]["sha256"]
    edited = api.post("/api/runs/from-discovery", json={"discovery_id": d["discovery_id"], "confirmed_targets": "recommended",
                                                        "mode": "demo", "memo_text": d["memo_text"] + "追記：私的なメモ\n"}).json()
    assert not edited["is_builtin_demo"]  # an edited memo is no longer the fictional built-in demo


def test_p3_2_from_discovery_refuses_office_only_tampered_hash_empty_and_client_defined_cells(api):
    d = api.post("/api/discover/demo", json={"demo_id": "form4", "mode": "demo"}).json()
    did = d["discovery_id"]
    rec = [c["candidate_id"] for c in d["candidates"] if c["recommended"]]
    office = [c["candidate_id"] for c in d["candidates"] if c["office_only"]]
    runs_before = len(api.app.state.store.runs)

    def fill(**body):
        return api.post("/api/runs/from-discovery", json={"discovery_id": did, "mode": "demo", **body})

    for empty in ([], "", "[]"):
        res = fill(confirmed_targets=empty)
        assert res.status_code == 422 and "no confirmed fillable slots" in res.json()["detail"], empty
    for targets in (rec + office[:1], ",".join(office), office[-1:]):
        res = fill(confirmed_targets=targets)
        assert res.status_code == 422 and "office/administrative use only" in res.json()["detail"], targets
    unknown = fill(confirmed_targets=["c01", "c99"])
    assert unknown.status_code == 422 and "unknown candidate id" in unknown.json()["detail"]
    # the client only names candidate ids: a client-side discovery, form hash or cell list is refused outright
    for extra in ({"discovery": d}, {"form_sha256": d["form_sha256"]},
                  {"targets": [{"slot_id": "受付番号", "table": 0, "row": 12, "col": 2}]}):
        assert fill(confirmed_targets=rec, **extra).status_code == 422, extra
    assert api.post("/api/runs/from-discovery", json={"discovery_id": "0" * 24, "confirmed_targets": rec}).status_code == 404
    assert api.post("/api/runs/from-discovery", json={"discovery_id": "../etc", "confirmed_targets": rec}).status_code == 404
    assert fill(confirmed_targets=rec, model_tier="gpt").status_code == 422
    assert fill(confirmed_targets=rec, memo_text="  \n").status_code == 400
    # tampered SHA-256: the stored discovery no longer matches the blank form, so nothing is filled
    entry = api.app.state.discoveries.items[did]
    entry["discovery"] = {**entry["discovery"], "form_sha256": "0" * 64}
    stale = fill(confirmed_targets=rec)
    assert stale.status_code == 422 and "blank form changed after discovery — re-run discovery" in stale.json()["detail"]
    assert len(api.app.state.store.runs) == runs_before  # no refused request produced a run
    assert api.post("/api/discover/demo", json={"demo_id": "form1"}).status_code == 404  # a marked demo is not unmarked
    assert api.post("/api/discover/demo", json={"demo_id": "form4", "model_tier": "gpt"}).status_code == 422


def test_p3_2_uploaded_unmarked_form_round_trip_and_tampered_staged_file(api, samples_dir):
    p = unmarked(samples_dir, "form5")
    blob, memo = p["form"].read_bytes(), read_memo(p["memo"])
    d = api.post("/api/discover", files={"file": ("備蓄品 申請.xlsx", blob)}, data={"mode": "demo", "model_tier": "auto"}).json()
    rec = [c["candidate_id"] for c in d["candidates"] if c["recommended"]]
    body = {"discovery_id": d["discovery_id"], "confirmed_targets": rec[:3], "mode": "demo"}
    no_memo = api.post("/api/runs/from-discovery", json=body)
    assert no_memo.status_code == 400 and "memo_text is required" in no_memo.json()["detail"]  # uploads store no memo
    ok = api.post("/api/runs/from-discovery", json={**body, "memo_text": memo}).json()
    assert ok["report"]["overall_status"] == "PASS" and [m["slot_id"] for m in ok["mappings"]] == ["申請日", "申請団体名", "代表者氏名"]
    assert ok["display_name"] == "備蓄品 申請.xlsx" and ok["demo_id"] is None and not ok["is_builtin_demo"]
    staged = api.app.state.discoveries.items[d["discovery_id"]]["path"]
    staged.write_bytes(blob + b"\0")  # the staged blank form changed on disk after discovery
    stale = api.post("/api/runs/from-discovery", json={**body, "memo_text": memo})
    assert stale.status_code == 422 and "blank form changed after discovery" in stale.json()["detail"]
    d2 = api.post("/api/discover", files={"file": ("x.xlsx", blob)}, data={"mode": "demo"}).json()
    multipart = api.post("/api/runs/upload", files={"file": ("x.xlsx", blob)},
                         data={"memo_text": memo, "mode": "demo", "discovery_id": d2["discovery_id"],
                               "confirmed_targets": "recommended", "model_tier": "auto"}).json()
    assert multipart["report"]["overall_status"] == "PASS" and len(multipart["mappings"]) == len(rec)
    for raw in (b'"samples/blank_forms/form1_research_plan_change.slots.json"', b"[]", b"7"):  # only an object is a spec
        bad = api.post("/api/runs/upload", files={"file": ("x.xlsx", blob), "slot_spec": ("s.json", raw)},
                       data={"memo_text": memo, "mode": "demo"})
        assert bad.status_code == 400 and bad.json()["detail"] == "slot spec must be a JSON object", raw


def test_p3_3_offline_runs_show_the_live_auto_routing_plan_on_every_field(demo_runs, samples_dir, tmp_path, capsys):
    r = demo_runs("form1")
    for m in r.mappings:
        tier, why = route_slot(r.scan.slot(m.slot_id), r.scan.slots, r.memo_text)
        assert (m.routed_model, m.route_reason) == (f"offline ({TIER_MODELS[tier]} in live auto)", why), m.slot_id
    by = {m.slot_id: m for m in r.mappings}
    assert by["提出日"].routed_model == f"offline ({MODEL_HAIKU} in live auto)"
    assert by["研究代表者氏名"].routed_model == f"offline ({MODEL_SONNET} in live auto)"
    assert by["変更理由および予算影響"].routed_model == f"offline ({MODEL_OPUS} in live auto)"
    routing = r.engine["usage"]["routing"]
    assert [(g["task"], g["model"], g["planned_in_live_auto"]) for g in routing] == [
        ("propose", MODEL_HAIKU, True), ("propose", MODEL_SONNET, True), ("propose", MODEL_OPUS, True)]
    assert sorted(s for g in routing for s in g["slots"]) == sorted(s.slot_id for s in r.scan.slots)
    assert r.engine["mode"] == "demo" and "requests" not in r.engine["usage"]  # no API call is claimed offline
    audit = json.loads((r.out_dir / "audit_log.json").read_text(encoding="utf-8"))
    assert all(m["routed_model"] and m["route_reason"] for m in audit["mappings"])
    html = (r.out_dir / "report.html").read_text(encoding="utf-8")
    assert "Planned model routing (live auto mode)" in html and "no model was called" in html
    assert "<h2>Model routing / モデルの使い分け</h2>" not in html
    from form_guard.cli import main

    p = paths(samples_dir, "form1")
    assert main(["--form", str(p["form"]), "--memo", str(p["memo"]), "--out-dir", str(tmp_path / "cli"), "--mode", "demo"]) == 0
    assert f"· planned in live auto (offline: no call) · propose → {MODEL_HAIKU}: 提出日" in capsys.readouterr().out
    # a field the validator still rejects would go to Opus for self-correction in live auto mode: the plan says so
    slot = _text_slot("所見", max_chars=4)
    run = map_fields([slot], "所見：今日は晴れです。明日は雨の予報です。\n", DemoEngine())
    (m,) = run.mappings
    assert m.status == "needs_human_input" and m.routed_model == f"offline ({MODEL_OPUS} → {MODEL_OPUS} (revision) in live auto)"
    assert m.route_reason.endswith("(condensation); then self-correction after validator rejection")
    assert run.usage["routing"][-1] == {"task": "revise", "model": MODEL_OPUS, "slots": ["所見"], "planned_in_live_auto": True,
                                        "reasons": {"所見": "self-correction after validator rejection"}}


def test_p3_3_live_mappings_record_the_served_model_revisions_and_thinking(samples_dir, fake_client_factory, tmp_path):
    from form_guard.mcp_server import FormGuardMCP

    p = paths(samples_dir, "form1")
    memo = read_memo(p["memo"])
    client = fake_client_factory([echo_model(_claude_fields(), thinking="read the numbered memo")] * 3
                                 + [echo_model(_revision_fix(), thinking="re-quoted line 8")])
    r = run_pipeline(p["form"], memo, tmp_path / "live", mode="live", client=client)
    by = {m.slot_id: m for m in r.mappings}
    assert (by["提出日"].routed_model, by["提出日"].route_reason) == (MODEL_HAIKU, "structured field (wareki_date)")
    assert (by["研究代表者氏名"].routed_model, by["研究代表者氏名"].route_reason) == (MODEL_SONNET, "standard person_name field")
    assert by["変更理由および予算影響"].routed_model == MODEL_OPUS and "(condensation)" in by["変更理由および予算影響"].route_reason
    assert by["所属部局"].routed_model == f"{MODEL_SONNET} → {MODEL_OPUS} (revision)"  # rejected 「工学部」, fixed by Opus
    assert by["所属部局"].route_reason == "standard text field; then self-correction after validator rejection"
    assert all(m.status == "filled" for m in r.mappings) and r.report.overall_status == "PASS"
    calls = r.engine["usage"]["calls"]
    assert [(c["task"], c["requested_model"]) for c in calls][-1] == ("revise", MODEL_OPUS) and calls[-1]["slots"] == ["所属部局"]
    thinking = {t["text"]: t for t in r.engine["usage"]["thinking_summaries"]}
    assert thinking["re-quoted line 8"]["slots"] == ["所属部局"] and thinking["re-quoted line 8"]["model"] == MODEL_OPUS
    assert sorted(s for t in r.engine["usage"]["thinking_summaries"][:3] for s in t["slots"]) == sorted(by)
    audit = json.loads((tmp_path / "live" / "audit_log.json").read_text(encoding="utf-8"))
    assert {m["slot_id"]: m["routed_model"] for m in audit["mappings"]}["所属部局"] == f"{MODEL_SONNET} → {MODEL_OPUS} (revision)"
    # the served model id is what counts: a server-side fallback to another model is recorded as such
    fields = _claude_fields({"所属部局": ("理工学研究科", ["・所属部局：理工学研究科"])})
    fallback = fake_client_factory([lambda kw: fake_message(fields, model=MODEL_OPUS)])
    run = map_fields(scan(p["form"]).slots, memo, ClaudeEngine(client=fallback, model_tier="haiku"))
    assert {(m.routed_model, m.route_reason) for m in run.mappings} == {
        (MODEL_OPUS, f"model tier 'haiku' (served by {MODEL_OPUS}; requested {MODEL_HAIKU})")}
    # reviewer entries and engines without routing are labelled too: every mapping says who produced the value
    p2 = paths(samples_dir, "form2")
    r2 = run_pipeline(p2["form"], read_memo(p2["memo"]), tmp_path / "human", mode="demo", overrides={"法人番号": "1234567890123"})
    hm = {m.slot_id: m for m in r2.mappings}["法人番号"]
    assert hm.status == "human_entered" and (hm.routed_model, hm.route_reason) == HUMAN_ROUTE
    fixed = map_fields([_text_slot("備考")], "備考：特になし\n", FixedEngine({"備考": Draft("備考", "filled", "特になし", ["特になし"], 0.9)}))
    assert (fixed.mappings[0].routed_model, fixed.mappings[0].route_reason) == ("fixed", "this engine has no per-field model routing")
    srv = FormGuardMCP(root=_mcp_root(tmp_path, samples_dir))
    mcp_fill = srv.call_tool("form_guard_fill", {"form_path": "samples/blank_forms/form1_research_plan_change.docx",
                                                 "memo_path": "samples/memos/memo1_research_change.txt", "mode": "demo"})
    assert all(f["routed_model"].startswith("offline (claude-") and f["route_reason"]
               for f in mcp_fill["structuredContent"]["fields"])


def test_p3_3_claude_suite_endpoint_reads_the_repository_assets(api, tmp_path):
    from form_guard.claude_suite import describe_suite
    from form_guard.mcp_server import TOOL_NAMES

    s = api.get("/api/claude-suite").json()
    assert [x["name"] for x in s["skills"]] == list(sorted(SKILLS))
    for x in s["skills"] + s["agents"]:
        assert x["description"] == frontmatter(ROOT / x["path"])[0]["description"], x["path"]  # read from the files
    agents = {a["name"]: a for a in s["agents"]}
    assert {name: (a["model"], a["tools"]) for name, a in agents.items()} == {k: (m, t) for k, (m, t) in AGENTS.items()}
    pre, post = s["settings"]["hooks"]
    assert (pre["event"], pre["stage"], pre["runner"], pre["script"], pre["timeout"]) == (
        "PreToolUse", "pre-tool-use", "python", "scripts/hook_guard.py", 10)
    assert set(pre["matcher"].split("|")) == {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "PowerShell"}
    assert [c["en"].split(" (")[0].split(":")[0] for c in pre["checks"]] == [
        "blank-template guard", "Anthropic API key pattern", "literal API key assignment", "local user-profile paths",
        "e-mail addresses outside the reserved example domains"]
    assert (post["event"], post["stage"], post["matcher"]) == ("PostToolUse", "post-tool-use", "Write|Edit|MultiEdit")
    assert [c["en"].split(" (")[0] for c in post["checks"]] == [
        "Python syntax", "JSON validity", "hygiene re-scan of the written file"]
    assert pre["feeds_back_to_claude"] and post["feeds_back_to_claude"] and not s["settings"]["bypass_permissions"]
    assert "exit code 2" in pre["when"]["en"] and "decision block" in post["when"]["en"]
    assert s["settings"]["deny"] == ["Read(./.env)", "Read(./.env.*)"]
    assert s["settings"]["env"] == {"CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR": "1"}
    assert s["mcp"]["servers"] == [{"name": "form-guard", "type": "stdio", "command": "python",
                                    "args": ["-m", "form_guard.mcp_server"]}]
    assert [t["name"] for t in s["mcp"]["tools"]] == TOOL_NAMES
    assert [f["step"] for f in s["flow"]] == [1, 2, 3, 4, 5] and all(u["present"] for f in s["flow"] for u in f["uses"])
    assert {p["task"]: p["model"] for p in s["routing"]["policy"]} == {
        "structured": MODEL_HAIKU, "classify": MODEL_HAIKU, "standard": MODEL_SONNET, "long_text": MODEL_OPUS,
        "revise": MODEL_OPUS}
    assert all(p["ja"] and p["en"] and p["why_ja"] and p["why_en"] for p in s["routing"]["policy"])
    t = s["workflow"]["totals"]
    assert (t["forms"], t["fields"], t["written"], t["chars"], t["tags"], t["text_blocks"], t["merges"], t["borders"],
            t["hitl"]) == (5, 41, 40, 844, 2119, 171, 124, 650, 51)
    assert t["pending_slots"] == ["form2:法人番号"] and t["all_checks_passed"] and t["checks_per_form"] == [21]
    assert [row["form"] for row in s["workflow"]["rows"]] == ["form1", "form2", "form3", "form4", "form5"]
    assert api.get("/api/claude-suite").json()["workflow"] == s["workflow"]  # measured once per process
    bare = describe_suite(tmp_path)  # e.g. an installed package without the repository's .claude/ folder
    assert bare["skills"] == bare["agents"] == [] and not bare["settings"]["installed"] and not bare["mcp"]["installed"]
    assert not any(u["present"] for f in bare["flow"] for u in f["uses"] if u["kind"] != "human")


def test_p3_4_web_ui_ships_the_inspector_tier_selector_claude_tab_walkthrough_and_language_toggle(api):
    html, js, css = (api.get(u).text for u in ("/", "/static/app.js", "/static/styles.css"))
    for needle in ('<html lang="ja"', 'data-lang="ja"', 'id="langToggle"', 'data-lang="en"', 'id="tierSelect"',
                   '<option value="auto" data-t="自動：Haiku / Sonnet / Opus|Auto: Haiku / Sonnet / Opus">',
                   ">Opus 5.5 only<", ">Sonnet 5.5 only<", ">Haiku 5.5 only<",
                   'data-tab="claude"', 'data-bi="Claude Code 連携|Claude Code · Architecture"', 'id="tab-claude"',
                   'id="unmarkedList"', "マーカーなし白紙様式・空欄自動発見|Unmarked blank forms", 'id="discoverBtn"',
                   "🔍 マーカーなし白紙から空欄を自動発見|🔍 Discover blank cells", 'id="discoveryView"', 'id="candRows"',
                   'id="discMemo"', "✅ 検出した空欄を承認して外科的差込を実行|✅ Approve the detected cells &amp; run the surgical fill",
                   'id="whyBanner"', "なぜ Form-Guard か？（解決する現場課題）|Why Form-Guard?", 'id="discPreview"',
                   "4段階・白紙空欄の自動検出結果（申請者記入欄 vs 事務局専用欄の自動仕分け）",
                   "🎬 審査員向け 90秒ガイドツアー|🎬 Judge Quick Walkthrough", "誰の課題か|Whose problem",
                   "どんな課題か|What problem", "どう解決するか|How Form-Guard solves it"):
        assert needle in html, needle
    assert re.findall(r'data-tour="(\d)"', html) == ["1", "2", "3", "4", "5"]
    for label in ("課題と視覚比較（Visual Diff）", "マーカーなし白紙からの空欄発見", "原文根拠ハイライトと推測拒否",
                  "Claude 活用・モデル使い分け", "21項目構造証明＆人間承認ロック"):
        assert label in html, label
    for endpoint in ('"/api/demos"', '"/api/run-demo"', '"/api/runs/from-discovery"', '"/api/claude-suite"',
                     '"/api/discover"', '"/api/runs/upload"', '"/api/runs"'):
        assert endpoint in js, endpoint
    assert js.count("model_tier: tier()") == 3 and js.count('fd.append("model_tier", tier())') == 2  # every run/discovery
    assert "disabled: locked" in js and "if (locked) { cb.checked = false; return; }" in js  # office-only: never checkable
    assert 'B("事務局専用欄（除外）", "Office-only — locked")' in js and 'B("申請者記入欄", "Applicant field")' in js
    assert "h(\"td\", {}, modelBadge(m))" in js and "routeBlock(m)," in js and "thinking_summaries" in js
    assert 'window.addEventListener("hashchange", handleDeepLink)' in js
    assert 'html[data-lang="ja"] [data-l="en"]' in css and ".mb-plan" in css
    node = shutil.which("node")
    if node:  # syntax check when Node.js is available (it is not a project requirement)
        assert subprocess.run([node, "--check", str(ROOT / "form_guard" / "static" / "app.js")], capture_output=True,
                              timeout=60).returncode == 0


def test_p3_5_readme_and_docs_answer_impact_continued_use_and_match_the_measured_numbers(samples_dir, tmp_path):
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "BUILT_WITH_CLAUDE_CODE.md").read_text(encoding="utf-8")
    for needle in ("## Why this matters & Who keeps using it after the hackathon",
                   "### 誰のどんな課題を解決し、ハッカソン後にどこで使われ続けるか", "差し戻し", "Post-hackathon continued use",
                   "ハッカソン後の継続利用場面", "approval.json", "report.html", ".mcp.json", "--discover-blanks",
                   "POST /api/discover/demo", "POST /api/runs/from-discovery", "GET /api/claude-suite", "41 designated fields",
                   "844 characters", "2,119 XML tags", "171 printed text blocks", "124 merged cells/ranges",
                   "650 border definitions", "51-item", "法人番号", "routed_model"):
        assert needle in readme, needle
    for needle in ("form4", "form5", "routed_model", "GET /api/claude-suite", "Claude Code 連携 · Architecture",
                   "マーカーなし白紙様式・空欄自動発見"):
        assert needle in doc, needle
    rows = measure(samples_dir, tmp_path / "measure")
    for line in markdown(rows).splitlines()[2:]:  # the counted columns in the README match a fresh run
        assert re.sub(r"\| [\d,]+ ms \|", "| ms |", line) in re.sub(r"\| [\d,]+ ms \|", "| ms |", readme), line
    collected = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
                                str(ROOT / "tests")], capture_output=True, text=True, cwd=ROOT, timeout=300)
    n = int(re.search(r"(\d+) tests? collected", collected.stdout).group(1))
    assert f"pytest -v                                # {n} tests" in readme, n  # the README states the real count


# ============================================ Claude Code assets: skills, subagents, two-stage hooks, MCP

HOOK = ROOT / "scripts" / "hook_guard.py"
TEMPLATE = "samples/blank_forms/form1_research_plan_change.docx"
SKILLS = ("form-scan", "surgical-fill", "structural-audit", "form-fill-verify", "visual-diff-audit")
AGENTS = {"memo-extractor": ("sonnet", ["Read", "Grep", "Glob"]),
          "form-auditor": ("opus", ["Read", "Grep", "Glob", "Bash"]),
          "field-mapper": (MODEL_OPUS, ["Read", "Glob", "Grep", "Bash"]),
          "structure-auditor": ("sonnet", ["Read", "Glob", "Grep", "Bash"])}
_BS = chr(92)
_KEY = "sk-" + "ant-api03-" + "A" * 24  # key-shaped, assembled at run time: never a literal anywhere in the repo


def frontmatter(path: Path) -> tuple[dict, str]:
    """Parse YAML front matter strictly enough to catch what YAML itself would reject."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), path
    head, body = text[4:].split("\n---\n", 1)
    meta = {}
    for line in head.splitlines():
        key, sep, value = line.partition(": ")
        assert sep and re.fullmatch(r"[a-z][a-z-]*", key), f"{path.name}: bad front-matter line {line!r}"
        value = value.strip()
        if value.startswith('"'):
            assert value.endswith('"') and '"' not in value[1:-1].replace('\\"', ""), f"{path.name}: {key}"
            value = value[1:-1].replace('\\"', '"')
        else:  # a plain scalar may not contain ": " / " #" or start with a YAML indicator
            assert ": " not in value and " #" not in value and value[:1] not in set("[]{}&*!|>'%@`#,?-"), key
        meta[key] = value
    try:
        import yaml
    except ImportError:
        pass
    else:
        assert yaml.safe_load(head) == meta
    return meta, body


def test_project_skills_have_valid_frontmatter_and_only_real_cli_flags(capsys):
    from form_guard.cli import main

    with pytest.raises(SystemExit):
        main(["--help"])
    cli_help = capsys.readouterr().out
    bodies = {}
    for name in SKILLS:
        meta, body = frontmatter(ROOT / ".claude" / "skills" / name / "SKILL.md")
        assert meta["name"] == name and len(meta["description"]) >= 150 and "Use when" in meta["description"], name
        bodies[name] = body
        for line in re.findall(r"python -m form_guard\.cli[^\n`]*", body):  # docs and CLI must not drift apart
            for flag in re.findall(r"(?<![\w-])--[a-z][a-z-]*", line):
                assert flag in cli_help, f"{name}: {flag} is not a CLI flag"
    for word in ("--scan", "--json", "--slot-spec", "--discover-blanks", "form_guard_list_demos", "office_only"):
        assert word in bodies["form-scan"], word
    for word in ("--form", "--memo", "--confirm", "memo-extractor", "field-mapper", "UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF"):
        assert word in bodies["surgical-fill"], word
    for word in ("--verify", "VIS-01", "HITL", "structure-auditor", "form-auditor", "ZIP bomb"):
        assert word in bodies["structural-audit"], word
    for word in ("--form", "--verify", "PASS", "NEEDS_HUMAN_INPUT", "FAIL", "never", "--model-tiering", "MODEL_FALLBACK_CHAIN"):
        assert word in bodies["form-fill-verify"], word
    for word in ("VIS-01", "--png", "--naive", "hitl_checklist", "overflow_count", "broken_naive_ai_form1.docx"):
        assert word in bodies["visual-diff-audit"], word
    for path in re.findall(r"samples/[\w/]+\.(?:docx|xlsx|txt)", "".join(bodies.values())):
        assert (ROOT / path).is_file(), path  # every sample a skill names exists


def test_subagents_are_read_only_and_assign_models_on_purpose():
    found = {p.stem for p in (ROOT / ".claude" / "agents").glob("*.md")}
    assert found == set(AGENTS)
    metas = {}
    for name, (model, tools) in AGENTS.items():
        meta, body = frontmatter(ROOT / ".claude" / "agents" / f"{name}.md")
        assert meta["name"] == name and meta["model"] == model and len(meta["description"]) >= 150, name
        assert [t.strip() for t in meta["tools"].split(",")] == tools, name
        assert not {"Edit", "Write", "MultiEdit", "NotebookEdit"} & set(tools), name  # no file-editing tool anywhere
        metas[name] = (meta, body)
    assert "never guesses" in metas["memo-extractor"][0]["description"].lower()
    assert "BEFORE" in metas["memo-extractor"][0]["description"] and "Bash" not in AGENTS["memo-extractor"][1]
    assert "form_guard.verifier" in metas["form-auditor"][0]["description"]
    assert "no file-editing tools" in metas["form-auditor"][0]["description"].lower()
    for word in ("--verify", "zipfile", "PreToolUse", "READY FOR HUMAN SIGN-OFF", "not verified"):
        assert word in metas["form-auditor"][1], word
    for word in ("needs_human_input", "verbatim", "和暦", "上限1,000,000円", "ただし", "--scan <form> --json"):
        assert word in metas["field-mapper"][1], word
    for word in ("--verify", "entry_hashes", "patches", "xml:space", "hitl_checklist", "not verified", "Read-only"):
        assert word in metas["structure-auditor"][1], word


def test_settings_register_two_stage_hooks_without_shell_expansion():
    cfg = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    (pre,), (post,) = cfg["hooks"]["PreToolUse"], cfg["hooks"]["PostToolUse"]
    assert set(pre["matcher"].split("|")) == {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "PowerShell"}
    assert set(post["matcher"].split("|")) == {"Write", "Edit", "MultiEdit"}
    for entry, event in ((pre, "pre-tool-use"), (post, "post-tool-use")):
        (h,) = entry["hooks"]
        assert h["type"] == "command" and h["command"] == f"python scripts/hook_guard.py {event}" and h["timeout"] <= 15
        assert not set(h["command"]) & set("$%`")  # identical in Git Bash, sh, PowerShell and cmd.exe
    # hooks run in Claude's current directory: keep it at the project root so the relative script path resolves
    assert cfg["env"] == {"CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR": "1"}
    assert cfg["permissions"] == {"deny": ["Read(./.env)", "Read(./.env.*)"]}  # never widened
    blob = json.dumps(cfg)
    assert "bypassPermissions" not in blob and "dangerously" not in blob and "allow" not in cfg["permissions"]
    server = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["form-guard"]
    assert server["type"] == "stdio" and server["command"] == "python" and server["args"] == ["-m", "form_guard.mcp_server"]
    assert server["env"] == {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def _hook(event, tool, tool_input, env=None, cwd=None, raw=None):
    base = {k: v for k, v in os.environ.items() if k not in ("FORM_GUARD_ALLOW_TEMPLATE_WRITE", "CLAUDE_PROJECT_DIR")}
    payload = {"hook_event_name": event, "tool_name": tool, "tool_input": tool_input, "cwd": str(cwd or ROOT)}
    data = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return subprocess.run([sys.executable, str(HOOK), "pre-tool-use" if event == "PreToolUse" else "post-tool-use"],
                          input=data, capture_output=True, timeout=60, env={**base, **(env or {})})


def _denied(proc) -> str:
    """Refused before the tool ran: exit code 2 and a PreToolUse deny decision (stdout) plus the reason (stderr)."""
    assert proc.returncode == 2, proc.stderr.decode("utf-8", "replace")
    out = json.loads(proc.stdout)
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse" and hso["permissionDecision"] == "deny"
    assert out["decision"] == "block" and out["reason"] == hso["permissionDecisionReason"]
    return hso["permissionDecisionReason"] + "\n" + proc.stderr.decode("utf-8")


@pytest.mark.parametrize("tool,tool_input", [
    ("Write", {"file_path": TEMPLATE}),
    ("Edit", {"file_path": str(ROOT / "samples" / "blank_forms" / "form3_facility_safety_inspection.xlsx")}),
    ("MultiEdit", {"file_path": "samples\\blank_forms\\form2_municipal_equipment_subsidy.docx"}),
    ("NotebookEdit", {"notebook_path": "samples/blank_forms/form5_unmarked_blank.xlsx", "new_source": "x"}),
    ("Bash", {"command": f"cp output/filled.docx {TEMPLATE}"}),
    ("Bash", {"command": "cp output/*.docx samples/blank_forms/"}),
    ("Bash", {"command": f"echo broken > {TEMPLATE}"}),
    ("Bash", {"command": f"ls && cat notes.txt >> '{TEMPLATE}' 2>&1"}),
    ("Bash", {"command": f"rm -f {TEMPLATE}"}),
    ("Bash", {"command": f"rm {TEMPLATE} --generate-samples"}),  # the generator exemption needs the generator itself
    ("Bash", {"command": f"mv {TEMPLATE} /tmp/"}),
    ("Bash", {"command": f"python -c \"import docx; d = docx.Document('{TEMPLATE}'); d.save('{TEMPLATE}')\""}),
    ("Bash", {"command": f"python - <<'EOF'\nfrom pathlib import Path\nPath('{TEMPLATE}').write_bytes(b'')\nEOF"}),
    ("Bash", {"command": f"sed -i 's/x/y/' {TEMPLATE}"}),
    ("Bash", {"command": f"zip {TEMPLATE} word/document.xml"}),
    ("PowerShell", {"command": "Copy-Item -Path output\\x.xlsx -Destination samples\\blank_forms\\"}),
    ("PowerShell", {"command": "Set-Content -Path samples\\blank_forms\\form1_research_plan_change.docx -Value x"}),
    ("PowerShell", {"command": "Remove-Item samples/blank_forms/form4_unmarked_blank.docx"}),
])
def test_pre_tool_use_hook_blocks_blank_template_mutation_before_execution(tool, tool_input):
    reason = _denied(_hook("PreToolUse", tool, tool_input))
    assert "BLOCKED by the Form-Guard template guard" in reason
    assert "--generate-samples" in reason and "--out-dir" in reason  # tells the agent what to do instead


@pytest.mark.parametrize("tool,tool_input", [
    ("Write", {"file_path": "output/filled_form1.docx"}),
    ("Edit", {"file_path": "form_guard/mapper.py", "old_string": "a", "new_string": "b"}),
    ("Edit", {"file_path": "samples/blank_forms/form1_research_plan_change.slots.json", "new_string": "{}"}),
    ("Bash", {"command": f"python -m form_guard.cli --form {TEMPLATE} --memo samples/memos/memo1_research_change.txt "
                         f"--out-dir output/run1"}),
    ("Bash", {"command": f"cp {TEMPLATE} output/copy.docx && python -m form_guard.cli --scan {TEMPLATE}"}),
    ("Bash", {"command": f"unzip -l {TEMPLATE} && git diff --stat {TEMPLATE}"}),
    ("Bash", {"command": "python -m form_guard.cli --generate-samples"}),
    ("Bash", {"command": 'git grep -n "sk-' + 'ant-" && git log --all -p -S "sk-' + 'ant-"'}),  # the pre-push check itself
    ("Bash", {"command": f"python -c \"import docx; d = docx.Document('{TEMPLATE}'); d.save('output/x.docx')\""}),
    ("PowerShell", {"command": "Get-Content samples\\blank_forms\\form1_research_plan_change.slots.json"}),
    ("Read", {"file_path": TEMPLATE}),
])
def test_pre_tool_use_hook_allows_normal_work(tool, tool_input):
    proc = _hook("PreToolUse", tool, tool_input)
    assert proc.returncode == 0 and proc.stdout == b"" and proc.stderr == b""


def test_template_guard_override_comes_from_the_hook_environment_only():
    inline = {"command": f"FORM_GUARD_ALLOW_TEMPLATE_WRITE=1 cp output/x.docx {TEMPLATE}"}
    _denied(_hook("PreToolUse", "Bash", inline))  # an agent cannot switch the guard off inline
    allowed = _hook("PreToolUse", "Write", {"file_path": TEMPLATE}, env={"FORM_GUARD_ALLOW_TEMPLATE_WRITE": "1"})
    assert allowed.returncode == 0 and b"allowed by FORM_GUARD_ALLOW_TEMPLATE_WRITE=1" in allowed.stderr


def test_pre_tool_use_hook_blocks_secrets_paths_and_emails_before_anything_is_written(tmp_path):
    target = ROOT / "output" / "hook_probe" / "notes.md"
    user = "taro" + "yamada"
    bad = {
        "Anthropic API key": ("Write", {"file_path": str(target), "content": "token " + _KEY}),
        "literal API key assignment": ("Write", {"file_path": str(ROOT / "output" / "hook_probe" / "cfg.py"),
                                                 "content": "ANTHROPIC_API_KEY = " + '"sk-' + "abc123456789" + '"\n'}),
        "local user-profile path": ("Edit", {"file_path": str(target), "old_string": "x",
                                             "new_string": "C:" + _BS + "Users" + _BS + user + _BS + "notes.txt"}),
        "local user-profile path (POSIX)": ("MultiEdit", {"file_path": str(target), "edits": [
            {"old_string": "a", "new_string": "/home/" + user + "/form-guard"}]}),
        "local user-profile path (Git Bash)": ("Edit", {"file_path": str(target), "old_string": "x",
                                                        "new_string": "/c/Users/" + user + "/form-guard"}),
        "e-mail address": ("Write", {"file_path": str(target), "content": "連絡先: " + user + "@" + "corp-mail.co.jp"}),
        "notebook key": ("NotebookEdit", {"notebook_path": str(ROOT / "output" / "hook_probe" / "n.ipynb"),
                                          "new_source": "key = '" + _KEY + "'"}),
    }
    for label, (tool, ti) in bad.items():
        reason = _denied(_hook("PreToolUse", tool, ti))
        assert "secret & hygiene guard" in reason and "before writing" in reason, label
        assert _KEY not in reason and user not in reason and "corp-mail" not in reason, label  # never echoes the value
    assert not target.exists() and not (ROOT / "output" / "hook_probe").exists()  # refused before the write
    shell = _denied(_hook("PreToolUse", "Bash", {"command": "echo " + _KEY + " > key.txt"}))
    assert "Anthropic API key" in shell and _KEY not in shell
    fine = [
        ("Write", {"file_path": str(target), "content": "C:" + _BS + "Users" + _BS + "<user>" + _BS + "x, /home/<user>/y, "
                                                        "%USERPROFILE%, info@example.jp, noreply@anthropic.com"}),
        ("Write", {"file_path": str(ROOT / ".env"), "content": "ANTHROPIC_API_KEY=" + _KEY}),  # where the key belongs
        ("Write", {"file_path": str(tmp_path / "outside.md"), "content": "token " + _KEY}),  # not a project file
        ("Edit", {"file_path": str(target), "old_string": "a", "new_string": "Co-Authored-By: Claude "
                                                                            "<noreply@anthropic.com>"}),
    ]
    for tool, ti in fine:
        proc = _hook("PreToolUse", tool, ti)
        assert proc.returncode == 0 and proc.stdout == b"", (tool, proc.stderr)


def test_post_tool_use_hook_reports_syntax_import_json_and_hygiene_problems(tmp_path):
    env = {"FORM_GUARD_HOOK_PYTHON": sys.executable, "CLAUDE_PROJECT_DIR": str(tmp_path)}
    started = time.perf_counter()
    ok = _hook("PostToolUse", "Edit", {"file_path": str(ROOT / "form_guard" / "mapper.py")}, env)
    elapsed = time.perf_counter() - started
    msg = json.loads(ok.stdout)["systemMessage"]
    assert ok.returncode == 0 and "py_compile OK" in msg and "import form_guard.mapper OK" in msg and elapsed < 2.5
    pkg = tmp_path / "form_guard"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    files = {
        "form_guard/broken.py": ("def f(:\n    pass\n", "does not compile"),
        "form_guard/boom.py": ("value = undefined_name\n", "NameError"),
        "settings.json": ("{not json", "not valid JSON"),
        "bom.json": ("\ufeff{}", "UTF-8 BOM"),
        "secret.md": ("token " + _KEY + "\n", "Anthropic API key"),
        "path.md": ("C:" + _BS + "Users" + _BS + "someone" + _BS + "x\n", "local user-profile path"),
        "mail.md": ("contact: someone" + "@" + "mail.invalid\n", "e-mail address"),
    }
    for rel, (text, needle) in files.items():
        (tmp_path / rel).write_text(text, encoding="utf-8")
        res = json.loads(_hook("PostToolUse", "Write", {"file_path": str(tmp_path / rel)}, env).stdout)
        assert res["decision"] == "block" and needle in res["reason"], rel
        assert str(tmp_path) not in res["reason"]  # paths are shown relative to the project
    (pkg / "optional.py").write_text("import surely_not_installed_pkg_xyz\n", encoding="utf-8")
    optional = json.loads(_hook("PostToolUse", "Edit", {"file_path": str(pkg / "optional.py")}, env).stdout)
    assert "decision" not in optional and "skipped" in optional["systemMessage"]  # a missing extra never blocks
    (tmp_path / "good.json").write_text('{"a": "日本語"}', encoding="utf-8")
    good = json.loads(_hook("PostToolUse", "Write", {"file_path": str(tmp_path / "good.json")}, env).stdout)
    assert "JSON OK" in good["systemMessage"] and "hygiene OK" in good["systemMessage"]
    (tmp_path / "example.md").write_text("contact: info@example.jp\n", encoding="utf-8")
    quiet = _hook("PostToolUse", "Write", {"file_path": str(tmp_path / "example.md")}, env)
    assert quiet.returncode == 0 and quiet.stdout == b""  # a clean non-code file produces no output
    for event in ("PreToolUse", "PostToolUse"):  # an odd payload never blocks the session
        for raw in (b"not json", b"[]", b"", b"\xef\xbb\xbf{}"):
            assert _hook(event, "Write", {}, raw=raw).returncode == 0
    assert subprocess.run([sys.executable, str(HOOK)], input=b"{}", capture_output=True, timeout=60).returncode == 1


def test_hook_guard_is_utf8_safe_on_a_cp932_console(tmp_path):
    env = {"PYTHONIOENCODING": "cp932", "PYTHONUTF8": "0"}  # a Japanese Windows console
    proc = _hook("PreToolUse", "Write", {"file_path": "samples/blank_forms/様式①.docx", "content": "記入値 ✓ 🧪"}, env)
    reason = _denied(proc)
    assert "様式①.docx" in reason and "BLOCKED" in reason  # UTF-8 bytes, nothing mangled or crashed
    jp = tmp_path / "メモ.md"
    jp.write_text("申請日：令和8年11月2日（月） ✓\n", encoding="utf-8")
    res = _hook("PostToolUse", "Write", {"file_path": str(jp)}, {**env, "CLAUDE_PROJECT_DIR": str(tmp_path)})
    assert res.returncode == 0 and res.stdout == b"" and res.stderr == b""


def test_settings_hook_commands_run_in_the_platform_shell(tmp_path):
    """The exact command strings from settings.json, through the platform's shell (cmd.exe / sh), from the root."""
    cfg = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    pre = cfg["hooks"]["PreToolUse"][0]["hooks"][0]["command"].replace("python", f'"{sys.executable}"', 1)
    post = cfg["hooks"]["PostToolUse"][0]["hooks"][0]["command"].replace("python", f'"{sys.executable}"', 1)
    env = {k: v for k, v in os.environ.items() if k != "FORM_GUARD_ALLOW_TEMPLATE_WRITE"}
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": TEMPLATE}}).encode()
    r = subprocess.run(pre, shell=True, input=payload, capture_output=True, cwd=ROOT, env=env, timeout=60)
    assert r.returncode == 2 and json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    (tmp_path / "bad.py").write_text("def f(:\n", encoding="utf-8")
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "bad.py")}}).encode()
    r = subprocess.run(post, shell=True, input=payload, capture_output=True, cwd=ROOT, timeout=60,
                       env={**env, "CLAUDE_PROJECT_DIR": str(tmp_path)})
    assert r.returncode == 0 and json.loads(r.stdout)["decision"] == "block"


# --------------------------------------------- MCP: list_demos, UTF-8 over stdio, protocol stream isolation


def test_mcp_list_demos_and_japanese_payloads_over_real_stdio_on_a_cp932_console(samples_dir, tmp_path):
    root = _mcp_root(tmp_path, samples_dir)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    env.update(FORM_GUARD_MCP_ROOT=str(root), PYTHONPATH=str(ROOT), PYTHONIOENCODING="cp932")
    proc = subprocess.Popen([sys.executable, "-m", "form_guard.mcp_server"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=root, env=env)

    def call(i, name, args):
        body = json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                           "params": {"name": name, "arguments": args}}, ensure_ascii=False).encode("utf-8")
        proc.stdin.write(body + b"\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline().decode("utf-8"))["result"]

    try:
        demos = call(1, "form_guard_list_demos", {})["structuredContent"]
        assert demos["count"] == 5 and demos["fictional"]
        by = {d["id"]: d for d in demos["demos"]}
        assert by["form4"]["title"].startswith("施設・備品一時使用許可申請書") and by["form5"]["unmarked"]
        assert by["form4"]["next_tool"] == "form_guard_discover_blank_cells" and by["form1"]["naive_path"]
        memo = read_memo(root / "samples" / "memos" / "memo1_research_change.txt")
        res = call(2, "form_guard_fill", {"form_path": "samples/blank_forms/form1_research_plan_change.docx",
                                          "memo_text": memo, "mode": "demo"})
        fill = res["structuredContent"]
        assert "令和8年11月2日（月）" in res["content"][0]["text"]  # Japanese survives the wire on a cp932 console
        values = {f["slot_id"]: f["value"] for f in fill["fields"]}
        assert values["提出日"] == "令和8年11月2日（月）" and values["研究課題番号"] == "JR-2026-047"
        assert fill["approval_status"] == "UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF"
        audit = json.loads((root / fill["audit_log"]).read_text(encoding="utf-8"))
        assert audit["approval_status"] == fill["approval_status"] and audit["approval"] is None
        body = zipfile.ZipFile(root / fill["draft_path"]).read("word/document.xml")
        assert b"UNAPPROVED" not in body and "未承認".encode() not in body  # no watermark inside the document
        ver = call(3, "form_guard_verify", {"original_path": "samples/blank_forms/form1_research_plan_change.docx",
                                            "filled_path": fill["draft_path"]})["structuredContent"]
        assert ver["overall_status"] == "PASS" and ver["visual"]["checked"] and ver["visual"]["drift_count"] == 0
        scan_res = call(4, "form_guard_scan", {"form_path": "samples/blank_forms/form2_municipal_equipment_subsidy.docx"})
        tiers = {s["slot_id"]: s["model_tier"] for s in scan_res["structuredContent"]["slots"]}
        assert tiers["申請日"] == "haiku" and tiers["申請事業者名"] == "sonnet"
    finally:
        proc.stdin.close()
        proc.wait(timeout=60)
    assert proc.returncode == 0, proc.stderr.read().decode("utf-8", "replace")


def test_mcp_protocol_stream_survives_stray_prints_at_the_fd_level(tmp_path):
    script = (
        "import os\n"
        "from form_guard import mcp_server as m\n"
        "def noisy(self, args):\n"
        "    os.write(1, b'C-level noise on file descriptor 1\\n')  # what a C extension printing to stdout does\n"
        "    print('python-level noise')\n"
        "    return {'ok': True, 'text': '日本語 ✓'}\n"
        "m.FormGuardMCP.tool_list_demos = noisy\n"
        "m.serve(m.FormGuardMCP(root=os.getcwd()))\n"
    )
    request = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"form_guard_list_demos","arguments":{}}}\n'
    proc = subprocess.run([sys.executable, "-c", script], input=request, capture_output=True, cwd=tmp_path, timeout=60,
                          env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "cp932"})
    lines = proc.stdout.splitlines()
    assert len(lines) == 1, proc.stdout  # only the JSON-RPC answer reached stdout
    assert json.loads(lines[0])["result"]["structuredContent"]["text"] == "日本語 ✓"
    assert b"C-level noise" in proc.stderr and b"python-level noise" in proc.stderr


def test_mcp_fill_never_writes_inside_samples(samples_dir, tmp_path):
    from form_guard.mcp_server import FormGuardMCP

    srv = FormGuardMCP(root=_mcp_root(tmp_path, samples_dir) / "samples")  # a root that IS the samples folder
    res = srv.call_tool("form_guard_fill", {"form_path": "blank_forms/form1_research_plan_change.docx",
                                            "memo_path": "memos/memo1_research_change.txt", "mode": "demo"})
    assert res["isError"] and "inside samples/" in res["content"][0]["text"]
    assert not (tmp_path / "mcp_root" / "samples" / "output").exists()


# ------------------------------------------------------ model fallback chain and escalation to Opus


def _sse_message(model: str, payload: dict) -> bytes:
    text = json.dumps(payload, ensure_ascii=False)
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_fb", "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None,
            "stop_sequence": None, "usage": {"input_tokens": 800, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 200}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {e}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n" for e, d in events).encode("utf-8")


@pytest.mark.filterwarnings("ignore:The model .claude-sonnet-4-5. is deprecated")
def test_model_fallback_chain_retries_a_404_model_id_through_the_real_sdk(samples_dir, tmp_path):
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    from form_guard.mapper import MODEL_FALLBACK_CHAIN

    assert MODEL_FALLBACK_CHAIN == {MODEL_OPUS: ("claude-opus-5", "claude-opus-4-6"),
                                    MODEL_SONNET: ("claude-sonnet-5", "claude-sonnet-4-5"),
                                    MODEL_HAIKU: ("claude-haiku-4-5-20251001", "claude-haiku-4-5")}
    p = paths(samples_dir, "form1")
    unavailable = {MODEL_SONNET, "claude-sonnet-5"}  # this API organisation has neither id
    payloads = [_claude_fields(), _revision_fix()]
    seen: list[dict] = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if body["model"] in unavailable:
            return httpx2.Response(404, json={"type": "error", "error": {"type": "not_found_error",
                                                                         "message": f"model: {body['model']}"}})
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               content=_sse_message(body["model"], payloads.pop(0)))

    client = anthropic.Anthropic(**{"api" + "_key": "mock"}, max_retries=0,
                                 http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    r = run_pipeline(p["form"], read_memo(p["memo"]), tmp_path / "fb", mode="live", client=client, model_tier="sonnet")
    assert [b["model"] for b in seen] == [MODEL_SONNET, "claude-sonnet-5", "claude-sonnet-4-5", "claude-sonnet-4-5"]
    legacy = seen[2]  # the fallback id gets a request built from its own capabilities (no thinking / effort → no 400)
    assert "thinking" not in legacy and "effort" not in legacy["output_config"] and "fallbacks" not in legacy
    assert seen[0]["thinking"] == {"type": "adaptive", "display": "summarized"}
    usage = r.engine["usage"]
    (fb,) = usage["model_fallbacks"]  # recorded once; the revision went straight to the id that answered
    assert fb["tier"] == "sonnet" and fb["requested_model"] == MODEL_SONNET and fb["used_model"] == "claude-sonnet-4-5"
    assert fb["unavailable"] == [MODEL_SONNET, "claude-sonnet-5"] and "not_found_error" in fb["reason"]
    assert [c["attempted_models"] for c in usage["calls"]] == [[MODEL_SONNET, "claude-sonnet-5", "claude-sonnet-4-5"],
                                                               ["claude-sonnet-4-5"]]
    assert {c["requested_model"] for c in usage["calls"]} == {MODEL_SONNET} and usage["models"] == ["claude-sonnet-4-5"]
    assert r.report.overall_status == "PASS"
    audit = json.loads((tmp_path / "fb" / "audit_log.json").read_text(encoding="utf-8"))
    assert audit["engine"]["usage"]["model_fallbacks"][0]["used_model"] == "claude-sonnet-4-5"  # requested + served


def test_model_fallback_chain_exhausted_is_a_live_engine_error_and_auto_mode_falls_back(samples_dir, tmp_path,
                                                                                         fake_client_factory):
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")

    def not_found(kwargs):
        req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        body = {"type": "error", "error": {"type": "not_found_error", "message": f"model: {kwargs['model']}"}}
        raise anthropic.NotFoundError(f"model: {kwargs['model']}", response=httpx2.Response(404, request=req, json=body),
                                      body=body)

    p = paths(samples_dir, "form1")
    client = fake_client_factory([not_found] * 3)
    with pytest.raises(LiveEngineError, match="none of claude-haiku-5-5, claude-haiku-4-5-20251001, claude-haiku-4-5"):
        map_fields(scan(p["form"]).slots, read_memo(p["memo"]), ClaudeEngine(client=client, model_tier="haiku"))
    r = run_pipeline(p["form"], read_memo(p["memo"]), tmp_path / "auto", mode="auto",
                     client=fake_client_factory([not_found] * 9), model_tier="opus")
    assert r.engine["mode"] == "demo" and "not_found_error" in r.engine["fallback_reason"]
    assert r.report.overall_status == "PASS"  # the offline engine finished the job and the report says why


def test_rejected_fast_tier_draft_is_escalated_to_opus_with_adaptive_thinking(samples_dir, fake_client_factory):
    p = paths(samples_dir, "form1")
    client = fake_client_factory([echo_model(_claude_fields())] * 3 + [echo_model(_revision_fix())])
    engine = ClaudeEngine(client=client)  # auto tier
    run = map_fields(scan(p["form"]).slots, read_memo(p["memo"]), engine)
    (esc,) = run.usage["escalations"]
    assert esc["slot_id"] == "所属部局" and (esc["from_tier"], esc["from_model"]) == ("sonnet", MODEL_SONNET)
    assert (esc["to_tier"], esc["to_model"]) == ("opus", MODEL_OPUS) and "quote not found verbatim" in esc["reason"]
    revise = client.beta.messages.calls[-1]
    assert revise["model"] == MODEL_OPUS and revise["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert {m.slot_id: m for m in run.mappings}["所属部局"].filled_value == "理工学研究科"
    fixed = ClaudeEngine(client=fake_client_factory([echo_model(_claude_fields()), echo_model(_revision_fix())]),
                         model_tier="sonnet")
    assert map_fields(scan(p["form"]).slots, read_memo(p["memo"]), fixed).usage["escalations"] == []  # one tier: none


def test_model_tier_and_model_tiering_switches(samples_dir, fake_client_factory, tmp_path, monkeypatch, capsys):
    from form_guard.cli import main
    from form_guard.mapper import resolve_model_tier

    assert resolve_model_tier(None, None) is None and resolve_model_tier("haiku", None) == "haiku"
    assert resolve_model_tier(None, "auto") == "auto" and resolve_model_tier(None, "off") == "opus"
    assert resolve_model_tier("sonnet", "off") == "sonnet" and resolve_model_tier("auto", "auto") == "auto"
    for tier, tiering in (("auto", "off"), ("haiku", "auto")):
        with pytest.raises(ValueError, match="contradicts"):
            resolve_model_tier(tier, tiering)
    monkeypatch.setenv("FORM_GUARD_MODEL_TIERING", "off")
    assert ClaudeEngine(client=object()).model_tier == "opus" and ClaudeEngine(client=object(), model_tier="haiku").model_tier == "haiku"
    monkeypatch.delenv("FORM_GUARD_MODEL_TIERING")
    p = paths(samples_dir, "form1")
    client = fake_client_factory([echo_model(_claude_fields()), echo_model(_revision_fix())])
    r = run_pipeline(p["form"], read_memo(p["memo"]), tmp_path / "off", mode="live", client=client,
                     model_tier=resolve_model_tier(None, "off"))
    assert {c["model"] for c in client.beta.messages.calls} == {MODEL_OPUS} and r.engine["model_tier"] == "opus"
    assert main(["--form", str(p["form"]), "--memo", str(p["memo"]), "--out-dir", str(tmp_path / "cli"), "--mode", "demo",
                 "--model-tiering", "auto"]) == 0
    capsys.readouterr()
    with pytest.raises(SystemExit):
        main(["--scan", str(p["form"]), "--model-tier", "haiku", "--model-tiering", "auto"])
    assert "contradicts" in capsys.readouterr().err
    assert main(["--scan", str(p["form"]), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["slot_count"] == 8 and data["read_only"] and {s["model_tier"] for s in data["slots"]} <= {"haiku", "sonnet", "opus"}


# ---------------------------------------------- archive gate: path traversal, ZIP bombs, DTD / entities


def _zip_with(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zi = zipfile.ZipInfo(name, date_time=(2026, 11, 2, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(zi, data)
    return path


def test_archive_gate_rejects_path_traversal_bombs_and_dtds(samples_dir, tmp_path):
    from form_guard.ziputil import (
        MAX_ARCHIVE_ENTRIES,
        MAX_UNCOMPRESSED_BYTES,
        UnsafeArchiveError,
        check_archive,
        unsafe_entry_name,
        xml_has_dtd,
    )

    assert (MAX_ARCHIVE_ENTRIES, MAX_UNCOMPRESSED_BYTES) == (2_000, 100 * 1024 * 1024)
    for name in ("../evil.xml", "word/../../evil.xml", "word" + _BS + ".." + _BS + "evil.xml", "/etc/passwd",
                 _BS + "share" + _BS + "x.xml", "C:/x.xml", "word/a\x00.xml"):
        assert unsafe_entry_name(name), name
    for name in ("word/document.xml", "[Content_Types].xml", "xl/worksheets/sheet1.xml", "word/a..b.xml"):
        assert not unsafe_entry_name(name), name
    ok = {"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:document/>"}
    assert len(check_archive(_zip_with(tmp_path / "ok.docx", ok))) == 2
    with pytest.raises(UnsafeArchiveError, match="path traversal"):
        check_archive(_zip_with(tmp_path / "trav.docx", {**ok, "../../evil.xml": b"<x/>"}))
    with pytest.raises(UnsafeArchiveError, match="absolute entry name"):
        check_archive(_zip_with(tmp_path / "abs.docx", {**ok, "/word/x.xml": b"<x/>"}))
    with pytest.raises(UnsafeArchiveError, match="duplicate entry name"):
        check_archive(_zip_with(tmp_path / "dup.docx", {**ok, "WORD/document.xml": b"<x/>"}))
    many = {f"word/media/{i}.xml": b"<x/>" for i in range(MAX_ARCHIVE_ENTRIES + 1)}
    with pytest.raises(UnsafeArchiveError, match="2,001 entries"):
        check_archive(_zip_with(tmp_path / "many.docx", many))
    bomb = _zip_with(tmp_path / "bomb.docx", {**ok, "word/media/zeros.bin": bytes(2 * 1024 * 1024)})
    with pytest.raises(UnsafeArchiveError, match="compression ratio"):  # 2 MiB of zeros deflate to ~2 KiB
        check_archive(bomb)
    with pytest.raises(UnsafeArchiveError, match="ZIP bomb guard"):
        check_archive(_zip_with(tmp_path / "big.docx", {**ok, "word/media/a.bin": os.urandom(4096)}), max_uncompressed=4000)
    dtd = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><w:document/>'
    assert xml_has_dtd(dtd) and xml_has_dtd(dtd.decode().encode("utf-16")) and not xml_has_dtd(b"<w:t>&lt;!DOCTYPE</w:t>")
    with pytest.raises(UnsafeArchiveError, match="DTD / entity"):
        check_archive(_zip_with(tmp_path / "xxe.docx", {**ok, "word/document.xml": dtd}))
    # every entry point goes through the gate: scan / discovery …
    p = paths(samples_dir, "form1")
    evil = tmp_path / "xxe_form1.docx"
    rewrite_entry(p["form"], evil, "word/document.xml",
                  lambda d: d.replace(b"?>", b'?><!DOCTYPE w:document [<!ENTITY x "boom">]>', 1))
    with pytest.raises(UnsafeArchiveError, match="DTD / entity"):
        scan(evil)
    with pytest.raises(UnsafeArchiveError):
        discover_blank_cells(evil)
    # … and the verifier: an unsafe filled copy fails ZIP-01 before anything is decompressed
    trav = tmp_path / "filled_trav.docx"
    shutil.copy(p["form"], trav)
    with zipfile.ZipFile(trav, "a") as zf:
        zf.writestr("../../outside.xml", b"<x/>")
    rep = verify(scan(p["form"]), trav, [], "")
    zip01 = next(c for c in rep.checks if c.check_id == "ZIP-01")
    assert rep.overall_status == "FAIL" and not zip01.passed and "path traversal" in zip01.details
    rep = verify(scan(p["form"]), evil, [], "")
    assert rep.overall_status == "FAIL" and "DTD / entity" in next(c for c in rep.checks if c.check_id == "ZIP-01").details


# ------------------------------------------- web: five demos, one-click unmarked demos, upload limits


@pytest.mark.parametrize("uid,office", [("form4", {"受付番号", "受付日", "事務局受付欄", "審査欄"}), ("form5", {"事務局使用欄"})])
def test_run_demo_one_click_discovery_auto_preview_then_reviewer_confirmed_fill(api, uid, office):
    res = api.post("/api/run-demo", json={"demo_id": uid, "mode": "demo"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["unmarked"] and body["auto_preview"]
    disc, run = body["discovery"], body["run"]
    assert re.fullmatch(r"[0-9a-f]{24}", disc["discovery_id"]) and disc["requires_human_confirmation"]
    assert {c["slot_id"] for c in disc["candidates"] if c["office_only"]} == office
    rec = [c["candidate_id"] for c in disc["candidates"] if c["recommended"]]
    assert run["confirmed_candidates"] == rec and run["auto_preview"] and not run["can_approve"]
    assert run["report"]["overall_status"] == "PASS" and run["previews"]["filled"].startswith("<svg")
    assert not office & {pr["slot_id"] for pr in run["patches"]}  # office-only cells are never written

    def approve(r):
        checked = [h["item_id"] for h in r["report"]["hitl_checklist"] if h["required"]]
        return api.post(f"/api/runs/{r['run_id']}/approve", json={"reviewer": "架空 花子", "checked": checked,
                                                                   "filled_sha256": r["filled"]["sha256"],
                                                                   "revision": r["revision"]})

    refused = approve(run)  # the server picked these cells: no sign-off until a reviewer confirms them
    assert refused.status_code == 409 and "auto-preview" in refused.json()["detail"]["message"]
    chosen = rec[1:]  # stage 4: the reviewer's own selection (here: one recommended cell left out)
    conf = api.post("/api/run-demo", json={"demo_id": uid, "mode": "demo", "discovery_id": disc["discovery_id"],
                                           "confirmed_targets": chosen})
    assert conf.status_code == 200, conf.text
    run2 = conf.json()["run"]
    assert not conf.json()["auto_preview"] and run2["confirmed_candidates"] == chosen and run2["can_approve"]
    assert approve(run2).json()["approved"]
    office_ids = [c["candidate_id"] for c in disc["candidates"] if c["office_only"]]
    bad = api.post("/api/run-demo", json={"demo_id": uid, "discovery_id": disc["discovery_id"],
                                          "confirmed_targets": office_ids[:1]})
    assert bad.status_code == 422 and "office/administrative use only" in bad.text


def test_run_demo_for_marked_forms_and_discover_by_demo_id_or_upload(api, samples_dir):
    r = api.post("/api/run-demo", json={"demo_id": "form1", "mode": "demo"}).json()
    assert not r["unmarked"] and r["run"]["report"]["overall_status"] == "PASS" and "naive" in r["run"]["previews"]
    assert api.post("/api/run-demo", json={"demo_id": "form1", "confirmed_targets": ["c01"]}).status_code == 400
    assert api.post("/api/run-demo", json={"demo_id": "form9"}).status_code == 404
    assert api.post("/api/run-demo", json={"demo_id": "form4", "approve": True}).status_code == 422  # extra fields refused
    d4 = api.post("/api/discover", data={"demo_id": "form4", "mode": "demo"})
    assert d4.status_code == 200 and d4.json()["demo_id"] == "form4" and len(d4.json()["candidates"]) == 11
    assert d4.json()["title"].startswith("施設・備品一時使用許可申請書")
    blob = unmarked(samples_dir, "form5")["form"].read_bytes()
    d5 = api.post("/api/discover", files={"file": ("防災備蓄品 貸出・返却管理票.xlsx", blob)}, data={"mode": "demo"})
    assert d5.status_code == 200 and len(d5.json()["candidates"]) == 6
    assert d5.json()["form"] == "防災備蓄品 貸出・返却管理票.xlsx"
    assert api.post("/api/discover", data={"mode": "demo"}).status_code == 400
    wrong = api.post("/api/run-demo", json={"demo_id": "form5", "discovery_id": d4.json()["discovery_id"],
                                            "confirmed_targets": ["c01"]})
    assert wrong.status_code == 400 and "another form" in wrong.text  # one form's discovery never fills another


def test_web_upload_limits_and_archive_gate(api, samples_dir, tmp_path):
    from form_guard.server import MAX_UPLOAD_BYTES

    assert MAX_UPLOAD_BYTES == 15 * 1024 * 1024
    big = api.post("/api/runs/upload", files={"file": ("big.docx", b"PK" + bytes(MAX_UPLOAD_BYTES))}, data={"memo_text": "m"})
    assert big.status_code == 413 and "15 MB" in big.text
    p = paths(samples_dir, "form1")
    trav = tmp_path / "trav.docx"
    shutil.copy(p["form"], trav)
    with zipfile.ZipFile(trav, "a") as zf:
        zf.writestr("../../evil.xml", b"<x/>")
    res = api.post("/api/runs/upload", files={"file": ("trav.docx", trav.read_bytes())}, data={"memo_text": "m"})
    assert res.status_code == 400 and "path traversal" in res.text
    xxe = tmp_path / "xxe.docx"
    rewrite_entry(p["form"], xxe, "word/document.xml",
                  lambda d: d.replace(b"?>", b'?><!DOCTYPE w:document [<!ENTITY x "boom">]>', 1))
    res = api.post("/api/discover", files={"file": ("xxe.docx", xxe.read_bytes())}, data={"mode": "demo"})
    assert res.status_code == 400 and "DTD / entity" in res.text
    bomb = tmp_path / "bomb.docx"
    shutil.copy(p["form"], bomb)
    with zipfile.ZipFile(bomb, "a", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/media/zeros.bin", bytes(4 * 1024 * 1024))
    res = api.post("/api/runs/upload", files={"file": ("bomb.docx", bomb.read_bytes())}, data={"memo_text": "m"})
    assert res.status_code == 400 and "ZIP bomb guard" in res.text


def test_cli_mcp_entry_point_serves_all_five_tools(tmp_path):
    msgs = (b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}\n'
            b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n')
    proc = subprocess.run([sys.executable, "-m", "form_guard.cli", "--mcp"], input=msgs, capture_output=True, cwd=ROOT,
                          timeout=120, env={**os.environ, "FORM_GUARD_MCP_ROOT": str(ROOT)})
    answers = [json.loads(line) for line in proc.stdout.splitlines()]
    assert proc.returncode == 0 and [a["id"] for a in answers] == [1, 2]
    assert [t["name"] for t in answers[1]["result"]["tools"]] == [
        "form_guard_list_demos", "form_guard_scan", "form_guard_discover_blank_cells", "form_guard_fill", "form_guard_verify"]


def test_readme_and_docs_describe_the_two_stage_hooks_fallback_chain_and_archive_gate():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "BUILT_WITH_CLAUDE_CODE.md").read_text(encoding="utf-8")
    for text in (readme, doc):
        for needle in ("scripts/hook_guard.py", "PreToolUse", "PostToolUse", "permissionDecision", "py_compile",
                       "form_guard_list_demos", "MODEL_FALLBACK_CHAIN", "not_found_error", "claude-sonnet-5",
                       "claude-haiku-4-5-20251001", "UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF", "2,000", "100 MB",
                       "15 MB", "検出した空欄を承認して外科的差込を実行"):
            assert needle in text, needle
    for needle in ('<a id="claude-usage"></a>', "## How Claude & Claude Code Are Used / Claude と Claude Code 機能の目的別使い分け",
                   "**No file-editing tools (`Edit`/`Write`)**: `memo-extractor` uses only `Read, Grep, Glob`, and "
                   "`form-auditor` (like `field-mapper` and `structure-auditor`) uses `Read, Grep, Glob, Bash` for "
                   "verification commands under `PreToolUse` protection", "Offline vs live", "デモ1–デモ5", "空欄自動検出",
                   "POST /api/run-demo", "GET /api/demos", "施設・備品一時使用許可申請書", "防災備蓄品 貸出・返却管理票",
                   "なぜ Form-Guard か？（解決する現場課題）", "`Offline Rule`"):
        assert needle in readme, needle
    assert "**No subagent has a file-editing tool (`Edit`/`Write`)**" in doc and "Hooks**（2 段）" in doc
    for name in ("研究設備共用利用申請書", "地域防災備蓄品貸出申請書"):  # the retitled unmarked forms
        assert name not in readme and name not in doc, name
