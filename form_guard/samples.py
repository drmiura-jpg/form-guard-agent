"""Generators for the 100% fictional, non-medical demo material.

Everything here was written from scratch for this project: three blank forms of
fictional organisations (a university, a municipality, a facility operator), the
free-text memos that contain the source facts, sidecar slot specs, and scripted
"naive AI regeneration" files that reproduce the typical failure modes of
re-generating a form from scratch (lost borders and merged cells, paraphrased
legal text, US-Letter page setup, invented numbers).

Two further forms are *unmarked* blank forms — no ``{{markers}}``, no ``[記入欄: …]``,
no ``.slots.json`` — for blank-cell discovery: a research-equipment application
(.docx) and a disaster-stockpile loan application (.xlsx), each with an office-only
area, merged cells and (in the workbook) a SUM formula.

Generation is deterministic: ZIP timestamps and document properties are fixed,
so re-running the generator reproduces byte-identical files on the same machine.
"""

from __future__ import annotations

import io
import json
import re
from datetime import datetime
from pathlib import Path

from .ziputil import rebuild_deterministic

SAMPLE_TIMESTAMP = datetime(2026, 11, 2, 0, 0, 0)
FICTION_NOTICE_JA = "※本様式は架空の組織を想定したデモ用サンプルです（Fictional sample — not a real form）"

DEMOS = [
    {
        "id": "form1",
        "title": "共同研究計画変更届出書（様式第3号）",
        "title_en": "Joint Research Plan Modification Form (Form No. 3)",
        "org": "架空学術大学（fictional university）",
        "file_type": "docx",
        "form": "form1_research_plan_change.docx",
        "memo": "memo1_research_change.txt",
        "naive": "broken_naive_ai_form1.docx",
        "highlights": ["和暦 date conversion", "full-width ID → half-width", "200-char condensation", "split-run markers"],
    },
    {
        "id": "form2",
        "title": "令和8年度 地域中小企業省エネ設備導入補助金 交付申請書（様式第1号）",
        "title_en": "Municipal SME Energy-Saving Equipment Subsidy Application (Form No. 1)",
        "org": "架空市（fictional city）",
        "file_type": "docx",
        "form": "form2_municipal_equipment_subsidy.docx",
        "memo": "memo2_subsidy_application.txt",
        "naive": "broken_naive_ai_form2.docx",
        "highlights": ["483万6千円 → 4,836,000円", "printed cap enforced (上限1,000,000円)", "missing 法人番号 → human input", "empty cell by coordinates"],
    },
    {
        "id": "form3",
        "title": "月次設備安全点検および改善報告書（様式A-4）",
        "title_en": "Monthly Facility & Equipment Safety Inspection Report (Form A-4)",
        "org": "架空市立サンプル技術交流センター（fictional facility）",
        "file_type": "xlsx",
        "form": "form3_facility_safety_inspection.xlsx",
        "memo": "memo3_safety_inspection.txt",
        "naive": "broken_naive_ai_form3.xlsx",
        "highlights": ["drop-down lists respected", "COUNTIF formulas preserved", "120-char condensation keeps the interim measure", "year inferred → flagged"],
    },
]


UNMARKED_FORMS = [
    {
        "id": "form4",
        "title": "施設・備品一時使用許可申請書（マーカーなし白紙様式）",
        "title_en": "Temporary Facility & Equipment Use Permit Application (unmarked blank form)",
        "org": "架空学術機関（fictional research institute）",
        "file_type": "docx",
        "form": "form4_unmarked_blank.docx",
        "memo": "memo4_equipment_use.txt",
        "highlights": ["no markers — 11 candidate cells", "4 office-only cells locked (受付番号・受付日・審査欄・事務局受付欄)",
                       "duplicate 「氏名」 → 研究代表者_氏名 / 事務担当者_氏名", "vMerge continuation rejected"],
    },
    {
        "id": "form5",
        "title": "防災備蓄品 貸出・返却管理票（マーカーなし白紙様式）",
        "title_en": "Disaster Stockpile Loan & Return Record (unmarked blank workbook)",
        "org": "架空市（fictional city）",
        "file_type": "xlsx",
        "form": "form5_unmarked_blank.xlsx",
        "memo": "memo5_stockpile_loan.txt",
        "highlights": ["5 applicant cells in merged ranges", "SUM formula cell rejected",
                       "事務局使用欄（決裁印・受付番号・返却確認） locked", "cell style ids kept"],
    },
]


def demo_by_id(demo_id: str) -> dict:
    for d in DEMOS:
        if d["id"] == demo_id:
            return d
    raise KeyError(demo_id)


def unmarked_by_id(form_id: str) -> dict:
    for u in UNMARKED_FORMS:
        if u["id"] == form_id:
            return u
    raise KeyError(form_id)


def unmarked_paths(item: dict, samples_dir: Path | None = None) -> dict[str, Path]:
    base = Path(samples_dir) if samples_dir else default_samples_dir()
    return {"form": base / "blank_forms" / item["form"], "memo": base / "memos" / item["memo"]}


def default_samples_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "samples"


def demo_paths(demo: dict, samples_dir: Path | None = None) -> dict[str, Path]:
    base = Path(samples_dir) if samples_dir else default_samples_dir()
    return {
        "form": base / "blank_forms" / demo["form"],
        "memo": base / "memos" / demo["memo"],
        "naive": base / "broken_before" / demo["naive"],
    }


# ============================================================================ memos

MEMO1 = """件名：【共同研究】計画変更届の作成依頼（課題番号は下記）
日時：2026/10/28（水）15:00-16:00　場所：架空学術大学 理工学研究科 第2会議室（オンライン併用）
出席：架空 太郎（研究代表者）、模擬 花子（研究協力課）、見本 次郎（共同研究先・架空精機株式会社）

■ 確認事項
・研究課題番号：ＪＲ－２０２６－０４７（※全角で書かれていたので届出書では半角に直すこと）
・研究代表者：架空 太郎（理工学研究科 機械システム専攻 教授）
・所属部局：理工学研究科
・変更種別：研究内容、研究経費
・提出日：2026年11月2日（月）に研究協力課へ提出予定

■ 変更前の計画
現行計画では、試作した小型熱交換器の性能を実機試験のみで評価する（試験回数12回）。

■ 変更後の計画
実機試験を6回に減らし、数値シミュレーションによる性能評価を追加する。シミュレーション用の計算サーバ利用料を計上する。

■ 変更理由と予算への影響（届出書は200字以内）
共同研究先の試験設備が2026年12月から改修工事に入り、当初予定していた実機試験12回のうち6回が実施できなくなった。そこで不足分を数値シミュレーションで補うこととした。なお、シミュレーションの妥当性は残る6回の実機試験結果と比較して検証する。予算面では、試験装置の借用料が42万円減る一方、計算サーバ利用料として80万円が新たに必要になるため、差し引きで38万円の増額となる。増額分は共同研究先からの追加負担金で賄うことで合意済み（直接経費総額に占める割合は約6％で、20％未満のため経費変更承認申請は不要）。

■ その他
・届出書の注意事項に従い、変更予定日（2026年12月15日（火））の30日前までに提出する。
・次回打合せは11月中旬に調整。
"""

