"""Form-Guard Agent test suite (offline; the live Claude path is exercised with a fake client)."""

from __future__ import annotations

import hashlib
import io
import re
import shutil
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

import docx
import openpyxl
import pytest
from lxml import etree

from conftest import fake_message
from form_guard.constraints import pattern_from_example, validate_value
from form_guard.grounding import affirmed_options, ground_value, locate_quote
from form_guard.mapper import ClaudeEngine, DemoEngine, Draft, LiveEngineError, human_mapping, map_fields
from form_guard.models import FormSlot
from form_guard.ooxml import S_NS, UnsafeXMLError, UnsupportedXMLError, add_preserve_attr, index_part, raw_char_offsets
from form_guard.pipeline import run_pipeline
from form_guard.report import build_report_html
from form_guard.samples import DEMOS, demo_paths, generate_samples, read_memo
from form_guard.scanner import scan
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
    run = map_fields(scan(p["form"]).slots, memo, ClaudeEngine(client=client))
    calls = client.beta.messages.calls
    assert len(calls) == 2
    first = calls[0]
    assert first["model"] == "claude-opus-5-5" and first["thinking"] == {"type": "adaptive"}
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
    assert [d["id"] for d in demos] == ["form1", "form2", "form3"] and "memo_text" in demos[0]


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
    run = map_fields(scan(p["form"]).slots, memo, ClaudeEngine(client=client))
    body = seen["body"]
    assert seen["url"].endswith("/v1/messages?beta=true") or "/v1/messages" in seen["url"]
    assert "server-side-fallback-2026-07-01" in seen["beta"]
    assert body["model"] == "claude-opus-5-5" and body["stream"] is True
    assert body["thinking"] == {"type": "adaptive"} and body["fallbacks"] == "default"
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
    for bad in ("2026年2月29日", "2026年2月30日", "2026/02/29", "2026年11月2日（火）", "11月2日"):
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
    m = map_fields(slots, "提出日：2026年11月2日（火）に提出\n", DemoEngine()).mappings[0]
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
