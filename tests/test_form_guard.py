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
from form_guard.scanner import (
    build_confirmed_slot_spec,
    discover_blank_cells,
    find_markers,
    office_only_targets,
    recommended_ids,
    scan,
)
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
    first_msg = fake_message(_claude_fields())
    first_msg.usage.cache_creation_input_tokens = 400
    second_msg = fake_message(revision)
    second_msg.usage.cache_read_input_tokens = 400
    client = fake_client_factory([first_msg, second_msg])
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
    assert run.usage["cache_creation_input_tokens"] == 400 and run.usage["cache_read_input_tokens"] == 400
    assert run.usage["calls"][0]["cache_creation_input_tokens"] == 400 and run.usage["calls"][0]["cache_read_input_tokens"] == 0
    assert run.usage["calls"][1]["cache_creation_input_tokens"] == 0 and run.usage["calls"][1]["cache_read_input_tokens"] == 400


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


def _seen(run):
    """What the reviewer is looking at: approval and download must name both."""
    return {"filled_sha256": run["filled"]["sha256"], "revision": run["revision"]}


def test_api_hitl_gate_blocks_download_until_full_human_approval(api):
    run = api.post("/api/runs", json={"demo_id": "form2", "mode": "demo"}).json()
    rid = run["run_id"]
    assert run["report"]["pending_slots"] == ["法人番号"] and run["can_approve"] is False
    assert api.get(run["links"]["download"]).status_code == 403
    assert api.post(f"/api/runs/{rid}/approve", json={"reviewer": "x", "checked": [], **_seen(run)}).status_code == 409
    bad = api.post(f"/api/runs/{rid}/overrides", json={"values": {"法人番号": "12-34"}}).json()
    assert bad["report"]["pending_slots"] == ["法人番号"]
    ok = api.post(f"/api/runs/{rid}/overrides", json={"values": {"法人番号": "1234567890123"}}).json()
    assert ok["report"]["overall_status"] == "PASS" and ok["can_approve"]
    required = [h["item_id"] for h in ok["report"]["hitl_checklist"] if h["required"]]
    partial = api.post(f"/api/runs/{rid}/approve", json={"reviewer": "架空 花子", "checked": required[:-1], **_seen(ok)})
    assert partial.status_code == 400 and partial.json()["detail"]["missing"] == required[-1:]
    assert api.post(f"/api/runs/{rid}/approve", json={"reviewer": " ", "checked": required, **_seen(ok)}).status_code == 400
    approved = api.post(f"/api/runs/{rid}/approve", json={"reviewer": "架空 花子", "checked": required, **_seen(ok)}).json()
    dl = api.get(approved["download_url"])
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
    assert [d["id"] for d in demos] == ["form1", "form2", "form3", "form4", "form5", "form6"] and "memo_text" in demos[0]
    assert [d["label"] for d in demos] == ["デモ1", "デモ2", "デモ3", "デモ4", "デモ5", "デモ6"]
    assert [d["unmarked"] for d in demos] == [False, False, False, True, True, True]
    assert [d["pdf"] for d in demos] == [False, False, False, False, False, True]
    assert [d["badge"] for d in demos] == ["", "", "", "空欄自動検出", "空欄自動検出", "固定PDF・空欄自動検出"]


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
    # (e) sub-multiplier compounds (2千万円, 3百万円, 1億2千万円, 上限2千万円) parse and ground as single totals
    from form_guard.constraints import _CAP_RE, infer_slot_constraints
    for raw, expected in (("2千万円", 20_000_000), ("3百万円", 3_000_000), ("50百万円", 50_000_000),
                          ("1億2千万円", 120_000_000), ("１億２千万", 120_000_000), ("1千2百万円", 12_000_000),
                          ("500千円", 500_000), ("483万6千円", 4_836_000), ("1万5000円", 15_000)):
        assert parse_compound(raw.rstrip("円")) == expected and quantities(raw) == {float(expected)}, raw
    assert quantities("予算100万 2026年度") == {1_000_000.0, 2026.0}
    assert infer_slot_constraints("補助金申請額", ["補助金申請額"], ["上限2千万円（千円未満切捨て）"], "")["constraints"]["max_value"] == 20_000_000
    assert infer_slot_constraints("補助金申請額", ["補助金申請額"], ["上限3百万円"], "")["constraints"]["max_value"] == 3_000_000
    assert infer_slot_constraints("補助金申請額", ["補助金申請額"], ["上限1,000,000円"], "")["constraints"]["max_value"] == 1_000_000
    t0 = time.perf_counter()
    assert _CAP_RE.search("上限" + "1" * 40 + "ドル") is None and (time.perf_counter() - t0) < 0.1
    # (f) deterministic property-based round-trip (true int -> Japanese compound notation -> parse_compound/quantities) + shrinking
    def _to_jp_compound(n: int, style: int) -> str:
        if n == 0:
            return "0円"
        parts = []
        oku, rem = divmod(n, 100_000_000)
        man, tail = divmod(rem, 10_000)
        if oku:
            parts.append(f"{oku}億")
        if man:
            if style == 1 and man % 1000 == 0:
                parts.append(f"{man // 1000}千万")
            elif style == 2 and man % 100 == 0:
                parts.append(f"{man // 100}百万")
            elif style == 3 and man >= 1000 and (man % 1000) % 100 == 0 and (man % 1000) > 0:
                parts.append(f"{man // 1000}千{(man % 1000) // 100}百万")
            else:
                parts.append(f"{man:,}万" if style == 4 else f"{man}万")
        if tail:
            if style in (1, 2) and tail % 1000 == 0:
                parts.append(f"{tail // 1000}千")
            else:
                parts.append(f"{tail:,}" if style == 4 else f"{tail}")
        s = "".join(parts) + "円"
        if style == 5:
            s = s.translate(str.maketrans("0123456789,", "０１２３４５６７８９，"))
        return s

    def _shrink_counterexample(text: str, pred) -> str:
        cur = text
        changed = True
        while changed:
            changed = False
            for i in range(len(cur)):
                cand = cur[:i] + cur[i + 1 :]
                if cand and pred(cand):
                    cur = cand
                    changed = True
                    break
        return cur

    import random
    rng = random.Random(20261103)
    for _ in range(100):
        val = rng.choice([0, rng.randint(1, 999) * 1000, rng.randint(1, 9999) * 10_000,
                          rng.randint(1, 50) * 1_000_000, rng.randint(1, 20) * 10_000_000,
                          rng.randint(1, 9) * 100_000_000 + rng.randint(0, 9) * 10_000_000])
        for st in range(6):
            s = _to_jp_compound(val, st)
            assert parse_compound(s.rstrip("円")) == val and quantities(s) == {float(val)}, (val, st, s)
    assert _shrink_counterexample("今年度の研究費申請額は 2千万円（税込）とする", lambda x: "2千万円" in x) == "2千万円"
    memo_sub = "今年度の研究費申請額は 2千万円（内訳：設備費 3百万円、人件費 1千7百万円）とする。\n"
    yen_slot = _text_slot("補助金申請額", fmt="yen")
    assert errors(ground_value("20,000,000円", ["研究費申請額は 2千万円"], memo_sub, yen_slot)) == []
    assert errors(ground_value("3,000,000円", ["設備費 3百万円"], memo_sub, yen_slot)) == []


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
    sha = run["filled"]["sha256"]
    # (b) a stale view (other hash / other revision) is refused
    assert api.post(url, json={**body, "filled_sha256": "0" * 64, "revision": 0}).status_code == 409
    assert api.post(url, json={**body, "filled_sha256": sha, "revision": 7}).status_code == 409
    # (a) the file was changed on disk after verification, before approval
    path.write_bytes(good + b"tampered")
    assert api.post(url, json={**body, "filled_sha256": sha, "revision": 0}).status_code == 409
    path.write_bytes(good)
    ok = api.post(url, json={**body, "filled_sha256": sha, "revision": 0})
    assert ok.status_code == 200 and ok.json()["filled_sha256"] == st.verified_sha256 and ok.json()["revision"] == 0
    # (c) the file was changed after approval: download refuses
    path.write_bytes(good[:-8] + b"XXXXXXXX")
    assert api.get(ok.json()["download_url"]).status_code == 409
    path.write_bytes(good)
    dl = api.get(ok.json()["download_url"])
    assert dl.status_code == 200 and dl.content == good  # the exact verified bytes, served from memory
    # an edit creates a new revision and clears the approval; the old view can no longer approve
    edited = api.post(f"/api/runs/{rid}/overrides", json={"values": {"所属部局": "理工学研究科"}}).json()
    assert edited["revision"] == 1 and edited["approval"] is None
    assert api.post(url, json={**body, "filled_sha256": sha, "revision": 0}).status_code == 409


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
    assert api.post(f"/api/runs/{rid}/approve", json={"reviewer": "架空 花子", "checked": required,
                                                     **_seen(run)}).status_code == 200
    edited = api.post(f"/api/runs/{rid}/overrides", json={"values": {"所属部局": "理工学研究科 機械システム専攻"}}).json()
    m = {x["slot_id"]: x for x in edited["mappings"]}["所属部局"]
    assert m["status"] == "human_entered" and m["filled_value"] == "理工学研究科 機械システム専攻"
    assert edited["approval"] is None and api.get(edited["links"]["download"]).status_code == 403
    assert api.get(run["links"]["download"]).status_code == 403  # the old link: no approval for any revision
    js = (ROOT / "form_guard" / "static" / "app.js").read_text(encoding="utf-8")
    detail = js[js.index("function renderDetail") : js.index("function editControl")]
    assert "editControl(s, m)" in detail and "needs_human_input" not in detail.split("editControl(s, m)")[1][:40]
    assert 'L("値を保存", "Save edit")' in js and "editControl(slot, m)" in js  # field detail + HITL checklist
    assert "filled_sha256: sha, revision: rev" in js  # the approve POST names the revision and file on screen


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


def test_tunnel_access_key_and_allowed_host(api, monkeypatch):
    import base64
    tun = "demo-abc.trycloudflare.com"
    assert api.get("/api/health", headers={"Host": tun}).status_code == 403  # not allowed by default
    monkeypatch.setenv("FORM_GUARD_ALLOWED_HOSTS", tun)
    assert api.get("/api/health", headers={"Host": tun}).status_code == 200  # no key set -> open (local default)
    monkeypatch.setenv("FORM_GUARD_ACCESS_KEY", "s3cret")
    r = api.get("/api/health", headers={"Host": tun})
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    good = "Basic " + base64.b64encode(b"anyone:s3cret").decode()
    bad = "Basic " + base64.b64encode(b"anyone:wrong").decode()
    assert api.get("/api/health", headers={"Host": tun, "Authorization": bad}).status_code == 401
    assert api.get("/api/health", headers={"Host": tun, "Authorization": good}).status_code == 200
    assert api.get("/api/health", headers={"Host": "evil.example.com", "Authorization": good}).status_code == 403
    assert api.get("/?key=wrong", headers={"Host": tun}, follow_redirects=False).status_code == 401
    link = api.get("/?key=s3cret", headers={"Host": tun}, follow_redirects=False)
    assert link.status_code == 303 and "fg_key=" in link.headers["set-cookie"]
    assert "samesite=lax" in link.headers["set-cookie"].lower() and "s3cret" not in link.headers["set-cookie"]
    cookie = link.headers["set-cookie"].split(";")[0]
    assert api.get("/api/health", headers={"Host": tun, "Cookie": cookie}).status_code == 200
    assert api.get("/api/health", headers={"Host": tun, "Cookie": "fg_key=bogus"}).status_code == 401
    # only "key" is dropped from the redirect; "//" can never become an external redirect
    keep = api.get("/start?run_id=20261103-101500-a1b2c3&key=s3cret&x=1", headers={"Host": tun}, follow_redirects=False)
    assert keep.headers["location"] == "/start?run_id=20261103-101500-a1b2c3&x=1"
    assert not api.get("//evil.example.com?key=s3cret", headers={"Host": tun},
                       follow_redirects=False).headers.get("location", "/").startswith("//")
    # Origin is checked before the passphrase: a cross-site POST with the right key is refused, no cookie is set
    cross = api.post("/api/runs?key=s3cret", json={}, headers={"Host": tun, "Origin": "https://evil.example.com",
                                                                "Authorization": good})
    assert cross.status_code == 403 and "set-cookie" not in cross.headers
    # a non-ASCII passphrase works through the cookie link
    monkeypatch.setenv("FORM_GUARD_ACCESS_KEY", "合言葉")
    jp = api.get("/start?key=合言葉", headers={"Host": tun}, follow_redirects=False)
    assert jp.status_code == 303
    assert api.get("/api/health", headers={"Host": tun, "Cookie": jp.headers["set-cookie"].split(";")[0]}).status_code == 200


def test_start_page_is_served_without_inline_script(api):
    page = api.get("/start")
    assert page.status_code == 200 and '<script src="/static/start.js"></script>' in page.text
    assert "<script>" not in page.text and "STEP 5" in page.text
    assert page.headers["cache-control"] == "no-cache"
    assert api.get("/static/start.js").status_code == 200
    assert 'href="/start"' in api.get("/").text


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
    current = {"filled_sha256": st.verified_sha256, "revision": 0}
    assert api.get(url, params={**current, "filled_sha256": "0" * 64}).status_code == 409  # stale hash from the client
    assert api.get(url, params={**current, "revision": 3}).status_code == 409  # stale revision from the client
    ok = api.get(url, params=current)
    assert ok.status_code == 200 and hashlib.sha256(ok.content).hexdigest() == st.verified_sha256
    path = st.result.filled_path
    good = path.read_bytes()
    path.write_bytes(good + b"x")  # the file on disk differs from the verified bytes
    assert api.get(url, params=current).status_code == 409
    assert api.post(f"/api/runs/{rid}/approve", json=approve).status_code == 409
    path.write_bytes(good)
    assert api.get(url, params=current).status_code == 200
    assert api.post("/api/runs", json={"demo_id": "form1", "mode": "demo", "model_tier": "gpt"}).status_code == 422


# ---------------------------------------------------- P0-1 multi-model routing


def test_p01_current_model_constants_and_capability_table():
    # the current model ids (claude-sonnet-5, claude-haiku-4-5-20251001), not ids that do not exist
    assert (MODEL_OPUS, MODEL_SONNET, MODEL_HAIKU) == ("claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5-20251001")
    assert DEFAULT_MODEL == MODEL_OPUS
    for m in (MODEL_OPUS, MODEL_SONNET, "claude-opus-4-6", "claude-sonnet-4-6", "claude-fable-5-1",
              "anthropic.claude-sonnet-5"):
        caps = model_capabilities(m)
        assert caps["adaptive_thinking"] and caps["effort"] and not caps["legacy"], m
    for m in (MODEL_HAIKU, "claude-haiku-4-5", "claude-sonnet-4-5-20250929", "claude-3-5-haiku-20241022",
              "claude-3-haiku-20240307", "not-a-claude-model"):
        caps = model_capabilities(m)
        assert not caps["adaptive_thinking"] and not caps["effort"] and caps["legacy"], m
    assert model_capabilities(MODEL_HAIKU)["known"] and model_capabilities(MODEL_HAIKU)["structured_outputs"]
    assert model_capabilities("claude-opus-4-5-20251101")["effort"] and not model_capabilities("claude-opus-4-5")["adaptive_thinking"]
    assert model_capabilities("claude-haiku-4-5")["structured_outputs"] and not model_capabilities("claude-3-haiku-20240307")["structured_outputs"]
    assert model_capabilities("claude-3-haiku-20240307")["max_output_tokens"] == 4096
    assert model_capabilities(MODEL_OPUS)["server_side_fallback"] and not model_capabilities(MODEL_HAIKU)["server_side_fallback"]
    assert not model_capabilities("claude-sonnet-4-6")["server_side_fallback"]
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
    haiku, sonnet, *opus = calls  # Haiku 4.5 is a legacy model: a plain structured-output request
    assert "thinking" not in haiku and "effort" not in haiku["output_config"] and "fallbacks" not in haiku
    assert haiku["output_config"]["format"]["type"] == "json_schema"
    for c in (sonnet, *opus):
        assert c["thinking"] == {"type": "adaptive", "display": "summarized"} and c["output_config"]["effort"] == "medium"
        assert c["cache_control"] == {"type": "ephemeral"}
    assert all(c["fallbacks"] == "default" for c in opus) and "fallbacks" not in sonnet
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
    adaptive = model_capabilities(model)["adaptive_thinking"]
    assert adaptive == (tier != "haiku")  # Haiku 4.5 gets neither adaptive thinking nor effort
    for c in calls:
        assert c["output_config"]["format"]["type"] == "json_schema"
        if adaptive:
            assert c["thinking"] == {"type": "adaptive", "display": "summarized"} and c["output_config"]["effort"] == "medium"
        else:
            assert "thinking" not in c and "effort" not in c["output_config"]
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
            ("form1", False), ("form2", False), ("form3", False), ("form4", True), ("form5", True), ("form6", True)]
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
    assert sum(r["written"] for r in rows) == 40 and sum(r["chars"] for r in rows) == 848
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
            t["hitl"]) == (5, 41, 40, 848, 2119, 171, 124, 650, 51)
    assert t["pending_slots"] == ["form2:法人番号"] and t["all_checks_passed"] and t["checks_per_form"] == [21]
    assert [row["form"] for row in s["workflow"]["rows"]] == ["form1", "form2", "form3", "form4", "form5"]
    assert api.get("/api/claude-suite").json()["workflow"] == s["workflow"]  # measured once per process
    bare = describe_suite(tmp_path)  # e.g. an installed package without the repository's .claude/ folder
    assert bare["skills"] == bare["agents"] == [] and not bare["settings"]["installed"] and not bare["mcp"]["installed"]
    assert not any(u["present"] for f in bare["flow"] for u in f["uses"] if u["kind"] != "human")


def test_p3_4_web_ui_ships_the_inspector_tier_selector_claude_tab_walkthrough_and_language_toggle(api):
    html, js, css = (api.get(u).text for u in ("/", "/static/app.js", "/static/styles.css"))
    for needle in ('<html lang="ja"', 'data-lang="ja"', 'id="langToggle"', 'data-lang="en"', 'id="tierSelect"',
                   '<option value="auto" data-t="自動：Haiku 4.5 / Sonnet 5 / Opus 5.5|Auto: Haiku 4.5 / Sonnet 5 / Opus 5.5">',
                   ">Opus 5.5 only<", ">Sonnet 5 only<", ">Haiku 4.5 only<",
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
                   "848 characters", "2,119 XML tags", "171 printed text blocks", "124 merged cells/ranges",
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
SKILLS = ("form-scan", "surgical-fill", "structural-audit", "form-fill-verify", "visual-diff-audit", "portal-form-fill")
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
        assert demos["count"] == 6 and demos["fictional"]
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
                                    MODEL_SONNET: ("claude-sonnet-4-5",),
                                    MODEL_HAIKU: ("claude-haiku-4-5",)}
    p = paths(samples_dir, "form1")
    unavailable = {MODEL_SONNET}  # this API organisation cannot use claude-sonnet-5
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
    assert [b["model"] for b in seen] == [MODEL_SONNET, "claude-sonnet-4-5", "claude-sonnet-4-5"]
    legacy = seen[1]  # the fallback id gets a request built from its own capabilities (no thinking / effort → no 400)
    assert "thinking" not in legacy and "effort" not in legacy["output_config"] and "fallbacks" not in legacy
    assert seen[0]["thinking"] == {"type": "adaptive", "display": "summarized"}
    usage = r.engine["usage"]
    (fb,) = usage["model_fallbacks"]  # recorded once; the revision went straight to the id that answered
    assert fb["tier"] == "sonnet" and fb["requested_model"] == MODEL_SONNET and fb["used_model"] == "claude-sonnet-4-5"
    assert fb["unavailable"] == [MODEL_SONNET] and "not_found_error" in fb["reason"]
    assert [c["attempted_models"] for c in usage["calls"]] == [[MODEL_SONNET, "claude-sonnet-4-5"], ["claude-sonnet-4-5"]]
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
    with pytest.raises(LiveEngineError, match="none of claude-haiku-4-5-20251001, claude-haiku-4-5 is available"):
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
    assert api.post("/api/run-demo", json={"demo_id": "form99"}).status_code == 404
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


# ===================================================== R1 (Phase 2/3 dual audit) fixes
# One block per finding of the dual audit of Phase 2/3 (R1-A1 … R1-A12, R1-O1 … R1-O6). Each test fails on the code
# before the fix and documents the exact failure mode it guards against.


def _required(run):
    return [h["item_id"] for h in run["report"]["hitl_checklist"] if h["required"]]


# ------------------------------------------- R1-A1 approve / download require filled_sha256 + revision


def test_r1_audit_a1_approve_and_download_require_the_sha256_and_revision_on_screen(api):
    run = api.post("/api/runs", json={"demo_id": "form1", "mode": "demo"}).json()
    rid, sha = run["run_id"], run["filled"]["sha256"]
    st = api.app.state.store.runs[rid]
    url = f"/api/runs/{rid}/approve"
    body = {"reviewer": "架空 花子", "checked": _required(run)}
    missing_or_bad = [{}, {"revision": 0}, {"filled_sha256": sha}, {"filled_sha256": None, "revision": 0},
                      {"filled_sha256": sha, "revision": None}, {"filled_sha256": "", "revision": 0},
                      {"filled_sha256": sha[:-1], "revision": 0}, {"filled_sha256": sha + "a", "revision": 0},
                      {"filled_sha256": "g" * 64, "revision": 0}, {"filled_sha256": sha, "revision": -1},
                      {"filled_sha256": sha, "revision": "0"}, {"filled_sha256": sha, "revision": True},
                      {"filled_sha256": sha, "revision": 0.5}]
    for extra in missing_or_bad:  # before the fix a client that simply left them out was approved
        assert api.post(url, json={**body, **extra}).status_code == 422, extra
    assert st.approval is None
    assert api.post(url, json={**body, "filled_sha256": "0" * 64, "revision": 0}).status_code == 409  # stale file
    assert api.post(url, json={**body, "filled_sha256": sha, "revision": 1}).status_code == 409       # stale revision
    ok = api.post(url, json={**body, "filled_sha256": sha.upper(), "revision": 0})  # hex case does not matter
    assert ok.status_code == 200 and ok.json()["filled_sha256"] == sha
    link = f"/api/runs/{rid}/download?filled_sha256={sha}&revision=0"
    assert ok.json()["download_url"] == link and api.get(f"/api/runs/{rid}").json()["links"]["download"] == link
    dl_url = f"/api/runs/{rid}/download"
    for params in ({}, {"revision": 0}, {"filled_sha256": sha}, {"filled_sha256": "xyz", "revision": 0},
                   {"filled_sha256": sha, "revision": -1}, {"filled_sha256": sha, "revision": "one"}):
        assert api.get(dl_url, params=params).status_code == 422, params  # was: a bare /download served the file
    assert api.get(dl_url, params={"filled_sha256": "0" * 64, "revision": 0}).status_code == 409
    assert api.get(dl_url, params={"filled_sha256": sha, "revision": 4}).status_code == 409
    dl = api.get(link)
    assert dl.status_code == 200 and hashlib.sha256(dl.content).hexdigest() == sha
    js = (ROOT / "form_guard" / "static" / "app.js").read_text(encoding="utf-8")
    approve_fn = js[js.index("async function onApprove") : js.index("function downloadHref")]
    assert "const { run_id: runId, revision: rev } = target;" in approve_fn  # the approve POST: what was on screen
    assert "const sha = target.filled && target.filled.sha256;" in approve_fn and "filled_sha256: sha, revision: rev" in approve_fn
    approval = js[js.index("function downloadHref") : js.index("/* ---", js.index("function renderApproval"))]
    assert "download?filled_sha256=${encodeURIComponent(a.filled_sha256)}" in approval
    assert "&revision=${encodeURIComponent(a.revision)}" in approval and "dl.href = downloadHref(r);" in approval
    assert "r.links.download" not in approval


# --------------------------------------------- R1-A2 one re-entrant lock: every response is one snapshot


def test_r1_audit_a2_concurrent_reads_and_edits_always_see_a_consistent_snapshot(api):
    import threading

    from fastapi.testclient import TestClient

    from form_guard.server import _payload

    run = api.post("/api/runs", json={"demo_id": "form2", "mode": "demo"}).json()
    rid = run["run_id"]
    st = api.app.state.store.runs[rid]
    assert isinstance(st.lock, type(threading.RLock()))
    with st.lock:  # re-entrant: a payload built while the lock is held (as overrides / approve do) never deadlocks
        assert _payload(rid, st)["revision"] == 0

    def field(d):
        return next(m["filled_value"] for m in d["mappings"] if m["slot_id"] == "所在地")

    values = ["架空県模擬市中央1-2-3", "架空県模擬市北町4-5-6"]
    posted = {0: (field(run), run["filled"]["sha256"])}
    seen, errors = [], []
    done = threading.Event()

    def writer():
        client = TestClient(api.app)
        try:
            for i in range(8):
                d = client.post(f"/api/runs/{rid}/overrides", json={"values": {"所在地": values[i % 2]}}).json()
                posted[d["revision"]] = (field(d), d["filled"]["sha256"])
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            done.set()

    def reader():
        client = TestClient(api.app)
        try:
            while not done.is_set():
                d = client.get(f"/api/runs/{rid}").json()
                seen.append((d["revision"], field(d), d["filled"]["sha256"], d["links"]["download"]))
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)
    assert not errors and not any(t.is_alive() for t in threads)
    assert sorted(posted) == list(range(9)) and len({sha for _v, sha in list(posted.values())[1:]}) == 2
    assert len(seen) >= 3
    for rev, value, sha, link in seen:  # the value, the SHA-256 and the link always belong to the same revision
        assert (value, sha) == posted[rev], rev
        assert link.endswith(f"?filled_sha256={sha}&revision={rev}"), rev
    final = api.get(f"/api/runs/{rid}").json()
    assert final["revision"] == 8 and (field(final), final["filled"]["sha256"]) == posted[8]


# --------------------------------------- R1-A3 designated Excel cells: strict, namespace-aware inline strings


def test_r1_audit_a3_excel_designated_cells_must_be_exactly_one_spreadsheetml_inline_string(tmp_path):
    src, sc, out, w, maps = _designated_xlsx(tmp_path)
    good = verify(sc, out, maps, "")
    assert good.overall_status == "PASS" and "4 slot cell(s) read back as the approved text" in check(good, "APP-01").details
    cell = '<c r="B2" s="1" t="inlineStr"><is><t>架空町内会</t></is></c>'.encode()
    assert cell in zipfile.ZipFile(out).read("xl/worksheets/sheet1.xml")
    # Before the fix seven of these passed every check (reverting the designated cell hid them), is_attribute crashed
    # the verifier inside openpyxl, and foreign_namespace was caught only by XML-02 (its new namespace declaration).
    attacks = {
        "wrapper_in_is": cell.replace(b"<is><t>", b"<is><extLst/><t>"),
        "comment_in_c": cell.replace(b"<is>", b"<!--x--><is>"),
        "pi_in_is": cell.replace(b"<is>", b"<is><?fg x?>"),
        "loose_text": cell.replace(b"</t></is>", b"</t>tail</is>"),
        "text_in_c": cell.replace(b"</is></c>", b"</is>x</c>"),
        "t_attribute": cell.replace(b"<t>", b'<t foo="1">'),
        "t_space_default": cell.replace(b"<t>", b'<t xml:space="default">'),
        "is_attribute": cell.replace(b"<is>", b'<is foo="1">'),
        "foreign_namespace": cell.replace(b"<is><t>", b'<x:is xmlns:x="urn:fg-test"><x:t>').replace(
            b"</t></is>", b"</x:t></x:is>"),
    }
    for name, new in attacks.items():
        bad = tmp_path / f"a3_{name}.xlsx"
        surgical_rewrite(out, bad, "xl/worksheets/sheet1.xml", lambda b, new=new: b.replace(cell, new))
        rep = verify(sc, bad, maps, "")
        assert "XML-03" in _failing(rep) and rep.overall_status == "FAIL", (name, _failing(rep))
        assert any(i.startswith("団体名: B2") for i in check(rep, "XML-03").evidence["designated_cell_issues"]), name
    rep = verify(sc, tmp_path / "a3_foreign_namespace.xlsx", maps, "")
    assert {"XML-03", "TEXT-02", "APP-01"} <= _failing(rep)  # and openpyxl cannot read the value at all
    assert "openpyxl reads 申請!B2 as «None», expected «架空町内会»" in check(rep, "APP-01").details
    assert any("no spreadsheetml inline string" in i for i in _cell_issues(rep))
    rep = verify(sc, tmp_path / "a3_t_space_default.xlsx", maps, "")
    assert any('may carry only xml:space="preserve"' in i for i in _cell_issues(rep))