MEMO2 = """From: 模擬 一郎 <ichiro@example.jp>
To: 架空市 産業振興課 補助金担当
Subject: 省エネ設備導入補助金の申請内容について（株式会社サンプル精工）

架空市 産業振興課 ご担当者様

いつもお世話になっております。株式会社サンプル精工の模擬です。
先日ご相談した省エネ設備導入補助金について、申請書に記載する内容を以下のとおり整理しました。

【申請者情報】
申請事業者名：株式会社サンプル精工
代表者：代表取締役　模擬 一郎
所在地：架空市中央区架空町一丁目2番3号
（法人番号は経理担当に確認中のため、後日お知らせします）

【導入する設備】
導入設備名称：高効率ヒートポンプ空調機（冷房能力28kW）2台
導入予定金額：税抜で483万6千円（見積書No.Q-2026-118による。税込では5,319,600円）
補助金申請額：補助率1/3で計算すると1,612,000円ですが、上限の100万円で申請します。
年間省エネ見込量：年間12,400kWhの電力削減（現行設備比で約18％削減）
事業実施期間：2026年12月1日（火）から2027年2月26日（金）まで（工事期間を含む）

【その他】
申請日：2026年11月2日（月）
連絡担当者：総務部 架空 次郎（jiro@example.jp）

以上、よろしくお願いいたします。
"""

MEMO3 = """11/2（月）定例点検メモ（記録者：点検責任者 模擬 三郎）

点検対象施設：架空市立サンプル技術交流センター（本館）
点検実施日：令和8年11月2日（月）
点検責任者：設備管理課 模擬 三郎

【点検結果】
・空調設備：2階東側の空調機で異音あり。フィルターの目詰まりも確認。判定は要是正。
・非常用電源：始動試験OK、燃料残量も規定値以上。判定は良。
・給排水設備：1階給湯室の排水の流れがやや悪い。判定は要注意。
・照明・電気設備：問題なし。判定は良。
・防火設備：防火扉の閉鎖試験OK、避難経路の障害物なし。判定は良。

【指摘事項と是正措置】
2階東側空調機（AC-2E）の送風ファン軸受の摩耗による異音を確認した。フィルターの目詰まりも著しく、冷房効率の低下が見られる。11月6日（金）までにフィルターを交換し、軸受は保守業者が部品を取り寄せて11月20日（金）までに交換する。交換完了までは同機の運転時間を短縮し、隣接区画の空調で補う。給湯室の排水不良については、清掃業者に高圧洗浄を依頼済み。

【次回点検】
次回点検は12月8日（火）に実施予定。
"""

MEMO4 = """件名：施設・備品一時使用許可申請の準備メモ（架空学術機関 施設管理課宛て）
作成：2026年11月2日（月）　作成者：模擬 花子（研究支援課）

■ 申請内容
・申請日：2026年11月2日（月）
・申請者氏名：架空 一郎（材料工学研究室 准教授）
・所属機関：架空学術機関 材料工学研究室
・利用目的：試作した多孔質セラミック試料の表面微細構造を観察し、焼成温度による気孔径の違いを比較する。
・利用希望期間：2026年11月16日（月）から2026年11月27日（金）まで

■ 連絡先
・研究代表者：架空 一郎
・事務担当者：模擬 花子

■ メモ
・受付番号・受付日・審査欄は事務局が記入するので、申請者側では空欄のままにする。
"""

MEMO5 = """架空市 防災課への防災備蓄品 貸出・返却管理票 記入メモ（架空町内会 防災部）
記録日：2026年11月2日（月）

【申請内容】
申請日：2026年11月2日（月）
申請団体名：架空町内会 防災部
代表者氏名：模擬 五郎（防災部長）
借用希望物品：簡易テント2張、発電機1台、投光器2台
使用予定金額：48,000円（燃料費・運搬費の見込み）
使用目的：2026年11月29日（日）の地域防災訓練で使用する。

【メモ】
・決裁印・受付番号・返却確認の欄は事務局が記入するため、空欄のままにしておく。
"""

MEMOS = {"memo1_research_change.txt": MEMO1, "memo2_subsidy_application.txt": MEMO2,
         "memo3_safety_inspection.txt": MEMO3, "memo4_equipment_use.txt": MEMO4,
         "memo5_stockpile_loan.txt": MEMO5}

# ======================================================================= slot specs

SPECS = {
    "form1_research_plan_change": {
        "form_title": "共同研究計画変更届出書（様式第3号）",
        "form_title_en": "Joint Research Plan Modification Form",
        "slots": {
            "提出日": {"aliases": ["提出日"], "description": "届出書の提出日（和暦）"},
            "研究課題番号": {"aliases": ["研究課題番号", "課題番号"], "description": "共同研究の課題番号（半角）"},
            "研究代表者氏名": {"aliases": ["研究代表者", "代表者氏名"], "description": "研究代表者の氏名のみ（役職・所属は含めない）"},
            "所属部局": {"aliases": ["所属部局", "所属"], "description": "研究代表者の所属部局"},
            "変更種別": {"aliases": ["変更種別", "変更の種別"], "description": "変更の種類（複数可）"},
            "変更前の計画概要": {"aliases": ["変更前の計画", "変更前", "現行計画"], "description": "現行（変更前）の計画の概要"},
            "変更後の計画概要": {"aliases": ["変更後の計画", "変更後"], "description": "変更後の計画の概要"},
            "変更理由および予算影響": {"aliases": ["変更理由と予算への影響", "変更理由および予算影響", "変更理由"],
                                     "description": "変更が必要な理由と予算への影響"},
        },
    },
    "form2_municipal_equipment_subsidy": {
        "form_title": "令和8年度 地域中小企業省エネ設備導入補助金 交付申請書（様式第1号）",
        "form_title_en": "Municipal SME Energy-Saving Equipment Subsidy Application",
        "slots": {
            "申請日": {"aliases": ["申請日"], "description": "申請書の提出日（和暦）"},
            "所在地": {"aliases": ["所在地", "住所"], "description": "申請事業者の所在地"},
            "申請事業者名": {"aliases": ["申請事業者名", "事業者名"], "description": "申請事業者の名称"},
            "代表者職氏名": {"aliases": ["代表者職氏名", "代表者"], "description": "代表者の役職と氏名"},
            "法人番号": {"aliases": ["法人番号"], "description": "13桁の法人番号（推測禁止）"},
            "導入設備名称": {"aliases": ["導入設備名称", "設備名称"], "description": "導入する設備の名称と台数"},
            "事業実施期間": {"aliases": ["事業実施期間", "実施期間"], "description": "補助事業の開始日～完了日（和暦）"},
            "年間省エネ見込量": {"aliases": ["年間省エネ見込量", "省エネ見込量"], "description": "年間の省エネルギー見込量"},
            "導入予定金額_税抜": {"aliases": ["導入予定金額", "導入金額"], "description": "設備費・工事費の税抜金額"},
            "補助金申請額": {"aliases": ["補助金申請額", "申請額"], "description": "申請する補助金額（上限・端数処理に従う）"},
            "担当者連絡先": {"aliases": ["連絡担当者", "担当者"], "description": "連絡担当者の所属・氏名・連絡先"},
        },
        "targets": [{"slot_id": "担当者連絡先", "table": 1, "row": 8, "col": 1, "label": "担当者氏名・連絡先"}],
    },
    "form3_facility_safety_inspection": {
        "form_title": "月次設備安全点検および改善報告書（様式A-4）",
        "form_title_en": "Monthly Facility & Equipment Safety Inspection Report",
        "slots": {
            "点検対象施設": {"aliases": ["点検対象施設", "対象施設"], "description": "点検した施設名"},
            "点検実施日": {"aliases": ["点検実施日", "実施日"], "description": "点検を実施した日（和暦）"},
            "点検責任者": {"aliases": ["点検責任者", "責任者"], "description": "点検責任者の所属・氏名"},
            "空調設備_判定": {"aliases": ["空調設備"], "description": "空調設備の判定"},
            "非常用電源_判定": {"aliases": ["非常用電源"], "description": "非常用電源の判定"},
            "給排水設備_判定": {"aliases": ["給排水設備"], "description": "給排水設備の判定"},
            "照明設備_判定": {"aliases": ["照明・電気設備", "照明設備"], "description": "照明・電気設備の判定"},
            "防火設備_判定": {"aliases": ["防火設備"], "description": "防火設備の判定"},
            "指摘事項および是正措置": {"aliases": ["指摘事項と是正措置", "指摘事項および是正措置", "指摘事項"],
                                     "description": "指摘事項と是正措置の要旨"},
            "次回点検予定日": {"aliases": ["次回点検予定日", "次回点検"], "description": "次回点検の予定日（和暦）"},
        },
    },
}

