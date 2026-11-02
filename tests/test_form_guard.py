"""Form-Guard Agent test suite (offline; the live Claude path is exercised with a fake client)."""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from datetime import date
from pathlib import Path

import docx
import openpyxl
import pytest
from lxml import etree

from conftest import fake_message
from form_guard.constraints import pattern_from_example, validate_value
from form_guard.grounding import ground_value, locate_quote
from form_guard.mapper import ClaudeEngine, DemoEngine, LiveEngineError, human_mapping, map_fields
from form_guard.ooxml import S_NS, UnsafeXMLError, index_part, raw_char_offsets
from form_guard.pipeline import run_pipeline
from form_guard.samples import DEMOS, demo_paths, generate_samples, read_memo
from form_guard.scanner import scan
from form_guard.textnorm import (
    condense_extractive,
    find_amounts,
    find_dates,
    kanji_to_int,
    parse_compound,
    to_wareki,
)
from form_guard.verifier import compare_naive, infer_values_from_filled, verify
from form_guard.visual_diff import compare_layouts, layout_docx
from form_guard.writer import fill_form, patch_part
from form_guard.ziputil import read_raw_zip

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
    assert to_wareki(date(2026, 11, 2)) == "令和8年11月2日"
    assert to_wareki(date(2019, 5, 1)) == "令和元年5月1日" and to_wareki(date(2019, 4, 30)) == "平成31年4月30日"
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
    assert codes("事業実施期間", "令和9年2月26日～令和8年12月1日") == ["format"]


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
    assert date_m.filled_value == "令和8年11月2日"
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
    assert by["事業実施期間"].filled_value == "令和8年12月1日～令和9年2月26日"


def test_missing_fact_is_never_guessed(demo_runs):
    r = demo_runs("form2")
    m = {x.slot_id: x for x in r.mappings}["法人番号"]
    assert m.status == "needs_human_input" and m.filled_value == "" and not m.suggested_value
    assert r.report.pending_slots == ["法人番号"] and r.report.overall_status == "NEEDS_HUMAN_INPUT"
    assert "[記入欄: 法人番号]" in "".join(p.text for t in docx.Document(r.filled_path).tables for row in t.rows for p in row.cells[1].paragraphs)


def test_inferred_year_is_flagged_for_the_reviewer(demo_runs):
    m = {x.slot_id: x for x in demo_runs("form3").mappings}["次回点検予定日"]
    assert m.filled_value == "令和8年12月8日" and "year_inferred" in m.normalizations
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
    assert sf["F8"].value == "要是正" and sf["D18"].value == "令和8年12月8日"
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