def test_r1_audit_a3_an_unfilled_designated_excel_cell_must_stay_absent_or_identical(tmp_path):
    from openpyxl.styles import Border, Side

    src, out = tmp_path / "pending.xlsx", tmp_path / "pending_filled.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "申請"
    ws["A1"], ws["A2"], ws["A3"] = "架空の申請書（テスト）", "団体名", "担当者"
    ws["B3"].border = Border(bottom=Side(style="thin"))
    wb.save(src)
    sc = scan(src, {"targets": [{"slot_id": "団体名", "sheet": 0, "cell": "B2"}, {"slot_id": "担当者", "sheet": 0, "cell": "B3"},
                                {"slot_id": "追記", "sheet": 0, "cell": "D9"}]})
    fill_form(sc, {"団体名": "架空町内会"}, out)  # 担当者 and 追記 stay empty: awaiting human input
    maps = [human_mapping(sc.slot("団体名"), "架空町内会")]
    rep = verify(sc, out, maps, "")
    assert rep.overall_status == "NEEDS_HUMAN_INPUT" and not _failing(rep)
    b3 = re.search(rb'<c r="B3" s="\d+" t="n"></c>', zipfile.ZipFile(out).read("xl/worksheets/sheet1.xml")).group(0)
    attacks = {  # the revert step removed / restored these cells, so they passed before the fix
        "styled_new_cell": lambda b: b.replace(b"</sheetData>", b'<row r="9"><c r="D9" s="1"/></row></sheetData>'),
        "empty_inline_string": lambda b: b.replace(b"</sheetData>", b'<row r="9"><c r="D9" t="inlineStr"/></row></sheetData>'),
        "retyped_existing": lambda b: b.replace(b3, b3.replace(b't="n"', b't="s"')),
        "restyled_existing": lambda b: b.replace(b3, re.sub(rb's="\d+"', b's="0"', b3)),
    }
    for name, fn in attacks.items():
        bad = tmp_path / f"pending_{name}.xlsx"
        surgical_rewrite(out, bad, "xl/worksheets/sheet1.xml", fn)
        rep = verify(sc, bad, maps, "")
        assert "XML-03" in _failing(rep) and rep.overall_status == "FAIL", (name, _failing(rep))
        assert any("was left unfilled but differs from the blank form's cell" in i for i in _cell_issues(rep)), name


def test_r1_audit_a3_openpyxl_reads_every_filled_excel_slot_back_as_the_approved_text(demo_runs, tmp_path):
    from form_guard.verifier import _app_check, expected_sheet_values

    r = demo_runs("form3")  # marker cells (inline and shared strings) of a demo form
    assert expected_sheet_values(r.scan, {m.slot_id: m.filled_value for m in r.mappings if m.is_written})
    assert "slot cell(s) read back as the approved text" in check(r.report, "APP-01").details
    src, sc, out, w, maps = _designated_xlsx(tmp_path)
    checks = []
    _app_check("xlsx", out.read_bytes(), checks, [("申請", "B2", "別の値", "団体名"), ("申請", "B3", "模擬 太郎", "担当者")])
    (app,) = checks
    assert not app.passed and app.details == "団体名: openpyxl reads 申請!B2 as «架空町内会», expected «別の値»"


# ---------------------------- R1-A4 / R1-A5 / R1-O1 office-only & not_a_field cannot be bypassed


def test_r1_audit_a4_live_classification_must_cover_every_candidate_exactly_once(samples_dir, fake_client_factory):
    form = unmarked(samples_dir, "form4")["form"]
    rules = discover_blank_cells(form)
    cells = [{"candidate_id": c["candidate_id"], "role": "applicant_fillable", "reason": "label"}
             for c in rules["candidates"] if c["candidate_id"] != "c01"]  # c01 申請日 left out
    cells += [{"candidate_id": "c02", "role": "office_only", "reason": "again"},  # c02 answered twice
              {"candidate_id": "c99", "role": "applicant_fillable", "reason": "no such candidate"}]
    d = discover_blank_cells(form, mode="live", client=fake_client_factory([echo_model({"cells": cells})]))
    by = {c["candidate_id"]: c for c in d["candidates"]}
    for cid in ("c01", "c02"):  # before the fix c01 stayed recommended and c02 took whichever answer came last
        assert (by[cid]["classified"], by[cid]["applicant_fillable"], by[cid]["recommended"]) == (False, False, False), cid
        assert by[cid]["category"] == "unclassified" and "not classified by the model" in by[cid]["reason"]
    assert d["engine"]["classification_issues"] == {"missing": ["c01"], "duplicated": ["c02"], "unknown": ["c99"]}
    assert recommended_ids(d) == ["c03", "c04", "c05", "c06", "c07"] and by["c03"]["classified"]
    with pytest.raises(ValueError, match="c01: not classified as an applicant field — never fillable"):
        build_confirmed_slot_spec(d, ["c01"], form)
    silent = discover_blank_cells(form, mode="live", client=fake_client_factory([echo_model({"cells": []})]))
    assert recommended_ids(silent) == [] and not any(c["classified"] for c in silent["candidates"])
    assert [c["category"] for c in silent["candidates"]] == ["unclassified"] * 7 + ["office_only"] * 4


def test_r1_audit_o1_confirmed_cells_are_rechecked_against_a_fresh_discovery_of_the_blank_form(samples_dir):
    import copy

    p = unmarked(samples_dir, "form4")
    d = discover_blank_cells(p["form"])
    stamp = next(c for c in d["candidates"] if c["slot_id"] == "受付番号")

    def edited(cid, **changes):
        t = copy.deepcopy(d)
        next(c for c in t["candidates"] if c["candidate_id"] == cid).update(changes)
        return t

    # coordinate tampering: applicant candidate c01 re-pointed at the office-only 受付番号 cell (filled before the fix)
    with pytest.raises(ValueError, match="c01: office/administrative use only"):
        build_confirmed_slot_spec(edited("c01", row=stamp["row"], col=stamp["col"]), ["c01"], p["form"])
    # every office flag scrubbed from the report: the fresh discovery of the actual form still refuses the cell
    scrubbed = copy.deepcopy(d)
    for c in scrubbed["candidates"]:
        c.update(office_only=False, applicant_fillable=True, recommended=True, category="applicant_fillable",
                 office_keywords=[], reason="applicant")
    with pytest.raises(ValueError, match="office/administrative use only"):
        build_confirmed_slot_spec(scrubbed, [stamp["candidate_id"]], p["form"])
    # coordinates that are no blank-cell candidate of the form (a printed label, outside the table, wrong file type)
    for change in ({"col": 0}, {"row": 99}, {"table": 7}, {"file_type": "xlsx"}):
        with pytest.raises(ValueError, match="not a blank-cell candidate of the current form"):
            build_confirmed_slot_spec(edited("c01", **change), ["c01"], p["form"])
    # not_a_field and non-applicant cells are never fillable — not even with the internal office-only override
    for change in ({"category": "not_a_field"}, {"claude_role": "not_a_field"}, {"applicant_fillable": False}):
        for allow in (False, True):
            with pytest.raises(ValueError, match="never fillable"):
                build_confirmed_slot_spec(edited("c03", **change), ["c03"], p["form"], allow_office_only=allow)
    # tampered slot ids, labels and printed rules have no effect: the spec is built from the fresh candidates
    spec = build_confirmed_slot_spec(edited("c04", slot_id="受付番号", label="x", max_chars=5000, constraints={},
                                            required_format="text"), ["c04"], p["form"])
    assert [(t["slot_id"], t["table"], t["row"], t["col"]) for t in spec["targets"]] == [("利用目的", 0, 6, 2)]
    assert spec["slots"]["利用目的"]["max_chars"] == 100 and spec["confirmed_candidates"] == ["c04"]
    # the blank form is required (or a report that names it)
    with pytest.raises(ValueError, match="current_form_path is required"):
        build_confirmed_slot_spec(d, ["c01"])
    assert build_confirmed_slot_spec({**d, "form_path": str(p["form"])}, ["c01"]) == build_confirmed_slot_spec(d, ["c01"], p["form"])
    # the same on the .xlsx form: 申請日 (C5) re-pointed at the 事務局使用欄 stamp cell (C18), then at the SUM formula cell
    p5 = unmarked(samples_dir, "form5")
    t5 = copy.deepcopy(discover_blank_cells(p5["form"]))
    c01 = next(c for c in t5["candidates"] if c["candidate_id"] == "c01")
    assert c01["cell"] == "C5"
    c01["cell"] = "C18"
    with pytest.raises(ValueError, match="office/administrative use only"):
        build_confirmed_slot_spec(t5, ["c01"], p5["form"])
    c01["cell"] = "E16"
    with pytest.raises(ValueError, match="not a blank-cell candidate"):
        build_confirmed_slot_spec(t5, ["c01"], p5["form"])


def test_r1_audit_a5_slot_specs_designating_office_only_cells_are_refused_before_filling(api, samples_dir, tmp_path,
                                                                                        capsys):
    from form_guard.cli import main
    from form_guard.mcp_server import FormGuardMCP

    p = unmarked(samples_dir, "form4")
    memo = read_memo(p["memo"])
    office = {"targets": [{"slot_id": "申請日", "table": 0, "row": 1, "col": 2},
                          {"slot_id": "受付", "table": 0, "row": 12, "col": 2}]}  # 受付番号（事務局記入欄）
    (found,) = office_only_targets(p["form"], office)
    assert found.startswith("受付 → table 0 row 12 col 2 (") and "受付番号" in found
    out = tmp_path / "office"
    with pytest.raises(ValueError, match="refused before filling — the slot spec designates office-only cell"):
        run_pipeline(p["form"], memo, out, mode="demo", slot_spec=office)  # before the fix: 受付 became a fill target
    assert not out.exists()
    applicant = {"targets": office["targets"][:1]}
    assert office_only_targets(p["form"], applicant) == []
    assert run_pipeline(p["form"], memo, tmp_path / "ok", mode="demo", slot_spec=applicant).report.overall_status == "PASS"
    forced = run_pipeline(p["form"], memo, tmp_path / "forced", mode="demo", slot_spec=office, allow_office_only=True)
    assert forced.filled_path.is_file()  # only the internal override lifts the guard
    side = tmp_path / "side" / p["form"].name  # a sidecar .slots.json is checked the same way
    side.parent.mkdir()
    shutil.copy(p["form"], side)
    side.with_suffix(".slots.json").write_text(json.dumps(office, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="office-only"):
        run_pipeline(side, memo, tmp_path / "side_out", mode="demo")
    # CLI --slot-spec: a clean refusal (exit code 2), no traceback and no output folder
    spec_file = tmp_path / "office.slots.json"
    spec_file.write_text(json.dumps(office, ensure_ascii=False), encoding="utf-8")
    cli_out = tmp_path / "cli_out"
    assert main(["--form", str(p["form"]), "--memo", str(p["memo"]), "--slot-spec", str(spec_file),
                 "--out-dir", str(cli_out), "--mode", "demo"]) == 2
    assert "refused before filling" in capsys.readouterr().err and not cli_out.exists()
    # a cell discovery does not list (its label is a sentence) is judged by the nearest printed text around it
    doc = tmp_path / "note.docx"
    dd = docx.Document()
    t = dd.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "申請者名"
    t.cell(1, 0).text = "※この欄は事務局が記入しますので、申請者は何も記入しないでください。"
    dd.save(doc)
    assert [c["row"] for c in discover_blank_cells(doc)["candidates"]] == [0]
    hidden = office_only_targets(doc, {"targets": [{"slot_id": "メモ", "table": 0, "row": 1, "col": 1}]})
    assert len(hidden) == 1 and "事務局" in hidden[0]
    assert office_only_targets(doc, {"targets": [{"slot_id": "名", "table": 0, "row": 0, "col": 1}]}) == []
    xl = tmp_path / "note.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"], wb.active["A2"] = "申請者名", "※この欄は事務局が記入しますので、申請者は何も記入しないでください。"
    wb.save(xl)
    assert office_only_targets(xl, {"targets": [{"slot_id": "メモ", "sheet": 0, "cell": "B2"}]})
    assert office_only_targets(xl, {"targets": [{"slot_id": "名", "sheet": 0, "cell": "B1"}]}) == []
    # MCP: a slot_spec_path designating an office-only cell is refused, and nothing is written
    root = _mcp_root(tmp_path, samples_dir)
    (root / "office.slots.json").write_text(json.dumps(office, ensure_ascii=False), encoding="utf-8")
    res = FormGuardMCP(root=root).call_tool("form_guard_fill", {
        "form_path": "samples/blank_forms/form4_unmarked_blank.docx", "memo_path": "samples/memos/memo4_equipment_use.txt",
        "slot_spec_path": "office.slots.json"})
    assert res["isError"] and "refused before filling" in res["content"][0]["text"]
    assert "office/administrative use only" in res["content"][0]["text"] and not (root / "output").exists()
    # web: an uploaded slot spec gets the same answer
    up = api.post("/api/runs/upload", files={"file": ("form4.docx", p["form"].read_bytes()),
                                             "slot_spec": ("office.json", json.dumps(office).encode())},
                  data={"memo_text": memo, "mode": "demo"})
    assert up.status_code == 422 and "office-only" in up.json()["detail"]


# ------------------------------------------------- R1-A6 MCP draft -> web sign-off of the exact same file


def test_r1_audit_a6_an_mcp_draft_is_signed_off_in_the_web_app_as_the_exact_same_file(samples_dir, tmp_path):
    from fastapi.testclient import TestClient

    from form_guard.mcp_server import APPROVAL_STATUS, FormGuardMCP
    from form_guard.server import create_app

    root = _mcp_root(tmp_path, samples_dir)
    srv = FormGuardMCP(root=root)
    form1, memo1 = "samples/blank_forms/form1_research_plan_change.docx", "samples/memos/memo1_research_change.txt"
    f = srv.call_tool("form_guard_fill", {"form_path": form1, "memo_path": memo1})["structuredContent"]
    rid = f["run_id"]
    assert f["review_url"] == f"http://127.0.0.1:8000/?run_id={rid}" and f["engine"]["mode"] == "demo"
    assert f["verification"]["overall_status"] == "PASS" and f["web_handoff"] == f"output/mcp_runs/{rid}/web_handoff.json"
    handoff = json.loads((root / f["web_handoff"]).read_text(encoding="utf-8"))
    assert handoff["approval_status"] == APPROVAL_STATUS and handoff["filled_sha256"] == f["filled_sha256"]
    assert (root / f["out_dir"] / handoff["form"]["blank_copy"]).read_bytes() == (root / form1).read_bytes()
    draft = (root / f["draft_path"]).read_bytes()
    app = create_app(run_root=tmp_path / "web_runs", samples_dir=samples_dir, project_root=root,
                     import_roots=[root / "output" / "mcp_runs", root / "output"])
    api = TestClient(app)
    run = api.get(f"/api/runs/{rid}").json()  # what /?run_id=<id> fetches: imported and re-verified on first use
    assert (run["run_id"], run["revision"], run["filled"]["sha256"]) == (rid, 0, f["filled_sha256"])
    assert run["imported_from"] == f"output/mcp_runs/{rid}" and run["can_approve"]
    assert run["report"]["overall_status"] == "PASS" and run["report"]["passed_count"] == 21
    assert [m["filled_value"] for m in run["mappings"]] == [x["value"] for x in f["fields"]]  # no model re-ran
    assert app.state.store.runs[rid].verified_bytes == draft
    appr = api.post(f"/api/runs/{rid}/approve", json={"reviewer": "架空 花子", "checked": _required(run),
                                                     **_seen(run)}).json()
    assert appr["approved"] and appr["source_draft"] == f"output/mcp_runs/{rid}"
    dl = api.get(appr["download_url"])
    assert dl.status_code == 200 and dl.content == draft  # exactly the bytes the MCP tool wrote
    assert api.post("/api/runs/import", json={"run_id": rid}).json()["approval"]["reviewer"] == "架空 花子"
    # a draft that still needs a human value is completed, re-verified and signed off in the web app
    form2, memo2 = "samples/blank_forms/form2_municipal_equipment_subsidy.docx", "samples/memos/memo2_subsidy_application.txt"
    f2 = srv.call_tool("form_guard_fill", {"form_path": form2, "memo_path": memo2})["structuredContent"]
    run2 = api.post("/api/runs/import", json={"run_dir": f"output/mcp_runs/{f2['run_id']}"}).json()
    assert run2["report"]["pending_slots"] == ["法人番号"] and not run2["can_approve"]
    ok = api.post(f"/api/runs/{f2['run_id']}/overrides", json={"values": {"法人番号": "1234567890123"}}).json()
    assert ok["revision"] == 1 and ok["report"]["overall_status"] == "PASS"
    appr2 = api.post(f"/api/runs/{f2['run_id']}/approve", json={"reviewer": "架空 花子", "checked": _required(ok),
                                                               **_seen(ok)}).json()
    assert hashlib.sha256(api.get(appr2["download_url"]).content).hexdigest() == ok["filled"]["sha256"]
    assert hashlib.sha256((root / f2["draft_path"]).read_bytes()).hexdigest() == f2["filled_sha256"]  # MCP copy untouched
    # a draft changed after MCP wrote it is refused (409) and never registered
    f3 = srv.call_tool("form_guard_fill", {"form_path": form1, "memo_path": memo1})["structuredContent"]
    d3 = root / f3["draft_path"]
    d3.write_bytes(d3.read_bytes() + b"x")
    res = api.post("/api/runs/import", json={"run_id": f3["run_id"]})
    assert res.status_code == 409 and "SHA-256" in res.json()["detail"]
    assert api.get(f"/api/runs/{f3['run_id']}").status_code == 409 and f3["run_id"] not in app.state.store.runs
    # re-sealed (draft, audit log and hand-off hashes all rewritten) but altered: verification refuses it (422)
    f4 = srv.call_tool("form_guard_fill", {"form_path": form1, "memo_path": memo1})["structuredContent"]
    d4, tmp = root / f4["draft_path"], tmp_path / "resealed.docx"
    surgical_rewrite(d4, tmp, "word/document.xml", lambda b: b.replace("研究".encode(), "研修".encode(), 1))
    shutil.copy(tmp, d4)
    new_sha = hashlib.sha256(d4.read_bytes()).hexdigest()
    for name, keys in (("audit_log.json", [("filled", "sha256")]),
                       ("web_handoff.json", [("filled", "sha256"), ("filled_sha256",)])):
        p_ = root / f4["out_dir"] / name
        data = json.loads(p_.read_text(encoding="utf-8"))
        for key in keys:
            (data[key[0]].__setitem__(key[1], new_sha) if len(key) == 2 else data.__setitem__(key[0], new_sha))
        p_.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    res = api.post("/api/runs/import", json={"run_id": f4["run_id"]})
    assert res.status_code == 422 and "fails re-verification" in res.json()["detail"] and "TEXT-01" in res.json()["detail"]
    # a swapped blank form copy is refused too
    f5 = srv.call_tool("form_guard_fill", {"form_path": form1, "memo_path": memo1})["structuredContent"]
    (root / f5["out_dir"] / "_blank" / "form1_research_plan_change.docx").write_bytes((root / form2).read_bytes())
    res = api.post("/api/runs/import", json={"run_id": f5["run_id"]})
    assert res.status_code == 409 and "blank form copy" in res.json()["detail"]
    # only run ids / run folders under output/mcp_runs or output are accepted
    assert api.post("/api/runs/import", json={}).status_code == 400
    assert api.post("/api/runs/import", json={"run_id": rid, "run_dir": f["out_dir"]}).status_code == 400
    assert api.post("/api/runs/import", json={"run_id": "../../etc/passwd"}).status_code == 400
    assert api.post("/api/runs/import", json={"run_dir": str(tmp_path)}).status_code == 400
    assert api.post("/api/runs/import", json={"run_dir": f"output/mcp_runs/{rid}/../../.."}).status_code == 400
    assert api.post("/api/runs/import", json={"run_id": "20991231-235959-abcdef"}).status_code == 404
    assert api.post("/api/runs/import", json={"run_id": rid, "approve": True}).status_code == 422
    assert api.get("/api/runs/20991231-235959-abcdef").status_code == 404
    js = (ROOT / "form_guard" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'new URLSearchParams(location.search).get("run_id")' in js
    assert "if (!(await openRunFromQuery())) await handleDeepLink();" in js
    assert "setRun(await api(`/api/runs/${encodeURIComponent(id)}`), true);" in js


# ---------------- R1-A10 / R1-O3 / R1-O6 MCP message limits, demo by default, open-world hints, credential paths


def _rpc_answers(raw: bytes) -> list:
    """What serve() wrote: Content-Length framed and newline-delimited answers, in order."""
    out, buf = [], io.BytesIO(raw)
    while True:
        line = buf.readline()
        if not line:
            return out
        if line.lower().startswith(b"content-length:"):
            n = int(line.split(b":")[1])
            buf.readline()
            out.append(json.loads(buf.read(n)))
        elif line.strip():
            out.append(json.loads(line))


def test_r1_audit_a10_mcp_messages_are_capped_and_never_read_with_a_negative_length(samples_dir, tmp_path, monkeypatch):
    from form_guard import mcp_server as m

    assert m.MAX_MCP_MESSAGE_BYTES == 10 * 1024 * 1024
    srv = m.FormGuardMCP(root=_mcp_root(tmp_path, samples_dir))
    ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
    framed_ping = b"Content-Length: %d\r\n\r\n" % len(ping) + ping

    def serve(raw: bytes) -> list:
        out = io.BytesIO()
        m.serve(srv, stdin=io.BytesIO(raw), stdout=out)
        return _rpc_answers(out.getvalue())

    neg = serve(b"Content-Length: -5\r\n\r\n" + framed_ping)  # before the fix: read(-5) swallowed the whole stream
    assert neg[0]["error"]["code"] == -32600 and "negative Content-Length" in neg[0]["error"]["message"]
    assert neg[1] == {"jsonrpc": "2.0", "id": 1, "result": {}}  # the stream stays in step
    assert serve(b"Content-Length: 12abc\r\n\r\n" + framed_ping)[0]["error"]["code"] == -32700
    cut = serve(b"Content-Length: 500\r\n\r\n" + ping)  # EOF inside the body
    assert len(cut) == 1 and cut[0]["error"]["code"] == -32700 and "EOF after" in cut[0]["error"]["message"]
    monkeypatch.setattr(m, "MAX_MCP_MESSAGE_BYTES", 256)  # the same logic as with 10 MB, without allocating 10 MB
    big = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {"pad": "x" * 400}}).encode()
    over = serve(b"Content-Length: %d\r\n\r\n" % len(big) + big + framed_ping)
    assert over[0]["error"]["code"] == -32700 and "exceeds the 256-byte message limit" in over[0]["error"]["message"]
    assert len(over) == 2 and over[1]["result"] == {}  # the refused body was skipped, never parsed
    line = serve(big + b"\n" + ping + b"\n")
    assert line[0]["error"]["code"] == -32700 and "message larger than 256 bytes" in line[0]["error"]["message"]
    assert len(line) == 2 and line[1]["result"] == {}
    huge = serve(b"Content-Length: 99999999999999\r\n\r\n")  # nothing is allocated; the stream simply ends
    assert len(huge) == 1 and huge[0]["error"]["code"] == -32700


def test_r1_audit_o3_mcp_defaults_to_demo_mode_marks_open_world_tools_and_refuses_credential_paths(
        samples_dir, tmp_path, fake_client_factory):
    from form_guard import mcp_server as m

    tools = {t["name"]: t for t in m.TOOLS}
    for name in ("form_guard_fill", "form_guard_discover_blank_cells"):
        mode = tools[name]["inputSchema"]["properties"]["mode"]
        assert mode["default"] == "demo" and "explicit opt-in" in mode["description"], name
        assert tools[name]["annotations"]["openWorldHint"] is True, name
    for name in ("form_guard_list_demos", "form_guard_scan", "form_guard_verify"):
        assert tools[name]["annotations"]["openWorldHint"] is False, name
    root = _mcp_root(tmp_path, samples_dir)
    client = fake_client_factory([])  # any call to Claude would fail this test
    srv = m.FormGuardMCP(root=root, client=client)
    form2, memo2 = "samples/blank_forms/form2_municipal_equipment_subsidy.docx", "samples/memos/memo2_subsidy_application.txt"
    f = srv.call_tool("form_guard_fill", {"form_path": form2, "memo_path": memo2})  # no mode: offline (was "auto")
    assert not f["isError"] and f["structuredContent"]["engine"]["mode"] == "demo"
    disc = srv.call_tool("form_guard_discover_blank_cells", {"form_path": "samples/blank_forms/form5_unmarked_blank.xlsx"})
    assert disc["structuredContent"]["engine"]["mode"] == "demo" and client.beta.messages.calls == []
    (root / ".env").write_text("X=1\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    for bad in (".env", "./.env", ".ENV", ".env.local", "sub/.env.production", ".git/config", ".ssh/id_test",
                ".claude-hackathon/settings.json", "samples/../.env", str(root / ".env"), str(root / ".git" / "config")):
        res = srv.call_tool("form_guard_scan", {"form_path": bad})
        assert res["isError"] and res["content"][0]["text"] == "access to .env or hidden credential files is forbidden", bad
    res = srv.call_tool("form_guard_fill", {"form_path": form2, "memo_path": ".env"})
    assert res["isError"] and "forbidden" in res["content"][0]["text"]
    assert ".worktrees/" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()  # R1-O6


# --------------------------- R1-A7 / R1-A11 / R1-O2 hook: e-mail domains, Unicode user names, extensions, .venv