# ============================================================ python-docx helpers

_TCPR_ORDER = ["cnfStyle", "tcW", "gridSpan", "hMerge", "vMerge", "tcBorders", "shd", "noWrap", "tcMar",
               "textDirection", "tcFitText", "vAlign", "hideMark"]
_TBLPR_ORDER = ["tblStyle", "tblpPr", "tblOverlap", "bidiVisual", "tblStyleRowBandSize", "tblStyleColBandSize",
                "tblW", "jc", "tblCellSpacing", "tblInd", "tblBorders", "shd", "tblLayout", "tblCellMar", "tblLook"]


def _insert_ordered(parent, child, order):
    from docx.oxml.ns import qn

    local = child.tag.split("}")[1]
    for existing in parent.findall(qn(f"w:{local}")):
        parent.remove(existing)
    idx = order.index(local)
    for i, el in enumerate(parent):
        el_local = el.tag.split("}")[1] if "}" in el.tag else el.tag
        if el_local in order and order.index(el_local) > idx:
            parent.insert(i, child)
            return child
    parent.append(child)
    return child


def _el(tag, **attrs):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    e = OxmlElement(tag)
    for k, v in attrs.items():
        e.set(qn(f"w:{k}"), str(v))
    return e


def _borders(tag, spec):
    box = _el(tag)
    for side, (val, sz) in spec.items():
        box.append(_el(f"w:{side}", val=val, sz=sz, space=0, color="000000" if val != "nil" else "auto"))
    return box


def _set_run_font(run, size=None, bold=None, east_asia=None, ascii_font=None):
    from docx.oxml.ns import qn
    from docx.shared import Pt

    if size:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if east_asia or ascii_font:
        rpr = run._r.get_or_add_rPr()
        rfonts = rpr.get_or_add_rFonts()
        if ascii_font:
            rfonts.set(qn("w:ascii"), ascii_font)
            rfonts.set(qn("w:hAnsi"), ascii_font)
        if east_asia:
            rfonts.set(qn("w:eastAsia"), east_asia)


def _para(doc_or_cell, text="", align=None, size=None, bold=None, gothic=False, before=0, after=0,
          indent_mm=None, hanging_mm=None, first=False):
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Mm, Pt

    if first and doc_or_cell.paragraphs:
        p = doc_or_cell.paragraphs[0]
    else:
        p = doc_or_cell.add_paragraph()
    if align:
        p.alignment = {"center": WD_ALIGN_PARAGRAPH.CENTER, "right": WD_ALIGN_PARAGRAPH.RIGHT,
                       "both": WD_ALIGN_PARAGRAPH.JUSTIFY, "left": WD_ALIGN_PARAGRAPH.LEFT}[align]
    pf = p.paragraph_format
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    if size and size != 10.5:
        from docx.enum.text import WD_LINE_SPACING

        pf.line_spacing = Pt(max(11, round(size * 1.45)))
        pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    if indent_mm is not None:
        pf.left_indent = Mm(indent_mm)
    if hanging_mm is not None:
        pf.first_line_indent = Mm(-hanging_mm)
    if text:
        r = p.add_run(text)
        _set_run_font(r, size=size, bold=bold, east_asia="游ゴシック" if gothic else None,
                      ascii_font="Yu Gothic" if gothic else None)
    return p


def _split_runs(p, pieces, size=None, proof_err=False, bookmark=None):
    """Write a marker as several runs, like Word does after editing (distinct rsids)."""
    from docx.oxml.ns import qn

    if bookmark:
        p._p.append(_el("w:bookmarkStart", id=bookmark[0], name=bookmark[1]))
    for i, piece in enumerate(pieces):
        r = p.add_run(piece)
        _set_run_font(r, size=size)
        r._r.set(qn("w:rsidR"), f"00{0xA1B2C0 + i * 17:06X}")
        if proof_err and i == 0:
            r._r.addnext(_el("w:proofErr", type="spellStart"))
        if proof_err and i == len(pieces) - 1:
            r._r.addnext(_el("w:proofErr", type="spellEnd"))
    if bookmark:
        p._p.append(_el("w:bookmarkEnd", id=bookmark[0]))
    return p


def _new_document(title: str):
    from docx import Document
    from docx.enum.text import WD_LINE_SPACING
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt

    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Mm(210), Mm(297)
    sec.top_margin, sec.bottom_margin = Mm(15), Mm(14)
    sec.left_margin, sec.right_margin = Mm(20), Mm(20)
    sec.header_distance, sec.footer_distance = Mm(9), Mm(8)
    normal = doc.styles["Normal"]
    normal.font.name = "Yu Mincho"
    normal.font.size = Pt(10.5)
    rpr = normal.element.get_or_add_rPr()
    rpr.get_or_add_rFonts().set(qn("w:eastAsia"), "游明朝")
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.line_spacing = Pt(15)
    normal.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    cp = doc.core_properties
    cp.title = title
    cp.subject = "Fictional sample form (Form-Guard Agent demo)"
    cp.author = "Form-Guard Agent sample generator"
    cp.last_modified_by = "Form-Guard Agent sample generator"
    cp.comments = "100% fictional demo form written from scratch; no real organisation, person or data."
    cp.keywords = "fictional; sample; Form-Guard"
    cp.created = SAMPLE_TIMESTAMP
    cp.modified = SAMPLE_TIMESTAMP
    cp.revision = 1
    return doc


def _header_footer(doc, header_text: str):
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    sec = doc.sections[0]
    hp = sec.header.paragraphs[0]
    hp.text = header_text
    hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for r in hp.runs:
        r.font.size = Pt(8)
    fp = sec.footer.paragraphs[0]
    fp.text = FICTION_NOTICE_JA
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for r in fp.runs:
        r.font.size = Pt(7.5)


def _table(doc, rows, widths_mm, outer=12, inner=4, align=None, borders=None):
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml.ns import qn
    from docx.shared import Mm

    t = doc.add_table(rows=rows, cols=len(widths_mm))
    tblPr = t._tbl.tblPr
    spec = borders or {"top": ("single", outer), "left": ("single", outer), "bottom": ("single", outer),
                       "right": ("single", outer), "insideH": ("single", inner), "insideV": ("single", inner)}
    _insert_ordered(tblPr, _borders("w:tblBorders", spec), _TBLPR_ORDER)
    _insert_ordered(tblPr, _el("w:tblLayout", type="fixed"), _TBLPR_ORDER)
    tw = _el("w:tblW", type="dxa", w=int(sum(widths_mm) * 56.6929))
    _insert_ordered(tblPr, tw, _TBLPR_ORDER)
    if align == "right":
        t.alignment = WD_TABLE_ALIGNMENT.RIGHT
    grid = t._tbl.tblGrid
    for gc, w in zip(grid.findall(qn("w:gridCol")), widths_mm):
        gc.set(qn("w:w"), str(int(w * 56.6929)))
    for row in t.rows:
        for cell, w in zip(row.cells, widths_mm):
            cell.width = Mm(w)
    return t