def test_r1_audit_a7_hook_email_domains_unicode_user_paths_and_case_insensitive_extensions(tmp_path):
    import importlib.util

    target = ROOT / "output" / "hook_probe" / "notes.md"
    at = "@"
    for mail in ("someone" + at + "notexample.com", "a" + at + "example.com.evil.org", "a" + at + "evil-example.jp",
                 "team" + at + "anthropic.com", "a" + at + "mail.anthropic.com", "a" + at + "example.net"):
        reason = _denied(_hook("PreToolUse", "Write", {"file_path": str(target), "content": "連絡先: " + mail}))
        assert "e-mail address outside the reserved example domains" in reason and mail not in reason, mail
    for mail in ("info" + at + "example.jp", "a" + at + "sub.example.com", "x" + at + "example.org",
                 "noreply" + at + "anthropic.com", "NoReply" + at + "Anthropic.COM", "1234+fg" + at + "users.noreply.github.com"):
        proc = _hook("PreToolUse", "Write", {"file_path": str(target), "content": "連絡先: " + mail})
        assert proc.returncode == 0 and proc.stdout == b"", mail
    name = "架空" + "太郎"  # a Japanese user name, assembled at run time
    for path in ("C:" + _BS + "Users" + _BS + name + _BS + "x.txt", "/home/" + name + "/x", "/Users/" + name + "/x",
                 "保存先" + "/home/" + name + "/x", "保存先" + "/Users/" + name, "D:/Users/" + name + "/a",
                 "/c/Users/" + name + "/x", "パス：" + "C:" + _BS + "Users" + _BS + "ｋａｋｕｕ"):
        reason = _denied(_hook("PreToolUse", "Edit", {"file_path": str(target), "old_string": "x", "new_string": path}))
        assert "local user-profile path" in reason and name not in reason, path
    for path in ("/home/runner/work", "/home/root/x", "/home/ubuntu/x", "/home/container/x", "/Users/<user>/x",
                 "C:" + _BS + "Users" + _BS + "<user>" + _BS + "x", "C:" + _BS + "Users" + _BS + "Public" + _BS + "x"):
        proc = _hook("PreToolUse", "Edit", {"file_path": str(target), "old_string": "x", "new_string": path})
        assert proc.returncode == 0 and proc.stdout == b"", path
    assert not target.exists()
    env = {"FORM_GUARD_HOOK_PYTHON": sys.executable, "CLAUDE_PROJECT_DIR": str(tmp_path)}
    files = {"BROKEN.PY": ("def f(:\n", "does not compile"), "BAD.JSON": ("{nope", "not valid JSON"),
             "NOTES.MD": ("/home/" + name + "/x\n", "local user-profile path")}
    for fname, (text, needle) in files.items():  # upper-case extensions are checked like lower-case ones
        (tmp_path / fname).write_text(text, encoding="utf-8")
        res = json.loads(_hook("PostToolUse", "Write", {"file_path": str(tmp_path / fname)}, env).stdout)
        assert res["decision"] == "block" and needle in res["reason"], fname
    (tmp_path / "Good.Json").write_text('{"a": 1}', encoding="utf-8")
    good = json.loads(_hook("PostToolUse", "Write", {"file_path": str(tmp_path / "Good.Json")}, env).stdout)
    assert "JSON OK" in good["systemMessage"]
    spec = importlib.util.spec_from_file_location("hook_guard_probe", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for path in (HOOK, ROOT / "README.md", ROOT / "docs" / "BUILT_WITH_CLAUDE_CODE.md", Path(__file__)):
        assert mod.hygiene_findings(path.read_text(encoding="utf-8")) == [], path.name  # never flags its own sources
    assert mod.EXAMPLE_MAIL_DOMAINS == ("example.com", "example.jp", "example.org", "users.noreply.github.com")
    assert mod.EXACT_MAIL_ALLOW == {"noreply" + at + "anthropic.com"}
    cfg = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    for event in ("PreToolUse", "PostToolUse"):
        assert "MultiEdit" in cfg["hooks"][event][0]["matcher"].split("|"), event
    for doc in ("README.md", "docs/BUILT_WITH_CLAUDE_CODE.md"):
        text = (ROOT / doc).read_text(encoding="utf-8")
        assert ".venv" + _BS + "Scripts" + _BS + "Activate.ps1" in text and "source .venv/bin/activate" in text, doc
        assert "python scripts/hook_guard.py" in text and "FORM_GUARD_HOOK_PYTHON" in text, doc


# --------------------------------------- R1-A8 explicit known-model table, honest wording about live requests


def test_r1_audit_a8_model_capabilities_use_an_explicit_known_model_table():
    from form_guard.mapper import KNOWN_MODEL_VERSIONS

    flags = ("adaptive_thinking", "effort", "structured_outputs", "server_side_fallback", "prompt_caching")
    for mid in ("claude-unrecognized-5-5", "claude-opus-99-9", "claude-sonnet-6-0", "claude-haiku-5-9", "claude-opus-4-9",
                "claude-mythos-5-1", "anthropic.claude-newfamily-5-5", "claude-opus-5-5-preview"):
        caps = model_capabilities(mid)  # before the fix any claude-<family>-<4.6+> id got every flag
        assert not any(caps[f] for f in flags) and caps["effort_levels"] == () and not caps["known"], mid
    assert set(KNOWN_MODEL_VERSIONS) == {"opus", "sonnet", "haiku", "fable"}
    for family in ("opus", "sonnet", "haiku"):
        assert KNOWN_MODEL_VERSIONS[family] == {(4, 5), (4, 6), (4, 7), (5, 0), (5, 5)}
    for mid in (MODEL_OPUS, MODEL_SONNET, MODEL_HAIKU, "claude-opus-5", "claude-sonnet-5", "claude-opus-4-7",
                "claude-opus-4-6", "claude-haiku-4-5-20251001", "claude-fable-5-1", "claude-3-haiku-20240307"):
        assert model_capabilities(mid)["known"], mid
    kwargs, caps, effort = ClaudeEngine(client=SimpleNamespace(), model="claude-opus-99-9")._request(
        "claude-opus-99-9", [{"role": "user", "content": "x"}], "system", None)
    assert set(kwargs) == {"model", "max_tokens", "system", "messages"} and effort is None  # a plain request
    en = ("Request formatting is verified against mock/transport tests; live API availability and model-specific "
          "features depend on the caller's Anthropic account tier.")
    ja = "モデル別に送信パラメータを切り替えます。確認済みなのはモックによるリクエスト形式で、実APIでの利用可否・対応機能はアカウント権限に依存します。"
    for doc in ("README.md", "docs/BUILT_WITH_CLAUDE_CODE.md"):
        text = " ".join((ROOT / doc).read_text(encoding="utf-8").split())
        assert en in text and ja in text and "model_capabilities()" in text, doc
        for claim in ("no call fails with a 400", "so no 400", "400 エラーは起きません", "400エラーは起きません",
                      "旧モデルでもエラーになりません"):
            assert claim not in text, (doc, claim)


# ------------------------------------- demo video script, README screenshots, every hook stage in the Claude Code tab


def test_demo_video_script_is_japanese_fits_175_seconds_and_follows_the_walkthrough():
    import importlib.util

    script = (ROOT / "docs" / "DEMO_VIDEO_SCRIPT_JA.md").read_text(encoding="utf-8")
    steps = re.findall(r"^### Step (\d)（(\d):(\d\d)–(\d):(\d\d)・(\d+)秒）.+ — `#tour=(\d)`$", script, re.M)
    assert [s[0] for s in steps] == ["1", "2", "3", "4", "5"]
    assert sorted(s[6] for s in steps) == ["1", "2", "3", "4", "5"]  # every walkthrough step is shown once
    clock = 0
    for n, m0, s0, m1, s1, secs, _ in steps:  # back-to-back windows that add up to the stated length
        start, end = int(m0) * 60 + int(s0), int(m1) * 60 + int(s1)
        assert start == clock and end - start == int(secs), n
        clock = end
    assert clock == 175 <= 180 and "175秒" in script and "180秒以内" in script and "日本語" in script
    sections = re.split(r"^### Step \d", script, flags=re.M)[1:]
    for (n, *_, secs, _), sec in zip(steps, sections):  # the stated narration length matches the text, ≤ 6 字/秒
        narration = re.search(r"\*\*ナレーション\*\*（(\d+)字[^\n]*\n\n((?:> .*\n)+)", sec)
        stated, quote = int(narration.group(1)), narration.group(2)
        text = "".join(line[2:] for line in quote.splitlines()).replace(" ", "")
        assert len(text) == stated and stated / int(secs) <= 6.0, (n, len(text), stated)
    for needle in ("http://127.0.0.1:8000/", "検出した空欄を承認して外科的差込を実行", "needs_human_input", "PreToolUse",
                   "PostToolUse", "MODEL_FALLBACK_CHAIN", "web_handoff.json", "VIS-01", "最終判断は、いつも人です"):
        assert needle in script, needle
    spec = importlib.util.spec_from_file_location("hook_guard_probe_script", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.hygiene_findings(script) == []  # no user-profile path, real e-mail address or key
    assert "](docs/DEMO_VIDEO_SCRIPT_JA.md)" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert "](DEMO_VIDEO_SCRIPT_JA.md)" in (ROOT / "docs" / "BUILT_WITH_CLAUDE_CODE.md").read_text(encoding="utf-8")


def test_readme_screenshots_are_real_landscape_pngs():
    import struct

    shots = sorted((ROOT / "docs" / "screenshots").glob("*.png"))
    assert [p.name for p in shots] == ["01_visual_diff.png", "02_fields_and_sources.png", "03_structural_audit.png",
                                       "04_human_approval.png", "05_slider_xlsx.png", "06_unmarked_discovery.png",
                                       "07_claude_code_architecture.png"]
    for p in shots:
        head = p.read_bytes()[:24]
        assert head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR", p.name
        w, h = struct.unpack(">II", head[16:24])
        assert w >= 1280 and 800 <= h <= w and p.stat().st_size > 100_000, (p.name, w, h)  # a real capture
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in ("01_visual_diff.png", "06_unmarked_discovery.png", "07_claude_code_architecture.png"):
        assert f"](docs/screenshots/{name})" in readme, name


def test_claude_code_tab_lists_every_hook_stage_and_puts_hooks_and_mcp_first():
    js = (ROOT / "form_guard" / "static" / "app.js").read_text(encoding="utf-8")
    suite = js[js.index("function suiteSections") :]
    assert "settings.hooks[0]" not in suite and "hooks.map((hk)" in suite  # was: only the first hook (PreToolUse)
    assert "L(hk.when.ja, hk.when.en)" in suite and "hookCmd(hk)" in suite and 'runner} -c "…"' not in suite
    assert 'h("div", { class: "suite-grid" }, hookCard, mcpCard, agentCard, skillCard)' in suite


# ===================================================== R1 audit (round 2) fixes
# One test per approved finding of the second R1 audit (R1-A1 … R1-A15, R1-O2, R1-O4, R1-O8). Each one fails on the
# code before the fix.

_JAPANESE = re.compile(r"[぀-ヿ㐀-䶿一-鿿！-ﾟ]")  # kana, kanji, full-width forms


def _js_function(js: str, name: str) -> str:
    """The source of a top-level function of app.js (from its declaration to the closing brace in column 0)."""
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\(.*?^\}\n", js, re.S | re.M)
    assert m, name
    return m.group(0)


def _node(tmp_path: Path, name: str, source: str, env=None):
    """Run a small JavaScript harness with Node.js and return its JSON output — None when Node.js is not installed
    (it is not a project requirement)."""
    node = shutil.which("node")
    if not node:
        return None
    script = tmp_path / f"{name}.js"
    script.write_text(source, encoding="utf-8")
    proc = subprocess.run([node, str(script)], capture_output=True, timeout=60, env={**os.environ, **(env or {})})
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return json.loads(proc.stdout.decode("utf-8"))


# ------------------------------------------- R1-A1 rows that hold designated Excel cells cannot hide changes


def _xlsx_with_rows(tmp_path):
    """A blank sheet with a self-closing empty row 7 (Excel writes such rows) and an empty <sheetData/> sheet; the
    designated cells fill an existing row (B2), the empty row (B7), a row that does not exist (D9) and the empty
    sheet (記入!B2)."""
    raw, mid, src, out = (tmp_path / n for n in ("rows_raw.xlsx", "rows_mid.xlsx", "rows.xlsx", "rows_filled.xlsx"))
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "申請"
    ws["A1"], ws["A2"], ws["A3"] = "架空の申請書（テスト）", "団体名", "連絡先"
    ws.row_dimensions[7].height = 24
    wb.create_sheet("記入")
    wb.save(raw)
    surgical_rewrite(raw, mid, "xl/worksheets/sheet1.xml", lambda b: b.replace(
        b'<row r="7" ht="24" customHeight="1"></row>', b'<row r="7" ht="24" customHeight="1"/>'))
    surgical_rewrite(mid, src, "xl/worksheets/sheet2.xml", lambda b: b.replace(b"<sheetData></sheetData>", b"<sheetData/>"))
    assert b'<row r="7" ht="24" customHeight="1"/>' in zipfile.ZipFile(src).read("xl/worksheets/sheet1.xml")
    assert b"<sheetData/>" in zipfile.ZipFile(src).read("xl/worksheets/sheet2.xml")
    sc = scan(src, {"targets": [{"slot_id": "団体名", "sheet": 0, "cell": "B2"}, {"slot_id": "連絡先", "sheet": 0, "cell": "B7"},
                                {"slot_id": "追記", "sheet": 0, "cell": "D9"}, {"slot_id": "記入欄", "sheet": 1, "cell": "B2"}]})
    values = {"団体名": "架空町内会", "連絡先": "info@example.jp", "追記": "特になし", "記入欄": "テスト"}
    fill_form(sc, values, out)
    return sc, out, [human_mapping(sc.slot(k), v) for k, v in values.items()]


def test_r1_audit2_a1_rows_and_sheet_data_holding_designated_excel_cells_keep_their_attributes(tmp_path):
    sc, out, maps = _xlsx_with_rows(tmp_path)
    s1 = zipfile.ZipFile(out).read("xl/worksheets/sheet1.xml")
    s2 = zipfile.ZipFile(out).read("xl/worksheets/sheet2.xml")
    assert b'<row r="7" ht="24" customHeight="1"><c r="B7" t="inlineStr">' in s1  # the empty row was expanded
    assert b'<row r="9"><c r="D9" t="inlineStr">' in s1 and s2.count(b"<sheetData><row r=\"2\"><c r=\"B2\"") == 1
    good = verify(sc, out, maps, "")
    assert good.overall_status == "PASS" and not _failing(good)  # existing, empty and new rows, an empty <sheetData/>
    row9 = re.search(rb'<row r="9">.*?</row>', s1).group(0)
    # Before the fix the revert step restored these rows / <sheetData> wholesale: nine of the twelve passed every check,
    # and the other three (height, row style, extLst child) were caught only indirectly by LAYOUT / APP, never by XML-03.
    attacks = {
        "created_row_hidden": ("sheet1", lambda b: b.replace(b'<row r="9">', b'<row r="9" hidden="1">'), "hidden"),
        "created_row_height": ("sheet1", lambda b: b.replace(b'<row r="9">', b'<row r="9" ht="20" customHeight="1">'), "ht"),
        "created_row_outline": ("sheet1", lambda b: b.replace(b'<row r="9">', b'<row r="9" outlineLevel="1">'), "outlineLevel"),
        "created_row_style": ("sheet1", lambda b: b.replace(b'<row r="9">', b'<row r="9" s="1" customFormat="1">'), "customFormat"),
        "created_row_child": ("sheet1", lambda b: b.replace(b'<row r="9">', b'<row r="9"><extLst/>'), "content other than <c>"),
        "expanded_row_hidden": ("sheet1", lambda b: b.replace(b'<row r="7" ht="24" customHeight="1">',
                                                               b'<row r="7" ht="24" customHeight="1" hidden="1">'), "hidden"),
        "expanded_row_comment": ("sheet1", lambda b: b.replace(b'customHeight="1"><c r="B7"', b'customHeight="1"><!--x--><c r="B7"'),
                                 "content other than <c>"),
        "rows_out_of_order": ("sheet1", lambda b: b.replace(row9, b"").replace(b'<row r="7"', row9 + b'<row r="7"'), "ascending order"),
        "cell_in_another_row": ("sheet1", lambda b: b.replace(b'<row r="9">', b'<row r="8">'), "of another row"),
        "sheet_data_attribute": ("sheet2", lambda b: b.replace(b"<sheetData>", b'<sheetData fg="1">'), "<sheetData> attributes"),
        "sheet_data_child": ("sheet2", lambda b: b.replace(b"<sheetData>", b"<sheetData><extLst/>"), "inside <sheetData>"),
        "sheet_data_row_hidden": ("sheet2", lambda b: b.replace(b'<row r="2">', b'<row r="2" hidden="1">'), "hidden"),
    }
    for name, (sheet, fn, needle) in attacks.items():
        bad = tmp_path / f"a1_{name}.xlsx"
        surgical_rewrite(out, bad, f"xl/worksheets/{sheet}.xml", fn)
        assert zipfile.ZipFile(bad).read(f"xl/worksheets/{sheet}.xml") != zipfile.ZipFile(out).read(f"xl/worksheets/{sheet}.xml"), name
        rep = verify(sc, bad, maps, "")
        assert "XML-03" in _failing(rep) and rep.overall_status == "FAIL", (name, _failing(rep))
        assert any(needle in i for i in _cell_issues(rep)), (name, _cell_issues(rep))


# ------------------------------------------- R1-A2 hook: folders, wildcards and comment-spoofed generator flags


@pytest.mark.parametrize("tool,command", [
    ("PowerShell", "Remove-Item -LiteralPath samples/blank_forms -Recurse"),
    ("Bash", "rm -rf samples/blank_forms"),
    ("Bash", "rm samples/blank_forms/*"),
    ("Bash", "rm samples/blank_forms/a.docx # form_guard --generate-samples"),  # was allowed: the flag sat in a comment
    ("Bash", f"rm {TEMPLATE} form_guard --generate-samples"),
    ("Bash", f"python -m form_guard.cli --generate-samples > {TEMPLATE}"),
    ("Bash", "rm -rf samples/*"),
    ("Bash", "rm -rf samples"),
    ("Bash", "cd samples && rm -rf blank_forms"),
    ("Bash", "rm -rf ."),
    ("Bash", "mv samples/blank_forms output/old_forms"),
    ("Bash", "find . -name '*.docx' -delete"),
    ("Bash", "ls samples/blank_forms/*.docx | xargs rm"),
    ("Bash", "git rm -r samples/blank_forms"),
    ("Bash", "rmdir /s /q samples" + _BS + "blank_forms"),
    ("Bash", "python -c \"import shutil; shutil.rmtree('samples/blank_forms')\""),
    ("PowerShell", "Remove-Item -Path samples" + _BS + "blank_forms" + _BS + "* -Force"),
    ("PowerShell", "Get-ChildItem samples" + _BS + "blank_forms | Remove-Item"),
])
def test_r1_audit2_a2_hook_blocks_folder_and_wildcard_deletion_and_comment_spoofed_generator_flags(tool, command):
    reason = _denied(_hook("PreToolUse", tool, {"command": command}))  # exit code 2 + deny, before the command runs
    assert "BLOCKED by the Form-Guard template guard" in reason and "--generate-samples" in reason


@pytest.mark.parametrize("tool,command", [
    ("Bash", "python -m form_guard.cli --generate-samples"),
    ("Bash", ".venv/Scripts/python.exe -X utf8 -m form_guard.cli --regenerate-samples --all-demos"),
    ("PowerShell", "." + _BS + ".venv" + _BS + "Scripts" + _BS + "python.exe -m form_guard.cli --generate-samples"),
    ("Bash", "rm -rf output/run1 output/*.tmp"),
    ("Bash", "find . -name '*.pyc' -delete"),
    ("PowerShell", "Remove-Item -Recurse -Force output" + _BS + "web_runs"),
    ("PowerShell", "Get-ChildItem -Recurse -Filter *.pyc | Remove-Item"),
    ("Bash", "rm samples/blank_forms/form1_research_plan_change.slots.json"),  # slot specs are data, not templates
])
def test_r1_audit2_a2_hook_still_allows_the_real_generator_and_ordinary_cleanup(tool, command):
    proc = _hook("PreToolUse", tool, {"command": command})
    assert proc.returncode == 0 and proc.stdout == b"", (command, proc.stderr)


# ------------------------------------------- R1-A3 a slot spec file must hold a JSON object


def test_r1_audit2_a3_a_slot_spec_must_be_a_json_object_and_is_never_opened_as_a_second_path(samples_dir, tmp_path,
                                                                                             monkeypatch):
    from form_guard.mcp_server import FormGuardMCP
    from form_guard.scanner import load_spec

    form = paths(samples_dir, "form1")["form"]
    outside = tmp_path / "outside" / "real.slots.json"  # what a JSON string in the spec would have pointed at
    outside.parent.mkdir()
    outside.write_text(json.dumps({"targets": []}), encoding="utf-8")
    reads: list[str] = []
    read_text = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (reads.append(self.name), read_text(self, *a, **k))[1])
    spec = tmp_path / "spec.json"
    for value, kind in ((str(outside), "string"), ([str(outside)], "array"), (42, "number"), (None, "null"),
                        (True, "boolean")):
        spec.write_text(json.dumps(value), encoding="utf-8")
        reads.clear()
        with pytest.raises(ValueError, match=f"slot spec JSON must be an object .*a JSON {kind}"):
            load_spec(form, spec)
        assert reads == ["spec.json"], value  # before the fix a JSON string was opened as a second spec file
        with pytest.raises(ValueError, match="must be an object"):
            scan(form, str(spec))
    side = tmp_path / "side.docx"
    shutil.copy(form, side)
    side.with_suffix(".slots.json").write_text(json.dumps(str(outside)), encoding="utf-8")
    with pytest.raises(ValueError, match="must be an object"):  # the sidecar is checked the same way
        scan(side)
    root = _mcp_root(tmp_path, samples_dir)
    (root / "evil.json").write_text(json.dumps(str(outside)), encoding="utf-8")
    srv = FormGuardMCP(root=root)
    rel = "samples/blank_forms/form1_research_plan_change.docx"
    reads.clear()
    for tool, args in (("form_guard_scan", {"form_path": rel}),
                       ("form_guard_fill", {"form_path": rel, "memo_text": "架空のメモ"}),
                       ("form_guard_verify", {"original_path": rel, "filled_path": rel})):
        res = srv.call_tool(tool, {**args, "slot_spec_path": "evil.json"})
        assert res["isError"] and res["content"][0]["text"] == "slot spec must be a JSON object", tool
    assert "real.slots.json" not in reads and not (root / "output" / "mcp_runs").exists()


# ------------------------------------------- R1-A4 DTD / entity declarations in every Unicode encoding


def test_r1_audit2_a4_dtd_and_entity_declarations_are_refused_in_utf16_and_utf32_with_or_without_bom(demo_runs, tmp_path):
    import codecs

    from form_guard.ziputil import UnsafeArchiveError, check_archive, xml_has_dtd

    dtd = '<?xml version="1.0"?><!DOCTYPE w:document [<!ENTITY x "boom">]><w:document/>'
    entity = '<?xml version="1.0"?><w:document><!ENTITY x "boom"></w:document>'
    escaped = '<?xml version="1.0"?><w:document><w:t>&lt;!DOCTYPE x&gt; &lt;!ENTITY y&gt;</w:t></w:document>'
    encodings = {"utf-8": lambda s: s.encode("utf-8"), "utf-8 BOM": lambda s: codecs.BOM_UTF8 + s.encode("utf-8"),
                 "utf-16 BOM": lambda s: s.encode("utf-16"), "utf-16-le": lambda s: s.encode("utf-16-le"),
                 "utf-16-be": lambda s: s.encode("utf-16-be"), "utf-32 BOM": lambda s: s.encode("utf-32"),
                 "utf-32-le": lambda s: s.encode("utf-32-le"), "utf-32-be": lambda s: s.encode("utf-32-be")}
    ok = {"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:document/>", "word/styles.xml": b"<w:styles/>"}
    n = 0
    for label, enc in encodings.items():
        assert xml_has_dtd(enc(dtd)) and xml_has_dtd(enc(entity)), label  # before the fix: BOM-less and UTF-32 missed
        assert not xml_has_dtd(enc(escaped)), label  # escaped text inside an element is content, not a declaration
        for part in ("word/document.xml", "word/styles.xml", "customXml/item1.xml"):  # main and auxiliary parts
            n += 1
            with pytest.raises(UnsafeArchiveError, match="DTD / entity"):
                check_archive(_zip_with(tmp_path / f"a4_{n}.docx", {**ok, part: enc(dtd)}))
        n += 1
        assert len(check_archive(_zip_with(tmp_path / f"a4_{n}.docx", {**ok, "word/styles.xml": enc(escaped)}))) == 3
    with pytest.raises(UnsafeXMLError):  # a target part read by the byte-exact indexer, BOM-less UTF-16
        index_part("word/document.xml", dtd.encode("utf-16-le"), "word")
    with pytest.raises(UnsupportedXMLError, match="UTF-16 / UTF-32"):
        index_part("word/document.xml", f'<w:document xmlns:w="{W}"/>'.encode("utf-32-be"), "word")
    r = demo_runs("form1")  # a filled copy whose auxiliary styles.xml became a BOM-less UTF-16 DTD fails ZIP-01
    bad = tmp_path / "a4_filled.docx"
    rewrite_entry(r.filled_path, bad, "word/styles.xml", lambda d: dtd.encode("utf-16-be"))
    rep = verify(r.scan, bad, r.mappings, r.memo_text)
    assert rep.overall_status == "FAIL" and "DTD / entity" in check(rep, "ZIP-01").details


# ------------------------------------------- R1-A5 an approval answer is bound to the run it was sent for


def test_r1_audit2_a5_on_approve_never_attaches_an_answer_to_another_run_or_revision(tmp_path):
    js = (ROOT / "form_guard" / "static" / "app.js").read_text(encoding="utf-8")
    fn = _js_function(js, "onApprove")
    assert fn.index("const target = S.run;") < fn.index("await api(")  # what is on screen is captured first
    assert "filled_sha256: sha, revision: rev" in fn and "S.run.filled.sha256" not in fn.split("const same")[0]
    assert fn.index("if (same())") < fn.index("S.run.approval = res;")
    assert "e.status === 409 && S.run && S.run.run_id === runId" in fn
    assert "if (S.run && S.run.run_id === runId) setRun(fresh, false);" in fn
    harness = """
const L = (ja, en) => ja;
const JSON_HEADERS = {};
const $ = () => ({ value: " 架空 花子 " });
let rendered = 0, setRuns = 0, script = [];
const renderHitl = () => { rendered += 1; };
const toast = () => {};
const S = { run: null, checked: new Set(["i1"]) };
const setRun = (r) => { setRuns += 1; S.run = r; };
async function api(path, opts) { return script.shift()(path, opts); }
const stale = () => Object.assign(new Error("stale"), { status: 409, body: {} });
const runA = () => ({ run_id: "A", revision: 0, filled: { sha256: "aa" } });
const runB = () => ({ run_id: "B", revision: 0, filled: { sha256: "bb" } });
""" + fn + """
(async () => {
  const out = {};
  let sent = null;
  S.run = runA(); rendered = 0;  // the reviewer opens another document while the request is in flight
  script = [(p, o) => { sent = { path: p, body: JSON.parse(o.body) }; S.run = runB(); return { approved: true }; }];
  await onApprove(); out.switched = { sent, approval: S.run.approval || null, rendered, run: S.run.run_id };
  S.run = runA(); rendered = 0;  // an edit created a new revision meanwhile
  script = [() => { S.run = { ...S.run, revision: 1, filled: { sha256: "a1" } }; return { approved: true }; }];
  await onApprove(); out.revised = { approval: S.run.approval || null, rendered };
  S.run = runA(); rendered = 0;  // nothing changed: the approval is shown
  script = [() => ({ approved: true })];
  await onApprove(); out.same = { approval: S.run.approval || null, rendered };
  S.run = runA(); setRuns = 0;  // 409 after the reviewer switched documents: the old run is not reloaded over it
  script = [() => { S.run = runB(); throw stale(); }];
  await onApprove(); out.stale_other = { setRuns, left: script.length, run: S.run.run_id };
  S.run = runA(); setRuns = 0;  // 409 on the same document: reload it
  let reloaded = null;
  script = [() => { throw stale(); }, (p) => { reloaded = p; return { run_id: "A", revision: 1, filled: { sha256: "a1" } }; }];
  await onApprove(); out.stale_same = { setRuns, reloaded, revision: S.run.revision };
  S.run = runA(); setRuns = 0;  // 409, but the reviewer switches documents while the reload is in flight
  script = [() => { throw stale(); }, () => { S.run = runB(); return { run_id: "A", revision: 1 }; }];
  await onApprove(); out.stale_reload_switched = { setRuns, run: S.run.run_id };
  process.stdout.write(JSON.stringify(out));
})();
"""
    out = _node(tmp_path, "on_approve", harness)
    if out is None:
        return  # Node.js is optional; the source checks above still ran
    assert out["switched"]["sent"] == {"path": "/api/runs/A/approve", "body": {
        "reviewer": "架空 花子", "checked": ["i1"], "filled_sha256": "aa", "revision": 0}}
    assert out["switched"]["approval"] is None and out["switched"]["rendered"] == 0 and out["switched"]["run"] == "B"
    assert out["revised"] == {"approval": None, "rendered": 0}
    assert out["same"] == {"approval": {"approved": True}, "rendered": 1}
    assert out["stale_other"] == {"setRuns": 0, "left": 0, "run": "B"}
    assert out["stale_same"] == {"setRuns": 1, "reloaded": "/api/runs/A", "revision": 1}
    assert out["stale_reload_switched"] == {"setRuns": 0, "run": "B"}


# ------------------------------------------- R1-A6 a live call that fails part-way keeps its usage on record


def test_r1_audit2_a6_a_live_run_that_fails_part_way_keeps_its_calls_and_tokens(samples_dir, fake_client_factory,
                                                                               tmp_path):
    p = paths(samples_dir, "form1")
    memo = read_memo(p["memo"])
    def haiku_with_cache(kw):
        m = echo_model(_claude_fields())(kw)
        m.usage.cache_creation_input_tokens = 500
        return m
    refusal = lambda kw: fake_message({}, "refusal", "cyber", model=kw["model"])  # noqa: E731 - answered, then declined
    client = fake_client_factory([haiku_with_cache, refusal])  # Haiku answers, then Sonnet declines
    r = run_pipeline(p["form"], memo, tmp_path / "partial", mode="auto", client=client)
    usage = r.engine["usage"]
    assert r.engine["mode"] == "demo" and "declined" in r.engine["fallback_reason"]
    assert usage["live_attempt_failed"] is True and usage["requests"] == 2  # before the fix: the offline engine's 0
    assert (usage["input_tokens"], usage["output_tokens"]) == (2400, 600)
    assert usage["cache_creation_input_tokens"] == 500 and usage["calls"][0]["cache_creation_input_tokens"] == 500
    assert [c["served_model"] for c in usage["calls"]] == [MODEL_HAIKU, MODEL_SONNET] == usage["models"]
    assert usage["model_fallbacks"] == [] and all(rt.get("planned_in_live_auto") for rt in usage["routing"])
    assert r.report.overall_status == "PASS"  # the offline engine produced every value
    html = (tmp_path / "partial" / "report.html").read_text(encoding="utf-8")
    assert "Live Claude API calls were made before the live run failed: 2 call(s), 2,400 input / 600 output tokens (cache write 500 / read 0)" in html
    assert "ライブの Claude API を 2 回呼び出した後で失敗しました（入力 2,400 / 出力 600 トークン（キャッシュ書込 500・読込 0）" in html
    assert "no model was called" not in html and "モデルは呼び出していません" not in html
    audit = json.loads((tmp_path / "partial" / "audit_log.json").read_text(encoding="utf-8"))
    assert audit["engine"]["usage"]["live_attempt_failed"] and len(audit["engine"]["usage"]["calls"]) == 2
    clean = run_pipeline(p["form"], memo, tmp_path / "offline", mode="demo")  # an offline run still says so plainly
    assert "live_attempt_failed" not in clean.engine["usage"]
    assert "no model was called" in (tmp_path / "offline" / "report.html").read_text(encoding="utf-8")
    js = (ROOT / "form_guard" / "static" / "app.js").read_text(encoding="utf-8")
    fn = _js_function(js, "liveFailure")
    for caller in ("renderEngineBadge", "routeBlock", "runLanes"):
        assert "liveFailure(" in _js_function(js, caller), caller
    out = _node(tmp_path, "live_failure", fn + """
process.stdout.write(JSON.stringify([
  liveFailure({ engine: { mode: "demo", fallback_reason: "x", usage: { live_attempt_failed: true, requests: 2 } } }),
  liveFailure({ engine: { mode: "demo", fallback_reason: "x", usage: { requests: 0 } } }),
  liveFailure({ engine: { mode: "demo", usage: { routing: [] } } }),
  liveFailure({ engine: { mode: "live", usage: { requests: 4 } } }),
]));
""")
    if out is not None:
        assert out == [{"live_attempt_failed": True, "requests": 2}, None, None, None]


def test_r1_audit2_a6_discovery_records_the_served_model_and_keeps_the_usage_of_a_failed_classification(
        samples_dir, fake_client_factory):
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")

    def not_found(kwargs):
        req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        body = {"type": "error", "error": {"type": "not_found_error", "message": f"model: {kwargs['model']}"}}
        raise anthropic.NotFoundError(f"model: {kwargs['model']}", response=httpx2.Response(404, request=req, json=body),
                                      body=body)

    form = unmarked(samples_dir, "form4")["form"]
    rules = discover_blank_cells(form)
    verdicts = {"cells": [{"candidate_id": c["candidate_id"], "role": "applicant_fillable", "reason": "label"}
                          for c in rules["candidates"]]}
    client = fake_client_factory([not_found, echo_model(verdicts)])  # the Haiku tier id is unavailable: 404 fallback
    d = discover_blank_cells(form, mode="live", client=client)
    assert [c["model"] for c in client.beta.messages.calls] == [MODEL_HAIKU, "claude-haiku-4-5"]
    assert d["engine"]["model"] == "claude-haiku-4-5" and d["engine"]["label"] == "rules + Claude (claude-haiku-4-5)"
    assert {c["classified_by"] for c in d["candidates"]} == {"rules + claude-haiku-4-5"}  # was: the requested id
    assert d["engine"]["usage"]["model_fallbacks"][0]["used_model"] == "claude-haiku-4-5" and "live_usage" not in d
    refused = discover_blank_cells(form, mode="auto", client=fake_client_factory([fake_message({}, "refusal", model=MODEL_HAIKU)]))
    assert "declined" in refused["engine"]["fallback_reason"] and refused["candidates"] == rules["candidates"]
    live = refused["live_usage"]  # the call the API answered (and billed) before declining stays on record
    assert live["requests"] == 1 and (live["input_tokens"], live["output_tokens"]) == (1200, 300)
    assert [c["served_model"] for c in live["calls"]] == [MODEL_HAIKU] and "live_usage" not in refused["engine"]
    assert "live_usage" not in rules


# ------------------------------------------- R1-A7 ensure_samples never overwrites an existing sample file


def test_r1_audit2_a7_ensure_samples_only_fills_in_missing_files(tmp_path):
    from form_guard.samples import BUILDERS, MEMOS, NAIVE_BUILDERS, ensure_samples

    base = tmp_path / "samples"
    generate_samples(base)  # forms, slot specs, memos, naive files and the comparison pages
    form = base / "blank_forms" / DEMOS[0]["form"]
    memo = base / "memos" / DEMOS[0]["memo"]
    form.write_bytes(form.read_bytes() + b"\0edited by a person")
    memo.write_text("（人が書き換えた架空のメモ）\n", encoding="utf-8")
    (base / "broken_before" / "comparison_form1.html").write_text("<p>edited</p>", encoding="utf-8")
    before = {p.relative_to(base): p.read_bytes() for p in base.rglob("*") if p.is_file()}
    missing = base / "broken_before" / DEMOS[1]["naive"]
    missing.unlink()
    assert ensure_samples(base) == base
    after = {p.relative_to(base): p.read_bytes() for p in base.rglob("*") if p.is_file()}
    assert set(after) == set(before)  # the deleted file came back, nothing else was added
    assert [k for k in before if after[k] != before[k]] == []  # before the fix every form and memo was regenerated
    assert after[missing.relative_to(base)] == NAIVE_BUILDERS[DEMOS[1]["naive"]]()
    assert generate_samples(base, with_comparisons=False, overwrite=False) == {}  # nothing is missing any more
    generate_samples(base, with_comparisons=False)  # an explicit --generate-samples still rebuilds everything
    assert form.read_bytes() == BUILDERS[form.name]() and memo.read_text(encoding="utf-8") == MEMOS[memo.name]


# ------------------------------------------- R1-A8 human-entered values are counted apart from AI values


def test_r1_audit2_a8_filled_tiles_separate_ai_values_with_quotes_from_reviewer_entries(demo_runs):
    import copy

    d = demo_runs("form2").to_dict(include_previews=False)
    ai = sum(1 for m in d["mappings"] if m["status"] == "filled")
    page = build_report_html(d)
    assert "fields filled (verbatim source) / 記入済み（原文照合済み）" in page and "・人の入力" not in page
    assert "ungrounded AI claims / AI記入値の根拠違反" in page and "ungrounded claims / 根拠なし" not in page
    edited = copy.deepcopy(d)
    m = next(x for x in edited["mappings"] if x["slot_id"] == "法人番号")
    m.update(status="human_entered", filled_value="1234567890123")  # typed by the reviewer: no memo quote
    page = build_report_html(edited)
    assert f"{ai + 1}/{len(d['mappings'])}" in page
    assert f"fields filled / 記入済み（AI原文照合 {ai}・人の入力 1）" in page and "（原文照合済み）" not in page
    js = (ROOT / "form_guard" / "static" / "app.js").read_text(encoding="utf-8")
    kpis = _js_function(js, "renderKpis")
    assert 'const aiFilled = r.mappings.filter((m) => m.status === "filled").length;' in kpis
    assert 'const humanFilled = r.mappings.filter((m) => m.status === "human_entered").length;' in kpis
    assert ("L(`AI記入（原文照合済み）${aiFilled}件・人の入力（本人確認）${humanFilled}件`, "
            "`${aiFilled} AI (verbatim source) · ${humanFilled} human-entered`)") in kpis
    assert kpis.index("humanFilled > 0") < kpis.index('L("すべて原文の根拠付き", "each with a verbatim source")')
    assert 'L("AI記入値の根拠違反", "ungrounded AI values")' in kpis and "values without evidence" not in kpis


# ------------------------------------------- R1-A10 the English UI shows no Japanese UI strings


def test_r1_audit2_a10_english_flag_labels_edit_controls_and_steps_have_no_japanese():
    from form_guard.verifier import _FLAG_TEXT

    js = (ROOT / "form_guard" / "static" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "form_guard" / "static" / "index.html").read_text(encoding="utf-8")
    start = js.index("const FLAG = {")
    block = js[start : js.index("};", start)]
    pairs = re.findall(r'(\w+): \["([^"]+)", "([^"]+)"\]', block)
    assert len(pairs) == block.count(": [") and set(_FLAG_TEXT) | {"needs_input", "has_suggestion"} <= {k for k, _, _ in pairs}
    for key, ja, en in pairs:  # before the fix each flag was one "和暦に変換 · to 和暦" string in both languages
        assert _JAPANESE.search(ja) and not _JAPANESE.search(en), (key, en)
    assert "const flagText = (f) => (FLAG[f] ? L(...FLAG[f]) : f);" in js and "FLAG[f] || f" not in js
    for snippet in ('L("値を入力", "Enter value")', 'L("値を修正", "Correct this value")', 'L("値を保存", "Save edit")',
                    'L("✎ 値を修正", "✎ Edit value")'):
        assert snippet in js, snippet
    for old in ("値を入力 · Enter value", "Save edit / 値を修正", "✎ 値を修正 / Edit value", "Correct this value · 値を修正"):
        assert old not in js, old
    step = re.search(r'data-t="欄と印刷ルール（字数・和暦・上限）を読む\|([^"]+)"', html).group(1)
    assert step == "find fields and printed rules (character limits, Japanese era dates, caps)" and not _JAPANESE.search(step)


# ------------------------------------------- R1-A11 / R1-O4 wording that matches what the checks show


def test_r1_audit2_a11_o4_wording_is_not_absolute_and_the_per_form_tile_never_reads_as_a_failure(tmp_path):
    html = (ROOT / "form_guard" / "static" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "form_guard" / "static" / "app.js").read_text(encoding="utf-8")
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    for claim in ("数学的に証明", 'proves "form invariance"', "ごと様式が壊れる", "生成AIに頼むと様式ごと作り直され",
                  "proves with 21 deterministic checks", "「他は何も変わっていない」ことを証明"):
        assert claim not in html, claim
    for text in ("文書を再生成する方式では、罫線・結合セル・定型文・用紙サイズが変わってしまうことがあります（画面の比較例は失敗を再現した模擬データです）。",
                 "Methods that re-generate the document can alter borders, merged cells, boilerplate text and page setup "
                 "(the comparison file is a simulated reproduction of those failures).",
                 "21項目の決定論的検査（描画のずれ・セルのはみ出しを検出した場合は VIS-01 を追加）＋視覚 Diff＋原文ハイライト＋人間承認ゲートで"
                 "指定欄以外の変更を検査します。Word / Excel 実機での表示・印刷と記入内容の最終確認は人が行います。",
                 "checks for any change outside the designated fields with 21 deterministic checks (plus VIS-01 if layout "
                 "drift or overflow is detected), a visual diff, source highlighting and a human approval gate. Final "
                 "display/print review in Word or Excel and fact sign-off stay with a person.",
                 "文書ごと作り直す方式では罫線・結合セル・定型文が変わることがあり",
                 "21項目の決定論的検査で指定欄以外の変更を検査。各値に原文の根拠を示し、人が確認するまでダウンロードをロックします。"
                 "Word / Excel での表示と内容の最終確認は人が行います。"):
        assert text in html, text
    assert "**re-creates the document**, so merged cells split" not in readme and "置くだけで、AIアシスタント" not in readme
    assert "and a re-created document can split merged cells, lose borders, paraphrase legal text" in readme
    assert "（付属の比較用ファイルは、こうした失敗を再現した模擬データです）" in readme
    assert 'join("/")} 項目合格' not in js and "checksPerFormText(t)" in js  # was: 「全様式で 21/22 項目合格」
    out = _node(tmp_path, "checks_per_form", "const S = { lang: \"ja\" };\nconst L = (ja, en) => (S.lang === \"en\" ? en : ja);\n"
                + _js_function(js, "checksPerFormText") + """
const one = { checks_per_form: [21], all_checks_passed: true }, two = { checks_per_form: [21, 22], all_checks_passed: true };
const out = { one: checksPerFormText(one), two: checksPerFormText(two), fail: checksPerFormText({ ...one, all_checks_passed: false }) };
S.lang = "en"; out.one_en = checksPerFormText(one); out.two_en = checksPerFormText(two);
process.stdout.write(JSON.stringify(out));
""")
    if out is not None:
        assert out == {"one": "全様式で全 21 項目合格", "two": "全様式で全項目合格（様式により 21〜22 項目）",
                       "fail": "不合格の検査あり（様式あたり 21 項目）", "one_en": "all 21 checks passed on every form",
                       "two_en": "all checks passed on every form (21–22 per form)"}


# ------------------------------------------- R1-A12 timestamps in Asia/Tokyo with the weekday


def test_r1_audit2_a12_timestamps_are_shown_in_tokyo_time_with_the_weekday(demo_runs, tmp_path):
    from datetime import datetime

    from form_guard.report import JST, _format_jst_with_weekday

    for stamp in ("2026-11-02T21:11:00+00:00", "2026-11-02T21:11:00Z", "2026-11-03T06:11:00+09:00",
                  "2026-11-02T16:11:00-05:00"):
        assert _format_jst_with_weekday(stamp) == "2026-11-03（火）06:11 JST (Tue)", stamp
    assert _format_jst_with_weekday("2026-11-08T10:59:59.5+00:00") == "2026-11-08（日）19:59 JST (Sun)"
    local = datetime(2026, 11, 2, 21, 11).astimezone().astimezone(JST)  # a naive timestamp is local time
    assert _format_jst_with_weekday("2026-11-02T21:11:00").startswith(f"{local:%Y-%m-%d}（{'月火水木金土日'[local.weekday()]}）{local:%H:%M}")
    assert _format_jst_with_weekday("not a date") == "not a date"
    d = demo_runs("form1").to_dict(include_previews=False)
    d["created_at"] = "2026-11-02T21:11:00+00:00"
    assert "verification report · 2026-11-03（火）06:11 JST (Tue)</div>" in build_report_html(d)
    assert d["created_at"] == "2026-11-02T21:11:00+00:00"  # the JSON keeps the ISO timestamp
    js = (ROOT / "form_guard" / "static" / "app.js").read_text(encoding="utf-8")
    assert "fmtDateWithWeekday(r.approval.approved_at)" in _js_function(js, "renderApproval")
    harness = "const S = { lang: \"ja\" };\nconst L = (ja, en) => (S.lang === \"en\" ? en : ja);\n" + \
        _js_function(js, "fmtDateWithWeekday") + """
const out = { ja: fmtDateWithWeekday("2026-11-02T21:11:00+00:00"), z: fmtDateWithWeekday("2026-11-02T21:11:00Z"),
              sunday: fmtDateWithWeekday("2026-11-08T10:59:59+00:00"), bad: fmtDateWithWeekday("not a date") };
S.lang = "en"; out.en = fmtDateWithWeekday("2026-11-02T21:11:00+00:00");
process.stdout.write(JSON.stringify(out));
"""
    out = _node(tmp_path, "jst", harness, env={"TZ": "America/Los_Angeles"})  # independent of the browser's zone
    if out is not None:
        assert out == {"ja": "2026-11-03（火）06:11 JST", "z": "2026-11-03（火）06:11 JST",
                       "sunday": "2026-11-08（日）19:59 JST", "bad": "not a date", "en": "2026-11-03 (Tue) 06:11 JST"}


# ------------------------------------------- R1-O2 current model ids everywhere


def test_r1_audit2_o2_no_retired_model_ids_or_names_anywhere_in_the_repository():
    retired = re.compile("|".join(["sonnet-5" + "-5", "haiku-5" + "-5", r"Sonnet 5\." + "5", r"Haiku 5\." + "5"]))
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, timeout=60)
    files = [ROOT / n for n in listed.stdout.decode("utf-8").split("\0") if n] if listed.returncode == 0 else \
        [p for d in ("form_guard", "tests", "docs", "scripts", ".claude") for p in (ROOT / d).rglob("*") if p.is_file()]
    text_files = [p for p in files if p.suffix.lower() in (".py", ".md", ".js", ".html", ".css", ".json", ".toml", ".txt")]
    assert len(text_files) > 20
    hits = [p.relative_to(ROOT).as_posix() for p in text_files if p.is_file() and retired.search(p.read_text(encoding="utf-8"))]
    assert hits == []
    html = (ROOT / "form_guard" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'data-t="Sonnet 5 のみ|Sonnet 5 only"' in html and 'data-t="Haiku 4.5 のみ|Haiku 4.5 only"' in html


# ------------------------------------------- R1-A14 the report is read under the run's lock


def test_r1_audit2_a14_the_report_is_read_under_the_runs_lock(api):
    import threading

    run = api.post("/api/runs", json={"demo_id": "form1", "mode": "demo"}).json()
    st = api.app.state.store.runs[run["run_id"]]
    held, released = threading.Event(), threading.Event()

    def hold():  # an edit in progress holds the lock while it rewrites report.html
        with st.lock:
            held.set()
            time.sleep(0.4)
            released.set()

    t = threading.Thread(target=hold)
    t.start()
    assert held.wait(5)
    resp = api.get(f"/api/runs/{run['run_id']}/report")
    answered_after_release = released.is_set()  # read before joining the holder thread
    t.join(5)
    assert resp.status_code == 200 and "<html" in resp.text
    assert answered_after_release  # before the fix the page was read while the edit still held the lock


# ------------------------------------------- R1-A15 / R1-O8 installing the Claude Code setup, subagent Bash limits


def test_r1_audit2_a15_o8_docs_say_how_to_install_the_claude_code_setup_and_what_limits_subagent_bash():
    agent = (ROOT / ".claude" / "agents" / "field-mapper.md").read_text(encoding="utf-8")
    assert "would write, move or delete a blank template under `samples/blank_forms/`." in agent
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    doc = " ".join((ROOT / "docs" / "BUILT_WITH_CLAUDE_CODE.md").read_text(encoding="utf-8").split())
    for text in (readme, doc):
        assert "pip install -r requirements.txt" in text and "from the repository root" in text
        assert "point `.mcp.json` and `.claude/settings.json` at" in text and "is not enough" in text
        assert "which blocks any write, move or deletion targeting `samples/blank_forms/`" in text
        assert "置くだけでは" in text and "リポジトリ直下から" in text
    assert "can drop `.mcp.json` and `.claude/` into their document folder" not in readme
    assert "the others use `Bash` solely for read-only verification commands" not in doc


# ------------------------------------------- Playtest 2026-11-03（火）: 60 trials (offline), regressions for its findings

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _form1_with_reason(samples_dir, reason: str) -> str:
    memo = read_memo(paths(samples_dir, "form1")["memo"])
    head, rest = memo.split("■ 変更理由と予算への影響（届出書は200字以内）\n", 1)
    return head + "■ 変更理由と予算への影響（届出書は200字以内）\n" + reason + "\n" + rest.split("\n", 1)[1]


def test_playtest_abbreviated_era_dates_like_r8_12_15_are_read_and_written_as_wareki():
    got = {s: [(d.year, d.month, d.day, d.weekday, d.era) for d in find_dates(s)]
           for s in ("R8.12.15（火）", "R8/12/15", "Ｒ８．１２．１５（火）", "H31.4.30（火）", "令和8.12.15", "S64.1.7")}
    assert got == {"R8.12.15（火）": [(2026, 12, 15, "火", "令和")], "R8/12/15": [(2026, 12, 15, None, "令和")],
                   "Ｒ８．１２．１５（火）": [(2026, 12, 15, "火", "令和")], "H31.4.30（火）": [(2019, 4, 30, "火", "平成")],
                   "令和8.12.15": [(2026, 12, 15, None, "令和")], "S64.1.7": [(1989, 1, 7, None, "昭和")]}
    assert [d.problem()[0] for d in find_dates("R1.4.30")] == ["date_invalid"]  # 令和 began on 2019-05-01
    for not_a_date in ("見積書No.Q-2026-118", "AR8.12.15", "R8.12.15.3", "R8.12/15", "補助率1/3", "AC-2E"):
        assert not find_dates(not_a_date), not_a_date
    slot = _text_slot("提出日", fmt="wareki_date")
    for src in ("R8.12.15（火）", "R8/12/15"):
        m = map_fields([slot], f"提出日：{src}に提出\n", DemoEngine()).mappings[0]
        assert m.status == "filled" and m.filled_value == "令和8年12月15日（火）", (src, m.errors)


def test_playtest_condensation_never_silently_drops_a_restriction_such_as_ni_kagiru(samples_dir, tmp_path):
    from form_guard.textnorm import RESTRICTIVE_WORDS

    for s in ("学内の共用計算機に限る。", "対象は市内の事業者に限ります。", "利用は平日に限定する。", "土日以外は使用しない。",
              "研究目的外の利用は対象外とする。", "夜間は入館できない。", "データは学外に持ち出してはならない。", "試料を廃棄しないこと。"):
        assert RESTRICTIVE_WORDS.search(s), s
    for s in ("6回が実施できなくなった。", "清掃のみで異常なし。"):  # a fact or a status remark is not a restriction
        assert not RESTRICTIVE_WORDS.search(s), s
    c = condense_extractive("第1文で日付は2026年12月1日。計算サーバは学内の共用計算機に限る。", 20)
    assert c is not None and c.unsafe and c.removed == ["計算サーバは学内の共用計算機に限る。"]  # only as a suggestion
    reason = ("共同研究先の試験設備が2026年12月から改修工事に入り、当初予定していた実機試験12回のうち6回が実施できなくなった。"
              "計算サーバの利用は学内の共用計算機に限る。"
              "そこで不足分を数値シミュレーションで補うこととした。なお、シミュレーションの妥当性は残る6回の実機試験結果と比較して検証する。"
              "予算面では、試験装置の借用料が42万円減る一方、計算サーバ利用料として80万円が新たに必要になるため、差し引きで38万円の増額となる。"
              "増額分は共同研究先からの追加負担金で賄うことで合意済み（直接経費総額に占める割合は約6％で、20％未満のため経費変更承認申請は不要）。")
    r = run_pipeline(paths(samples_dir, "form1")["form"], _form1_with_reason(samples_dir, reason), tmp_path / "t12", mode="demo")
    m = next(x for x in r.mappings if x.slot_id == "変更理由および予算影響")
    # before the fix 「…に限る。」 was the first sentence deleted, without any warning, and the value was written
    assert m.status == "filled" and "計算サーバの利用は学内の共用計算機に限る。" in m.filled_value and len(m.filled_value) <= 200
    assert r.report.overall_status == "PASS"


def test_playtest_memo_intake_removes_invisible_characters_and_composes_nfd_kana(samples_dir, tmp_path, demo_runs):
    from form_guard.pipeline import verify_filled_copy
    from form_guard.textnorm import clean_text

    assert clean_text("株式会社サンプル​精工") == ("株式会社サンプル精工", [
        "1 invisible character(s) removed (zero-width space, BOM, soft hyphen …)",
        "1 kana + combining sound mark pair(s) composed (NFD → NFC, e.g. フ+゜ → プ)"])
    assert clean_text("神神 ﾌﾟ ア゙") == ("神神 ﾌﾟ ア゙", [])  # 異体字, half-width kana: never changed
    p2 = paths(samples_dir, "form2")
    memo2 = read_memo(p2["memo"]).replace("申請事業者名：株式会社サンプル精工", "申請事業者名：株式会社サンプル精工")
    r2 = run_pipeline(p2["form"], memo2, tmp_path / "nfd", mode="demo")
    assert next(x for x in r2.mappings if x.slot_id == "申請事業者名").filled_value == "株式会社サンプル精工"
    assert "゚" not in r2.memo_text and r2.to_dict()["memo"]["normalized"] == [
        "1 kana + combining sound mark pair(s) composed (NFD → NFC, e.g. フ+゜ → プ)"]
    assert not _failing(verify_filled_copy(p2["form"], r2.filled_path, memo2).report)  # the raw memo verifies as well
    p1 = paths(samples_dir, "form1")
    memo1 = read_memo(p1["memo"]).replace("ＪＲ－２０２６－０４７", "ＪＲ－２０２６​－０４７")
    r1 = run_pipeline(p1["form"], memo1, tmp_path / "zwsp", mode="demo")
    assert next(x for x in r1.mappings if x.slot_id == "研究課題番号").filled_value == "JR-2026-047"  # was: needs input
    assert r1.report.overall_status == "PASS" and "​" not in r1.memo_text
    assert "normalized" not in demo_runs("form1").to_dict()["memo"] and demo_runs("form1").builtin_demo


def test_playtest_two_different_values_for_one_field_in_the_memo_go_to_the_reviewer():
    date_slot, yen_slot = _text_slot("申請日", fmt="wareki_date"), _text_slot("導入予定金額", fmt="yen")
    memo = "申請日：2026年11月2日（月）\n導入予定金額：483万6千円\n（訂正）\n申請日：2026年11月9日（月）\n導入予定金額：500万円\n"
    maps = {m.slot_id: m for m in map_fields([date_slot, yen_slot], memo, DemoEngine()).mappings}
    for sid, first, second in (("申請日", "L1", "L4"), ("導入予定金額", "L2", "L5")):
        m = maps[sid]  # before the fix the first statement was written silently
        assert m.status == "needs_human_input" and not m.filled_value and m.suggested_value
        assert any("more than once" in e and first in e and second in e for e in m.errors), m.errors
    same = "申請日：2026/11/2（月）\n申請日：令和8年11月2日（月）\n導入予定金額：483万6千円\n導入予定金額：4,836,000円\n"
    maps = {m.slot_id: m for m in map_fields([date_slot, yen_slot], same, DemoEngine()).mappings}
    assert maps["申請日"].filled_value == "令和8年11月2日（月）" and maps["導入予定金額"].filled_value == "4,836,000円"


def test_playtest_reviewer_values_are_normalised_into_the_printed_format(samples_dir, api):
    sc = scan(paths(samples_dir, "form2")["form"])
    cases = {
        ("法人番号", "１２３４５６７８９０１２３"): ("1234567890123", ""), ("法人番号", "1234-5678-90123"): ("1234567890123", ""),
        ("法人番号", "123456789012"): ("123456789012", "13 digits required, 12 given / 13桁の数字が必要です（入力は12桁）"),
        ("補助金申請額", "52000"): ("52,000円", ""), ("補助金申請額", "５万２千円"): ("52,000円", ""),
        ("補助金申請額", "52,00"): ("52,00", "not a yen amount"), ("補助金申請額", "約5万円"): ("約5万円", "not a yen amount"),
        ("補助金申請額", "1200000"): ("1,200,000円", "exceeds the printed cap"),
        ("申請日", "2026/11/4"): ("令和8年11月4日（水）", ""), ("申請日", "2026年11月4日" + "（木）"): ("2026年11月4日" + "（木）", "曜日"),
        ("事業実施期間", "2026/12/1～2027/2/26"): ("令和8年12月1日（火）～令和9年2月26日（金）", ""),
    }
    for (sid, typed), (value, error) in cases.items():
        m = human_mapping(sc.slot(sid), typed)
        assert m.filled_value == value and (error in " ".join(m.errors) if error else not m.errors), (typed, m.errors)
        assert any("normalised" in w for w in m.warnings) == (value != typed)
    run = api.post("/api/runs", json={"demo_id": "form2", "mode": "demo"}).json()
    ok = api.post(f"/api/runs/{run['run_id']}/overrides", json={"values": {"法人番号": "１２３４５６７８９０１２３"}}).json()
    m = next(x for x in ok["mappings"] if x["slot_id"] == "法人番号")
    assert ok["report"]["overall_status"] == "PASS" and m["filled_value"] == "1234567890123" and "halfwidth" in m["normalizations"]


def test_playtest_negative_coordinates_in_a_slot_spec_never_wrap_around(samples_dir):
    form4 = unmarked_paths(next(u for u in UNMARKED_FORMS if u["id"] == "form4"), samples_dir)["form"]
    for tgt in ({"table": 0, "row": -1, "col": 2}, {"table": -1, "row": 1, "col": 2}, {"table": 0, "row": 1, "col": -1},
                {"table": 0, "row": True, "col": 2}, {"table": 0, "row": 1.0, "col": 2}, {"table": "0", "row": "x", "col": 2}):
        spec = {"targets": [{"slot_id": "x", **tgt}]}
        sc = scan(form4, spec)  # row -1 used to address the last row (受付・審査欄) of the table
        assert not sc.slots and "not found" in sc.warnings[0], tgt
        assert office_only_targets(form4, spec) == [], tgt
    assert [s.slot_id for s in scan(form4, {"targets": [{"slot_id": "x", "table": "0", "row": "1", "col": "2"}]}).slots] == ["x"]
    assert office_only_targets(form4, {"targets": [{"slot_id": "x", "table": 0, "row": 14, "col": 2}]})  # still guarded


def test_playtest_every_run_endpoint_refuses_a_blank_memo(api, samples_dir):
    blank = "  \n\t"
    d = api.post("/api/discover/demo", json={"demo_id": "form4", "mode": "demo"}).json()
    with paths(samples_dir, "form1")["form"].open("rb") as fh:
        up = api.post("/api/runs/upload", files={"file": ("form1.docx", fh, DOCX_MIME)}, data={"memo_text": blank, "mode": "demo"})
    answers = [api.post("/api/runs", json={"demo_id": "form1", "memo_text": blank, "mode": "demo"}),
               api.post("/api/run-demo", json={"demo_id": "form2", "memo_text": blank, "mode": "demo"}),
               api.post("/api/run-demo", json={"demo_id": "form5", "memo_text": "", "mode": "demo"}),
               api.post("/api/runs/from-discovery", json={"discovery_id": d["discovery_id"], "confirmed_targets": "recommended",
                                                          "memo_text": blank, "mode": "demo"}), up]
    assert [r.status_code for r in answers] == [400] * 5  # /api/runs, /api/run-demo and the upload used to answer 200
    assert all("memo_text is required" in r.json()["detail"] for r in answers)
    assert api.post("/api/runs", json={"demo_id": "form1", "mode": "demo"}).status_code == 200  # the sample memo


def test_playtest_vis01_names_the_field_whose_value_no_longer_fits(api, samples_dir, tmp_path):
    run = api.post("/api/runs", json={"demo_id": "form2", "mode": "demo"}).json()
    rid = run["run_id"]
    bad = api.post(f"/api/runs/{rid}/overrides", json={"values": {
        "法人番号": "1234567890123", "所在地": "架空市中央区架空町一丁目2番3号 架空ビル5階"}}).json()
    vis = next(c for c in bad["report"]["checks"] if c["check_id"] == "VIS-01")
    assert bad["report"]["overall_status"] == "FAIL" and not bad["can_approve"]
    assert "caused by 「所在地」" in vis["details"] and "「所在地」の値が欄に収まりません" in vis["details"]
    assert vis["evidence"]["fields"] == ["所在地"] and bad["visual"]["grown_slots"][0]["slot_id"] == "所在地"
    ok = api.post(f"/api/runs/{rid}/overrides", json={"values": {"所在地": "架空市中央区架空町1-2-3"}}).json()
    assert ok["report"]["overall_status"] == "PASS" and ok["visual"]["grown_slots"] == []
    p3 = paths(samples_dir, "form3")  # Excel cells never grow: a value wider than its cell overflows — named as well
    memo3 = read_memo(p3["memo"]).replace("点検対象施設：架空市立サンプル技術交流センター（本館）",
                                          "点検対象施設：" + "架空市立サンプル技術交流センター本館" * 3)
    v3 = check(run_pipeline(p3["form"], memo3, tmp_path / "wide", mode="demo").report, "VIS-01")
    assert not v3.passed and "caused by 「点検対象施設」" in v3.details


def test_playtest_edits_work_on_discovery_runs_and_on_imported_unmarked_mcp_drafts(samples_dir, tmp_path):
    from fastapi.testclient import TestClient

    from form_guard.mcp_server import FormGuardMCP
    from form_guard.server import create_app

    root = _mcp_root(tmp_path, samples_dir)
    app = create_app(run_root=tmp_path / "web_runs", samples_dir=samples_dir, project_root=root,
                     import_roots=[root / "output" / "mcp_runs", root / "output"])
    api = TestClient(app)
    d = api.post("/api/discover/demo", json={"demo_id": "form4", "mode": "demo"}).json()
    run = api.post("/api/runs/from-discovery", json={"discovery_id": d["discovery_id"], "confirmed_targets": "recommended",
                                                     "mode": "demo"}).json()
    value = "架空学術機関 材料工学研究室（第2研究棟）"
    edited = api.post(f"/api/runs/{run['run_id']}/overrides", json={"values": {"所属機関": value}}).json()
    assert edited["report"]["overall_status"] == "PASS" and edited["revision"] == 1  # the run's slot spec is kept
    assert edited["confirmed_candidates"] == run["confirmed_candidates"] and len(edited["slots"]) == 7
    st = app.state.store.runs[run["run_id"]]
    assert infer_values_from_filled(st.result.scan, st.result.filled_path)["所属機関"] == value
    srv = FormGuardMCP(root=root)
    form4 = "samples/blank_forms/form4_unmarked_blank.docx"
    disc = srv.call_tool("form_guard_discover_blank_cells", {"form_path": form4})["structuredContent"]
    f = srv.call_tool("form_guard_fill", {"form_path": form4, "memo_path": "samples/memos/memo4_equipment_use.txt",
                                          "discovery": disc, "confirmed_ids": recommended_ids(disc)})["structuredContent"]
    imported = api.get(f"/api/runs/{f['run_id']}").json()  # imported with the confirmed cells recorded in web_handoff.json
    e = api.post(f"/api/runs/{f['run_id']}/overrides", json={"values": {"所属機関": "架空学術機関 材料工学研究室 第3実験室"}}).json()
    assert e["report"]["overall_status"] == "PASS" and [s["slot_id"] for s in e["slots"]] == [s["slot_id"] for s in imported["slots"]]
    appr = api.post(f"/api/runs/{f['run_id']}/approve", json={"reviewer": "架空 花子", "checked": _required(e), **_seen(e)})
    assert appr.status_code == 200 and api.get(appr.json()["download_url"]).status_code == 200


def test_playtest_hitl_prompts_come_in_each_language_and_the_recorded_prompt_is_unchanged(demo_runs):
    items = {it.item_id: it for d in ("form1", "form2") for it in demo_runs(d).report.hitl_checklist}
    assert all(it.prompt_ja and it.prompt_en and " / " in it.prompt for it in items.values())
    reason = items["slot:変更理由および予算影響"]
    assert "字数制限のため削除した文" in reason.prompt_ja and "Condensed for char limit" not in reason.prompt_ja
    assert "deleted to meet the character limit" in reason.prompt_en and "字数制限" not in reason.prompt_en
    assert items["slot:法人番号"].prompt_en == "«法人番号» was left empty: the memo gives no evidence for it. Enter the value yourself."
    assert items["slot:提出日"].prompt_en == "Confirm «提出日» = «令和8年11月2日（月）» against the quoted source L10."
    assert items["global:visual"].prompt_ja == "比較表示で、指定欄以外（罫線・結合セル・注意事項・誓約文）に変更が無いことを目視確認した"
    assert items["slot:提出日"].prompt.startswith(items["slot:提出日"].prompt_ja + " / Confirm this value")  # audit log text


def test_playtest_web_ui_language_and_layout_fixes(api, demo_runs):
    from form_guard.pipeline import _drift_check

    html, js, css = (api.get(u).text for u in ("/", "/static/app.js", "/static/styles.css"))
    for needle in ('data-bi="SHA-256（原本）|SHA-256 (original)"', 'data-bi="結果|Result"', 'data-bi="変更内容|Change"',
                   'data-bi="バイト範囲|Bytes"'):
        assert needle in html, needle
    for needle in ("L(d.label, `Demo ${d.number}`)", 'L("空欄自動検出", "auto-detect")', '"(empty — awaiting input)"',
                   '"Japanese era date"', "L(it.prompt_ja || it.prompt, it.prompt_en || it.prompt)", "failedChecksText(run)",
                   "modelName(alias || id)", "engineText(d.engine.label)", "statusText(r.status)"):
        assert needle in js, needle
    assert "（未記入 — awaiting input）" not in js and '"和暦 date"' not in js
    # every check description the verifier emits (21 checks, naive comparison, VIS-01) has a Japanese translation
    descs = {c.description for d in ("form1", "form2", "form3") for rep in (demo_runs(d).report, demo_runs(d).naive_report)
             for c in rep.checks}
    descs.add(_drift_check({"drift_outside_slots": [], "overflow_count": 0, "drift_count": 1}).description)
    assert len(descs) >= 30 and not [x for x in descs if json.dumps(x, ensure_ascii=False) + ":" not in js]
    assert ".run-links .pill, .run-links .pill .pri, .run-links .pill .sub { white-space: normal; }" in css  # 1280 px
    assert "@media (max-width: 1366px) { .top-actions .sub { display: none; } }" in css
    assert ".preview-side .btn { white-space: normal;" in css and "background: var(--surface); max-width: 300px; }" in css


# ------------------------------------------- playtest round 2: R1 /check fixes and the T11 / T25 extensions


def test_playtest_r1_a1_conditions_in_a_trailing_note_and_interim_measures_are_never_cut_silently():
    from form_guard.textnorm import strip_trailing_note

    for note in ("（※利用は平日に限る）", "（※利用は平日のみ）", "(note: 交換完了までは使用しない)"):
        assert strip_trailing_note("設備を使用する。" + note) == "設備を使用する。" + note, note  # part of the value
    assert strip_trailing_note("架空 太郎（※先方に確認済み）") == "架空 太郎"  # a note to self is still dropped
    m = map_fields([_text_slot("利用条件")], "利用条件：設備を使用する。（※利用は平日に限る）\n", DemoEngine()).mappings[0]
    assert m.status == "filled" and m.filled_value == "設備を使用する。（※利用は平日に限る）"  # the note used to be cut
    # an interim measure / scope limit (protection 1) goes only as a last resort, and then a human decides
    text = "点検結果を報告する。交換完了までは運転時間を短縮する。"
    c = condense_extractive(text, 10)
    assert c is not None and c.text == "点検結果を報告する。" and c.unsafe and c.held == ["交換完了までは運転時間を短縮する。"]
    slot = _text_slot("報告", max_chars=10)
    m = map_fields([slot], text, FixedEngine({"報告": Draft("報告", value=text, quotes=[text], confidence=0.9)})).mappings[0]
    assert m.status == "needs_human_input" and not m.is_written and "unsafe_condensation" in m.normalizations
    assert any("「交換完了までは運転時間を短縮する。」" in e for e in m.errors), m.errors  # the cut measure is named
    remark = condense_extractive("点検は完了した。なお、給湯室の換気扇は清掃のみで異常なし。", 8)
    assert remark is not None and not remark.unsafe  # 'のみ' in a status remark is no scope limit


def test_playtest_r1_a2_odd_digit_grouping_and_fractions_of_a_yen_are_never_read_as_amounts(samples_dir):
    for bad in ("52,00", "1,,000", "1,00,000", "1.5", "0.5", "52,00万"):
        with pytest.raises(ValueError):
            parse_compound(bad)
    assert [parse_compound(s) for s in ("1.5万", "1.5千", "0万", "0万5千", "2億0万", "1,612,000", "1億2,000万", "483万6千")] \
        == [15_000, 1_500, 0, 5_000, 200_000_000, 1_612_000, 120_000_000, 4_836_000]  # exact decimal arithmetic
    assert kanji_to_int("〇万") == 0 and kanji_to_int("〇万五千") == 5_000 and kanji_to_int("万") == 10_000
    assert [a.value for a in find_amounts("〇万円")] == [0] and [a.value for a in find_amounts("〇万五千円")] == [5_000]
    for text in ("52,00円", "1,,000円", "1.5円", "0.5円"):  # used to become 5,200円 / 1,000円 / 2円 / 0円
        assert find_amounts(text) == [], text
    for text in ("52,00万円", "52,00 万円", "1.23456万円", "12,34千円"):  # nor is their bare 万円 / 千円 read as 1万 / 1千
        assert find_amounts(text) == [] and quantities(text) == set(), text
    assert [a.value for a in find_amounts("千円、5千円、1.5万円、数万円、何千円、十数万円")] == [1_000, 5_000, 15_000]
    cap = _text_slot("補助金申請額", fmt="yen", max_value=1_000_000, multiple_of=1000)
    m = map_fields([cap], "補助金申請額：52,00万円\n", DemoEngine()).mappings[0]
    assert m.status == "needs_human_input" and not m.filled_value  # was written as 10,000円
    sc = scan(paths(samples_dir, "form2")["form"])
    for typed in ("52,00円", "52,00", "1,,000円", "1.5円", "0.5円"):
        m = human_mapping(sc.slot("補助金申請額"), typed)
        assert m.filled_value == typed and not m.validation_passed, typed
        assert any("not a yen amount" in e for e in m.errors), (typed, m.errors)
    for typed, want in (("52000", "52,000円"), ("５万２千円", "52,000円"), ("1.5万円", "15,000円")):
        m = human_mapping(sc.slot("補助金申請額"), typed)
        assert m.filled_value == want and not m.errors, (typed, m.errors)


def test_playtest_r1_a3_contradicting_lines_under_two_labels_or_with_two_weekdays_go_to_the_reviewer():
    from form_guard.mapper import _same_fact

    slot = FormSlot(slot_id="提出日", label="提出日", location_path="test", file_type="docx", current_text="",
                    required_format="wareki_date", aliases=["申請日"])
    m = map_fields([slot], "申請日：2026/11/2\n提出日：2026/11/9\n", DemoEngine()).mappings[0]
    assert m.status == "needs_human_input" and not m.filled_value  # the 提出日 line used to be ignored
    assert any("more than once" in e and "L1" in e and "L2" in e for e in m.errors), m.errors
    date_slot = _text_slot("申請日", fmt="wareki_date")
    wrong = "2026/11/2" + "（火）"  # 2026-11-02 is a Monday
    assert not _same_fact(date_slot, "2026/11/2（月）", wrong)
    assert _same_fact(date_slot, "2026/11/2（月）", "令和8年11月2日（月）") and _same_fact(date_slot, "2026/11/2（月）", "2026/11/2")
    m = map_fields([date_slot], f"申請日：2026/11/2（月）\n申請日：{wrong}\n", DemoEngine()).mappings[0]
    assert m.status == "needs_human_input" and any("more than once" in e for e in m.errors), m.errors
    same = map_fields([date_slot], "申請日：2026/11/2（月）\n申請日：令和8年11月2日（月）\n", DemoEngine()).mappings[0]
    assert same.status == "filled" and same.filled_value == "令和8年11月2日（月）"
    # a generated short name (変更 for 変更予定日) neither supplies the value nor raises a conflict
    planned = _text_slot("変更予定日", fmt="wareki_date")
    m = map_fields([planned], "変更予定日：未定（理事会で決定）\n・変更：2026年11月2日（月）に理事会へ諮った\n", DemoEngine()).mappings[0]
    assert m.status == "needs_human_input" and not m.filled_value and not m.suggested_value
    name = FormSlot(slot_id="申請者氏名", label="申請者氏名", location_path="test", file_type="docx", current_text="",
                    required_format="person_name")
    m = map_fields([name], "申請者氏名：見本 太郎\n担当：\n・氏名：架空 花子\n", DemoEngine()).mappings[0]
    assert m.status == "filled" and m.filled_value == "見本 太郎" and not m.errors


def test_playtest_r1_a4_dates_before_showa_are_refused_cleanly_in_wareki_fields(api):
    slot = _text_slot("提出日", fmt="wareki_date")
    for v in ("1926/12/24", "1926年12月24日"):
        issues = validate_value(slot, v)
        assert [i.code for i in issues] == ["date_invalid"] and "before the supported eras" in issues[0].message, issues
        m = human_mapping(slot, v)  # used to raise ValueError: date before supported eras
        assert m.filled_value == v and m.errors and "昭和より前" in m.errors[0]
    assert human_mapping(slot, "1926/12/25").filled_value == "昭和元年12月25日（土）" == to_wareki(date(1926, 12, 25))
    m = map_fields([slot], "提出日：1926年12月24日に提出\n", DemoEngine()).mappings[0]
    assert m.status == "needs_human_input" and not m.filled_value and any("before the supported eras" in e for e in m.errors)
    run = api.post("/api/runs", json={"demo_id": "form2", "mode": "demo"}).json()
    r = api.post(f"/api/runs/{run['run_id']}/overrides", json={"values": {"申請日": "1926/12/24"}})
    assert r.status_code == 200  # was a 500
    m2 = next(x for x in r.json()["mappings"] if x["slot_id"] == "申請日")
    assert m2["status"] == "needs_human_input" and "昭和より前" in " ".join(m2["errors"])


def test_playtest_r1_a5_every_date_path_adds_the_weekday_and_condensation_never_drops_one_silently(demo_runs):
    wareki, western = _text_slot("申請日", fmt="wareki_date"), _text_slot("申請日", fmt="date")
    for slot, typed, want in ((wareki, "令和8年11月4日", "令和8年11月4日（水）"), (wareki, "2026/11/4", "令和8年11月4日（水）"),
                              (western, "2026/11/4", "2026年11月4日（水）"), (western, "2026年11月4日", "2026年11月4日（水）"),
                              (western, "令和8年11月4日", "令和8年11月4日（水）"), (wareki, "令和8年11月4日（水）", "令和8年11月4日（水）")):
        m = human_mapping(slot, typed)  # a valid date without its weekday used to be kept as typed
        assert m.filled_value == want and not m.errors, (typed, m.filled_value, m.errors)
        assert ("weekday" in m.normalizations) == ("（" not in typed), (typed, m.normalizations)
    m = map_fields([western], "申請日：2026/11/4\n", DemoEngine()).mappings[0]
    assert m.status == "filled" and m.filled_value == "2026年11月4日（水）" and "weekday" in m.normalizations
    short = _text_slot("申請日", fmt="date", max_chars=11)  # the weekday is only added where it fits
    for typed in ("2026年11月4日", "2026/11/4"):
        m = human_mapping(short, typed)
        assert m.filled_value == typed and not m.errors, (typed, m.errors)
    c = condense_extractive("11月6日（金）までにフィルターを交換する。", 21)
    assert c is not None and c.text == "11月6日までにフィルターを交換する。" and c.unsafe and c.held == ["（金）"]
    c = condense_extractive("11月6日（金）までにフィルターを交換する。部品は3点を交換した。", 30)  # a safe deletion first
    assert c is not None and c.text == "11月6日（金）までにフィルターを交換する。" and not c.unsafe
    m3 = next(x for x in demo_runs("form3").mappings if x.slot_id == "指摘事項および是正措置")
    assert m3.status == "filled" and "11月6日（金）までに" in m3.filled_value and "11月20日（金）までに" in m3.filled_value
    assert "（金）" not in m3.condensed_removed and len(m3.filled_value) == 120  # both weekdays survive the condensation


def test_playtest_r1_o4_bidirectional_control_characters_are_removed_at_intake(samples_dir, tmp_path):
    from form_guard.textnorm import clean_text

    for ch in ("‮", "‪", "‭", "⁦", "⁩", "؜"):
        assert clean_text(f"JR-2026{ch}-047") == ("JR-2026-047", [
            "1 invisible character(s) removed (zero-width space, BOM, soft hyphen …)"]), hex(ord(ch))
    p1 = paths(samples_dir, "form1")
    memo = read_memo(p1["memo"]).replace("ＪＲ－２０２６－０４７", "ＪＲ－２０２６‮－０４７")  # RIGHT-TO-LEFT OVERRIDE
    r = run_pipeline(p1["form"], memo, tmp_path / "rlo", mode="demo")
    assert next(x for x in r.mappings if x.slot_id == "研究課題番号").filled_value == "JR-2026-047"
    assert "‮" not in r.memo_text and r.report.overall_status == "PASS"


def test_playtest_r1_o5_o6_held_values_explain_themselves_in_each_language_and_check_texts_are_english(api, demo_runs,
                                                                                                      samples_dir, tmp_path):
    from form_guard.mapper import ROUTING_POLICY

    run = api.post("/api/runs", json={"demo_id": "form2", "mode": "demo"}).json()
    bad = api.post(f"/api/runs/{run['run_id']}/overrides", json={"values": {"申請日": "2026年11月4日" + "（木）"}}).json()
    item = next(h for h in bad["report"]["hitl_checklist"] if h["slot_id"] == "申請日")
    assert "曜日が暦と一致しません" in item["prompt_ja"] and "is a 水曜日, not" not in item["prompt_ja"]
    assert "is a 水曜日, not （木）" in item["prompt_en"] and "曜日が暦と一致しません" not in item["prompt_en"]
    assert "is a 水曜日, not （木） / 曜日が暦と一致しません" in item["prompt"]  # the recorded prompt keeps both halves
    p2 = paths(samples_dir, "form2")  # an English half that itself lists values with " / " is split at the last one
    memo = read_memo(p2["memo"]).replace("【その他】\n申請日：2026年11月2日（月）",
                                         "【その他】\n申請日：2026年11月2日（月）\n申請日：2026年11月9日（月）")
    r = run_pipeline(p2["form"], memo, tmp_path / "dup", mode="demo")
    held = next(h for h in r.report.hitl_checklist if h.slot_id == "申請日")
    assert "メモに同じ項目の異なる値があります" in held.prompt_ja and "a human must decide" not in held.prompt_ja
    assert "more than once" in held.prompt_en and "メモに同じ項目" not in held.prompt_en
    r1 = demo_runs("form1")  # a reason with no " / " (an English-only validator message) is shown in both languages
    why = "変更 is not an allowed option (研究内容／研究経費)"
    maps = [m.model_copy(update={"status": "needs_human_input", "filled_value": "", "suggested_value": "変更",
                                 "errors": [why]}) if m.slot_id == "変更種別" else m for m in r1.mappings]
    item = next(h for h in build_hitl_checklist(r1.scan, maps, r1.report) if h.slot_id == "変更種別")
    assert f"({why})" in item.prompt_en and f"（{why}）" in item.prompt_ja
    con02 = check(demo_runs("form1").report, "CON-02")
    assert con02.description == "Formats (Japanese era dates, yen amounts, ID patterns) are valid"
    js = (ROOT / "form_guard" / "static" / "app.js").read_text(encoding="utf-8")
    assert '"Formats (Japanese era dates, yen amounts, ID patterns) are valid":' in js and '"Formats (和暦' not in js
    for d in ("form1", "form2", "form3"):  # check descriptions and routing texts are English in the English UI
        assert not [c.description for c in demo_runs(d).report.checks if _JAPANESE.search(c.description)], d
    assert not [p["en"] for p in ROUTING_POLICY if _JAPANESE.search(p["en"] + p["why_en"])]


FORM1_REASON_181 = ("共同研究先の試験設備が2026年12月から改修工事に入り、当初予定していた実機試験12回のうち6回が実施できなくなった。"
                    "そこで不足分を数値シミュレーションで補うこととした。予算面では、試験装置の借用料が42万円減る一方、計算サーバ利用料"
                    "として80万円が新たに必要になるため、差し引きで38万円の増額となる。増額分は共同研究先からの追加負担金で賄うことで合意済み。")


def test_playtest_r1_o7_condensation_keeps_the_decision_that_answers_the_problem(demo_runs):
    from form_guard.textnorm import protection

    remedy = "そこで不足分を数値シミュレーションで補うこととした。"
    assert protection(remedy) == 1 and protection("なお、詳細は後日報告することとした。") == 0
    reason = ("共同研究先の試験設備が2026年12月から改修工事に入り、当初予定していた実機試験12回のうち6回が実施できなくなった。"
              "計算サーバの利用は学内の共用計算機に限る。" + remedy + "なお、シミュレーションの妥当性は残る6回の実機試験結果と比較して"
              "検証する。予算面では、試験装置の借用料が42万円減る一方、計算サーバ利用料として80万円が新たに必要になるため、差し引きで"
              "38万円の増額となる。増額分は共同研究先からの追加負担金で賄うことで合意済み（直接経費総額に占める割合は約6％で、20％未満の"
              "ため経費変更承認申請は不要）。")
    c = condense_extractive(reason, 200)  # the remedy used to be the sentence deleted (the text jumped to the restriction)
    assert c is not None and not c.unsafe and len(c.text) <= 200
    assert remedy in c.text and "計算サーバの利用は学内の共用計算機に限る。" in c.text and "38万円の増額となる。" in c.text
    held = condense_extractive("試験が実施できなくなった。そこで手順を変更することとした。", 13)
    assert held is not None and held.unsafe and held.held == ["そこで手順を変更することとした。"]  # only for a human
    r1 = demo_runs("form1")
    m1 = next(x for x in r1.mappings if x.slot_id == "変更理由および予算影響")
    assert m1.filled_value == FORM1_REASON_181 and len(m1.filled_value) == 181  # demo 1: the same 257 → 181 condensation
    assert r1.report.overall_status == "PASS" and r1.report.passed_count == 21 and not r1.report.failed_count


def test_playtest_t11_a_printed_advance_notice_rule_is_checked_across_the_two_dates(samples_dir, tmp_path, demo_runs, api):
    from form_guard.constraints import advance_notice_rules

    assert advance_notice_rules("1　本届出書は、変更予定日の30日前までに研究推進部研究協力課へ提出すること。") == [
        {"target": "変更予定日", "days": 30, "action": "提出", "printed": "変更予定日の30日前までに研究推進部研究協力課へ提出"}]
    assert advance_notice_rules("完了後30日以内に報告すること。") == []
    from form_guard.mapper import advance_notice_warnings
    from form_guard.models import FieldMapping

    filled = FieldMapping(slot_id="提出日", status="filled", filled_value="令和8年12月1日（火）", validation_passed=True)

    def warned(printed: str, memo: str) -> list[str]:
        rules = advance_notice_rules(printed)
        return advance_notice_warnings([_text_slot("提出日", fmt="wareki_date", advance_notice=rules)], [filled],
                                       memo)[0].warnings

    # the printed phrase is kept; the longest ending that is a memo label (or a field) names the other date
    assert advance_notice_rules("本届出書は変更予定日の30日前までに提出すること。")[0]["target"] == "本届出書は変更予定日"
    for printed in ("本届出書は変更予定日の30日前までに提出すること。", "必ず変更予定日の30日前までに提出する。"):
        w = warned(printed, "・変更予定日（2026年12月15日（火））\n")
        assert len(w) == 1 and "変更予定日 2026年12月15日（火） (memo L1) is only 14 day(s) after" in w[0], (printed, w)
    assert warned("取りやめ日の7日前までに届け出ること。", "締め日：2026年12月3日（木）\n") == []  # 「め日」 is no label
    for bad in ("30日前", [None], [{"target": "変更予定日", "days": "abc"}], [{"days": 30}]):  # a hand-written spec
        assert advance_notice_warnings([_text_slot("提出日", fmt="wareki_date", advance_notice=bad)], [filled],
                                       "変更予定日：2026年12月15日（火）\n")[0].warnings == [], bad
    p = paths(samples_dir, "form1")
    assert scan(p["form"]).slot("提出日").constraints["advance_notice"][0]["days"] == 30
    m0 = next(x for x in demo_runs("form1").mappings if x.slot_id == "提出日")  # 2026-11-02 → 2026-12-15: 43 days
    assert not [w for w in m0.warnings if w.startswith("advance notice")] and m0.status == "filled"
    memo = read_memo(p["memo"]).replace("提出日：2026年11月2日（月）に", "提出日：2026年12月1日（火）に")
    r = run_pipeline(p["form"], memo, tmp_path / "t11", mode="demo")
    m = next(x for x in r.mappings if x.slot_id == "提出日")
    warn = [w for w in m.warnings if w.startswith("advance notice")]
    assert m.status == "filled" and m.filled_value == "令和8年12月1日（火）" and len(warn) == 1  # the memo's date is kept
    assert "only 14 day(s) after" in warn[0] and "変更予定日の30日前までに提出" in warn[0] and "(memo L22)" in warn[0]
    item = next(h for h in r.report.hitl_checklist if h.slot_id == "提出日")
    assert "advance_notice" in item.flags and "まで14日しかありません" in item.prompt_ja and "14 day(s)" in item.prompt_en
    assert "まで14日しかありません" not in item.prompt_en and r.report.overall_status == "PASS"  # the reviewer decides
    run = api.post("/api/runs", json={"demo_id": "form1", "memo_text": memo, "mode": "demo"}).json()
    fixed = api.post(f"/api/runs/{run['run_id']}/overrides", json={"values": {"提出日": "2026/11/2"}}).json()
    mf = next(x for x in fixed["mappings"] if x["slot_id"] == "提出日")  # the reviewer's date is checked afresh
    assert mf["status"] == "human_entered" and not [w for w in mf["warnings"] if w.startswith("advance notice")]


def _era_template_docx(path: Path) -> None:
    d = docx.Document()
    d.add_paragraph("架空協会 会員情報変更届（テスト用の架空様式）")
    t = d.add_table(rows=3, cols=2)
    t.style = "Table Grid"
    for r, (label, printed) in enumerate((("届出日", "令和　　年　　月　　日"), ("氏名", ""), ("生年月日", "　　年　　月　　日"))):
        t.cell(r, 0).text = label
        if printed:
            t.cell(r, 1).text = printed
    d.save(path)


def test_playtest_t25_a_printed_era_date_template_cell_is_discovered_and_completed_in_place(tmp_path):
    from form_guard.scanner import era_date_template

    assert era_date_template("令和　　年　　月　　日") == (0, 11) and era_date_template(" 令和 年 月 日（　） ") == (1, 12)
    for no in ("令和8年　月　日", "　　年　　月　　日", "令和年月日", "届出日"):
        assert era_date_template(no) is None, no
    form = tmp_path / "era.docx"
    _era_template_docx(form)
    d = discover_blank_cells(form)
    c = next(x for x in d["candidates"] if x["slot_id"] == "届出日")  # was skipped as a non-empty cell
    assert c["required_format"] == "wareki_date" and c["recommended"] and c["printed_template"] == "令和　　年　　月　　日"
    assert [x["slot_id"] for x in d["candidates"]] == ["届出日", "氏名"]  # 「　　年　　月　　日」 names no era: not offered
    spec = build_confirmed_slot_spec(d, recommended_ids(d), form)
    r = run_pipeline(form, "届出日：2026年11月2日（月）\n氏名：見本 七郎\n", tmp_path / "filled", mode="demo", slot_spec=spec)
    assert r.report.overall_status == "PASS" and not _failing(r.report)
    t = docx.Document(str(r.filled_path)).tables[0]
    assert t.cell(0, 1).text == "令和8年11月2日（月）" and t.cell(2, 1).text == "　　年　　月　　日"
    assert [(p.kind, p.old_text) for p in r.write.patches if p.slot_id == "届出日"] == [("text", "令和　　年　　月　　日")]
    assert infer_values_from_filled(r.scan, r.filled_path)["届出日"] == "令和8年11月2日（月）"
    pending = run_pipeline(form, "氏名：見本 七郎\n", tmp_path / "pending", mode="demo", slot_spec=spec)
    assert pending.report.overall_status == "NEEDS_HUMAN_INPUT" and not _failing(pending.report)
    assert docx.Document(str(pending.filled_path)).tables[0].cell(0, 1).text == "令和　　年　　月　　日"
    sc = scan(form, {"targets": [{"slot_id": "x", "table": 0, "row": 0, "col": 0}]})  # other printed text: still refused
    assert not sc.slots and "not empty" in sc.warnings[0]
    tampered = tmp_path / "tampered.docx"  # text next to the date changed as well: caught
    rewrite_entry(r.filled_path, tampered, "word/document.xml",
                  lambda b: b.replace("令和8年11月2日（月）".encode(), "令和8年11月2日（月）提出済".encode()))
    assert "TEXT-02" in _failing(verify(r.scan, tampered, r.mappings, "届出日：2026年11月2日（月）\n氏名：見本 七郎\n"))
    wb = openpyxl.Workbook()  # Excel: the cell becomes one inline string; another cell with the same printed text stays
    ws = wb.active
    ws["A1"], ws["A2"], ws["B2"], ws["A3"] = "架空協会 届出書（テスト用の架空様式）", "届出日", "令和　　年　　月　　日", "氏名"
    ws["A4"], ws["B4"] = "変更日", "令和　　年　　月　　日"
    ws.column_dimensions["B"].width = 34
    xl = tmp_path / "era.xlsx"
    wb.save(xl)
    dx = discover_blank_cells(xl)
    cells = {x["cell"]: x for x in dx["candidates"]}
    assert cells["B2"]["printed_template"] == "令和　　年　　月　　日" and cells["B4"]["required_format"] == "wareki_date"
    spec_x = build_confirmed_slot_spec(dx, [cells[ref]["candidate_id"] for ref in ("B2", "B3", "B4")], xl)
    rx = run_pipeline(xl, "届出日：2026年11月2日（月）\n氏名：見本 七郎\n", tmp_path / "xl", mode="demo", slot_spec=spec_x)
    ws2 = openpyxl.load_workbook(rx.filled_path).active
    assert ws2["B2"].value == "令和8年11月2日（月）" and ws2["B3"].value == "見本 七郎" and ws2["B4"].value == "令和　　年　　月　　日"
    assert rx.report.overall_status == "NEEDS_HUMAN_INPUT" and not _failing(rx.report)  # 変更日 awaits the reviewer
    assert [p.kind for p in rx.write.patches if p.slot_id == "届出日"] == ["fill_template_cell"]


# ===================================================================== Phase 4
# P4-1 printed text checkboxes (□ → ■) in Word / Excel / PDF; P4-2 fixed PDF forms (pdf_core + pdf_support:
# PDF gate, blank-box discovery, incremental-update overlay, the 21 checks, previews and pixel diff, naive
# regeneration); P4-3 the fictional grant portal (/portal, /api/portal/forms), MCP / CLI / web flows and the
# portal-form-fill skill.

from form_guard import pdf_core, pdf_support  # noqa: E402
from form_guard.constraints import (  # noqa: E402
    canonical_checkbox_value,
    checkbox_constraints,
    checkbox_hold,
    normalize_checkbox_value,
    parse_checkbox_options,
)
from form_guard.samples import PDF_FORMS, SPECS, build_form6, form6_layout, pdf_paths  # noqa: E402

CHECKBOX_LINE = "□ 合意済み　　□ 協議中（協議予定日：　　　年　　月　　日）"  # form1, table 0, row 10, col 2
TWENTY_ONE = ["ZIP-01", "ZIP-02", "ZIP-03", "ZIP-04", "ZIP-05", "XML-01", "XML-02", "XML-03", "LAYOUT-01", "LAYOUT-02",
              "LAYOUT-03", "LAYOUT-04", "TEXT-01", "TEXT-02", "TEXT-03", "GRD-01", "GRD-02", "CON-01", "CON-02", "CON-03",
              "APP-01"]


def form6(samples_dir):
    return pdf_paths(PDF_FORMS[0], samples_dir)


def _form1_checkbox_spec():
    spec = json.loads(json.dumps(SPECS["form1_research_plan_change"]))
    spec["slots"]["共同研究先との合意"] = {"aliases": ["共同研究先との合意", "合意"], "description": "合意状況"}
    spec["targets"] = [{"slot_id": "共同研究先との合意", "table": 0, "row": 10, "col": 2, "kind": "checkbox_text"}]
    return spec


def failed(rep):
    return sorted(c.check_id for c in rep.checks if not c.passed)


# ------------------------------------------------------------------- P4-1 □ → ■ checkboxes


def test_p4_checkbox_options_are_parsed_normalised_and_held_for_a_human():
    opts = parse_checkbox_options(CHECKBOX_LINE)
    assert [(o["label"], o["offset"], o["needs_input"], o["checked"]) for o in opts] == [
        ("合意済み", 0, False, False), ("協議中", 8, True, False)]
    assert opts[1]["detail"] == "（協議予定日：　　　年　　月　　日）"
    assert parse_checkbox_options("合意済み") == [] and parse_checkbox_options("□ はい　□ はい") == []
    assert [o["checked"] for o in parse_checkbox_options("■ はい　□ いいえ")] == [True, False]
    for v in ("合意済み", "☑ 合意済み", "■合意済み", "■ 合意済み", "□合意済み", " 合意済み "):
        assert normalize_checkbox_value(v, ["合意済み", "協議中"]) == ["合意済み"], v
    assert normalize_checkbox_value("協議中（協議予定日：未定）", ["合意済み", "協議中"]) == ["協議中"]
    assert normalize_checkbox_value("☑協議中 ☑合意済み", ["合意済み", "協議中"]) == ["合意済み", "協議中"]  # printed order
    assert normalize_checkbox_value("保留", ["合意済み", "協議中"]) == ["保留"]  # unknown: kept, validation rejects it
    slot = _text_slot("合意状況", fmt="choice", **checkbox_constraints(opts))
    assert slot.constraints["requires_human_if_selected"] == ["協議中"] and slot.constraints["exclusive"]
    assert canonical_checkbox_value("☑ 合意済み", slot) == "合意済み" and canonical_checkbox_value("保留", slot) == "保留"
    assert [i.code for i in checkbox_hold(slot, "■ 協議中")] == ["requires_human"] and checkbox_hold(slot, "合意済み") == []
    assert [i.code for i in validate_value(slot, "合意済み、協議中") if i.severity == "error"]  # exclusive: one only
    assert [i.code for i in validate_value(slot, "保留") if i.severity == "error"] == ["allowed_values"]
    hm = human_mapping(slot, "■ 合意済み")
    assert (hm.status, hm.filled_value, hm.errors) == ("human_entered", "合意済み", [])


def test_p4_word_checkbox_ticks_exactly_one_square_in_place(samples_dir, tmp_path):
    p = paths(samples_dir, "form1")
    spec = _form1_checkbox_spec()
    sc = scan(p["form"], spec)
    s = sc.slot("共同研究先との合意")
    assert (s.required_format, s.constraints["allowed_values"], s.occurrences[0].marker_kind) == (
        "choice", ["合意済み", "協議中"], "checkbox_text")
    assert s.current_text == CHECKBOX_LINE and "printed □ options designated by coordinates" in s.location_path
    out = tmp_path / "only_checkbox.docx"
    res = fill_form(sc, {"共同研究先との合意": "☑ 合意済み"}, out)  # a marked value names the option
    (patch,) = res.patches
    assert (patch.kind, patch.old_text, patch.new_text, patch.byte_end - patch.byte_start) == ("checkbox", "□", "■", 3)
    o = zipfile.ZipFile(p["form"]).read("word/document.xml")
    f = zipfile.ZipFile(out).read("word/document.xml")
    diff = [i for i in range(len(o)) if o[i] != f[i]]
    assert len(o) == len(f) and diff == [patch.byte_start + 2]  # E2 96 A1 → E2 96 A0: one byte, same length
    assert o[patch.byte_start : patch.byte_end] == b"\xe2\x96\xa1" and f[patch.byte_start : patch.byte_end] == b"\xe2\x96\xa0"
    assert f.count("■".encode()) == 1 and "□ 協議中".encode() in f  # the unselected □ is untouched
    raw_o, raw_f = read_raw_zip(p["form"].read_bytes()), read_raw_zip(out.read_bytes())
    assert [e.raw_sha256 for e in raw_o.entries if e.name != "word/document.xml"] == \
        [e.raw_sha256 for e in raw_f.entries if e.name != "word/document.xml"]
    assert infer_values_from_filled(sc, out) == {"共同研究先との合意": "合意済み"}
    r = run_pipeline(p["form"], read_memo(p["memo"]), tmp_path / "run", mode="demo", slot_spec=spec)
    m = {x.slot_id: x for x in r.mappings}["共同研究先との合意"]
    assert m.status == "filled" and m.filled_value == "合意済み" and "合意済み" in m.source_quote
    assert r.report.overall_status == "PASS" and [c.check_id for c in r.report.checks] == TWENTY_ONE
    assert docx.Document(r.filled_path).tables[0].cell(10, 2).text == "■ 合意済み　　□ 協議中（協議予定日：　　　年　　月　　日）"
    assert r.visual["identical_outside_slots"] and 'data-slot-id="共同研究先との合意"' in r.previews["filled"]


def test_p4_word_checkbox_tampering_fails_xml03_text01_text02_and_con03(samples_dir, tmp_path):
    p = paths(samples_dir, "form1")
    spec = _form1_checkbox_spec()
    memo = read_memo(p["memo"])
    r = run_pipeline(p["form"], memo, tmp_path / "run", mode="demo", slot_spec=spec)
    cases = {
        "both ticked": (lambda b: b.replace("□ 協議中".encode(), "■ 協議中".encode()),
                        ["CON-03", "TEXT-01", "TEXT-02", "XML-03"]),
        "wrong option": (lambda b: b.replace("■ 合意済み".encode(), "□ 合意済み".encode())
                         .replace("□ 協議中".encode(), "■ 協議中".encode()), ["TEXT-01", "TEXT-02", "XML-03"]),
        "printed note changed": (lambda b: b.replace("協議予定日".encode(), "協議期日の".encode()),
                                 ["TEXT-01", "TEXT-02", "XML-03"]),
        "approved but not ticked": (lambda b: b.replace("■ 合意済み".encode(), "□ 合意済み".encode()), ["TEXT-02"]),
        "square as a character reference": (lambda b: b.replace("□ 協議中".encode(), "&#9633; 協議中".encode()),
                                            ["XML-03"]),
    }
    for name, (fn, expected) in cases.items():
        dst = tmp_path / f"{name.replace(' ', '_')}.docx"
        surgical_rewrite(r.filled_path, dst, "word/document.xml", fn)
        rep = verify(r.scan, dst, r.mappings, memo)
        assert failed(rep) == expected, (name, failed(rep))
        assert rep.overall_status == "FAIL", name


def test_p4_an_option_that_needs_more_input_is_never_ticked_by_the_ai(samples_dir, tmp_path):
    p = paths(samples_dir, "form1")
    spec = _form1_checkbox_spec()
    memo = read_memo(p["memo"]).replace("で合意済み（", "の扱いは協議中（")
    r = run_pipeline(p["form"], memo, tmp_path / "ai", mode="demo", slot_spec=spec)
    m = {x.slot_id: x for x in r.mappings}["共同研究先との合意"]
    assert m.status == "needs_human_input" and m.suggested_value == "協議中" and "a human must decide" in " ".join(m.errors)
    assert r.report.overall_status == "NEEDS_HUMAN_INPUT" and r.report.pending_slots == ["共同研究先との合意"]
    assert "■".encode() not in zipfile.ZipFile(r.filled_path).read("word/document.xml")
    # a verifier never accepts an AI value that ticks it (CON-03), whatever produced the file
    forged = [x.model_copy(update={"status": "filled", "filled_value": "協議中", "source_quote": "の扱いは協議中",
                                   "source_spans": []}) if x.slot_id == "共同研究先との合意" else x for x in r.mappings]
    bad = tmp_path / "forged.docx"
    fill_form(r.scan, {x.slot_id: x.filled_value for x in forged if x.is_written}, bad)
    rep = verify(r.scan, bad, forged, memo)
    assert "CON-03" in failed(rep) and "a human must decide" in check(rep, "CON-03").details
    # the reviewer may tick it (and completes the printed date by hand)
    human = run_pipeline(p["form"], memo, tmp_path / "human", mode="demo", slot_spec=spec,
                         overrides={"共同研究先との合意": "■ 協議中"})
    hm = {x.slot_id: x for x in human.mappings}["共同研究先との合意"]
    assert (hm.status, hm.filled_value) == ("human_entered", "協議中") and human.report.overall_status == "PASS"
    assert "■ 協議中".encode() in zipfile.ZipFile(human.filled_path).read("word/document.xml")


def _excel_shared_strings(src: Path, dst: Path) -> None:
    """openpyxl writes inline strings; Excel writes one shared string per distinct text — rebuild it that way."""
    zin = zipfile.ZipFile(src)
    sheet = etree.fromstring(zin.read("xl/worksheets/sheet1.xml"))
    strings: list[str] = []
    for c in sheet.iter(f"{{{S_NS}}}c"):
        if c.get("t") == "inlineStr":
            is_ = c.find(f"{{{S_NS}}}is")
            text = "".join(t.text or "" for t in is_.iter(f"{{{S_NS}}}t"))
            c.remove(is_)
            c.set("t", "s")
            if text not in strings:
                strings.append(text)
            etree.SubElement(c, f"{{{S_NS}}}v").text = str(strings.index(text))
    sst = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<sst xmlns="{S_NS}" uniqueCount="{len(strings)}">'
           + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>").encode()
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


def test_p4_excel_checkbox_in_a_shared_string_used_by_one_cell_only(tmp_path):
    raw, src, out = tmp_path / "check_inline.xlsx", tmp_path / "check.xlsx", tmp_path / "check_filled.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "点検票"
    ws["A1"], ws["A3"], ws["B3"] = "架空の点検票（テスト）", "同意", "□ はい　□ いいえ"
    ws["A4"], ws["B4"], ws["A5"], ws["B5"] = "再確認", "□ はい　□ いいえ", "判定", "□ 良　□ 要注意"
    wb.save(raw)
    inline = scan(raw, {"targets": [{"slot_id": "同意", "sheet": 0, "cell": "B3", "kind": "checkbox_text"}]})
    assert inline.slot("同意").occurrences[0].part_name == "xl/worksheets/sheet1.xml"  # an inline string is its own text
    _excel_shared_strings(raw, src)
    spec = {"targets": [{"slot_id": "同意", "sheet": 0, "cell": "B3", "kind": "checkbox_text"},
                        {"slot_id": "判定", "sheet": 0, "cell": "B5", "kind": "checkbox_text"}]}
    sc = scan(src, spec)
    assert [s.slot_id for s in sc.slots] == ["判定"]  # B3 shares its string with B4: ticking it would tick both
    assert any("shares its text with 1 other cell" in w for w in sc.warnings)
    assert sc.slot("判定").constraints["allowed_values"] == ["良", "要注意"] and sc.target_parts == ["xl/sharedStrings.xml"]
    res = fill_form(sc, {"判定": "要注意"}, out)
    assert [(x.kind, x.old_text, x.new_text) for x in res.patches] == [("checkbox", "□", "■")]
    rep = verify(sc, out, [human_mapping(sc.slot("判定"), "■ 要注意")], "")
    assert rep.overall_status == "PASS" and [c.check_id for c in rep.checks] == TWENTY_ONE
    ws2 = openpyxl.load_workbook(out)["点検票"]
    assert (ws2["B3"].value, ws2["B4"].value, ws2["B5"].value) == ("□ はい　□ いいえ", "□ はい　□ いいえ", "□ 良　■ 要注意")
    assert infer_values_from_filled(sc, out) == {"判定": "要注意"}
    d = discover_blank_cells(src)
    boxes = {c["slot_id"]: c for c in d["candidates"]}
    assert boxes["判定"]["kind"] == "checkbox_text" and boxes["判定"]["constraints"]["checkbox"]
    assert any("shared string other cells use too" in x["reason"] for x in d["rejected"])


def test_p4_unmarked_word_discovery_proposes_a_checkbox_cell(samples_dir, tmp_path):
    p = paths(samples_dir, "form1")
    d = discover_blank_cells(p["form"])
    (cb,) = [c for c in d["candidates"] if c.get("kind") == "checkbox_text"]
    assert (cb["slot_id"], cb["table"], cb["row"], cb["col"], cb["required_format"]) == ("共同研究先との合意", 0, 10, 2, "choice")
    assert [o["label"] for o in cb["options"]] == ["合意済み", "協議中"] and cb["recommended"]
    office = [c["slot_id"] for c in d["candidates"] if c["office_only"]]
    assert office == ["研究協力課受付印"]
    spec = build_confirmed_slot_spec(d, [cb["candidate_id"]], p["form"])
    assert spec["targets"] == [{"slot_id": "共同研究先との合意", "part": "word/document.xml", "table": 0, "row": 10, "col": 2,
                                "label": "共同研究先との合意", "kind": "checkbox_text"}]
    r = run_pipeline(p["form"], read_memo(p["memo"]), tmp_path / "disc", mode="demo", slot_spec=spec)
    assert r.report.failed_count == 0 and (r.mappings[0].slot_id, r.mappings[0].filled_value) == ("共同研究先との合意", "合意済み")


# ------------------------------------------------------------------- P4-2 fixed PDF forms


def _pdf_with(page_extra: dict, catalog_extra: dict | None = None, content: bytes = b"0.5 w 50 700 300 30 re S\n",
              fonts: dict | None = None) -> bytes:
    from form_guard.pdf_core import Name, PDFBuilder

    b = PDFBuilder("1.7")
    catalog, pages, page = b.reserve(), b.reserve(), b.reserve()
    res = b.add({"Font": fonts or {}})
    body = b.add({}, content)
    b.set(page, {"Type": Name("Page"), "Parent": pages, "MediaBox": [0, 0, 595.28, 841.89], "Resources": res,
                 "Contents": body, **page_extra})
    b.set(pages, {"Type": Name("Pages"), "Kids": [page], "Count": 1})
    b.set(catalog, {"Type": Name("Catalog"), "Pages": pages, **(catalog_extra or {})})
    return b.build(catalog)


def test_p4_pdf_gate_refuses_broken_encrypted_active_compressed_and_bomb_pdfs(tmp_path):
    import zlib

    from form_guard.pdf_core import Name, PDFBuilder, PDFError, check_pdf

    good = _pdf_with({})
    assert check_pdf(good).pages()[0].mediabox == (0.0, 0.0, 595.28, 841.89)
    n = len(b"0.5 w 50 700 300 30 re S\n")
    assert b"/Length %d" % n in good  # the page's content stream
    bad = {
        "header": (b"%PDF", "not a PDF file"),
        "truncated": (good[:-30], "truncated PDF"),
        "javascript": (_pdf_with({}, {"OpenAction": {"S": Name("JavaScript"), "JS": b"app.alert(1)"}}), "active content"),
        "launch": (_pdf_with({"AA": {"O": {"S": Name("Launch"), "F": b"calc.exe"}}}), "active content"),
        "encrypted": (good.replace(b"/Root 1 0 R", b"/Root 1 0 R /Encrypt 3 0 R"), "encrypted PDF"),
        "length": (good.replace(b"/Length %d" % n, b"/Length %d" % (n + 5)), "stream length mismatch"),
        # startxref pointing at an object = a PDF 1.5 cross-reference stream
        "xref stream": (good[: good.rindex(b"startxref\n")] + b"startxref\n%d\n%%%%EOF\n" % good.index(b"1 0 obj"),
                        "cross-reference streams"),
    }
    bomb = zlib.compress(b"\0" * (pdf_core.MAX_STREAM_DECODED + 1024), 9)  # ~20 KB that inflates past the cap
    pb = PDFBuilder()
    cat, pgs, pg, body = pb.reserve(), pb.reserve(), pb.reserve(), pb.reserve()
    pb.objects[body.num] = pdf_core.serialize({"Filter": Name("FlateDecode"), "Length": len(bomb)}) + \
        b"\nstream\n" + bomb + b"\nendstream"
    pb.set(pg, {"Type": Name("Page"), "Parent": pgs, "MediaBox": [0, 0, 595.28, 841.89], "Contents": body})
    pb.set(pgs, {"Type": Name("Pages"), "Kids": [pg], "Count": 1})
    pb.set(cat, {"Type": Name("Catalog"), "Pages": pgs})
    bad["bomb"] = (pb.build(cat), "PDF bomb")
    for name, (data, needle) in bad.items():
        with pytest.raises(PDFError) as exc:
            check_pdf(data)
        assert needle in str(exc.value), (name, str(exc.value))
    f = tmp_path / "x.pdf"
    f.write_bytes(bad["javascript"][0])
    with pytest.raises(ValueError, match="active content"):
        scan(f)


def test_p4_form6_is_a_valid_deterministic_fictional_a4_fixed_pdf(samples_dir):
    p = form6(samples_dir)
    blob = p["form"].read_bytes()
    assert blob.startswith(b"%PDF-1.7\n") and blob.rstrip().endswith(b"%%EOF") and blob == build_form6()
    doc = pdf_core.check_pdf(blob)
    (page,) = doc.pages()
    assert page.mediabox == (0.0, 0.0, 595.28, 841.89) and "AcroForm" not in doc.root
    fonts = pdf_core.font_resources(doc, page.resources)
    assert {n: (f.base_font, f.cmap, f.unicode_encoding) for n, f in fonts.items()} == {
        "F1": ("HeiseiKakuGo-W5", "UniJIS-UCS2-H", True), "F2": ("HeiseiMin-W3", "UniJIS-UCS2-H", True)}
    form = pdf_support.load_form(blob)
    text = form.doc_text
    for needle in ("次世代AI・分散システム研究会", "研究助成申請書（様式第6号・固定PDF様式）", CHECKBOX_LINE.replace("　　　年", "　　年"),
                   "事務局使用欄（申請者は記入しないでください）", "架空"):
        assert needle in text, needle
    banned = ["病院", "患者", "診断", "診療", "医療", "処方", "看護", "病名", "治療"]
    memo = read_memo(p["memo"])
    assert not [w for w in banned if w in text + memo] and "@" not in memo
    for d in find_dates(memo):
        assert d.weekday and "月火水木金土日"[date(d.year or 2026, d.month, d.day).weekday()] == d.weekday
    sidecar = json.loads((samples_dir / "blank_forms" / "form6_study_group_grant.slots.json").read_text(encoding="utf-8"))
    assert sidecar == SPECS["form6_study_group_grant"]
    assert [t["rect"] for t in sidecar["targets"]] == [r["rect"] for r in form6_layout()["rows"]]
    assert len(form.pages[0].cells) == 19 and len(form.pages[0].tables) == 2
    naive = pdf_core.check_pdf(p["naive"].read_bytes()).pages()[0]
    assert (naive.width, naive.height) == (612.0, 792.0)  # the naive regeneration switched to US Letter


def test_p4_pdf_discovery_finds_the_seven_boxes_and_locks_the_office_boxes(samples_dir):
    p = form6(samples_dir)
    d = discover_blank_cells(p["form"])
    assert d["file_type"] == "pdf" and d["requires_human_confirmation"] and d["rejected"] == []
    assert d["pdf"]["pages"][0]["boxes"] == 19 and d["pdf"]["acroform_fields"] == []
    assert [(c["slot_id"], c["office_only"], c["required_format"]) for c in d["candidates"]] == [
        ("申請日", False, "wareki_date"), ("研究課題名", False, "text"), ("申請代表者氏名", False, "person_name"),
        ("所属機関・部局・職名", False, "text"), ("助成申請金額", False, "yen"), ("共同研究先との合意状況", False, "choice"),
        ("研究概要", False, "text"), ("事務局受付欄", True, "text"), ("受付印・審査結果欄", True, "text")]
    by = {c["slot_id"]: c for c in d["candidates"]}
    assert by["助成申請金額"]["constraints"]["max_value"] == 500_000 and by["研究概要"]["max_chars"] == 150
    assert by["共同研究先との合意状況"]["kind"] == "checkbox_text" and by["共同研究先との合意状況"]["constraints"] == {
        "allowed_values": ["合意済み", "協議中"], "multi_select": False, "checkbox": True, "exclusive": True,
        "requires_human_if_selected": ["協議中"]}
    assert {"事務局", "受付番号"} <= set(by["事務局受付欄"]["office_keywords"]) and "受付印" in by["受付印・審査結果欄"]["office_keywords"]
    assert by["事務局受付欄"]["section"] == "事務局使用欄（申請者は記入しないでください）"
    rec = recommended_ids(d)
    spec = build_confirmed_slot_spec(d, rec, p["form"])
    sidecar = json.loads((samples_dir / "blank_forms" / "form6_study_group_grant.slots.json").read_text(encoding="utf-8"))
    assert spec["targets"] == sidecar["targets"]  # discovery finds exactly the boxes an expert designated by hand
    office = [c["candidate_id"] for c in d["candidates"] if c["office_only"]]
    with pytest.raises(ValueError, match="office/administrative use only"):
        build_confirmed_slot_spec(d, rec + office[:1], p["form"])
    moved = json.loads(json.dumps(d))
    moved["candidates"][0]["rect"] = [100, 100, 200, 200]
    with pytest.raises(ValueError, match="not a blank-cell candidate"):
        build_confirmed_slot_spec(moved, ["c01"], p["form"])


def test_p4_pdf_fill_is_an_incremental_update_that_passes_all_21_checks(samples_dir, tmp_path):
    p = form6(samples_dir)
    memo = read_memo(p["memo"])
    r = run_pipeline(p["form"], memo, tmp_path / "run", mode="demo", naive_path=p["naive"], demo_id="form6", png=True)
    rep = r.report
    assert rep.overall_status == "PASS" and [c.check_id for c in rep.checks] == TWENTY_ONE and rep.file_type == "pdf"
    values = {m.slot_id: m.filled_value for m in r.mappings}
    summary = values.pop("研究概要")
    assert values == {"申請日": "令和8年11月3日（火）", "研究課題名": "分散エッジ推論における通信量削減手法の比較検証",
                      "申請代表者氏名": "見本 六郎", "所属機関・部局・職名": "架空情報大学 大学院情報科学研究科 准教授",
                      "助成申請金額": "480,000円", "共同研究先との合意状況": "合意済み"}
    assert len(summary) <= 150 and summary in memo
    values["研究概要"] = summary
    assert all(m.status == "filled" and m.source_quote for m in r.mappings) and r.builtin_demo
    orig, filled = p["form"].read_bytes(), r.filled_path.read_bytes()
    assert filled[: len(orig)] == orig and filled.count(b"%%EOF") == 2  # the blank file is an exact prefix
    odoc, fdoc = pdf_core.PDFDocument(orig), pdf_core.PDFDocument(filled)
    (opage,), (fpage,) = odoc.pages(), fdoc.pages()
    oobj, fobj = odoc.indirect(opage.num), fdoc.indirect(fpage.num)
    for key in ("Type", "Parent", "MediaBox", "Resources"):
        a, b = oobj.dict_spans[key], fobj.dict_spans[key]
        assert orig[a[0] : a[1]] == filled[b[0] : b[1]], key  # spliced through byte for byte
    refs = [r_ for r_, _ in fdoc.page_streams(fpage)]
    assert refs[1:2] == [r_ for r_, _ in odoc.page_streams(opage)] and len(refs) == 4
    assert [fdoc.get(refs[0]).raw, fdoc.get(refs[2]).raw] == [b"q\n", b"Q\n"]
    assert check(rep, "XML-01").evidence["content_sha256"] == [hashlib.sha256(odoc.page_streams(opage)[0][1].decoded()).hexdigest()]
    assert [(x.kind, x.part_name) for x in r.write.patches].count(("pdf_text", "pdf/page1")) == 6
    (mark,) = [x for x in r.write.patches if x.kind == "pdf_mark"]
    assert (mark.old_text, mark.new_text) == ("□", "■ 合意済み") and r.write.modified_parts == ["pdf/page1"]
    assert filled[mark.byte_start : mark.byte_end].count(b"<25A0>") == 1
    occ = r.scan.slot("共同研究先との合意状況").occurrences[0]
    x0, y0, x1, y1 = occ.rect  # the printed notes also say 「□を■に」: count the marks inside the checkbox box only
    marks = [gl for run in pdf_core.interpret_page(fdoc, fpage).runs for gl in run.glyphs
             if gl.ch == "■" and x0 <= gl.x <= x1 and y0 <= gl.y <= y1]
    assert [(round(m.x, 2), round(m.y, 2), m.size) for m in marks] == [
        (round(occ.checkbox_options[0]["x"], 2), round(occ.checkbox_options[0]["y"], 2), occ.checkbox_options[0]["size"])]
    assert infer_values_from_filled(r.scan, r.filled_path) == {k: v for k, v in values.items()}
    px = r.visual["pixel_diff"]
    if px["method"] == "pixel":
        assert px["outside"] == 0 and px["inside"] > 1000
    assert r.visual["identical_outside_slots"] and r.visual["drift_count"] == 0
    assert all(f'data-slot-id="{sid}"' in r.previews["filled"] for sid in values) and "fg-locked" in r.previews["filled"]
    assert "naive" in r.previews and r.naive_report.overall_status == "FAIL"
    assert set(failed(r.naive_report)) >= {"LAYOUT-01", "LAYOUT-02", "LAYOUT-04", "TEXT-01", "GRD-02", "ZIP-04"}
    assert "600,000円" in check(r.naive_report, "GRD-02").details
    audit = json.loads((r.out_dir / "audit_log.json").read_text(encoding="utf-8"))
    assert audit["form"]["file_type"] == "pdf" and audit["report"]["overall_status"] == "PASS"
    assert "原本" in (r.out_dir / "report.html").read_text(encoding="utf-8")


def _overlay_edit(filled: Path, dst: Path, old: bytes, new: bytes, after: int) -> None:
    """A same-length edit inside the appended incremental update (the cross-reference offsets stay valid)."""
    blob = filled.read_bytes()
    assert len(old) == len(new)
    i = blob.index(old, after)
    dst.write_bytes(blob[:i] + new + blob[i + len(old):])


def test_p4_pdf_tampering_is_caught_by_the_21_checks_and_the_pixel_diff(samples_dir, tmp_path):
    from form_guard.pipeline import build_previews

    p = form6(samples_dir)
    memo = read_memo(p["memo"])
    r = run_pipeline(p["form"], memo, tmp_path / "run", mode="demo")
    n = len(p["form"].read_bytes())
    blob = r.filled_path.read_bytes()
    by = {x.slot_id: x for x in r.write.patches}
    date_tm = re.search(rb"1 0 0 1 ([\d.]+) ([\d.]+) Tm", blob[by["申請日"].byte_start : by["申請日"].byte_end])
    y_old = date_tm.group(2)
    occ = r.scan.slot("共同研究先との合意状況").occurrences[0]
    x_ok, x_other = (pdf_core.fmt_num(round(o["x"], 3)) for o in occ.checkbox_options)
    cases = {
        "original byte changed": (b"/Trapped /False", b"/Trapped /True ", 0, ["ZIP-02", "ZIP-03", "ZIP-04"]),
        "value moved outside its box": (b" " + y_old + b" Tm", b" " + b"234.9".ljust(len(y_old)) + b" Tm", n,
                                        ["XML-03"]),
        "value written into the office-only box": (b" " + y_old + b" Tm", b" " + b"310.9".ljust(len(y_old)) + b" Tm", n,
                                                   ["LAYOUT-04", "XML-03"]),
        "■ on the other option": (b"1 0 0 1 " + x_ok + b" ", b"1 0 0 1 " + x_other + b" ", n, ["TEXT-02"]),
        "page size changed": (b"/MediaBox [0 0 595.28 841.89]", b"/MediaBox [0 0 612.00 792.00]", n,
                              ["LAYOUT-02", "XML-02"]),
    }
    for name, (old, new, after, expected) in cases.items():
        dst = tmp_path / f"{name.replace(' ', '_')}.pdf"
        _overlay_edit(r.filled_path, dst, old, new, after)
        rep = verify(r.scan, dst, r.mappings, memo)
        assert set(expected) <= set(failed(rep)) and rep.overall_status == "FAIL", (name, failed(rep))
    moved = tmp_path / "value_moved_outside_its_box.pdf"
    visual = build_previews(r.scan, moved, {m.slot_id: m.filled_value for m in r.mappings}, None, None)[1]
    if visual["pixel_diff"]["method"] == "pixel":
        assert visual["pixel_diff"]["outside"] > 0 and not visual["identical_outside_slots"]
    # a second incremental update that re-defines the catalog: one update only, no foreign object re-defined
    doc = pdf_core.PDFDocument(blob)
    cat = doc.indirect(1)
    twice, _ = pdf_core.incremental_update(blob, doc, {1: blob[cat.dict_span[0] : cat.dict_span[1]]})
    (tmp_path / "twice.pdf").write_bytes(twice)
    rep = verify(r.scan, tmp_path / "twice.pdf", r.mappings, memo)
    assert {"ZIP-03", "ZIP-04"} <= set(failed(rep))


def test_p4_pdf_spec_targets_must_be_ruled_empty_boxes_and_never_office_only(samples_dir, tmp_path):
    from form_guard.mcp_server import FormGuardMCP

    p = form6(samples_dir)
    lay = form6_layout()
    spec = {"targets": [{"slot_id": "どこでもない", "page": 0, "rect": [10, 10, 50, 50]},
                        {"slot_id": "ラベル", "page": 0, "rect": lay["rows"][0]["label_rect"]},
                        {"slot_id": "偽チェック", "page": 0, "rect": lay["rows"][1]["rect"], "kind": "checkbox_text"},
                        {"slot_id": "表の座標", "table": 0, "row": 1, "col": 1},
                        {"slot_id": "申請日", "page": 0, "rect": lay["rows"][0]["rect"]}]}
    sc = scan(p["form"], spec)
    assert [s.slot_id for s in sc.slots] == ["申請日"]
    joined = " | ".join(sc.warnings)
    for needle in ("no ruled box at page 0", "the box is not empty", "prints no □ options", "use page + rect"):
        assert needle in joined, needle
    office = {"targets": [{"slot_id": "受付印", "page": 0, "rect": lay["office"][1]["rect"]}]}
    assert office_only_targets(p["form"], office) and "受付印" in office_only_targets(p["form"], office)[0]
    with pytest.raises(ValueError, match="office-only"):
        run_pipeline(p["form"], read_memo(p["memo"]), tmp_path / "x", mode="demo", slot_spec=office)
    root = _mcp_root(tmp_path, samples_dir)
    (root / "office.slots.json").write_text(json.dumps(office), encoding="utf-8")
    res = FormGuardMCP(root=root).call_tool("form_guard_fill", {
        "form_path": "samples/blank_forms/form6_study_group_grant.pdf", "slot_spec_path": "office.slots.json",
        "memo_path": "samples/memos/memo6_study_group_grant.txt"})
    assert res["isError"] and "office" in res["content"][0]["text"]


def test_p4_pdf_overlay_never_changes_page_resources_and_acroform_fields_are_not_filled(tmp_path):
    from form_guard.pdf_core import Name

    helv = {"F1": {"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name("Helvetica"),
                   "Encoding": Name("WinAnsiEncoding")}}
    content = b"0.5 w 50 700 100 30 re S 150 700 250 30 re S BT /F1 10 Tf 1 0 0 1 55 711 Tm (Name) Tj ET\n"
    src = tmp_path / "latin.pdf"
    src.write_bytes(_pdf_with({}, None, content, helv))
    d = discover_blank_cells(src)
    assert [c["slot_id"] for c in d["candidates"]] == ["Name"]
    spec = build_confirmed_slot_spec(d, ["c01"], src)
    with pytest.raises(pdf_core.PDFError, match="never changes a page's /Resources"):
        run_pipeline(src, "Name: Taro Example\n", tmp_path / "run", mode="demo", slot_spec=spec)
    widget = {"Type": Name("Annot"), "Subtype": Name("Widget"), "FT": Name("Tx"), "T": b"applicant", "TU": b"Applicant",
              "Rect": [150, 600, 400, 630]}
    form = tmp_path / "acro.pdf"
    from form_guard.pdf_core import PDFBuilder

    b = PDFBuilder()
    cat, pgs, pg = b.reserve(), b.reserve(), b.reserve()
    w = b.add({**widget, "P": pg})
    body = b.add({}, content)
    b.set(pg, {"Type": Name("Page"), "Parent": pgs, "MediaBox": [0, 0, 595.28, 841.89], "Resources": {"Font": helv},
               "Contents": body, "Annots": [w]})
    b.set(pgs, {"Type": Name("Pages"), "Kids": [pg], "Count": 1})
    b.set(cat, {"Type": Name("Catalog"), "Pages": pgs, "AcroForm": {"Fields": [w]}})
    form.write_bytes(b.build(cat))
    d = discover_blank_cells(form)
    assert d["pdf"]["acroform_fields"][0]["field"] == "applicant"
    assert any("interactive AcroForm field" in x["reason"] for x in d["rejected"])


def test_p4_web_demo6_discovery_preview_confirm_approve_and_download_the_pdf(api, samples_dir):
    p = form6(samples_dir)
    res = api.post("/api/run-demo", json={"demo_id": "form6", "mode": "demo"}).json()
    assert res["unmarked"] and res["auto_preview"] and not res["run"]["can_approve"]
    assert res["run"]["form"]["file_type"] == "pdf" and "naive" in res["run"]["previews"]
    rec = [c["candidate_id"] for c in res["discovery"]["candidates"] if c["recommended"]]
    office = [c["candidate_id"] for c in res["discovery"]["candidates"] if c["office_only"]]
    assert len(rec) == 7 and len(office) == 2
    refused = api.post("/api/runs/from-discovery", json={"discovery_id": res["discovery"]["discovery_id"],
                                                         "confirmed_targets": rec + office, "mode": "demo"})
    assert refused.status_code == 422 and "office/administrative use only" in refused.json()["detail"]
    run = api.post("/api/runs/from-discovery", json={"discovery_id": res["discovery"]["discovery_id"],
                                                     "confirmed_targets": rec, "mode": "demo"}).json()
    assert run["report"]["overall_status"] == "PASS" and run["can_approve"] and run["is_builtin_demo"]
    assert api.get(run["links"]["download"]).status_code == 403
    required = [i["item_id"] for i in run["report"]["hitl_checklist"] if i["required"]]
    ok = api.post(f"/api/runs/{run['run_id']}/approve", json={"reviewer": "架空 花子", "checked": required, **_seen(run)})
    assert ok.status_code == 200, ok.text
    dl = api.get(run["links"]["download"])
    assert dl.status_code == 200 and dl.headers["content-type"] == "application/pdf"
    assert dl.content.startswith(p["form"].read_bytes()) and hashlib.sha256(dl.content).hexdigest() == run["filled"]["sha256"]
    sidecar = api.post("/api/runs", json={"demo_id": "form6", "mode": "demo"}).json()
    assert sidecar["report"]["overall_status"] == "PASS" and sidecar["demo_id"] == "form6"
    up = api.post("/api/discover", files={"file": ("研究会 申請書.pdf", p["form"].read_bytes())}, data={"mode": "demo"})
    assert up.status_code == 200 and len(up.json()["candidates"]) == 9
    for raw in (b"%PDF-1.7 not really a pdf", _pdf_with({}, {"OpenAction": {"S": pdf_core.Name("JavaScript"), "JS": b"x"}})):
        bad = api.post("/api/runs/upload", files={"file": ("x.pdf", raw)}, data={"memo_text": "m", "mode": "demo"})
        assert bad.status_code == 400, bad.text


def test_p4_portal_lists_forms_rules_sha256_with_download_and_open_links(api, samples_dir):
    page = api.get("/portal")
    assert page.status_code == 200 and "default-src 'self'" in page.headers["content-security-policy"]
    html = page.text
    for needle in ("架空学術振興ポータル — 公募・研究会指定様式ダウンロード窓口", "Fictional Academic Grant &amp; Study Group Form Portal",
                   "⬇ 白紙様式をダウンロード (.pdf)", "🚀 Form-Guard で自動空欄発見・外科的差込を開く", 'href="/"',
                   "portal-form-fill", "form_guard_discover_blank_cells", "review_url", "架空のもの"):
        assert needle in html, needle
    assert "<script" not in html  # server-rendered, readable without running any script
    forms = api.get("/api/portal/forms").json()
    assert forms["fictional"] and [f["id"] for f in forms["forms"]] == ["form6", "form4", "form5", "form1", "form2", "form3"]
    for f in forms["forms"]:
        assert f'data-form-id=\'{f["id"]}\'' in html and f["sha256"] in html
        blob = (samples_dir / "blank_forms" / f["form"]).read_bytes()
        assert f["sha256"] == hashlib.sha256(blob).hexdigest() and f["size"] == len(blob)
        dl = api.get(f["download_url"])
        assert dl.status_code == 200 and dl.content == blob and dl.headers["x-form-guard-sha256"] == f["sha256"]
        assert "attachment" in dl.headers["content-disposition"]
    f6 = forms["forms"][0]
    assert (f6["file_type"], f6["open_url"], f6["next_tool"]) == ("pdf", "/#pdf=form6", "form_guard_discover_blank_cells")
    assert f6["office_only"] == ["事務局受付欄", "受付印・審査結果欄"] and f6["deadline"] == "2026年11月30日（月）17:00"
    rules = {r["field"]: r["rule"] for r in f6["rules"]}
    assert "上限500,000円" in rules["助成申請金額"] and "150字以内" in rules["研究概要"]
    assert rules["共同研究先との合意状況"].startswith("□→■ で選択：合意済み／協議中（一つだけ）")
    assert {f["id"]: f["open_url"] for f in forms["forms"]}["form1"] == "/#demo=form1"
    assert api.get("/api/portal/forms/form9/download").status_code == 404
    index, js = api.get("/").text, api.get("/static/app.js").text
    assert 'href="/portal"' in index and "🌐 公募様式ポータル (/portal)" in index and 'id="pdfList"' in index
    assert 'params.get("pdf")' in js and 'accept=".docx,.xlsx,.pdf"' in index


def test_p4_mcp_fills_the_fixed_pdf_and_the_web_app_signs_off_the_exact_draft(samples_dir, tmp_path):
    from fastapi.testclient import TestClient

    from form_guard.mcp_server import FormGuardMCP
    from form_guard.server import create_app

    root = _mcp_root(tmp_path, samples_dir)
    srv = FormGuardMCP(root=root)
    demos = srv.call_tool("form_guard_list_demos", {})["structuredContent"]
    (d6,) = [d for d in demos["demos"] if d["id"] == "form6"]
    assert d6["pdf"] and d6["file_type"] == "pdf" and d6["next_tool"] == "form_guard_discover_blank_cells"
    assert d6["slot_spec_path"].endswith("form6_study_group_grant.slots.json")
    scanned = srv.call_tool("form_guard_scan", {"form_path": d6["form_path"]})["structuredContent"]
    assert scanned["file_type"] == "pdf" and scanned["slot_count"] == 7
    assert {s["slot_id"]: s["marker_kind"] for s in scanned["slots"]}["共同研究先との合意状況"] == "checkbox_text"
    disc = srv.call_tool("form_guard_discover_blank_cells", {"form_path": d6["form_path"]})["structuredContent"]
    rec = [c["candidate_id"] for c in disc["candidates"] if c["recommended"]]
    fill = srv.call_tool("form_guard_fill", {"form_path": d6["form_path"], "memo_path": d6["memo_path"],
                                             "discovery": disc, "confirmed_ids": rec, "mode": "demo"})["structuredContent"]
    assert fill["approval_status"] == "UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF"
    assert fill["verification"]["overall_status"] == "PASS" and fill["draft_path"].endswith(".pdf")
    ver = srv.call_tool("form_guard_verify", {"original_path": d6["form_path"], "filled_path": fill["draft_path"],
                                              "slot_spec_path": fill["slot_spec_path"]})["structuredContent"]
    assert ver["overall_status"] == "PASS" and ver["grounding_verified"] and ver["visual"]["checked"]
    assert ver["values_found"]["共同研究先との合意状況"] == "合意済み"
    api = TestClient(create_app(run_root=tmp_path / "web", samples_dir=samples_dir, import_roots=[root / "output" / "mcp_runs"]))
    run = api.get(f"/api/runs/{fill['run_id']}").json()
    assert run["report"]["overall_status"] == "PASS" and run["filled"]["sha256"] == fill["filled_sha256"]
    assert run["form"]["file_type"] == "pdf" and run["imported_from"]


def test_p4_cli_scans_discovers_fills_and_verifies_a_fixed_pdf(samples_dir, tmp_path, capsys):
    from form_guard.cli import main

    p = form6(samples_dir)
    assert main(["--scan", str(p["form"])]) == 0
    out = capsys.readouterr().out
    assert "(pdf) — 7 slots" in out and "page 1 › box" in out
    assert main(["--discover-blanks", str(p["form"]), "--out-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "9 candidate cells (7 recommended)" in out and "checkbox □→■ 合意済み/協議中" in out and "page 1 box (" in out
    assert main(["--form", str(p["form"]), "--memo", str(p["memo"]), "--out-dir", str(tmp_path / "run"), "--mode", "demo"]) == 0
    assert "PASS · 7/7 fields filled · 21/21 checks passed" in capsys.readouterr().out
    filled = tmp_path / "run" / "filled_form6_study_group_grant.pdf"
    assert main(["--verify", str(p["form"]), str(filled), "--memo", str(p["memo"])]) == 0
    assert "PASS (21 passed, 0 failed)" in capsys.readouterr().out


def test_p4_hook_guard_protects_blank_pdf_templates():
    pdf = "samples/blank_forms/form6_study_group_grant.pdf"
    for tool, ti in (("Write", {"file_path": pdf}), ("Bash", {"command": f"cp output/x.pdf {pdf}"}),
                     ("Bash", {"command": f"rm {pdf}"}), ("PowerShell", {"command": f"Remove-Item {pdf}"})):
        assert "BLOCKED by the Form-Guard template guard" in _denied(_hook("PreToolUse", tool, ti)), (tool, ti)
    for tool, ti in (("Read", {"file_path": pdf}), ("Bash", {"command": f"cp {pdf} output/copy.pdf"}),
                     ("Write", {"file_path": "samples/blank_forms/form6_study_group_grant.slots.json"})):
        proc = _hook("PreToolUse", tool, ti)
        assert proc.returncode == 0 and proc.stdout == b"", (tool, ti)


def test_p4_portal_form_fill_skill_and_docs_describe_the_pdf_checkbox_and_portal_workflow():
    meta, body = frontmatter(ROOT / ".claude" / "skills" / "portal-form-fill" / "SKILL.md")
    assert meta["name"] == "portal-form-fill" and "Use when" in meta["description"] and "/portal" in meta["description"]
    for word in ("/api/portal/forms", "Claude in Chrome", "form_guard_discover_blank_cells", "form_guard_fill",
                 "form_guard_verify", "review_url", "--discover-blanks", "unapproved", "office-only", "□ → ■",
                 "X-Form-Guard-SHA256", "samples/memos/memo6_study_group_grant.txt"):
        assert word in body, word
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "BUILT_WITH_CLAUDE_CODE.md").read_text(encoding="utf-8")
    script = (ROOT / "docs" / "DEMO_VIDEO_SCRIPT_JA.md").read_text(encoding="utf-8")
    for text in (readme, doc, script):
        for needle in ("form6_study_group_grant.pdf", "□", "■", "/portal", "Claude in Chrome"):
            assert needle in text, needle
    for text in (readme, doc):
        for needle in ("incremental update", "増分更新", "portal-form-fill", "GET /api/portal/forms", "pdf_support.py",
                       "requires_human_if_selected", "VIS-01"):
            assert needle in text, needle


def test_p4_ballot_box_u2610_checkbox_is_parsed_written_as_u25a0_and_verified(tmp_path):
    from docx import Document

    from form_guard.constraints import parse_checkbox_options
    from form_guard.pipeline import run_pipeline

    opts = parse_checkbox_options("☐ 合意済み　　☐ 協議中（協議予定日：　　年　　月　　日）")
    assert [o["label"] for o in opts] == ["合意済み", "協議中"] and not opts[0]["checked"] and opts[1]["needs_input"]
    doc = Document()
    tbl = doc.add_table(rows=1, cols=2)
    tbl.cell(0, 0).text = "共同研究先との合意"
    tbl.cell(0, 1).text = "☐ 合意済み　　☐ 協議中（協議予定日：　　年　　月　　日）"
    form_path = tmp_path / "ballot_box_form.docx"
    doc.save(form_path)
    spec = {"targets": [{"slot_id": "共同研究先との合意", "table": 0, "row": 0, "col": 1, "kind": "checkbox_text"}]}
    res = run_pipeline(form_path, "共同研究先との合意：合意済み\n", tmp_path / "out", mode="demo", slot_spec=spec)
    assert res.report.overall_status == "PASS" and res.report.passed_count == 21
    assert res.write.patches[0].old_text == "☐"
    filled_doc = Document(res.filled_path)
    assert filled_doc.tables[0].cell(0, 1).text == "■ 合意済み　　☐ 協議中（協議予定日：　　年　　月　　日）"


def test_p4_r1_audit_fixes_zero_amounts_negated_quotes_checkbox_locks_and_expected_sha256(samples_dir, tmp_path):
    from form_guard.constraints import finish_checkbox_constraints, parse_checkbox_options
    from form_guard.grounding import ground_value
    from form_guard.models import FormSlot
    from form_guard.scanner import discover_blank_cells
    from form_guard.textnorm import find_amounts, kanji_to_int, parse_compound, quantities

    # #1: explicit 0 before unit (0万円 / 0万5千円 / 2億0万円 / 〇万円) vs 2千万
    assert parse_compound("0万") == 0 and find_amounts("0万円")[0].value == 0
    assert kanji_to_int("〇万") == 0 and find_amounts("〇万円")[0].value == 0
    assert parse_compound("0万5千") == 5_000 and find_amounts("0万5千円")[0].value == 5_000
    assert parse_compound("2億0万") == 200_000_000 and find_amounts("2億0万円")[0].value == 200_000_000
    assert parse_compound("2千万") == 20_000_000 and kanji_to_int("二千万") == 20_000_000
    assert {0, 200_000_000}.issubset(quantities("助成申請額：0万円（自己負担2億0万円）"))

    # #2: partial quote cut out of a negated sentence must be rejected as choice_unsupported
    slot = FormSlot(
        slot_id="共同研究先との合意状況",
        label="共同研究先との合意状況",
        location_path="page 1",
        file_type="pdf",
        current_text="",
        required_format="choice",
        constraints={"allowed_values": ["合意済み", "協議中"], "checkbox_options": [
            {"label": "合意済み", "detail": "", "needs_input": False},
            {"label": "協議中", "detail": "", "needs_input": False},
        ]},
    )
    neg_memo = "共同研究先との合意状況：合意済みではない。\n"
    gr = ground_value("合意済み", ["合意済み"], neg_memo, slot)
    assert [i.code for i in gr.issues if i.severity == "error"] == ["choice_unsupported"]

    # #3: broader needs_input patterns & requires_human_if_selected union with printed rules
    opts = parse_checkbox_options("□ 通常枠　　□ その他（具体的に：　　）　　□ 特別枠（____年__月）")
    assert [o["needs_input"] for o in opts] == [False, True, True]
    finished = finish_checkbox_constraints({"requires_human_if_selected": []}, opts)
    assert set(finished["requires_human_if_selected"]) == {"その他", "特別枠"}

    # #12: expected_sha256 verified on discover_blank_cells for both PDF and Word/Excel
    p6 = form6(samples_dir)["form"]
    with pytest.raises(ValueError, match="SHA-256"):
        discover_blank_cells(p6, mode="demo", expected_sha256="0" * 64)
    p4 = samples_dir / "blank_forms" / "form4_unmarked_blank.docx"
    with pytest.raises(ValueError, match="SHA-256"):
        discover_blank_cells(p4, mode="demo", expected_sha256="0" * 64)

    # R1-A1: MCP serve() survives 5000-digit integer ValueError and lone surrogate \ud800 in both framings
    from form_guard import mcp_server as m

    srv = m.FormGuardMCP(root=_mcp_root(tmp_path, samples_dir))
    huge_int = b'{"jsonrpc":"2.0","id":' + (b"9" * 5000) + b',"method":"ping"}'
    surrogate_req = b'{"jsonrpc":"2.0","id":"\\ud800","method":"\\ud800"}'
    ping_req = b'{"jsonrpc":"2.0","id":99,"method":"ping"}'
    nl_stream = huge_int + b"\n" + surrogate_req + b"\n" + ping_req + b"\n"
    nl_out = io.BytesIO()
    m.serve(srv, stdin=io.BytesIO(nl_stream), stdout=nl_out)
    nl_msgs = [json.loads(line.decode("utf-8")) for line in nl_out.getvalue().splitlines() if line.strip()]
    assert len(nl_msgs) == 3 and nl_msgs[0]["error"]["code"] == -32700
    assert nl_msgs[1]["error"]["code"] == -32601 and nl_msgs[2] == {"jsonrpc": "2.0", "id": 99, "result": {}}

    def _frame(b: bytes) -> bytes:
        return b"Content-Length: %d\r\n\r\n" % len(b) + b

    fr_out = io.BytesIO()
    m.serve(srv, stdin=io.BytesIO(_frame(huge_int) + _frame(surrogate_req) + _frame(ping_req)), stdout=fr_out)
    fr_raw = fr_out.getvalue()
    assert b'"id":99,"result":{}' in fr_raw and b"\\ud800" in fr_raw

    # R1-A2: build_fastmcp form_guard_discover_blank_cells accepts expected_sha256 (match / mismatch / None)
    import types

    registered: dict[str, Any] = {}

    class _FakeFastMCP:
        def __init__(self, name, instructions=None):
            self.name = name

        def tool(self, name, description=""):
            def _dec(fn):
                registered[name] = fn
                return fn

            return _dec

    fake_mcp_mod = types.ModuleType("mcp")
    fake_server_mod = types.ModuleType("mcp.server")
    fake_fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
    fake_fastmcp_mod.FastMCP = _FakeFastMCP  # type: ignore[attr-defined]
    sys.modules["mcp"] = fake_mcp_mod
    sys.modules["mcp.server"] = fake_server_mod
    sys.modules["mcp.server.fastmcp"] = fake_fastmcp_mod
    try:
        m.build_fastmcp(srv)
    finally:
        for k in ("mcp.server.fastmcp", "mcp.server", "mcp"):
            sys.modules.pop(k, None)
    disc_fn = registered["form_guard_discover_blank_cells"]
    res_none = disc_fn("samples/blank_forms/form6_study_group_grant.pdf")
    res_match = disc_fn("samples/blank_forms/form6_study_group_grant.pdf", expected_sha256=res_none["form_sha256"])
    assert res_match["form_sha256"] == res_none["form_sha256"]
    with pytest.raises(RuntimeError, match="refused before discovery:.*SHA-256"):
        disc_fn("samples/blank_forms/form6_study_group_grant.pdf", expected_sha256="0" * 64)


@pytest.mark.parametrize(
    "script_rel",
    ["form_guard", "form_guard/__main__.py", "form_guard/cli.py", "form_guard/mcp_server.py"],
)
def test_cli_and_mcp_server_start_when_run_directly_as_scripts(script_rel):
    def run(*args):
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(ROOT / script_rel), *args],
            capture_output=True, text=True, encoding="utf-8", timeout=20, stdin=subprocess.DEVNULL,
        )

    ok = run("--help")
    assert ok.returncode == 0 and "usage:" in ok.stdout.lower(), ok.stderr
    bad = run("--__nonexistent_flag_testplay__")
    assert bad.returncode == 2 and "Traceback (most recent call last):" not in (bad.stdout + bad.stderr)


def test_r1_1449_audit_regressions(samples_dir, tmp_path, fake_client_factory):
    """Regression tests for R1-A1..A6, R1-O6, R1-O7 from 2026-11-03_1449 audit and auto-adapt R1 audit."""
    from form_guard.constraints import (
        advance_notice_rules,
        checkbox_constraints,
        normalize_checkbox_value,
        parse_checkbox_options,
    )
    from form_guard.mapper import advance_notice_warnings, human_mapping
    from form_guard.models import FieldMapping
    from form_guard.textnorm import condense_extractive, find_amounts, protection, quantities, value_quantities

    # R1-A1: negations and unknown suffixes on checkbox labels must never be normalised into the positive option
    opts = parse_checkbox_options("□ 合意済み　　□ 協議中（協議予定日：　　　年　　月　　日）")
    cb_slot = _text_slot("合意状況", fmt="choice", **checkbox_constraints(opts))
    for neg in ("合意済みではない", "合意済み以外", "合意済みか不明", "■ 合意済みではない"):
        assert normalize_checkbox_value(neg, ["合意済み", "協議中"]) == [neg.replace("■", "").strip()]
        hm = human_mapping(cb_slot, neg)
        assert not hm.validation_passed and hm.errors, (neg, hm)
    for pos in ("合意済み", "☑ 合意済み", "■合意済み", "協議中（協議予定日：未定）"):
        assert normalize_checkbox_value(pos, ["合意済み", "協議中"])[0] in ("合意済み", "協議中")

    # R1-A2: negative signs and multi-dot numbers must be refused as a whole, never partially extracted as positive
    for bad_amt in ("増減額：-1,000円", "増減額：−1,000円", "増減額：－1,000円", "増減額：▲1,000円", "増減額：1.2.5万円"):
        assert find_amounts(bad_amt) == [], bad_amt
        assert quantities(bad_amt) == set(), bad_amt
        assert value_quantities(bad_amt) == [], bad_amt
    assert [a.value for a in find_amounts("2千万円と3百万円と1億2千万円")] == [20_000_000, 3_000_000, 120_000_000]

    # R1-A3, R1-A4, R1-O6: period start date, conflicting memo dates, custom slot label, 0-day gap
    rules = advance_notice_rules("研究期間の30日前までに申請すること。")
    s_sub = _text_slot("申請日", fmt="wareki_date", advance_notice=rules)
    s_per = _text_slot("研究期間", fmt="wareki_period")
    m_sub = FieldMapping(slot_id="申請日", status="filled", filled_value="令和8年12月1日（火）", validation_passed=True)
    m_per_14 = FieldMapping(
        slot_id="研究期間", status="filled",
        filled_value="令和8年12月15日（火）～令和8年12月31日（木）", validation_passed=True,
    )
    w_14 = advance_notice_warnings([s_sub, s_per], [m_sub, m_per_14], "研究期間：2027年1月15日（金）～2027年3月31日（水）\n")[0].warnings
    assert len(w_14) == 1 and "only 14 day(s) after" in w_14[0] and "申請日と研究期間を確認してください" in w_14[0]

    # 0-day gap uses "on the same day as"
    m_per_0 = FieldMapping(
        slot_id="研究期間", status="filled",
        filled_value="令和8年12月1日（火）～令和8年12月31日（木）", validation_passed=True,
    )
    w_0 = advance_notice_warnings([s_sub, s_per], [m_sub, m_per_0], "")[0].warnings
    assert len(w_0) == 1 and "on the same day as" in w_0[0]

    # unconfirmed / invalid period field does not silently fall back to older memo; emits warning
    m_per_bad = FieldMapping(
        slot_id="研究期間", status="needs_human_input", filled_value="", suggested_value="令和8年12月31日（木）～令和8年12月15日（火）",
        validation_passed=False,
    )
    w_bad = advance_notice_warnings([s_sub, s_per], [m_sub, m_per_bad], "研究期間：2027年1月15日（金）\n")[0].warnings
    assert len(w_bad) == 1 and "could not be verified" in w_bad[0]

    # conflicting memo dates (when no field holds the target) warn regardless of order
    rules_chg = advance_notice_rules("変更予定日の30日前までに提出すること。")
    s_sub2 = _text_slot("提出日", fmt="wareki_date", advance_notice=rules_chg)
    for memo_order in (
        "変更予定日：2027年1月15日（金）\n変更予定日：2026年12月15日（火）\n",
        "変更予定日：2026年12月15日（火）\n変更予定日：2027年1月15日（金）\n",
    ):
        w_conf = advance_notice_warnings([s_sub2], [m_sub.model_copy(update={"slot_id": "提出日"})], memo_order)[0].warnings
        assert len(w_conf) == 1 and "multiple conflicting dates" in w_conf[0]

    # R1-A5 & R1-O7: rich-text era skeleton in Excel is not overwritten as plain inlineStr; era-less skeleton is in rejected
    form_docx = tmp_path / "eraless.docx"
    _era_template_docx(form_docx)
    d_docx = discover_blank_cells(form_docx)
    assert any("era-less date skeleton" in r["reason"] for r in d_docx["rejected"])

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"], ws["A2"], ws["B2"] = "架空様式", "届出日", "令和　　年　　月　　日"
    ws["A3"], ws["B3"] = "生年月日", "　　年　　月　　日"
    ws.merge_cells("A1:B1")
    xl_inline = tmp_path / "rich_era_inline.xlsx"
    wb.save(xl_inline)
    xl_sst = tmp_path / "rich_era_sst.xlsx"
    to_shared_strings(xl_inline, xl_sst)
    # Inject rich-text <r><rPr><b/></rPr><t>令和　　年　　月　　日</t></r> into inlineStr and sharedStrings for B2
    rewrite_entry(
        xl_inline, xl_inline, "xl/worksheets/sheet1.xml",
        lambda b: re.sub(
            rb'(<c r="B2"[^>]*><is>)<t[^>]*>.*?</t>(</is></c>)',
            rb"\1<r><rPr><b/></rPr><t>" + "令和　　年　　月　　日".encode("utf-8") + rb"</t></r>\2",
            b,
        ),
    )
    rewrite_entry(
        xl_sst, xl_sst, "xl/sharedStrings.xml",
        lambda b: re.sub(
            rb"<t[^>]*>" + "令和　　年　　月　　日".encode("utf-8") + rb"</t>",
            "<r><rPr><b/></rPr><t>令和　　年　　月　　日</t></r>".encode("utf-8"),
            b,
        ),
    )
    for xl_path in (xl_inline, xl_sst):
        d_xl = discover_blank_cells(xl_path)
        assert [c["slot_id"] for c in d_xl["candidates"]] == []
        assert any("era-less date skeleton" in r["reason"] for r in d_xl["rejected"])
        assert any(("rich-text" in r["reason"] or "already holds content" in r["reason"]) for r in d_xl["rejected"])

    # R1-A6: restriction sentence starting with 「なお、」 keeps protection > 0 and triggers unsafe=True if dropped
    assert protection("なお、利用は平日のみ。") == 1
    assert protection("なお、フィルター清掃のみで異常なし。") == 0
    res_cond = condense_extractive("設備を導入する。なお、利用は平日のみ。", 8)
    assert res_cond.text == "設備を導入する。" and res_cond.removed and res_cond.unsafe is True

    # --auto-adapt: unmarked form discovery bound to SHA-256 and VIS-01 overflow max_chars tightening
    from form_guard.cli import main as cli_main
    from form_guard.pipeline import run_pipeline

    u4 = unmarked(samples_dir, "form4")
    r_unmarked = run_pipeline(u4["form"], u4["memo"].read_text(encoding="utf-8"), tmp_path / "adapt_u4",
                              mode="demo", auto_adapt=True)
    assert r_unmarked.report.overall_status == "PASS"
    assert any(a["action"] == "auto_discover_unmarked" for a in r_unmarked.adapt_history)
    assert r_unmarked.scan.spec.get("form_sha256") == hashlib.sha256(u4["form"].read_bytes()).hexdigest()

    wb_narrow = openpyxl.Workbook()
    ws_narrow = wb_narrow.active
    ws_narrow.title = "申請書"
    ws_narrow["A1"] = "各種届出書（架空）"
    ws_narrow.merge_cells("A1:B1")
    ws_narrow["A2"] = "申請理由"
    ws_narrow["B2"] = ""
    ws_narrow.column_dimensions["A"].width = 14
    ws_narrow.column_dimensions["B"].width = 24
    narrow_xlsx = tmp_path / "narrow_form.xlsx"
    wb_narrow.save(narrow_xlsx)
    memo_file = tmp_path / "memo.txt"
    memo_file.write_text("申請理由：設備更新を実施する。なお、フィルター清掃のみで異常なし。\n", encoding="utf-8")

    r_no_adapt = run_pipeline(narrow_xlsx, memo_file.read_text(encoding="utf-8"), tmp_path / "no_adapt",
                              mode="demo", auto_adapt=False)
    assert len(r_no_adapt.scan.slots) == 0 and r_no_adapt.report.overall_status == "FAIL"

    out_cli = tmp_path / "with_adapt"
    rc = cli_main(["--form", str(narrow_xlsx), "--memo", str(memo_file), "--out-dir", str(out_cli),
                   "--mode", "demo", "--auto-adapt"])
    assert rc == 0
    adapted_spec_path = out_cli / "narrow_form.adapted.slots.json"
    assert adapted_spec_path.is_file()
    spec_data = json.loads(adapted_spec_path.read_text(encoding="utf-8"))
    assert spec_data.get("form_sha256") == hashlib.sha256(narrow_xlsx.read_bytes()).hexdigest()
    audit_data = json.loads((out_cli / "audit_log.json").read_text(encoding="utf-8"))
    assert audit_data["report"]["overall_status"] == "PASS"
    assert audit_data["mappings"][0]["filled_value"] == "設備更新を実施する。"
    assert any(a["action"] == "tighten_max_chars" for a in audit_data.get("auto_adapt", []))

    # R1-A1 / R1-A2 / R1-O1: condense_extractive step 5 (clause trimming) always unsafe, preserves removed/held & verbatim fragments
    t1 = "定期点検を毎月第一月曜日に実施し、その結果を記録簿に記入して保管する。ただし祝日の場合は翌日とする。"
    c1 = condense_extractive(t1, 20)
    assert c1 is not None and c1.unsafe is True
    assert any("ただし" in h for h in c1.held) and all(f in t1 for f in c1.fragments)

    t2 = "令和8年11月6日（金）に設備を搬入し、据付および試運転を実施する。"
    c2 = condense_extractive(t2, 20)
    assert c2 is not None and c2.unsafe is True and all(f in t2 for f in c2.fragments)

    t3 = "新設備（型番A-1、出力5kW、三相200V仕様）を導入し、既存設備を撤去して段階的に全面更新を実施する。"
    c3 = condense_extractive(t3, 30)
    assert c3 is not None and c3.text == "新設備（型番A-1、出力5kW、三相200V仕様）を導入し"
    assert c3.unsafe is True and all(f in t3 for f in c3.fragments)

    t4 = "定期点検を毎月第一月曜日に実施し、その結果を記録簿に記入して3年間保管する。"
    c4 = condense_extractive(t4, 20)
    assert c4 is not None and c4.unsafe is True and len(c4.held) >= 1

    t5 = "申請者は同意した、という事実はありません。"
    c5 = condense_extractive(t5, 8)
    assert c5 is not None and c5.text == "申請者は同意した" and c5.unsafe is True and len(c5.held) >= 1

    # R1-A3: Word bottom-most table row growth sets identical_outside_slots=False and triggers VIS-01 / auto_adapt
    doc_grow = docx.Document()
    tbl_grow = doc_grow.add_table(rows=1, cols=2)
    tbl_grow.cell(0, 0).text = "申請理由"
    tbl_grow.cell(0, 1).text = "{{申請理由}}"
    grow_docx = tmp_path / "grow_bottom.docx"
    doc_grow.save(grow_docx)
    memo_grow = "申請理由：業務効率化のため新設備を導入する。なお、既存機器の点検で異常なし。\n"
    r_grow_no = run_pipeline(grow_docx, memo_grow, tmp_path / "grow_no_adapt", mode="demo", auto_adapt=False)
    assert r_grow_no.visual["grown_slots"] and r_grow_no.visual["identical_outside_slots"] is False
    assert r_grow_no.report.overall_status == "FAIL"
    r_grow_yes = run_pipeline(grow_docx, memo_grow, tmp_path / "grow_with_adapt", mode="demo", auto_adapt=True)
    assert r_grow_yes.report.overall_status == "PASS"
    assert r_grow_yes.mappings[0].filled_value == "業務効率化のため新設備を導入する。"
    assert (tmp_path / "grow_with_adapt" / "grow_bottom.adapted.slots.json").is_file()
    grow_spec_saved = json.loads((tmp_path / "grow_with_adapt" / "grow_bottom.adapted.slots.json").read_text(encoding="utf-8"))
    assert grow_spec_saved.get("form_sha256") == hashlib.sha256(grow_docx.read_bytes()).hexdigest()

    # R1-A4 / R1-O5 / R1-A10 / R1-A13 / R1-O8: prior & overrides preservation, slot_spec deepcopy, max_adapt_rounds validation
    with pytest.raises(ValueError, match="max_adapt_rounds"):
        run_pipeline(grow_docx, memo_grow, tmp_path / "bad_rounds", mode="demo", auto_adapt=True, max_adapt_rounds=-1)
    with pytest.raises(ValueError, match="max_adapt_rounds"):
        run_pipeline(grow_docx, memo_grow, tmp_path / "bad_rounds_bool", mode="demo", auto_adapt=True, max_adapt_rounds=True)

    import copy

    caller_spec = {"slots": {"申請理由": {"label": "申請理由"}}}
    caller_spec_copy = copy.deepcopy(caller_spec)
    run_pipeline(grow_docx, memo_grow, tmp_path / "deepcopy_spec", mode="demo", slot_spec=caller_spec, auto_adapt=True)
    assert caller_spec == caller_spec_copy

    # overrides with human_entered is never tightened away by auto_adapt
    r_human_ov = run_pipeline(narrow_xlsx, memo_file.read_text(encoding="utf-8"), tmp_path / "human_ov",
                              mode="demo", auto_adapt=True, overrides={"申請理由": "確認者が直接指定した特別理由テキスト"})
    assert r_human_ov.mappings[0].status == "human_entered"
    assert r_human_ov.mappings[0].filled_value == "確認者が直接指定した特別理由テキスト"

    # R1-A12: DataTableFormula attributes included in xlsx_structure comparison
    from openpyxl.worksheet.formula import DataTableFormula
    from form_guard.verifier import xlsx_structure

    wb_dt1 = openpyxl.Workbook()
    ws_dt1 = wb_dt1.active
    ws_dt1["B2"] = DataTableFormula(ref="B2:C5", r1="D1")
    b_dt1 = io.BytesIO()
    wb_dt1.save(b_dt1)

    wb_dt2 = openpyxl.Workbook()
    ws_dt2 = wb_dt2.active
    ws_dt2["B2"] = DataTableFormula(ref="B2:C5", r1="E1")
    b_dt2 = io.BytesIO()
    wb_dt2.save(b_dt2)
    assert xlsx_structure(b_dt1.getvalue())["sheets"][0]["formulas"] != xlsx_structure(b_dt2.getvalue())["sheets"][0]["formulas"]

    # Web UI & MCP auto_adapt extension
    from form_guard.mcp_server import FormGuardMCP

    mcp_inst = FormGuardMCP(root=tmp_path)
    mcp_res = mcp_inst.call_tool("form_guard_fill", {
        "form_path": str(narrow_xlsx.relative_to(tmp_path)),
        "memo_text": memo_file.read_text(encoding="utf-8"),
        "mode": "demo",
        "auto_adapt": True,
    })
    assert mcp_res["isError"] is False
    assert mcp_res["structuredContent"]["verification"]["overall_status"] == "PASS"
    assert mcp_res["structuredContent"]["fields"][0]["value"] == "設備更新を実施する。"

    # R1-A4: prior human_entered mapping preserved across adaptation rounds when another field overflows
    doc_two = docx.Document()
    tbl_two = doc_two.add_table(rows=2, cols=2)
    tbl_two.cell(0, 0).text = "申請者氏名"
    tbl_two.cell(0, 1).text = "{{申請者氏名}}"
    tbl_two.cell(1, 0).text = "申請理由"
    tbl_two.cell(1, 1).text = "{{申請理由}}"
    two_docx = tmp_path / "two_fields.docx"
    doc_two.save(two_docx)
    memo_two = "申請者氏名：旧名 太郎\n申請理由：業務効率化のため新設備を導入する。なお、既存機器の点検で異常なし。\n"
    r_two_init = run_pipeline(two_docx, memo_two, tmp_path / "two_init", mode="demo", auto_adapt=False)
    prior_maps = [
        m.model_copy(update={"status": "human_entered", "filled_value": "新名 花子", "char_count": 5})
        if m.slot_id == "申請者氏名" else m
        for m in r_two_init.mappings
    ]
    r_two_adapt = run_pipeline(two_docx, memo_two, tmp_path / "two_adapt", mode="demo",
                               prior=(prior_maps, {"usage": {"requests": 5, "input_tokens": 100, "output_tokens": 50}}),
                               auto_adapt=True)
    assert r_two_adapt.report.overall_status == "PASS"
    by_slot = {m.slot_id: m for m in r_two_adapt.mappings}
    assert by_slot["申請者氏名"].status == "human_entered" and by_slot["申請者氏名"].filled_value == "新名 花子"
    assert by_slot["申請理由"].filled_value == "業務効率化のため新設備を導入する。"
    assert r_two_adapt.engine.get("prior_usage", {}).get("requests") == 5
    assert r_two_adapt.engine["usage"].get("requests", 0) == 0

    # R1-A5 / R1-O4 / R1-A6: live discovery + multi-round adaptation usage accumulation & model forwarding
    from conftest import echo_model

    disc_resp = echo_model({"cells": [{"candidate_id": "c01", "role": "applicant_fillable", "reason": "ok"}]})
    map_resp1 = echo_model({"fields": [{
        "slot_id": "申請理由", "status": "filled",
        "value": "設備更新を実施する。なお、フィルター清掃のみで異常なし。",
        "source_quotes": ["設備更新を実施する。なお、フィルター清掃のみで異常なし。"],
        "confidence": 0.95, "reasoning": "ok",
    }]})
    map_resp2 = echo_model({"fields": [{
        "slot_id": "申請理由", "status": "filled",
        "value": "設備更新を実施する。",
        "source_quotes": ["設備更新を実施する。"],
        "confidence": 0.95, "reasoning": "ok",
    }]})

    fake_live = fake_client_factory([disc_resp, map_resp1, map_resp2])
    r_live_adapt = run_pipeline(
        narrow_xlsx, memo_file.read_text(encoding="utf-8"), tmp_path / "live_adapt",
        mode="live", model="claude-custom-pin", client=fake_live, auto_adapt=True,
    )
    assert all(c["model"] == "claude-custom-pin" for c in fake_live.beta.messages.calls)
    assert r_live_adapt.engine["usage"]["requests"] == len(fake_live.beta.messages.calls)
    assert r_live_adapt.engine["usage"]["input_tokens"] == 1200 * len(fake_live.beta.messages.calls)
    assert r_live_adapt.engine["discovery_usage"]["requests"] == 1
    assert len(r_live_adapt.engine["usage"]["usage_by_round"]) >= 2

    # R1-A7: adapted spec rejects a modified blank form on reuse
    tampered_docx = tmp_path / "tampered_grow.docx"
    doc_tamper = docx.Document()
    tbl_t = doc_tamper.add_table(rows=1, cols=2)
    tbl_t.cell(0, 0).text = "申請理由（改訂版）"
    tbl_t.cell(0, 1).text = "{{申請理由}}"
    doc_tamper.save(tampered_docx)
    with pytest.raises(ValueError, match="blank form changed after discovery"):
        run_pipeline(tampered_docx, memo_grow, tmp_path / "tamper_run", mode="demo",
                     slot_spec=tmp_path / "grow_with_adapt" / "grow_bottom.adapted.slots.json")









def test_extra_everyday_forms_are_discovered_filled_and_pass(api):
    """form7–form10 (fictional everyday forms for /start): the expected applicant cells are recommended with the memo's
    values, every office-only candidate is locked and cannot be confirmed, and the draft passes."""
    expected = {
        "form7": {"申請日": "令和8年11月12日（木）", "申請者氏名": "見本 七海", "所属部署": "営業部 第二営業課",
                  "出張先": "架空県架空市（架空産業展示会 会場）", "出張期間": "令和8年11月24日（火）～令和8年11月25日（水）",
                  "用務内容": "架空産業展示会に出展し、新製品の説明と取引先3社との打合せを行う。", "概算旅費": "58,400円"},
        "form8": {"申請日": "令和8年11月13日（金）", "申請者氏名": "架空 一郎", "所属": "材料工学研究室",
                  "品名・数量": "精密電子天びん 1台", "購入予定金額": "162,800円",
                  "購入理由": "既存の天びんが故障し、試料の重量測定ができないため更新する。", "納品希望日": "令和8年12月1日（火）"},
        "form9": {"届出日": "令和8年11月16日（月）", "氏名": "模擬 九郎", "所属・職名": "市民課 主事", "休暇の種類": "年次有給休暇",
                  "休暇期間": "令和8年11月26日（木）～令和8年11月27日（金）", "理由": "家族の通院の付き添いのため。",
                  "休暇中の連絡先": "携帯電話（架空番号 090-0000-0000）"},
        "form10": {"申請日": "令和8年11月17日（火）", "団体名": "架空町内会 子ども会", "代表者氏名": "見本 十和子",
                   "利用希望日": "令和8年12月3日（木）", "利用時間": "18時00分から20時00分まで", "利用人数": "18名",
                   "利用目的": "年末のクリスマス会の打合せと飾り付けの準備を行う。"},
    }
    for demo, values in expected.items():
        d = api.post("/api/discover/demo", json={"demo_id": demo, "mode": "demo"}).json()
        rec = {c["label"] for c in d["candidates"] if c["recommended"]}
        assert rec == set(values), demo
        office = [c for c in d["candidates"] if not c["recommended"]]
        assert office and all(c["office_only"] for c in office), demo
        bad = api.post("/api/runs/from-discovery", json={"discovery_id": d["discovery_id"], "mode": "demo",
                                                         "confirmed_targets": [office[0]["candidate_id"]]})
        assert bad.status_code == 422, demo  # an office-only cell can never be confirmed
        r = api.post("/api/runs/from-discovery", json={"discovery_id": d["discovery_id"],
                                                       "confirmed_targets": "recommended", "mode": "demo"}).json()
        assert r["report"]["overall_status"] == "PASS" and not r["report"]["pending_slots"], demo
        assert {m["slot_id"]: m["filled_value"] for m in r["mappings"]} == values, demo
        assert all(m["source_quote"] for m in r["mappings"]), demo
    assert 'value="form10"' in api.get("/start").text


def test_start_page_explains_every_flag_and_category_and_loads_chat_drafts(api):
    from form_guard.verifier import _FLAG_TEXT
    js = api.get("/static/start.js").text
    for flag in _FLAG_TEXT:
        assert f"{flag}:" in js, flag  # every flag the verifier can raise has a plain-Japanese explanation
    for cat in ("zip", "xml", "layout", "text", "grounding", "constraints", "application", "visual"):
        assert f"  {cat}: [" in js, cat
    assert 'get("run_id")' in js and "/api/runs/${encodeURIComponent(runId)}" in js


def test_start_page_media_and_optional_demo_video(api, monkeypatch, tmp_path):
    page = api.get("/start").text
    for name in ("hero", "step1", "step2", "step3", "step4", "step5"):
        assert f"/static/media/{name}.jpg" in page and api.get(f"/static/media/{name}.jpg").status_code == 200
    monkeypatch.setenv("FORM_GUARD_DEMO_VIDEO", str(tmp_path / "missing.mp4"))
    assert api.get("/media/demo.mp4").status_code == 404  # the page then keeps the player hidden
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"\x00" * 64)
    monkeypatch.setenv("FORM_GUARD_DEMO_VIDEO", str(video))
    r = api.get("/media/demo.mp4", headers={"Range": "bytes=0-0"})
    assert r.status_code == 206 and r.headers["content-type"] == "video/mp4"