def _fix_widths(t, widths_mm):
    """After merges, make each tcW equal to the sum of the grid columns it spans."""
    for tr in t._tbl.tr_lst:
        col = 0
        for tc in tr.tc_lst:
            span = tc.grid_span
            w = sum(widths_mm[col : col + span])
            tcPr = tc.get_or_add_tcPr()
            _insert_ordered(tcPr, _el("w:tcW", type="dxa", w=int(w * 56.6929)), _TCPR_ORDER)
            col += span


def _shade(cell, fill):
    tcPr = cell._tc.get_or_add_tcPr()
    _insert_ordered(tcPr, _el("w:shd", val="clear", color="auto", fill=fill), _TCPR_ORDER)


def _cell_borders(cell, spec):
    tcPr = cell._tc.get_or_add_tcPr()
    _insert_ordered(tcPr, _borders("w:tcBorders", spec), _TCPR_ORDER)


def _vcenter(cell):
    from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT

    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER


def _row_height(row, mm, exact=False):
    from docx.enum.table import WD_ROW_HEIGHT_RULE
    from docx.shared import Mm

    row.height = Mm(mm)
    row.height_rule = WD_ROW_HEIGHT_RULE.EXACTLY if exact else WD_ROW_HEIGHT_RULE.AT_LEAST


def _label(cell, text, hint=None, center=False):
    _para(cell, text, first=True, bold=True, gothic=True, size=9.5, align="center" if center else None)
    if hint:
        _para(cell, hint, size=7.5, align="center" if center else None)
    _vcenter(cell)


def _banner(t, r, text):
    row = t.rows[r]
    cell = row.cells[0].merge(row.cells[-1])
    _para(cell, text, first=True, bold=True, gothic=True, size=10)
    _shade(cell, "D9E2F3")
    _vcenter(cell)
    _row_height(row, 7)
    return cell


def _docx_bytes(doc) -> bytes:
    buf = io.BytesIO()
    doc.save(buf)
    return _finalise_docx(buf.getvalue())


def _finalise_docx(blob: bytes) -> bytes:
    def transform(name: str, data: bytes) -> bytes:
        if name == "_rels/.rels":
            data = re.sub(rb'<Relationship [^>]*Target="docProps/thumbnail\.jpeg"[^>]*/>', b"", data)
        return data

    return rebuild_deterministic(blob, drop={"docProps/thumbnail.jpeg"}, transform=transform)


# ============================================================== blank form builders


def build_form1() -> bytes:
    doc = _new_document("共同研究計画変更届出書（様式第3号）")
    _header_footer(doc, "架空学術大学 研究推進部 研究協力課")
    _para(doc, "様式第3号（第8条関係）", size=9, first=True)
    _para(doc, "共同研究計画変更届出書", align="center", size=16, bold=True, gothic=True, before=4, after=6)
    _para(doc, "提出日：{{提出日}}", align="right", after=4)
    _para(doc, "架空学術大学長　殿", after=4)
    _para(doc, "　下記のとおり共同研究計画を変更したいので、架空学術大学共同研究取扱規程第8条の規定により届け出ます。",
          align="both", after=2)
    _para(doc, "記", align="center", after=2)

    widths = [30, 22, 59, 59]
    t = _table(doc, 12, widths)
    _banner(t, 0, "1．研究課題")
    # R1 研究課題番号
    _label(t.cell(1, 0), "研究課題番号", "（半角英数字　例：JR-2026-001）")
    c = t.cell(1, 1).merge(t.cell(1, 3))
    _para(c, "{{研究課題番号}}", first=True)
    _vcenter(c)
    _row_height(t.rows[1], 10)
    # R2-R3 研究代表者 (vMerge) / 氏名 / 所属部局
    lead = t.cell(2, 0).merge(t.cell(3, 0))
    _label(lead, "研究代表者", center=True)
    _label(t.cell(2, 1), "氏名", center=True)
    c = t.cell(2, 2).merge(t.cell(2, 3))
    _split_runs(c.paragraphs[0], ["{{研究", "代表者", "氏名}}"], proof_err=True)
    _vcenter(c)
    _label(t.cell(3, 1), "所属部局", center=True)
    c = t.cell(3, 2).merge(t.cell(3, 3))
    _para(c, "{{所属部局}}", first=True)
    _vcenter(c)
    _row_height(t.rows[2], 9)
    _row_height(t.rows[3], 9)
    _banner(t, 4, "2．変更の内容")
    # R5 変更種別
    _label(t.cell(5, 0), "変更種別")
    c = t.cell(5, 1).merge(t.cell(5, 3))
    _para(c, "{{変更種別}}", first=True)
    _para(c, "※研究期間／研究分担者／研究経費／研究内容のうち該当するものを記入（複数可）", size=7.5, before=2)
    _row_height(t.rows[5], 13)
    # R6-R7 計画概要 (vMerge) / 変更前 / 変更後
    lead = t.cell(6, 0).merge(t.cell(7, 0))
    _label(lead, "計画概要", "（各100字以内）", center=True)
    _label(t.cell(6, 1), "変更前", center=True)
    c = t.cell(6, 2).merge(t.cell(6, 3))
    _para(c, "{{変更前の計画概要}}", first=True)
    _label(t.cell(7, 1), "変更後", center=True)
    c = t.cell(7, 2).merge(t.cell(7, 3))
    _para(c, "{{変更後の計画概要}}", first=True)
    _row_height(t.rows[6], 22)
    _row_height(t.rows[7], 22)
    # R8 変更理由および予算影響 (split marker + bookmark)
    c = t.cell(8, 0).merge(t.cell(8, 1))
    _label(c, "変更理由および予算影響", "（200字以内）", center=True)
    c = t.cell(8, 2).merge(t.cell(8, 3))
    _split_runs(c.paragraphs[0], ["{{変更理由", "および予算影響}}"], bookmark=("1", "reason_field"))
    _row_height(t.rows[8], 40)
    _banner(t, 9, "3．確認事項")
    c = t.cell(10, 0).merge(t.cell(10, 1))
    _label(c, "共同研究先との合意", center=True)
    c = t.cell(10, 2).merge(t.cell(10, 3))
    _para(c, "□ 合意済み　　□ 協議中（協議予定日：　　　年　　月　　日）", first=True, size=9.5)
    _vcenter(c)
    _row_height(t.rows[10], 9)
    c = t.cell(11, 0).merge(t.cell(11, 1))
    _label(c, "研究協力課 受付印", center=True)
    c = t.cell(11, 2).merge(t.cell(11, 3))
    _cell_borders(c, {"left": ("single", 4), "bottom": ("double", 6), "right": ("single", 12)})
    _row_height(t.rows[11], 14)
    _fix_widths(t, widths)

    _para(doc, "（注意事項）", bold=True, gothic=True, size=9.5, before=6, after=1)
    notes = [
        "1　本届出書は、変更予定日の30日前までに研究推進部研究協力課へ提出すること。",
        "2　研究経費の変更額が直接経費総額の20％を超える場合は、別途「経費変更承認申請書（様式第4号）」を併せて提出すること。",
        "3　研究分担者の変更を伴う場合は、新たな研究分担者の承諾書を添付すること。",
        "4　日付は和暦で記入すること。",
        "5　本様式の書式（罫線・欄の配置・注意事項の文言）は変更しないこと。",
    ]
    for n in notes:
        _para(doc, n, size=9, indent_mm=4, hanging_mm=4)
    _para(doc, "届出者　所属：{{所属部局}}　　氏名：{{研究代表者氏名}}", align="right", before=6)
    return _docx_bytes(doc)


def build_form2() -> bytes:
    doc = _new_document("令和8年度 地域中小企業省エネ設備導入補助金 交付申請書（様式第1号）")
    _header_footer(doc, "架空市 産業振興課")
    _para(doc, "様式第1号（第5条関係）", size=9, first=True)
    _para(doc, "[記入欄: 申請日]", align="right", after=4)
    _para(doc, "架空市長　様", after=4)
    widths_a = [34, 70]
    bottom_only = {"top": ("nil", 0), "left": ("nil", 0), "bottom": ("single", 4), "right": ("nil", 0),
                   "insideH": ("single", 4), "insideV": ("nil", 0)}
    a = _table(doc, 4, widths_a, align="right", borders=bottom_only)
    rows = [("所在地", "{{所在地}}"), ("名称", "{{申請事業者名}}"), ("代表者職氏名", "{{代表者職氏名}}"),
            ("法人番号（13桁）", "[記入欄: 法人番号]")]
    for r, (lab, val) in enumerate(rows):
        _para(a.cell(r, 0), lab, first=True, size=9.5, gothic=True)
        _para(a.cell(r, 1), val, first=True)
        _row_height(a.rows[r], 8)
        _vcenter(a.cell(r, 0))
        _vcenter(a.cell(r, 1))
    _para(doc, "令和8年度　架空市地域中小企業省エネ設備導入補助金交付申請書", align="center", size=13.5, bold=True,
          gothic=True, before=10, after=6)
    _para(doc, "　架空市地域中小企業省エネ設備導入補助金交付要綱第5条の規定により、下記のとおり補助金の交付を申請します。",
          align="both", after=2)
    _para(doc, "記", align="center", after=2)

    widths = [38, 44, 40, 48]
    t = _table(doc, 9, widths)
    _banner(t, 0, "1　補助事業の概要")
    _label(t.cell(1, 0), "導入設備名称", "（40字以内）")
    c = t.cell(1, 1).merge(t.cell(1, 3))
    _para(c, "{{導入設備名称}}", first=True)
    _vcenter(c)
    _row_height(t.rows[1], 11)
    _label(t.cell(2, 0), "事業実施期間")
    c = t.cell(2, 1).merge(t.cell(2, 3))
    _para(c, "{{事業実施期間}}", first=True)
    _vcenter(c)
    _row_height(t.rows[2], 9)
    _label(t.cell(3, 0), "年間省エネ見込量", "（kWh/年。算定根拠を別紙に記載）")
    c = t.cell(3, 1).merge(t.cell(3, 3))
    _para(c, "{{年間省エネ見込量}}", first=True)
    _vcenter(c)
    _row_height(t.rows[3], 11)
    _banner(t, 4, "2　経費の内訳（単位：円）")
    heads = ["区分", "導入予定金額（税抜）", "補助率", "補助金申請額（千円未満切捨て・上限1,000,000円）"]
    for i, h in enumerate(heads):
        cell = t.cell(5, i)
        _para(cell, h, first=True, bold=True, gothic=True, size=8.5, align="center")
        _shade(cell, "F2F2F2")
        _vcenter(cell)
    _row_height(t.rows[5], 11)
    _para(t.cell(6, 0), "設備費・工事費", first=True, size=9.5, align="center")
    _para(t.cell(6, 1), "{{導入予定金額_税抜}}", first=True, align="right")
    _para(t.cell(6, 2), "1/3以内", first=True, size=9.5, align="center")
    _para(t.cell(6, 3), "{{補助金申請額}}", first=True, align="right")
    for i in range(4):
        _vcenter(t.cell(6, i))
    _row_height(t.rows[6], 10)
    _banner(t, 7, "3　連絡担当者")
    _label(t.cell(8, 0), "担当者氏名・連絡先")
    c = t.cell(8, 1).merge(t.cell(8, 3))
    _vcenter(c)
    _row_height(t.rows[8], 10)
    _fix_widths(t, widths)

    _para(doc, "（誓約事項）", bold=True, gothic=True, size=9.5, before=6, after=1)
    _para(doc, "申請者は、本申請に当たり、次の事項について誓約します。", size=9)
    pledges = [
        "(1)　本申請書及び添付書類の記載内容は、事実と相違ありません。",
        "(2)　架空市補助金等交付規則に定める交付対象外の者に該当しません。",
        "(3)　同一の設備について、国又は他の地方公共団体の補助金の交付を重複して受けていません。",
        "(4)　補助事業の完了後5年間は、補助対象設備を補助目的以外に使用し、譲渡し、又は廃棄しません。",
        "(5)　市が必要と認めるときは、補助事業に関する調査及び報告に応じます。",
    ]
    for pl in pledges:
        _para(doc, pl, size=9, indent_mm=7, hanging_mm=7)
    _para(doc, "（添付書類）　1 見積書の写し　2 導入設備のカタログ　3 省エネ効果算定書　4 市税の納税証明書", size=9, before=4)
    return _docx_bytes(doc)


def build_form3() -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.worksheet.datavalidation import DataValidation
    from openpyxl.worksheet.page import PageMargins

    wb = Workbook()
    ws = wb.active
    ws.title = "点検報告書"
    widths = {"A": 5.5, "B": 12, "C": 10, "D": 14, "E": 14, "F": 11, "G": 12, "H": 12}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    base = Font(name="游ゴシック", size=10.5)
    bold = Font(name="游ゴシック", size=10.5, bold=True)
    thin, medium = Side(style="thin", color="000000"), Side(style="medium", color="000000")
    label_fill = PatternFill("solid", fgColor="F2F2F2")
    head_fill = PatternFill("solid", fgColor="DDEBF7")

    def put(rng, value=None, font=base, h=None, v="center", wrap=False, fill=None, fmt=None):
        first = rng.split(":")[0]
        if ":" in rng:
            ws.merge_cells(rng)
        cell = ws[first]
        if value is not None:
            cell.value = value
        cell.font = font
        cell.alignment = Alignment(horizontal=h, vertical=v, wrap_text=wrap)
        if fill is not None:
            for row in ws[rng] if ":" in rng else [[cell]]:
                for c in row:
                    c.fill = fill
        if fmt:
            cell.number_format = fmt
        return cell

    def grid(rng, outline=medium):
        rows = ws[rng]
        n_r, n_c = len(rows), len(rows[0])
        for i, row in enumerate(rows):
            for j, c in enumerate(row):
                c.border = Border(left=outline if j == 0 else thin, right=outline if j == n_c - 1 else thin,
                                  top=outline if i == 0 else thin, bottom=outline if i == n_r - 1 else thin)

    put("A1:H1", "月次設備安全点検および改善報告書", Font(name="游ゴシック", size=16, bold=True), h="center")
    put("A2:H2", "様式A-4（架空施設管理規程第12条関係）", Font(name="游ゴシック", size=9), h="right")
    ws.row_dimensions[1].height = 30
    ws.row_dimensions[3].height = 6
    put("A4:C4", "点検対象施設", bold, h="center", fill=label_fill)
    put("D4:H4", "{{点検対象施設}}")
    put("A5:C5", "点検実施日", bold, h="center", fill=label_fill)
    put("D5:E5", "{{点検実施日}}")
    put("F5", "点検責任者", bold, h="center", fill=label_fill)
    put("G5:H5", "{{点検責任者}}")
    grid("A4:H5")
    for r in (4, 5):
        ws.row_dimensions[r].height = 22
    ws.row_dimensions[6].height = 6
    for rng, text in (("A7", "No."), ("B7:C7", "点検項目"), ("D7:E7", "点検内容"), ("F7", "判定"), ("G7:H7", "備考")):
        put(rng, text, bold, h="center", fill=head_fill)
    items = [("空調設備", "運転音・フィルター・冷媒漏れ", "空調設備_判定"),
             ("非常用電源", "始動試験・燃料残量", "非常用電源_判定"),
             ("給排水設備", "漏水・排水の流れ", "給排水設備_判定"),
             ("照明・電気設備", "点灯状態・分電盤", "照明設備_判定"),
             ("防火設備", "防火扉の閉鎖・避難経路", "防火設備_判定")]
    for i, (name, what, slot) in enumerate(items):
        r = 8 + i
        put(f"A{r}", i + 1, h="center")
        put(f"B{r}:C{r}", name)
        put(f"D{r}:E{r}", what, Font(name="游ゴシック", size=9))
        put(f"F{r}", "{{" + slot + "}}", h="center")
        put(f"G{r}:H{r}", None)
        ws.row_dimensions[r].height = 21
    put("A13:B13", "判定集計", bold, h="center", fill=label_fill)
    for col_l, col_v, label in (("C", "D", "良"), ("E", "F", "要注意"), ("G", "H", "要是正")):
        put(f"{col_l}13", label, bold, h="center", fill=label_fill)
        put(f"{col_v}13", f'=COUNTIF($F$8:$F$12,"{label}")', h="center", fmt='0"件"')
    ws.row_dimensions[13].height = 21
    grid("A7:H13")
    ws.row_dimensions[14].height = 6
    put("A15:C17", "指摘事項および是正措置\n（120字以内）", bold, h="center", wrap=True, fill=label_fill)
    put("D15:H17", "{{指摘事項および是正措置}}", h="left", v="top", wrap=True)
    put("A18:C18", "次回点検予定日", bold, h="center", fill=label_fill)
    put("D18:E18", "{{次回点検予定日}}")
    put("F18:H18", None)
    for r in (15, 16, 17):
        ws.row_dimensions[r].height = 24
    ws.row_dimensions[18].height = 22
    grid("A15:H18")
    put("A20:H20", "（記入上の注意）", bold, h="left")
    notes = ["1　判定欄は「良」「要注意」「要是正」のいずれかを記入すること（プルダウンから選択）。",
             "2　「要是正」の項目がある場合は、是正完了後に是正完了報告書（様式A-5）を提出すること。",
             "3　日付は和暦で記入すること。本様式の書式・計算式は変更しないこと。"]
    for i, n in enumerate(notes):
        put(f"A{21 + i}:H{21 + i}", n, Font(name="游ゴシック", size=9), h="left")
    put("A25:C26", "施設管理者確認", bold, h="center", fill=label_fill)
    put("D25:E26", None)
    put("F25:F26", "確認日", bold, h="center", fill=label_fill)
    put("G25:H26", "令和　　年　　月　　日", h="center")
    grid("A25:H26")
    for r in (25, 26):
        ws.row_dimensions[r].height = 18
    put("A28:H28", FICTION_NOTICE_JA, Font(name="游ゴシック", size=8, color="666666"), h="center")

    dv = DataValidation(type="list", formula1='"良,要注意,要是正"', allow_blank=True, showErrorMessage=True,
                        errorTitle="判定", error="「良」「要注意」「要是正」から選択してください。")
    ws.add_data_validation(dv)
    dv.add("F8:F12")
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.orientation = "portrait"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins = PageMargins(left=0.6, right=0.6, top=0.7, bottom=0.7)
    ws.print_area = "A1:H28"
    ws.oddFooter.center.text = "架空サンプル様式（Fictional sample form）"
    wb.properties.creator = "Form-Guard Agent sample generator"
    wb.properties.title = "月次設備安全点検および改善報告書（様式A-4）"
    wb.properties.created = SAMPLE_TIMESTAMP
    wb.properties.modified = SAMPLE_TIMESTAMP
    buf = io.BytesIO()
    wb.save(buf)
    return _finalise_xlsx(buf.getvalue())


def _finalise_xlsx(blob: bytes) -> bytes:
    stamp = SAMPLE_TIMESTAMP.strftime("%Y-%m-%dT%H:%M:%SZ").encode()

    def transform(name: str, data: bytes) -> bytes:
        if name == "docProps/core.xml":
            data = re.sub(rb"(<dcterms:(?:created|modified)[^>]*>)[^<]*(</dcterms:)", rb"\g<1>" + stamp + rb"\g<2>", data)
        return data

    return rebuild_deterministic(blob, transform=transform)


# ============================================================ unmarked blank forms


def build_form4() -> bytes:
    """Unmarked .docx: empty value cells next to printed labels, a duplicated 「氏名」 label, an office-only area."""
    doc = _new_document("施設・備品一時使用許可申請書（マーカーなし白紙様式）")
    _header_footer(doc, "架空学術機関 施設管理課")
    _para(doc, "様式第2号（第6条関係）", size=9, first=True)
    _para(doc, "施設・備品一時使用許可申請書", align="center", size=16, bold=True, gothic=True, before=4, after=6)
    _para(doc, "架空学術機関長　殿", after=4)
    _para(doc, "　架空学術機関施設・備品使用規程第6条の規定により、下記のとおり施設・備品の一時使用の許可を申請します。",
          align="both", after=2)
    _para(doc, "記", align="center", after=2)

    widths = [30, 24, 56, 20, 40]
    t = _table(doc, 15, widths)
    _banner(t, 0, "1．申請者")
    for r, label in ((1, "申請日"), (2, "申請者氏名"), (3, "所属機関")):
        _label(t.cell(r, 0).merge(t.cell(r, 1)), label)
        _vcenter(t.cell(r, 2).merge(t.cell(r, 4)))
        _row_height(t.rows[r], 9)
    _banner(t, 4, "2．使用内容")
    _label(t.cell(5, 0).merge(t.cell(5, 1)), "使用施設・備品")
    c = t.cell(5, 2).merge(t.cell(5, 4))
    _para(c, "共用実験室B・走査型電子顕微鏡（架空機種 FG-100）", first=True, size=9.5)
    _vcenter(c)
    _row_height(t.rows[5], 9)
    _label(t.cell(6, 0).merge(t.cell(6, 1)), "利用目的", "（100字以内）")
    t.cell(6, 2).merge(t.cell(6, 4))
    _row_height(t.rows[6], 20)
    _label(t.cell(7, 0).merge(t.cell(7, 1)), "利用希望期間", "（和暦で記入）")
    _vcenter(t.cell(7, 2).merge(t.cell(7, 4)))
    _row_height(t.rows[7], 10)
    _banner(t, 8, "3．連絡先")
    for r, group in ((9, "研究代表者"), (10, "事務担当者")):
        _label(t.cell(r, 0), group, center=True)
        _label(t.cell(r, 1), "氏名", center=True)
        _vcenter(t.cell(r, 2).merge(t.cell(r, 4)))
        _row_height(t.rows[r], 9)
    _banner(t, 11, "4．事務局使用欄（申請者は記入しないでください）")
    _label(t.cell(12, 0).merge(t.cell(12, 1)), "受付番号（事務局記入欄）")
    _vcenter(t.cell(12, 2))
    _label(t.cell(12, 3).merge(t.cell(12, 4)), "事務局受付欄", center=True)
    _label(t.cell(13, 0).merge(t.cell(13, 1)), "受付日（事務局記入欄）")
    _vcenter(t.cell(13, 2))
    stamp = t.cell(13, 3).merge(t.cell(14, 4))  # stamp area spanning two rows (vMerge)
    _cell_borders(stamp, {"top": ("single", 4), "left": ("single", 4), "bottom": ("double", 6), "right": ("single", 12)})
    _label(t.cell(14, 0).merge(t.cell(14, 1)), "審査欄（事務局記入欄）")
    _vcenter(t.cell(14, 2))
    for r in (12, 13, 14):
        _row_height(t.rows[r], 9)
    _fix_widths(t, widths)

    _para(doc, "（注意事項）", bold=True, gothic=True, size=9.5, before=6, after=1)
    notes = [
        "1　日付は和暦で記入すること。",
        "2　利用目的は100字以内で具体的に記入すること。",
        "3　「4．事務局使用欄」は事務局が記入するため、申請者は記入しないこと。",
        "4　本様式の書式（罫線・欄の配置・注意事項の文言）は変更しないこと。",
    ]
    for n in notes:
        _para(doc, n, size=9, indent_mm=4, hanging_mm=4)
    return _docx_bytes(doc)


def build_form5() -> bytes:
    """Unmarked .xlsx: empty merged value cells, a reference table with a SUM formula, an office-only stamp area."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.worksheet.page import PageMargins

    wb = Workbook()
    ws = wb.active
    ws.title = "貸出・返却管理票"
    for col, w in {"A": 6, "B": 12, "C": 12, "D": 12, "E": 10, "F": 12, "G": 12, "H": 12}.items():
        ws.column_dimensions[col].width = w
    base = Font(name="游ゴシック", size=10.5)
    bold = Font(name="游ゴシック", size=10.5, bold=True)
    small = Font(name="游ゴシック", size=9)
    thin, medium = Side(style="thin", color="000000"), Side(style="medium", color="000000")
    label_fill = PatternFill("solid", fgColor="F2F2F2")
    head_fill = PatternFill("solid", fgColor="DDEBF7")

    def put(rng, value=None, font=base, h=None, v="center", wrap=False, fill=None, fmt=None):
        first = rng.split(":")[0]
        if ":" in rng:
            ws.merge_cells(rng)
        cell = ws[first]
        if value is not None:
            cell.value = value
        cell.font = font
        cell.alignment = Alignment(horizontal=h, vertical=v, wrap_text=wrap)
        if fill is not None:
            for row in ws[rng] if ":" in rng else [[cell]]:
                for c in row:
                    c.fill = fill
        if fmt:
            cell.number_format = fmt
        return cell

    def grid(rng, outline=medium):
        rows = ws[rng]
        n_r, n_c = len(rows), len(rows[0])
        for i, row in enumerate(rows):
            for j, c in enumerate(row):
                c.border = Border(left=outline if j == 0 else thin, right=outline if j == n_c - 1 else thin,
                                  top=outline if i == 0 else thin, bottom=outline if i == n_r - 1 else thin)

    put("A1:H1", "防災備蓄品 貸出・返却管理票", Font(name="游ゴシック", size=16, bold=True), h="center")
    put("A2:H2", "様式B-2（架空市防災備蓄品貸出要領第4条関係）", small, h="right")
    put("A3:H3", "架空市長　様　架空市防災備蓄品貸出要領第4条の規定により、下記のとおり備蓄品の貸出を申請します。", small, h="left")
    ws.row_dimensions[1].height = 30
    put("A4:H4", "1　申請者記入欄", bold, h="left", fill=head_fill)
    for r, label, rng in ((5, "申請日", "C5:H5"), (6, "申請団体名", "C6:H6"), (7, "代表者氏名", "C7:H7")):
        put(f"A{r}:B{r}", label, bold, h="center", fill=label_fill)
        put(rng, None)
        ws.row_dimensions[r].height = 22
    put("A8:B8", "借用希望物品\n（30字以内）", bold, h="center", wrap=True, fill=label_fill)
    put("C8:H8", None, h="left", wrap=True)
    put("A9:B9", "使用予定金額\n（単位：円）", bold, h="center", wrap=True, fill=label_fill)
    put("C9:E9", None, h="right")
    put("F9:H9", "※燃料費・運搬費を含む", small, h="left")
    for r in (8, 9):
        ws.row_dimensions[r].height = 30
    grid("A5:H9")
    put("A11:H11", "2　貸出可能な備蓄品（参考）", bold, h="left", fill=head_fill)
    for rng, text in (("A12", "No."), ("B12:D12", "品目"), ("E12", "在庫数"), ("F12:H12", "保管場所")):
        put(rng, text, bold, h="center", fill=label_fill)
    stock = [("簡易テント", 6, "架空市防災倉庫A"), ("発電機", 3, "架空市防災倉庫B"), ("投光器", 8, "架空市防災倉庫A")]
    for i, (item, qty, place) in enumerate(stock):
        r = 13 + i
        put(f"A{r}", i + 1, h="center")
        put(f"B{r}:D{r}", item)
        put(f"E{r}", qty, h="right", fmt='0"点"')
        put(f"F{r}:H{r}", place, small)
    put("A16:D16", "在庫合計（点）", bold, h="center", fill=label_fill)
    put("E16", "=SUM(E13:E15)", h="right", fmt='0"点"')
    put("F16:H16", "（自動計算）", small, h="left")
    grid("A12:H16")
    put("A17:H17", "3　事務局記入欄（申請者は記入しないこと）", bold, h="left", fill=head_fill)
    put("A18:B19", "※事務局使用欄（決裁印・受付番号・返却確認）", bold, h="center", wrap=True, fill=label_fill)
    put("C18:H19", None)
    for r in (18, 19):
        ws.row_dimensions[r].height = 24
    grid("A18:H19")
    put("A21:H21", "（記入上の注意）", bold, h="left")
    notes = ["1　日付は和暦で記入すること。",
             "2　※印の欄は事務局が記入するため、申請者は記入しないこと。",
             "3　本様式の書式・計算式は変更しないこと。"]
    for i, n in enumerate(notes):
        put(f"A{22 + i}:H{22 + i}", n, small, h="left")
    put("A26:H26", FICTION_NOTICE_JA, Font(name="游ゴシック", size=8, color="666666"), h="center")

    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.orientation = "portrait"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins = PageMargins(left=0.6, right=0.6, top=0.7, bottom=0.7)
    ws.print_area = "A1:H26"
    ws.oddFooter.center.text = "架空サンプル様式（Fictional sample form）"
    wb.properties.creator = "Form-Guard Agent sample generator"
    wb.properties.title = "防災備蓄品 貸出・返却管理票（マーカーなし白紙様式 .xlsx）"
    wb.properties.created = SAMPLE_TIMESTAMP
    wb.properties.modified = SAMPLE_TIMESTAMP
    buf = io.BytesIO()
    wb.save(buf)
    return _finalise_xlsx(buf.getvalue())


# ============================================================ naive "regeneration"

NAIVE_NOTE = "naive-regeneration simulation (scripted to reproduce common failure modes; values intentionally flawed)"


def _naive_doc(title: str):
    """A document as a from-scratch generator would make it: library defaults, US Letter, no form styling."""
    from docx import Document

    doc = Document()
    cp = doc.core_properties
    cp.title = title
    cp.author = "naive-regeneration simulation"
    cp.comments = NAIVE_NOTE
    cp.created = SAMPLE_TIMESTAMP
    cp.modified = SAMPLE_TIMESTAMP
    cp.revision = 1
    return doc


def build_naive_form1() -> bytes:
    doc = _naive_doc("共同研究計画変更届")
    doc.add_heading("共同研究計画変更届", level=1)
    doc.add_paragraph("提出日：2026年11月2日")
    doc.add_paragraph("架空学術大学長 殿")
    doc.add_paragraph("以下の通り、共同研究計画の変更についてお届けいたします。")
    t = doc.add_table(rows=8, cols=2)
    rows = [
        ("項目", "内容"),
        ("研究課題番号", "ＪＲ－２０２６－０４７"),
        ("研究代表者", "架空 太郎 教授（理工学研究科 機械システム専攻）"),
        ("変更種別", "研究内容の変更および研究経費の増額"),
        ("変更前の計画", "小型熱交換器の性能を実機試験12回で評価する。"),
        ("変更後の計画", "実機試験を6回に減らし、数値シミュレーションを追加する。"),
        ("変更理由・予算", "共同研究先の試験設備が改修工事に入るため、実機試験の一部が実施できなくなりました。"
                     "そのため数値シミュレーションで補います。予算については、試験装置の借用料が減る一方で"
                     "計算サーバの利用料が必要となるため、差し引きで約50万円の増額となる見込みです。"
                     "増額分については共同研究先と協議の上で負担することで合意しており、経費変更の承認申請は不要と考えております。"),
        ("所属部局", "理工学研究科"),
    ]
    for r, (a, b) in enumerate(rows):
        t.cell(r, 0).text = a
        t.cell(r, 1).text = b
    doc.add_paragraph("")
    doc.add_paragraph("【注意】変更がある場合は、なるべく早めに研究協力課へ提出してください。"
                      "経費が大きく変わる場合は別の申請が必要になることがあります。")
    return _finalise_docx_naive(doc)


def build_naive_form2() -> bytes:
    doc = _naive_doc("省エネ設備導入補助金 申請書")
    doc.add_heading("省エネ設備導入補助金 申請書", level=1)
    doc.add_paragraph("申請日：2026年11月2日")
    doc.add_paragraph("架空市長 様")
    t = doc.add_table(rows=10, cols=2)
    rows = [
        ("申請事業者名", "株式会社サンプル精工"),
        ("代表者", "代表取締役 模擬 一郎"),
        ("所在地", "架空市中央区架空町1-2-3"),
        ("法人番号", "1234567890123"),
        ("導入設備", "高効率ヒートポンプ空調機 2台"),
        ("導入予定金額", "5,319,600円"),
        ("補助金申請額", "1,612,000円"),
        ("年間省エネ見込量", "年間約30%の電力削減"),
        ("事業実施期間", "2026年12月〜2027年2月"),
        ("連絡担当者", "総務部 架空 次郎"),
    ]
    for r, (a, b) in enumerate(rows):
        t.cell(r, 0).text = a
        t.cell(r, 1).text = b
    doc.add_paragraph("")
    doc.add_paragraph("申請者は、記載内容に誤りがないこと等を誓約します。")
    doc.add_paragraph("添付書類：見積書、カタログなど")
    return _finalise_docx_naive(doc)


def _finalise_docx_naive(doc) -> bytes:
    buf = io.BytesIO()
    doc.save(buf)
    return _finalise_docx(buf.getvalue())


def build_naive_form3() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    rows = [
        ("設備点検報告書", ""),
        ("点検対象施設", "架空市立サンプル技術交流センター"),
        ("点検実施日", "2026/11/02"),
        ("点検責任者", "模擬 三郎"),
        ("空調設備", "NG"),
        ("非常用電源", "OK"),
        ("給排水設備", "注意"),
        ("照明・電気設備", "OK"),
        ("防火設備", "OK"),
        ("判定集計", "OK 3件 / NG 1件"),
        ("指摘事項", "2階東側空調機（AC-2E）で送風ファン軸受の摩耗による異音とフィルターの目詰まりを確認し、"
                 "冷房効率が低下しています。11月6日までにフィルターを交換し、軸受は保守業者が部品を取り寄せのうえ"
                 "11月20日までに交換します。交換完了までは運転時間を短縮し隣接区画の空調で補います。"
                 "給湯室の排水不良は清掃業者に高圧洗浄を依頼済みで、念のため来月も状況を確認します。"),
        ("次回点検予定日", "2026/12/08"),
    ]
    for r, (a, b) in enumerate(rows, start=1):
        ws.cell(row=r, column=1, value=a)
        ws.cell(row=r, column=2, value=b)
    wb.properties.creator = "naive-regeneration simulation"
    wb.properties.created = SAMPLE_TIMESTAMP
    wb.properties.modified = SAMPLE_TIMESTAMP
    buf = io.BytesIO()
    wb.save(buf)
    return _finalise_xlsx(buf.getvalue())


# ===================================================================== entry point

BUILDERS = {
    "form1_research_plan_change.docx": build_form1,
    "form2_municipal_equipment_subsidy.docx": build_form2,
    "form3_facility_safety_inspection.xlsx": build_form3,
}
NAIVE_BUILDERS = {
    "broken_naive_ai_form1.docx": build_naive_form1,
    "broken_naive_ai_form2.docx": build_naive_form2,
    "broken_naive_ai_form3.xlsx": build_naive_form3,
}
UNMARKED_BUILDERS = {  # no slot spec: the cells to fill come from discovery + human confirmation
    "form4_unmarked_blank.docx": build_form4,
    "form5_unmarked_blank.xlsx": build_form5,
}


def generate_samples(samples_dir: Path | str | None = None, with_comparisons: bool = True,
                     overwrite: bool = True) -> dict[str, Path]:
    """Write blank forms, slot specs, memos and naive-regeneration files. Returns written paths.

    ``overwrite=False`` writes only the files that do not exist yet: an existing blank form, slot spec or memo is
    never replaced (``ensure_samples``). ``--generate-samples`` rebuilds everything (``overwrite=True``)."""
    base = Path(samples_dir) if samples_dir else default_samples_dir()
    blank, memos, broken = base / "blank_forms", base / "memos", base / "broken_before"
    for d in (blank, memos, broken):
        d.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    def put(p: Path, make) -> None:
        if overwrite or not p.exists():
            data = make()
            if isinstance(data, bytes):
                p.write_bytes(data)
            else:
                p.write_text(data, encoding="utf-8", newline="\n")
            written[p.name] = p

    for name, fn in BUILDERS.items():
        put(blank / name, fn)
        spec = SPECS[Path(name).stem]
        put(blank / f"{Path(name).stem}.slots.json", lambda spec=spec: json.dumps(spec, ensure_ascii=False, indent=2) + "\n")
    for name, fn in UNMARKED_BUILDERS.items():
        put(blank / name, fn)
    for name, text in MEMOS.items():
        put(memos / name, lambda text=text: text)
    for name, fn in NAIVE_BUILDERS.items():
        put(broken / name, fn)
    if with_comparisons:
        from .report import write_naive_comparisons

        written.update(write_naive_comparisons(base, overwrite=overwrite))
    return written


def ensure_samples(samples_dir: Path | str | None = None) -> Path:
    """Make sure the sample set exists; a missing file is created, nothing that exists is overwritten."""
    base = Path(samples_dir) if samples_dir else default_samples_dir()
    needed = [base / "blank_forms" / d["form"] for d in DEMOS + UNMARKED_FORMS] + \
             [base / "memos" / d["memo"] for d in DEMOS + UNMARKED_FORMS] + \
             [base / "broken_before" / d["naive"] for d in DEMOS]
    if not all(p.exists() for p in needed):
        generate_samples(base, overwrite=False)
    return base


def read_memo(path: Path | str) -> str:
    return Path(path).read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
