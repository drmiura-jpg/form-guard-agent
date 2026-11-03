/* Form-Guard Agent — vanilla JS front-end. User-derived text is always inserted with textContent. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const S = {
  demos: [], unmarked: [], run: null, sel: null, cmp: "naive", layout: "split", checked: new Set(), health: null,
  activeDemo: null, view: "empty", tab: "visual", disc: null, suite: null, suitePromise: null, tour: 0, lang: "ja",
};

/* ------------------------------------------------------------- language */
// Japanese first (all judges read Japanese): primary text in the selected language, the other as a sub-label.

const L = (ja, en) => (S.lang === "en" ? en : ja);

function biKids(ja, en) {
  const [p, s] = S.lang === "en" ? [en, ja] : [ja, en];
  return [h("span", { class: "pri" }, p), h("span", { class: "sub" }, s)];
}

// A bilingual label node; it re-renders itself when the language changes (data-bi).
function B(ja, en, cls) {
  return h("span", { class: cls ? `bi ${cls}` : "bi", dataset: { bi: `${ja}|${en}` } }, ...biKids(ja, en));
}

function applyLang() {
  const root = document.documentElement;
  root.lang = S.lang;
  root.dataset.lang = S.lang;
  const pair = (v) => { const [ja, en] = v.split("|"); return L(ja, en === undefined ? ja : en); };
  $$("[data-bi]").forEach((el) => { const [ja, en] = el.dataset.bi.split("|"); el.replaceChildren(...biKids(ja, en)); });
  $$("[data-t]").forEach((el) => { el.textContent = pair(el.dataset.t); });
  $$("[data-ph]").forEach((el) => { el.placeholder = pair(el.dataset.ph); });
  $$("[data-tip]").forEach((el) => { el.title = pair(el.dataset.tip); });
  $$("#langToggle button").forEach((b) => {
    b.classList.toggle("on", b.dataset.lang === S.lang);
    b.setAttribute("aria-pressed", String(b.dataset.lang === S.lang));
  });
}

function setLang(lang) {
  if (lang !== "ja" && lang !== "en") return;
  S.lang = lang;
  try { localStorage.setItem("fg-lang", lang); } catch (_) { /* private mode */ }
  applyLang();
  rerender();
}

// Flag codes from the server (mapping.normalizations, checklist flags) as [ja, en]; shown with flagText().
const FLAG = {
  wareki: ["和暦に変換", "Japanese era date"], halfwidth: ["半角に統一", "half-width"], yen: ["金額表記を統一", "amount format"],
  condensed: ["抽出要約", "extractive condensation"], re_extracted: ["制約違反→再抽出", "re-extracted under the printed rule"],
  order: ["様式の順に整列", "reordered as printed"], name: ["敬称・注記を除去", "name cleaned"], note: ["注記を除去", "note removed"],
  year_inferred: ["年は推定", "year inferred from context"], weekday_ok: ["曜日一致", "weekday verified"],
  human: ["人が入力", "entered by the reviewer"], re_anchored: ["引用を付け替え", "quote re-anchored — check the wording"],
  low_conf: ["低信頼度", "low confidence"], needs_input: ["要入力", "needs input"], has_suggestion: ["候補あり", "has a suggestion"],
  unsafe_condensation: ["要約で条件が欠ける", "condensing would drop a condition"], weekday: ["曜日を付記", "weekday added"],
  recovered_unverified: ["根拠未検証", "not verified"], advance_notice: ["提出期限を確認", "check the printed deadline"],
};
const flagText = (f) => (FLAG[f] ? L(...FLAG[f]) : f);
const ATTENTION = new Set(["year_inferred", "low_conf", "needs_input", "re_extracted", "condensed", "unsafe_condensation", "re_anchored", "advance_notice"]);
const FORMAT = {
  text: ["文章", "text"], person_name: ["氏名", "person name"], wareki_date: ["和暦日付", "Japanese era date"],
  wareki_period: ["和暦期間", "Japanese era period"], date: ["日付", "date"], yen: ["金額（円）", "yen amount"],
  digits: ["数字", "digits"], pattern: ["書式（ID）", "ID pattern"], choice: ["選択肢", "choice"],
};
const fmtLabel = (f) => { const x = FORMAT[f || "text"]; return x ? L(x[0], x[1]) : String(f); };
const fmtJa = (f) => (FORMAT[f] || [f])[0];

// Reasons come from the server as fixed English phrases (they are also in the audit log). In Japanese mode the UI
// shows a translation; the original text stays in the tooltip.
const REASON_JA = [
  [/^applicant field — empty cell next to a printed label$/, () => "申請者欄 — 印字ラベルの隣の空欄"],
  [/^applicant field — printed era-date template next to a printed label; the template is replaced by the date$/,
    () => "申請者欄 — 印字ラベルの隣の「令和　年　月　日」欄（印刷された部分を日付で置き換え）"],
  [/^office\/administrative use only — excluded from applicant fill(.*)$/, (m) => `事務局専用 — 申請者の記入対象から除外${m[1]}`],
  [/^not an input field \(Claude: (.*)\)$/, (m) => `入力欄ではない（Claude: ${m[1]}）`],
  [/^vertically merged continuation \(vMerge\)/, () => "縦に結合したセルの続き（vMerge）— 結合の先頭セルだけが記入対象"],
  [/^merged continuation of (\S+) — only the top-left cell (\S+) can hold a value$/, (m) => `結合範囲 ${m[1]} の続き — 値を入れられるのは左上の ${m[2]} だけ`],
  [/^formula cell \((.*)\) — formulas are never fill targets$/, (m) => `計算式のセル（${m[1]}）— 計算式は記入対象にしない`],
  [/^the cell already holds content$/, () => "既に内容が入っているセル"],
  [/^the cell holds a field, content control or object/, () => "フィールド・コンテンツコントロール・図を含むセル — 記入対象にしない"],
  [/^the worksheet has rows\/cells without an r attribute/, () => "r 属性の無い行・セルがあり、位置を安全に特定できない"],
];
function reasonText(r) {
  if (S.lang !== "ja" || !r) return r || "";
  for (const [re, fn] of REASON_JA) { const m = re.exec(r); if (m) return fn(m); }
  return r;
}
const ROUTE_JA = [
  [/structured field \((\w+)\)/g, (_, f) => `定型の欄（${fmtJa(f)}）`],
  [/standard (\w+) field/g, (_, f) => `通常の欄（${fmtJa(f)}）`],
  [/long text: memo passage of (\d+) chars for a (\d+)-char field \(condensation\)/g, (_, a, b) => `長文：メモの該当箇所 ${a} 字を ${b} 字の欄へ要約`],
  [/long free-text field \((\d+)-char limit\)/g, (_, a) => `長い自由記述欄（${a} 字以内）`],
  [/model tier '(\w+)'/g, (_, t) => `モデル固定（${t}）`],
  [/model set explicitly/g, () => "モデルを明示指定"],
  [/; then self-correction after validator rejection/g, () => "；検証器に差し戻された後に自己修正"],
  [/ \(served by ([^;\s]+); requested ([^)\s]+)\)/g, (_, s, q) => `（応答モデル ${s}／要求 ${q}）`],
  [/entered by the human reviewer — no model involved/g, () => "確認者が入力（モデルは関与なし）"],
  [/this engine has no per-field model routing/g, () => "このエンジンには欄ごとのモデル割り当てがありません"],
  [/no model: .*/g, () => "モデルなし：記入済みファイルから読み戻した値を検証"],
];
const routeReasonText = (r) => (S.lang !== "ja" || !r ? r || "" : ROUTE_JA.reduce((s, [re, fn]) => s.replace(re, fn), r));

// The verifier's check descriptions and details are fixed English phrases (also in the audit log and the report).
// In Japanese mode the UI shows a translation; the English original stays in the tooltip.
const CHECK_JA = {
  "Archive opens and every entry passes its CRC-32 check": "アーカイブが開け、全エントリが CRC-32 検査に合格",
  "Same ZIP entries in the same order": "ZIP のエントリ構成と順序が同一",
  "ZipInfo metadata identical (timestamps, flags, method, attributes, extra)": "ZIP エントリのメタデータが同一（日時・フラグ・圧縮方式・属性・拡張領域）",
  "Every non-target entry is byte-for-byte identical (raw compressed stream + SHA-256 content)": "記入対象外のエントリはすべてバイト単位で同一（圧縮データ＋内容の SHA-256）",
  "Non-target parts byte-identical": "記入対象外のパーツがバイト単位で同一",
  "Only parts that contain designated slots were modified": "変更したのは指定欄を含むパーツだけ",
  "Modified XML parts are well-formed (no DTD, no entity tricks)": "変更した XML パーツが整形式（DTD・実体参照の細工なし）",
  "Namespace declarations preserved exactly": "名前空間宣言をそのまま保持",
  "Markup skeleton byte-identical outside slot text (every tag, attribute, property)": "指定欄の文字以外の XML 骨格がバイト単位で同一（全タグ・属性・書式）",
  "Table topology identical (tables, rows, cells, gridSpan, vMerge, tblGrid)": "表の構造が同一（表・行・セル・横結合・縦結合・列定義）",
  "Table topology identical": "表の構造が同一",
  "Sheet topology identical (sheets, merged ranges)": "シートの構造が同一（シート・結合範囲）",
  "Merged ranges identical": "結合範囲が同一",
  "Borders, shading and cell properties identical (tblBorders, tcBorders, shd, tcW)": "罫線・網掛け・セル書式が同一（表とセルの罫線・網掛け・セル幅）",
  "Borders and shading identical": "罫線・網掛けが同一",
  "Borders, fills, fonts and number formats identical for every cell": "全セルの罫線・塗りつぶし・フォント・表示形式が同一",
  "Borders and cell styles identical": "罫線・セルのスタイルが同一",
  "Paragraph and run formatting identical (pPr / rPr)": "段落・文字の書式が同一（pPr / rPr）",
  "Column widths and row heights identical": "列幅・行の高さが同一",
  "Page setup identical (size, margins, header/footer references)": "ページ設定が同一（用紙サイズ・余白・ヘッダー／フッター）",
  "Page setup identical": "ページ設定が同一",
  "Formulas, drop-down validations and print setup identical": "計算式・ドロップダウンの入力規則・印刷設定が同一",
  "Formulas and drop-down validations identical": "計算式・ドロップダウンの入力規則が同一",
  "All boilerplate text unchanged (notes, legal clauses, labels)": "定型文がすべて無変更（注意事項・誓約文・項目名）",
  "All boilerplate text unchanged": "定型文がすべて無変更",
  "Each slot contains exactly its approved value (text around markers intact)": "各欄に承認対象の値だけが入っている（マーカー前後の文字は無変更）",
  "No unreplaced markers except fields awaiting human input": "人の入力待ちの欄以外にマーカーが残っていない",
  "Every AI-filled value is backed by verbatim quotes found in the memo": "AI が記入した値はすべて、メモにそのまま存在する引用で裏付けられている",
  "Numbers and dates in values appear in the quoted evidence": "値に含まれる数値・日付が引用の中にある",
  "No numbers that are absent from both the memo and the blank form": "メモにも白紙様式にも無い数値が入っていない",
  "Character limits printed on the form are respected": "様式に印刷された字数制限を守っている",
  "Formats (Japanese era dates, yen amounts, ID patterns) are valid": "書式（和暦の日付・円金額・番号の形式）が正しい",
  "Form constraints respected (best-effort label matching)": "様式の記入ルールを守っている（項目名から推定して照合）",
  "Options, caps and rounding rules are respected": "選択肢・上限額・端数処理のルールを守っている",
  "File opens in an independent OOXML library": "別の OOXML ライブラリでファイルを開ける",
  "Rendered layout outside the designated fields is unchanged and every value fits its cell": "指定欄以外の描画レイアウトが無変更で、すべての値が欄に収まっている",
};
const VIS_JA = [
  [/^(\d+) value\(s\) overflow their cell \((.*)\)$/, (m) => `${m[1]} 個の値がセルからはみ出し（${m[2]}）`],
  [/^(\d+) boxes moved \(max ([\d.]+)px\)$/, (m) => `${m[1]} 個の枠が移動（最大 ${m[2]}px）`],
  [/^pages (\d+)→(\d+)$/, (m) => `ページ数 ${m[1]}→${m[2]}`],
];
const visJa = (s) => s.split(" · ").map((p) => { for (const [re, fn] of VIS_JA) { const m = re.exec(p); if (m) return fn(m); } return p; }).join("・");
const DETAIL_JA = [
  [/^all CRCs valid$/, () => "全エントリの CRC が正常"],
  [/^(\d+) entries, identical order$/, (m) => `${m[1]} エントリ・順序も同一`],
  [/^identical for all entries$/, () => "全エントリで同一"],
  [/^(\d+) entries copied verbatim incl\. (.+)$/, (m) => `${m[1]} エントリをそのまま複製（${m[2]} など）`],
  [/^changed: (.+)$/, (m) => `変更あり: ${m[1]}`],
  [/^modified: (.+)$/, (m) => `変更したパーツ: ${m[1]}`],
  [/^(\d+) part\(s\) parsed$/, (m) => `${m[1]} パーツを解析`],
  [/^identical prefixes and URIs$/, () => "接頭辞と URI が同一"],
  [/^(\d+) tags and (\d+) text nodes compared \(xml:space and text of every non-slot node included\); only slot characters differ(?:; (\d+) designated empty cell\(s\) hold plain text only)?$/,
    (m) => `${m[1]} タグ・${m[2]} テキストノードを比較（指定欄以外の xml:space と文字も対象）— 違いは指定欄の文字だけ${m[3] ? `；座標指定の空欄 ${m[3]} 個は書式なしの文字だけ` : ""}`],
  [/^(\d+) tables, (\d+) rows, (\d+) cells, (\d+) merged cells — identical$/, (m) => `表 ${m[1]}・行 ${m[2]}・セル ${m[3]}・結合セル ${m[4]} — 同一`],
  [/^cells (\d+)→(\d+), merged cells (\d+)→(\d+)$/, (m) => `セル ${m[1]}→${m[2]}、結合セル ${m[3]}→${m[4]}`],
  [/^(\d+) border definitions and (\d+) shading fills — identical$/, (m) => `罫線定義 ${m[1]}・網掛け ${m[2]} — 同一`],
  [/^border definitions (\d+)→(\d+), shading (\d+)→(\d+)$/, (m) => `罫線定義 ${m[1]}→${m[2]}、網掛け ${m[3]}→${m[4]}`],
  [/^(\d+) cell border edges and all cell styles — identical(?:; style id \(s\) of (\d+) designated cell\(s\) kept)?$/,
    (m) => `セル罫線 ${m[1]} 辺と全セルのスタイル — 同一${m[2] ? `；記入した ${m[2]} セルのスタイル番号も維持` : ""}`],
  [/^border edges (\d+)→(\d+)$/, (m) => `セル罫線 ${m[1]}→${m[2]} 辺`],
  [/^(\d+) sheet\(s\), (\d+) merged ranges — identical$/, (m) => `シート ${m[1]}・結合範囲 ${m[2]} — 同一`],
  [/^merged ranges (\d+)→(\d+)$/, (m) => `結合範囲 ${m[1]}→${m[2]}`],
  [/^(\d+) paragraphs checked(?:; (\d+) designated empty cell\(s\) formatted exactly like the blank cell \(pPr, paragraph-mark rPr\))?$/,
    (m) => `段落 ${m[1]} 個を確認${m[2] ? `；座標指定の空欄 ${m[2]} 個は白紙のセルと同じ書式` : ""}`],
  [/^identical$/, () => "同一"],
  [/^page (\(.*\)) twips — identical$/, (m) => `用紙 ${m[1]} twips — 同一`],
  [/^page (\(.*\)) → (\(.*\)) twips$/, (m) => `用紙 ${m[1]} → ${m[2]} twips`],
  [/^(\d+) formulas, (\d+) data validations, print area — identical$/, (m) => `計算式 ${m[1]}・入力規則 ${m[2]}・印刷範囲 — 同一`],
  [/^formulas (\d+)→(\d+), data validations (\d+)→(\d+)$/, (m) => `計算式 ${m[1]}→${m[2]}、入力規則 ${m[3]}→${m[4]}`],
  [/^(\d+) non-slot text blocks identical$/, (m) => `指定欄以外の文字ブロック ${m[1]} 個 — 同一`],
  [/^(\d+) printed text blocks altered or missing$/, (m) => `印刷された文字ブロック ${m[1]} 個が変更・欠落`],
  [/^all present$/, () => "すべて残っている"],
  [/^(\d+) slot text blocks match the approved values$/, (m) => `指定欄 ${m[1]} 個の文字が承認対象の値と一致`],
  [/^no markers left$/, () => "マーカーの残りなし"],
  [/^pending human input: (.+)$/, (m) => `人の入力待ち: ${m[1]}`],
  [/^(\d+) values grounded(?:; (\d+) human-entered \(attested by reviewer\))?(?:; (\d+) recovered without provenance \(not verified\))?$/,
    (m) => `${m[1]} 件の値に原文の根拠あり${m[2] ? `；確認者の入力 ${m[2]} 件` : ""}${m[3] ? `；出所不明 ${m[3]} 件（未検証）` : ""}`],
  [/^all numbers\/dates supported$/, () => "数値・日付はすべて引用で裏付け"],
  [/^all within limits$/, () => "すべて字数制限内"],
  [/^all formats valid$/, () => "書式はすべて正しい"],
  [/^all options \/ caps \/ rounding valid$/, () => "選択肢・上限・端数処理はすべて正しい"],
  [/^python-docx opened it: (\d+) paragraphs, (\d+) tables$/, (m) => `python-docx で開けた: 段落 ${m[1]}・表 ${m[2]}`],
  [/^openpyxl opened it: sheets (\[.*?\])(?:; (\d+) slot cell\(s\) read back as the approved text)?$/,
    (m) => `openpyxl で開けた: シート ${m[1]}${m[2] ? `；指定セル ${m[2]} 個を読み戻して承認対象の値と一致` : ""}`],
  [/^untraceable numbers: (.+)$/, (m) => `出所の無い数値: ${m[1]}`],
  [/^none(?: found)?$/, () => "なし"],
  [/^(\d+) violations$/, (m) => `違反 ${m[1]} 件`],
  [/^(.*?) — caused by .* \/ (.+)$/, (m) => `${visJa(m[1])} — ${m[2]}`],  // the Japanese half follows the last " / "
  [/^(.*) — a value is probably too long for its field$/, (m) => `${visJa(m[1])} — 欄に収まらない値がある可能性`],
];
const checkText = (c) => (S.lang === "ja" && CHECK_JA[c.description]) || c.description;
function detailText(d) {
  if (S.lang !== "ja" || !d) return d || "";
  for (const [re, fn] of DETAIL_JA) { const m = re.exec(d); if (m) return fn(m); }
  return d;
}
const ENGINE_JA = [
  [/^Offline demo engine \(deterministic rules, no API key\)/, () => "オフラインエンジン（決定論的ルール・API キー不要）"],
  [/^deterministic rules \(offline\)$/, () => "決定論的ルール（オフライン）"],
  [/ — live call failed, used offline engine$/, () => "（ライブ呼び出しに失敗したためオフラインエンジンを使用）"],
];
const engineText = (l) => (S.lang !== "ja" || !l ? l || "" : ENGINE_JA.reduce((s, [re, fn]) => s.replace(re, fn), l));
const STATUS_TEXT = { PASS: ["合格", "PASS"], NEEDS_HUMAN_INPUT: ["人の入力待ち", "NEEDS_HUMAN_INPUT"], FAIL: ["不合格", "FAIL"] };
const statusText = (s) => (STATUS_TEXT[s] ? L(...STATUS_TEXT[s]) : s);

// Python counts characters as Unicode code points; JS .length counts UTF-16 units (𠮷 or 🧪 = 2).
// Every offset / length that comes from the server is therefore applied to Array.from(text).
const cpLength = (s) => Array.from(s || "").length;
const GROUPS = [
  ["zip", "ZIP package", "パッケージ"], ["xml", "XML", "XML構造"], ["layout", "Layout", "罫線・結合・書式"],
  ["text", "Text", "定型文"], ["grounding", "Grounding", "根拠"], ["constraints", "Constraints", "記入ルール"],
  ["application", "Application", "読込確認"], ["visual", "Visual", "見た目"],
];
const TABS = ["visual", "fields", "audit", "hitl", "claude"];
const JSON_HEADERS = { "Content-Type": "application/json" };

class ApiError extends Error {
  constructor(status, body) {
    const d = body && body.detail;
    super(typeof d === "string" ? d : (d && d.message) || (Array.isArray(d) && d[0] && d[0].msg) || `HTTP ${status}`);
    this.status = status; this.body = body;
  }
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch (_) { /* non-JSON */ }
  if (!res.ok) throw new ApiError(res.status, body);
  return body;
}

// replaceChildren() would print null/false as text — filter them out.
function fill(el, ...kids) {
  el.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));
  return el;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 4800);
}

function busy(on, step) {
  $("#busy").hidden = !on;
  if (step) $("#busyStep").textContent = step;
}

const mode = () => $("#modeSelect").value;
const tier = () => $("#tierSelect").value;
const PIPELINE_STEPS = () => L("スキャン → 対応付け・自己修正 → 外科的な書き込み → 構造検査 → 見た目の比較",
  "Scan → Map & self-correct → Surgical write → Verify → Visual diff");

/* ------------------------------------------------------------------ init */

document.addEventListener("DOMContentLoaded", async () => {
  const params = new URLSearchParams(location.hash.slice(1));
  let saved = null;
  try { saved = localStorage.getItem("fg-lang"); } catch (_) { /* private mode */ }
  S.lang = ["ja", "en"].includes(params.get("lang")) ? params.get("lang") : (saved === "en" ? "en" : "ja");
  applyLang();
  const theme = localStorage.getItem("fg-theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.documentElement.dataset.theme = theme;
  $("#themeToggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("fg-theme", next);
  });
  $$("#langToggle button").forEach((b) => b.addEventListener("click", () => setLang(b.dataset.lang)));
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  $$("#cmpMode button").forEach((b) => b.addEventListener("click", () => { S.cmp = b.dataset.v; renderVisual(); }));
  $$("#cmpLayout button").forEach((b) => b.addEventListener("click", () => { S.layout = b.dataset.v; renderVisual(); }));
  $$(".tour-step").forEach((b) => b.addEventListener("click", () => startTour(Number(b.dataset.tour))));
  $("#tourStart").addEventListener("click", () => startTour(1));
  $("#suiteBtn").addEventListener("click", openSuite);
  $("#suiteBtn2").addEventListener("click", openSuite);
  $("#backToDisc").addEventListener("click", () => { showView("discovery"); renderDiscovery(); });
  $("#sliderRange").addEventListener("input", (e) => $("#sliderStage").style.setProperty("--pos", `${e.target.value}%`));
  bindSliderDrag();
  $("#uploadForm").addEventListener("submit", onUpload);
  $("#discoverBtn").addEventListener("click", onDiscoverUpload);
  $("#fileInput").addEventListener("change", (e) => {
    const f = e.target.files[0];
    $("#fileName").textContent = f ? `${f.name} (${Math.round(f.size / 1024)} KB)` : "";
    $("#fileName").hidden = !f;
    $("#fileHint").hidden = Boolean(f);
  });
  $("#discMemo").addEventListener("input", (e) => { if (S.disc) S.disc.memo = e.target.value; });
  $("#candRecommended").addEventListener("click", () => {
    if (!S.disc) return;
    S.disc.checked = new Set(S.disc.d.candidates.filter((c) => c.recommended && !c.office_only).map((c) => c.candidate_id));
    renderDiscovery();
  });
  $("#candNone").addEventListener("click", () => { if (S.disc) { S.disc.checked = new Set(); renderDiscovery(); } });
  $("#fillConfirmed").addEventListener("click", onFillConfirmed);
  $("#checkAll").addEventListener("click", () => {
    $$("#checklist input[type=checkbox]:not(:disabled)").forEach((c) => { c.checked = true; S.checked.add(c.value); });
    updateApprove();
  });
  $("#reviewerInput").addEventListener("input", updateApprove);
  $("#approveBtn").addEventListener("click", onApprove);
  try {
    S.health = await api("/api/health");
    $("#modeSelect").value = "auto";
    if (S.health.model_tiers.includes(S.health.default_model_tier)) $("#tierSelect").value = S.health.default_model_tier;
    const m = S.health.models || {};
    $$("#tierSelect option").forEach((o) => { if (m[o.value]) o.title = m[o.value]; });
    renderEngineBadge();
  } catch (e) { toast(L(`ヘルスチェックに失敗: ${e.message}`, `Health check failed: ${e.message}`)); }
  try {
    const all = await api("/api/demos");  // デモ1–5: marked forms and unmarked blank forms (blank-cell discovery)
    S.demos = all.filter((d) => !d.unmarked);
    S.unmarked = all.filter((d) => d.unmarked);
    renderDemos();
  } catch (e) { toast(L(`デモを読み込めません: ${e.message}`, `Could not load demos: ${e.message}`)); }
  if (!(await openRunFromQuery())) await handleDeepLink();
  window.addEventListener("hashchange", handleDeepLink);  // editing the hash while recording also works
});

// /?run_id=<run id>: an unapproved draft written by form_guard_fill (MCP, its review_url). GET /api/runs/<run id>
// imports it on first use — the exact file, re-verified on the server against its blank form, no model run — so
// the reviewer inspects, edits, approves and downloads that very file here.
async function openRunFromQuery() {
  const id = new URLSearchParams(location.search).get("run_id");
  if (!id) return false;
  if (!/^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$/.test(id)) {
    toast(L(`run_id の形式が正しくありません: ${id}`, `Not a run id: ${id}`));
    return true;
  }
  busy(true, L("MCP の下書きを読み込み、サーバーで再検査しています…", "Loading the MCP draft and re-verifying it on the server…"));
  try {
    setRun(await api(`/api/runs/${encodeURIComponent(id)}`), true);
    showTab("hitl");
    toast(L("MCP の下書きを読み込みました（未承認）。確認して承認してください。", "MCP draft loaded (unapproved) — review it and sign off."));
  } catch (e) {
    toast(L(`下書き ${id} を読み込めません: ${e.message}`, `Could not load draft ${id}: ${e.message}`));
  } finally { busy(false); }
  return true;
}

// Deep links for demos / recordings: /#demo=form2&tab=hitl&slot=法人番号&layout=slider, #unmarked=form4,
// #tour=3, #view=claude, #lang=en
async function handleDeepLink() {
  const params = new URLSearchParams(location.hash.slice(1));
  if (["ja", "en"].includes(params.get("lang")) && params.get("lang") !== S.lang) setLang(params.get("lang"));
  if (params.get("layout") === "slider") S.layout = "slider";
  if (params.get("tour")) {
    await startTour(Number(params.get("tour")));
  } else if (params.get("demo") && S.demos.some((d) => d.id === params.get("demo"))) {
    await runDemo(params.get("demo"));
    if (params.get("slot")) selectSlot(params.get("slot"), true);
    if (params.get("tab")) showTab(params.get("tab"));
  } else if ((params.get("unmarked") || params.get("demo")) &&
             S.unmarked.some((d) => d.id === (params.get("unmarked") || params.get("demo")))) {
    await discoverDemo(params.get("unmarked") || params.get("demo"));
  } else if (params.get("view") === "claude") {
    await openSuite();
  }
}

/* ---------------------------------------------------------------- views */

function showView(name) {
  S.view = name;
  const ids = { empty: "emptyState", discovery: "discoveryView", suite: "suiteView", run: "runView" };
  Object.entries(ids).forEach(([k, id]) => { $(`#${id}`).hidden = k !== name; });
  if (name === "run" && $("#whyBanner")) $("#whyBanner").open = false;  // keep the review desk compact
  renderEngineBadge();
}

function rerender() {
  renderDemos();
  renderEngineBadge();
  renderTourBar();
  if (S.view === "discovery") renderDiscovery();
  if (S.view === "suite") renderSuiteInto($("#suiteStandalone"), null);
  if (S.view === "run" && S.run) {
    renderRun();
    if (S.sel) selectSlot(S.sel, false);
  }
}

// A live run that failed after the API had answered: the values come from the offline engine, but calls were made
// (and billed) — the usage of those calls, or null.
function liveFailure(run) {
  const eng = (run && run.engine) || {};
  const u = eng.usage || {};
  return eng.mode !== "live" && (u.live_attempt_failed || (eng.fallback_reason && u.requests > 0)) ? u : null;
}

function renderEngineBadge() {
  const b = $("#engineBadge");
  const eng = S.view === "run" && S.run ? S.run.engine : null;
  if (eng) {
    const live = eng.mode === "live";
    const failed = liveFailure(S.run);
    b.textContent = live ? `Claude · ${eng.model === "auto" ? L("自動モデル振り分け", "auto model routing") : eng.model}`
      : failed ? L(`オフラインエンジン（ライブ API ${failed.requests} 回の後に失敗）`, `Offline engine (live run failed after ${failed.requests} API calls)`)
        : L("オフラインエンジン（API 呼び出しなし）", "Offline demo engine (no API call)");
    b.className = `pill ${live ? "pill-live" : "pill-neutral"}`;
  } else if (S.health) {
    b.textContent = S.health.live_available ? L(`Claude 利用可 · ${S.health.default_model}`, `Claude ready · ${S.health.default_model}`)
      : L("オフライン（API キーなし）", "Offline demo engine (no API key)");
    b.className = `pill ${S.health.live_available ? "pill-live" : "pill-neutral"}`;
    const live = $("#modeSelect option[value=live]");
    if (!S.health.live_available) live.textContent = L("ライブ Claude（要 API キー）", "Live Claude (needs an API key)");
  }
}

function showTab(name) {
  if (!TABS.includes(name)) return;
  S.tab = name;
  $$(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  TABS.forEach((t) => { $(`#tab-${t}`).hidden = t !== name; });
  if (name === "claude") renderClaudeTab();
}

/* ---------------------------------------------------------- guided tour */
// Five proof points for a judge, each one click away (also #tour=N for recordings).

const TOUR = [
  {
    ja: "左＝生成AIで様式ごと作り直した版（罫線・結合セルの消失、定型文の言い換え、メモに無い数値）。右＝Form-Guard（指定欄以外のずれ 0 px）。スライダーを左右に動かして重ねて比べてください。",
    en: "Left: a naive from-scratch regeneration (borders and merged cells lost, legal text paraphrased, numbers the memo never stated). Right: Form-Guard — 0 px drift outside the designated fields. Drag the slider to compare.",
    async go() {
      if (!(S.run && S.run.previews && S.run.previews.naive)) await runDemo("form1");
      if (!S.run || !S.run.previews.naive) return;
      S.cmp = "naive"; S.layout = "slider";
      showView("run"); showTab("visual"); renderVisual();
    },
    links: () => [[L("左右に並べて比較", "Side by side"), () => { S.layout = "split"; renderVisual(); }]],
  },
  {
    ja: "マーカーも設定ファイルも無い白紙様式から、印字ラベルの横の空欄を自動で発見。申請者欄（✓）は推奨、事務局専用欄（⊘）はロックされ選べません。計算式・結合セルの続きは除外し、確定した欄だけに記入します。",
    en: "From a blank form with no markers and no slot file, the empty cells next to printed labels are discovered. Applicant cells (✓) are recommended, office-only cells (⊘) are locked, formulas and merged continuations are rejected — and only confirmed cells are filled.",
    async go() { await discoverDemo("form4"); },
    links: () => [[L("Excel 版（form5）も見る", "Try the Excel form (form5)"), () => discoverDemo("form5")]],
  },
  {
    ja: "黄色の原文「483万6千円」から「4,836,000円」へ。補助金申請額は様式に印刷された上限で 1,612,000円 → 1,000,000円 に再抽出。メモに無い法人番号は推測せず、人の入力待ちです。",
    en: "From the highlighted memo text 「483万6千円」 to 4,836,000円. The subsidy amount is re-extracted under the printed cap (1,612,000円 → 1,000,000円). The 法人番号 the memo never states is not guessed — it waits for a person.",
    async go() {
      if (!(S.run && S.run.demo_id === "form2" && S.run.revision === 0)) await runDemo("form2");
      if (!S.run || S.run.demo_id !== "form2") return;
      showView("run"); showTab("fields"); selectSlot("導入予定金額_税抜", true);
    },
    links: () => [
      [L("補助金申請額（上限で再抽出）", "Subsidy amount (printed cap)"), () => { showTab("fields"); selectSlot("補助金申請額", true); }],
      [L("法人番号（人の入力待ち）", "法人番号 (awaiting a person)"), () => showTab("hitl")],
    ],
  },
  {
    ja: "欄ごとに Haiku（定型の日付・ID・選択肢）／Sonnet（文章・金額・氏名）／Opus（長文の要約・差し戻し後の自己修正）を割り当てます。開発では Skills・サブエージェント・Hook・MCP が連携します。",
    en: "Each field goes to Haiku (structured dates, IDs, choices), Sonnet (text, amounts, names) or Opus (long-text condensation, self-correction). In development, Skills, subagents, a hook and an MCP server work together.",
    async go() {
      if (!(S.run && (S.run.engine.mode === "live" || S.run.demo_id === "form1"))) await runDemo("form1");
      if (!S.run) return;
      showView("run"); showTab("claude");
    },
    links: () => [[L("「記入値と根拠」で欄ごとのモデルを見る", "Per-field model in Fields & sources"), () => showTab("fields")]],
  },
  {
    ja: "21項目の決定論的検査、ZIP 内パーツごとの SHA-256、実際に書き換えたバイト範囲。承認は画面上の版と SHA-256 に結び付き、人が全項目を確認するまでダウンロードはサーバー側でロックされます。",
    en: "21 deterministic checks, per-part SHA-256 and the exact byte ranges rewritten. Approval is bound to the revision on screen and the SHA-256; the download stays locked on the server until a person confirms every item.",
    async go() {
      if (!S.run) await runDemo("form1");
      if (!S.run) return;
      showView("run"); showTab("audit");
    },
    links: () => [[L("人による最終確認を開く", "Open Human approval"), () => showTab("hitl")]],
  },
];

async function startTour(n) {
  if (!(n >= 1 && n <= TOUR.length)) return;
  S.tour = n;
  renderTourBar();
  await TOUR[n - 1].go();
  renderTourBar();
}

function renderTourBar() {
  $$(".tour-step").forEach((b) => b.classList.toggle("on", Number(b.dataset.tour) === S.tour));
  const cap = $("#tourCaption");
  if (!S.tour) { cap.hidden = true; return; }
  const step = TOUR[S.tour - 1];
  cap.hidden = false;
  fill(cap,
    h("b", { class: "tour-num" }, "①②③④⑤"[S.tour - 1]),
    h("span", { class: "tour-text" }, L(step.ja, step.en)),
    h("span", { class: "tour-links" },
      step.links().map(([label, fn]) => h("button", { class: "linkish", type: "button", onclick: fn }, `${label} →`)),
      S.tour < TOUR.length ? h("button", { class: "btn btn-primary btn-sm", type: "button", onclick: () => startTour(S.tour + 1) }, L("次へ ▶", "Next ▶")) : null,
      h("button", { class: "icon-btn icon-sm", type: "button", "aria-label": L("ガイドを閉じる", "Close the walkthrough"),
        onclick: () => { S.tour = 0; renderTourBar(); } }, "✕")));
}

/* ---------------------------------------------------------------- demos */

function demoButton(d, onclick, unmarked) {
  return h("button", { class: `demo${unmarked ? " demo-unmarked" : ""}${S.activeDemo === d.id ? " active" : ""}`, type: "button", onclick },
    h("div", { class: "demo-top" }, d.label ? h("span", { class: "demo-no" }, L(d.label, `Demo ${d.number}`)) : null,
      h("span", { class: "ftype" }, `.${d.file_type}`), h("span", { class: "demo-title" }, d.title),
      unmarked ? h("span", { class: "badge-auto", title: L("マーカーの無い白紙様式。空欄を自動検出し、人が確定した欄だけに記入します",
        "No markers: blank cells are discovered, and only the cells you confirm are filled") }, L("空欄自動検出", "auto-detect")) : null),
    h("div", { class: "demo-org" }, `${d.org} · ${d.title_en}`),
    unmarked ? h("div", { class: "demo-cta" }, "🔍 ", B("空欄を自動発見 → 人が確定 → 記入", "discover → you confirm → fill")) : null,
    h("div", { class: "chips" }, (d.highlights || []).map((x) => h("span", { class: "chip" }, x))));
}

function renderDemos() {
  $("#demoList").replaceChildren(...S.demos.map((d) => demoButton(d, () => runDemo(d.id), false)));
  $("#unmarkedList").replaceChildren(...S.unmarked.map((d) => demoButton(d, () => discoverDemo(d.id), true)));
}

async function runDemo(id) {
  S.activeDemo = id;
  renderDemos();
  busy(true, PIPELINE_STEPS());
  try {
    const run = await api("/api/runs", {
      method: "POST", headers: JSON_HEADERS,
      body: JSON.stringify({ demo_id: id, mode: mode(), model_tier: tier() }),
    });
    setRun(run, true);
  } catch (e) {
    toast(e.status === 422 && mode() === "live"
      ? L(`ライブモードに失敗: ${e.message}。自動かオフラインを選んでください。`, `Live mode failed: ${e.message}. Use Auto or Offline demo.`)
      : L(`実行に失敗: ${e.message}`, `Run failed: ${e.message}`));
  } finally { busy(false); }
}

async function onUpload(ev) {
  ev.preventDefault();
  const f = $("#fileInput").files[0];
  const memo = $("#memoInput").value;
  if (!f || !memo.trim()) { toast(L("様式を選び、メモを貼り付けてください。", "Choose a form and paste a memo first.")); return; }
  const fd = new FormData();
  fd.append("file", f);
  fd.append("memo_text", memo);
  fd.append("mode", mode());
  fd.append("model_tier", tier());
  const spec = $("#specInput").files[0];
  if (spec) fd.append("slot_spec", spec);
  S.activeDemo = null;
  renderDemos();
  busy(true, L("アップロードして実行中…", "Uploading and running the pipeline…"));
  try {
    const run = await api("/api/runs/upload", { method: "POST", body: fd });
    if (!run.slots.length) {  // an unmarked blank form: discover its empty cells instead of showing a failed run
      busy(false);
      toast(L("マーカーの無い白紙様式です — 空欄を自動検出します。", "No markers in this form — discovering its blank cells."));
      await onDiscoverUpload();
      return;
    }
    setRun(run, true);
  } catch (e) { toast(L(`アップロードに失敗: ${e.message}`, `Upload failed: ${e.message}`)); } finally { busy(false); }
}

/* ------------------------------------------------ blank-cell discovery */
// Stages 1–3 run on the server; stage 4 is this inspector: the reviewer confirms cells, office-only cells are
// locked, and POST /api/runs/from-discovery fills only the confirmed ones (the server re-checks everything).

const DISCOVERY_STEPS = () => L("空欄を発見中… ① ヒューリスティック抽出 → ② 意味分類 → ③ 安全検査",
  "Discovering blank cells… ① heuristics → ② classification → ③ safety checks");

async function discoverDemo(id) {
  S.activeDemo = id;
  renderDemos();
  busy(true, DISCOVERY_STEPS());
  try {
    // one click: stages 1–3 and an auto-preview fill of the recommended applicant cells (never approvable);
    // stage 4 stays with the reviewer, who confirms the cells to get a run that can be signed off
    const res = await api("/api/run-demo", {
      method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ demo_id: id, mode: mode(), model_tier: tier() }),
    });
    setDiscovery(res.discovery, res.run);
  } catch (e) { toast(L(`空欄の発見に失敗: ${e.message}`, `Discovery failed: ${e.message}`)); } finally { busy(false); }
}

async function onDiscoverUpload() {
  const f = $("#fileInput").files[0];
  if (!f) { toast(L("先に .docx / .xlsx の様式を選んでください。", "Choose a .docx / .xlsx form first.")); return; }
  const fd = new FormData();
  fd.append("file", f);
  fd.append("mode", mode());
  fd.append("model_tier", tier());
  S.activeDemo = null;
  renderDemos();
  busy(true, DISCOVERY_STEPS());
  try {
    const d = await api("/api/discover", { method: "POST", body: fd });
    setDiscovery({ ...d, title: d.form_title || d.form, memo_text: $("#memoInput").value, uploaded: true });
  } catch (e) { toast(L(`空欄の発見に失敗: ${e.message}`, `Discovery failed: ${e.message}`)); } finally { busy(false); }
}

function setDiscovery(d, preview) {
  const eligible = d.candidates.filter((c) => c.recommended && !c.office_only).map((c) => c.candidate_id);
  S.disc = { d, checked: new Set(eligible), memo: d.memo_text || "", memo0: d.memo_text || "", preview: preview || null };
  $("#discMemo").value = S.disc.memo;
  showView("discovery");
  renderDiscovery();
}

function cellWhere(c) {
  if (c.file_type === "xlsx" || c.cell) {
    return [h("b", { class: "mono" }, c.cell || "?"),
      c.merged_range ? h("div", { class: "f-ctx" }, L(`結合 ${c.merged_range}`, `merged ${c.merged_range}`)) : null,
      c.sheet_name ? h("div", { class: "f-ctx" }, c.sheet_name) : null];
  }
  return [h("span", { class: "mono" }, L(`表${c.table}・行${c.row}・列${c.col}`, `tbl ${c.table} · r${c.row} · c${c.col}`))];
}

function ruleText(c) {
  const parts = [fmtLabel(c.required_format)];
  if (c.max_chars) parts.push(L(`${c.max_chars}字以内`, `≤ ${c.max_chars} chars`));
  const opts = (c.constraints || {}).allowed_values;
  if (opts && opts.length) parts.push(opts.join("／"));
  return parts.join(" · ");
}

function roleBadge(c) {
  if (c.office_only) return h("span", { class: "role role-office" }, "⊘ ✗ ", B("事務局専用欄（除外）", "Office-only — locked"));
  if (c.recommended) return h("span", { class: "role role-app" }, "✓ ", B("申請者記入欄", "Applicant field"));
  return h("span", { class: "role role-maybe" }, "– ", B("記入欄ではない可能性", "Probably not a field"));
}

function stageCard(n, ja, en, value, note, extra) {
  return h("li", { class: `stage${n === "4" ? " current" : ""}` },
    h("div", { class: "stage-n" }, n),
    h("div", {}, h("div", { class: "stage-t" }, B(ja, en)), h("div", { class: "stage-v", ...(extra || {}) }, value),
      h("div", { class: "f-ctx" }, note)));
}

function renderDiscovery() {
  const D = S.disc;
  if (!D) return;
  const d = D.d;
  const cands = d.candidates;
  const st = Object.fromEntries((d.stages || []).map((x) => [x.stage, x]));
  const office = cands.filter((c) => c.office_only).length;
  const applicant = cands.length - office;
  const rec = cands.filter((c) => c.recommended && !c.office_only).length;
  $("#discTitle").textContent = d.title || d.form_title || d.form;
  $("#discSub").textContent = [d.title_en, d.org, d.form, `${L("第2段階", "stage 2")}: ${engineText(d.engine.label)}`].filter(Boolean).join(" · ");
  const found = (st[1] || {}).found ?? cands.length + d.rejected.length;
  fill($("#discStages"),
    stageCard("1", "ヒューリスティック抽出", "Heuristic extraction", L(`${found} 個の空欄`, `${found} empty cells`),
      L("印字ラベルの右・列見出しの下の空欄", "next to a printed label / under a column header")),
    stageCard("2", "意味分類・ルール推定", "Classification & rule inference",
      L(`申請者 ${applicant}・事務局 ${office}`, `${applicant} applicant · ${office} office-only`), engineText(d.engine.label)),
    stageCard("3", "決定論的安全検査", "Deterministic safety check",
      L(`合格 ${(st[3] || {}).passed ?? cands.length}・除外 ${d.rejected.length}`, `${(st[3] || {}).passed ?? cands.length} passed · ${d.rejected.length} rejected`),
      L("計算式・結合セルの続き・既存の内容は除外", "formulas, merged continuations and filled cells are rejected")),
    stageCard("4", "人による記入前確定", "Human pre-fill confirmation", "", L("確定した欄だけに記入", "only confirmed cells are filled"),
      { id: "discStage4" }));
  fill($("#discKpis"),
    tile(B("白紙様式の SHA-256", "Blank form SHA-256", "stack"), h("code", { class: "sha", title: d.form_sha256 }, `${d.form_sha256.slice(0, 12)}…`),
      L("記入前にサーバーが再照合", "re-checked on the server before any fill")),
    tile(B("空欄候補", "Candidate cells", "stack"), String(cands.length), L(`第1段階で ${found} 個を検出`, `${found} found in stage 1`)),
    tile(B("推奨の申請者欄", "Recommended applicant cells", "stack"), String(rec),
      L(`事務局専用の ${office} 欄はロック`, `${office} office-only cell(s) locked`), "✓", "icon-good"),
    tile(B("安全のため除外したセル", "Cells rejected for safety", "stack"), String(d.rejected.length),
      L("計算式・結合セルの続きなどは記入対象にしない", "formulas, merged continuations … are never filled"), "🛡", "icon-good"));
  $("#candRows").replaceChildren(...cands.map((c) => {
    const locked = Boolean(c.office_only);
    const cb = h("input", { type: "checkbox", value: c.candidate_id, disabled: locked,
      "aria-label": `${c.candidate_id} ${c.slot_id}`, title: locked ? L("事務局専用欄は選べません", "Office-only cells can never be confirmed") : "" });
    cb.checked = !locked && D.checked.has(c.candidate_id);
    cb.addEventListener("change", () => {
      if (locked) { cb.checked = false; return; }  // the server refuses office-only cells too
      if (cb.checked) D.checked.add(c.candidate_id); else D.checked.delete(c.candidate_id);
      updateDiscConfirm();
    });
    const [first, ...hints] = String(c.evidence_label || "").split("\n");
    const ctx = [c.section, c.group_label].filter(Boolean).join(" › ");
    return h("tr", { class: locked ? "locked" : c.recommended ? "rec" : "maybe" },
      h("td", {}, cb),
      h("td", { class: "mono" }, c.candidate_id),
      h("td", { title: c.location, class: "nowrap" }, cellWhere(c)),
      h("td", {}, h("div", { class: "f-label" }, first), hints.length ? h("div", { class: "f-ctx" }, hints.join(" ")) : null,
        ctx ? h("div", { class: "f-ctx" }, ctx) : null, h("div", { class: "f-ctx mono" }, `→ ${c.slot_id}`)),
      h("td", {}, ruleText(c)),
      h("td", {}, roleBadge(c)),
      h("td", { class: "f-ctx", title: c.reason }, reasonText(c.reason), (c.office_keywords || []).length
        ? h("div", { class: "chips" }, c.office_keywords.map((k) => h("span", { class: "chip" }, k))) : null));
  }));
  fill($("#discRejected"),
    h("div", { class: "card-h" }, B("除外したセル（第3段階）", "Rejected cells (stage 3)")),
    d.rejected.length ? h("ul", { class: "rej" }, d.rejected.map((r) => h("li", {},
      h("b", { class: "mono" }, r.cell || L(`表${r.table}・行${r.row}・列${r.col}`, `tbl ${r.table} · r${r.row} · c${r.col}`)),
      ` ${String(r.evidence_label || "").split("\n")[0]}`, h("div", { class: "f-ctx", title: r.reason }, reasonText(r.reason)))))
      : h("p", { class: "muted" }, L("なし", "none")));
  if ($("#discMemo").value !== D.memo) $("#discMemo").value = D.memo;
  renderDiscPreview();
  updateDiscConfirm();
}

// The auto-preview: the recommended applicant cells filled by the server, shown next to the discovery table so the
// result can be inspected in one click. It can never be approved; confirming the cells creates the run to sign off.
function renderDiscPreview() {
  const box = $("#discPreview");
  const r = S.disc && S.disc.preview;
  box.hidden = !r;
  if (!r) return;
  const rep = r.report;
  const office = S.disc.d.candidates.filter((c) => c.office_only).map((c) => c.slot_id);
  const written = r.mappings.filter((m) => m.status === "filled");
  const si = statusInfo(rep.overall_status);
  fill(box,
    h("div", { class: "card-h" }, B("⚡ 自動プレビュー：推奨の申請者欄に仮記入した結果", "⚡ Auto-preview: the recommended applicant cells, filled"),
      h("span", {}, L("このプレビューは承認できません — 下のボタンで空欄を確定してください", "this preview cannot be approved — confirm the cells below"))),
    h("div", { class: "preview-grid" },
      h("div", { class: "svg-box preview-svg" }),
      h("div", { class: "preview-side" },
        h("div", { class: `status ${si.cls}` }, si.node),
        h("ul", { class: "preview-facts" },
          h("li", {}, `✓ ${rep.passed_count}/${rep.passed_count + rep.failed_count} ${L("項目の決定論的検査に合格", "deterministic checks passed")}`),
          h("li", {}, `✓ ${written.length} ${L("欄に原文引用つきで記入", "fields written, each with a verbatim quote")}`),
          h("li", {}, `⊘ ${L("事務局専用欄は空欄のまま", "office-only cells left empty")}: ${office.join("・") || "—"}`),
          h("li", {}, `↔ ${L("指定欄以外の描画のずれ", "layout drift outside the fields")}: ${(r.visual || {}).max_shift_px ?? 0} px`)),
        h("div", { class: "preview-fields" }, written.map((m) => h("div", { class: "pf" },
          h("b", {}, m.slot_id), h("span", { class: "f-value" }, m.filled_value), modelBadge(m)))),
        h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => setRun(r, true) },
          B("プレビューを詳しく見る（見た目の比較・根拠）", "Inspect the preview (visual diff, sources)")))));
  $(".preview-svg", box).innerHTML = r.previews.filled;  // server-generated SVG, all text escaped server-side
}

function updateDiscConfirm() {
  const D = S.disc;
  if (!D) return;
  const n = D.checked.size;
  const s4 = $("#discStage4");
  if (s4) s4.textContent = L(`${n} 欄を選択中`, `${n} cell(s) selected`);
  const msg = $("#discCount");
  msg.className = `msg ${n ? "ok" : "err"}`;
  msg.textContent = n ? L(`${n} 欄を確定して記入します（事務局専用欄は含められません）`, `${n} cell(s) will be filled (office-only cells can never be included)`)
    : L("記入する空欄を1つ以上選んでください", "Select at least one cell to fill");
  $("#fillConfirmed").disabled = !n;
}

async function onFillConfirmed() {
  const D = S.disc;
  if (!D) return;
  const ids = D.d.candidates.filter((c) => D.checked.has(c.candidate_id) && !c.office_only).map((c) => c.candidate_id);
  const memo = $("#discMemo").value;
  if (!ids.length) { toast(L("記入する空欄を1つ以上選んでください。", "Confirm at least one cell first.")); return; }
  if (!memo.trim()) { toast(L("入力メモを貼り付けてください。", "Paste the source memo first.")); return; }
  busy(true, L("確定した空欄だけに外科的記入 → 21項目検査 → 見た目の比較", "Surgical fill of the confirmed cells → 21 checks → visual diff"));
  try {
    const run = await api("/api/runs/from-discovery", {
      method: "POST", headers: JSON_HEADERS,
      body: JSON.stringify({
        discovery_id: D.d.discovery_id, confirmed_targets: ids, mode: mode(), model_tier: tier(),
        // a built-in demo with its untouched sample memo lets the server use the memo it stored
        memo_text: !D.d.uploaded && memo === D.memo0 ? null : memo,
      }),
    });
    setRun(run, true);
  } catch (e) {
    toast(e.status === 404 ? L("発見結果の期限が切れました。もう一度空欄を発見してください。", "This discovery expired — run discovery again.")
      : L(`記入を拒否しました: ${e.message}`, `Refused: ${e.message}`));
  } finally { busy(false); }
}

/* ------------------------------------------------------------------ run */

function setRun(run, fresh) {
  S.run = run;
  S.checked = new Set(run.approval ? run.approval.checked : []);
  if (fresh) { S.sel = null; S.cmp = run.previews.naive ? "naive" : "blank"; }
  showView("run");
  renderRun();
  if (S.sel) selectSlot(S.sel, false);
  if (fresh) showTab("visual");
}

function renderRun() {
  renderHeader();
  renderKpis();
  renderVisual();
  renderFields();
  renderAudit();
  renderHitl();
  if (S.tab === "claude") renderClaudeTab();
}

function mapsById() {
  return Object.fromEntries(S.run.mappings.map((m) => [m.slot_id, m]));
}

function statusInfo(st) {
  if (st === "PASS") return { cls: "st-pass", node: B("✓ 合格 — 人の最終確認へ", "✓ PASS — ready for human sign-off") };
  if (st === "NEEDS_HUMAN_INPUT") return { cls: "st-need", node: B("⚠ 人の入力が必要", "⚠ Needs human input") };
  return { cls: "st-fail", node: B("✗ 不合格 — 使用しないでください", "✗ FAIL — do not use") };
}

function renderHeader() {
  const r = S.run;
  const si = statusInfo(r.report.overall_status);
  const st = $("#runStatus");
  st.className = `status ${si.cls}`;
  fill(st, si.node);
  $("#runTitle").textContent = r.form.title;
  const eng = r.engine || {};
  $("#runSub").textContent = `${r.form.title_en ? r.form.title_en + " · " : ""}${r.display_name} → ${r.filled.name} · ${L("エンジン", "engine")}: ${engineText(eng.label) || "?"}`
    + (eng.fallback_reason ? ` (fallback: ${eng.fallback_reason})` : "")
    + (r.confirmed_candidates && r.confirmed_candidates.length ? ` · ${L("確定した空欄", "confirmed cells")}: ${r.confirmed_candidates.join(", ")}` : "")
    + (r.imported_from ? ` · ${L("MCP の下書き（再検査済み・未承認）", "MCP draft (re-verified, unapproved)")}: ${r.imported_from}` : "")
    + (r.auto_preview ? ` · ⚠ ${L("自動プレビュー（推奨の空欄で仮記入）— 承認するには空欄を確定してください", "auto-preview of the recommended cells — confirm the cells to sign off")}` : "");
  $("#reportLink").href = r.links.report;
  $("#auditLink").href = r.links.audit;
  $("#backToDisc").hidden = !(S.disc && r.confirmed_candidates && r.confirmed_candidates.length);
  renderEngineBadge();
  const pending = r.report.pending_slots.length;
  const hitlTab = $(".tabs button[data-tab=hitl]");
  hitlTab.querySelector(".badge-count")?.remove();
  if (pending) hitlTab.append(h("span", { class: "badge-count warn" }, String(pending)));
}

function tile(label, value, note, icon, iconCls) {
  return h("div", { class: "tile" }, h("div", { class: "label" }, label),
    h("div", { class: "value" }, icon ? h("span", { class: iconCls }, `${icon} `) : null, value), h("div", { class: "note" }, note));
}

function renderKpis() {
  const r = S.run;
  const rep = r.report;
  // a reviewer-entered value has no memo quote: count it apart instead of claiming every value has a verbatim source
  const aiFilled = r.mappings.filter((m) => m.status === "filled").length;
  const humanFilled = r.mappings.filter((m) => m.status === "human_entered").length;
  const filled = aiFilled + humanFilled;
  const pending = rep.pending_slots.length;
  const total = rep.passed_count + rep.failed_count;
  const untouchedAll = rep.entry_hashes.filter((e) => e.role === "untouched").length;
  const untouched = rep.entry_hashes.filter((e) => e.role === "untouched" && e.identical && e.raw_identical).length;
  const vis = r.visual || {};
  $("#kpis").replaceChildren(
    tile(B("記入済み", "Fields filled", "stack"), `${filled}/${r.mappings.length}`, humanFilled > 0
      ? L(`AI記入（原文照合済み）${aiFilled}件・人の入力（本人確認）${humanFilled}件`, `${aiFilled} AI (verbatim source) · ${humanFilled} human-entered`)
      : L("すべて原文の根拠付き", "each with a verbatim source")),
    tile(B("人の入力待ち", "Awaiting human input", "stack"), String(pending),
      pending ? L("メモに無いので推測しない", "not in the memo — never guessed") : L("なし", "none"), pending ? "⚠" : "✓", pending ? "icon-warn" : "icon-good"),
    tile(B("合格した検査", "Checks passed", "stack"), `${rep.passed_count}/${total}`, L("構造＋根拠の決定論的検査", "structural + grounding"),
      rep.failed_count ? "✗" : "✓", rep.failed_count ? "icon-bad" : "icon-good"),
    tile(B("無変更のパーツ", "Parts byte-identical", "stack"), untouched === untouchedAll ? String(untouched) : `${untouched}/${untouchedAll}`,
      untouched === untouchedAll ? L("ZIP 内パーツをバイト単位で複製", "ZIP entries copied verbatim")
        : L(`対象外パーツが ${untouchedAll - untouched} 個変化`, `${untouchedAll - untouched} non-target part(s) changed`),
      untouched === untouchedAll ? null : "✗", "icon-bad"),
    tile(B("指定欄以外のずれ", "Layout drift outside fields", "stack"), `${vis.max_shift_px ?? 0} px`,
      L(`${vis.boxes_compared ?? 0} 個の枠を比較`, `${vis.boxes_compared ?? 0} boxes compared`),
      vis.identical_outside_slots ? "✓" : "✗", vis.identical_outside_slots ? "icon-good" : "icon-bad"),
    tile(B("根拠のない記入", "Ungrounded claims", "stack"), String(rep.ungrounded_claims_count), L("AI記入値の根拠違反", "ungrounded AI values"),
      rep.ungrounded_claims_count ? "✗" : "✓", rep.ungrounded_claims_count ? "icon-bad" : "icon-good"),
  );
}

/* --------------------------------------------------------------- visual */

function captionNode(kind) {
  const r = S.run;
  if (kind === "naive") {
    const n = r.naive_report;
    return [B("生成AIで作り直した版（再現）", "Naive AI regeneration (simulated)"),
      h("span", { class: "st st-bad" }, L(`✗ ${n ? n.failed_count : "?"} 項目不合格`, `✗ ${n ? n.failed_count : "?"} checks failed`))];
  }
  if (kind === "blank") return [B("白紙の原本", "Blank original"), h("span", { class: "st st-human" }, L("▭ 指定欄", "▭ designated fields"))];
  const rep = r.report;
  return [B("Form-Guard（指定欄だけ記入）", "Form-Guard Agent — designated fields only"),
    h("span", { class: `st ${rep.failed_count ? "st-pending" : "st-filled"}` },
      `${rep.failed_count ? "✗" : "✓"} ${rep.passed_count}/${rep.passed_count + rep.failed_count} ${L("項目合格", "checks")}`)];
}

function renderVisual() {
  const r = S.run;
  if (!r) return;
  const hasNaive = Boolean(r.previews.naive);
  if (!hasNaive && S.cmp === "naive") S.cmp = "blank";
  $$("#cmpMode button").forEach((b) => {
    b.classList.toggle("on", b.dataset.v === S.cmp);
    b.disabled = b.dataset.v === "naive" && !hasNaive;
    b.title = b.disabled ? L("この様式には作り直し版の比較がありません", "No naive regeneration is available for this form") : "";
  });
  $$("#cmpLayout button").forEach((b) => b.classList.toggle("on", b.dataset.v === S.layout));
  const leftKind = S.cmp;
  const leftSvg = r.previews[leftKind] || r.previews.blank;
  $("#cmpSplit").hidden = S.layout !== "split";
  $("#cmpSlider").hidden = S.layout !== "slider";
  if (S.layout === "split") {
    $("#leftCap").replaceChildren(...captionNode(leftKind));
    $("#rightCap").replaceChildren(...captionNode("filled"));
    $("#leftSvg").innerHTML = leftSvg;   // server-generated SVG, all text escaped server-side
    $("#rightSvg").innerHTML = r.previews.filled;
  } else {
    $("#sliderA").innerHTML = leftSvg;
    $("#sliderB").innerHTML = r.previews.filled;
    $("#sliderCapA").textContent = leftKind === "naive" ? L("◀ 生成AIで作り直した版", "◀ Naive AI regeneration") : L("◀ 白紙の原本", "◀ Blank original");
    $("#sliderCapB").textContent = "Form-Guard ▶";
  }
  $$(".svg-box [data-slot-id], .slider-stage [data-slot-id]").forEach((el) => {
    el.addEventListener("click", () => { selectSlot(el.dataset.slotId, true); showTab("fields"); });
  });
  renderVerdicts();
  if (S.sel) highlightSvg(S.sel);
}

function renderVerdicts() {
  const r = S.run;
  const nv = $("#naiveVerdict");
  if (r.naive_report) {
    const fails = r.naive_report.checks.filter((c) => !c.passed);
    nv.hidden = false;
    fill(nv, h("h3", {}, B(`✗ 作り直しで壊れたもの（${fails.length}）`, `✗ What a from-scratch regeneration broke (${fails.length})`)),
      h("ul", {}, fails.map((c) => h("li", { title: `${c.check_id}: ${c.description} — ${c.details}` }, h("span", { class: "bad-i" }, "✗ "),
        h("b", {}, checkText(c)), ` — ${detailText(c.details)}`))));
  } else {
    nv.hidden = false;
    fill(nv, h("h3", {}, B("この様式には作り直し版の比較はありません", "No naive comparison for this form")),
      h("p", { class: "muted" }, L("付属のマーカーありデモ3件には、よくある失敗を再現した作り直し版が付いています。",
        "The three marked demo forms include a scripted naive regeneration that reproduces the usual failure modes.")));
  }
  const keep = ["ZIP-04", "XML-03", "LAYOUT-01", "LAYOUT-02", "TEXT-01", "GRD-01", "GRD-02", "CON-03"];
  const checks = r.report.checks.filter((c) => keep.includes(c.check_id) || !c.passed);
  fill($("#fgVerdict"), h("h3", {}, B("✓ Form-Guard がこのファイルで証明したこと", "✓ What Form-Guard proved for this file")),
    h("ul", {}, checks.map((c) => h("li", { title: `${c.check_id}: ${c.description} — ${c.details}` },
      h("span", { class: c.passed ? "ok-i" : "bad-i" }, c.passed ? "✓ " : "✗ "), h("b", {}, checkText(c)), ` — ${detailText(c.details)}`))));
}

function bindSliderDrag() {
  const stage = $("#sliderStage");
  let dragging = false;
  const move = (e) => {
    const rect = stage.getBoundingClientRect();
    const pct = Math.min(100, Math.max(0, ((e.clientX - rect.left) / rect.width) * 100));
    stage.style.setProperty("--pos", `${pct}%`);
    $("#sliderRange").value = String(Math.round(pct));
  };
  stage.addEventListener("pointerdown", (e) => { dragging = true; stage.setPointerCapture(e.pointerId); move(e); });
  stage.addEventListener("pointermove", (e) => { if (dragging) move(e); });
  stage.addEventListener("pointerup", () => { dragging = false; });
}

function highlightSvg(id) {
  $$("[data-slot-id].is-active").forEach((el) => el.classList.remove("is-active"));
  if (!id) return;
  $$(`[data-slot-id="${CSS.escape(id)}"]`).forEach((el) => el.classList.add("is-active"));
}

/* ---------------------------------------------------------- model routing */
// routed_model is the model id(s) that served the field (live), or "offline (<model> in live auto)" — a plan.

const MODEL_ID = /claude-[a-z]+-\d+(?:-\d{1,2})?(?:-\d{8})?/g;

function modelName(id) {  // claude-opus-5-5 -> Opus 5.5, claude-sonnet-5 -> Sonnet 5, claude-haiku-4-5-20251001 -> Haiku 4.5
  const m = /^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$/.exec(id || "");
  return m ? `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}${m[3] ? `.${m[3]}` : ""}` : String(id || "");
}

function modelFamily(id) {
  return (/claude-(haiku|sonnet|opus|fable)/.exec(id || "") || [])[1] || "other";
}

function routeInfo(m) {
  const r = m.routed_model || "";
  if (m.engine === "human" || r === "human reviewer") return { kind: "human", models: [] };
  const models = r.match(MODEL_ID) || [];
  return { kind: r.startsWith("offline (") ? "plan" : (models.length ? "live" : "other"), models, revised: /\(revision/.test(r) };
}

function modelChip(id, plan, alias) {
  const full = Boolean(alias) && /^claude-/.test(alias);  // a subagent's front matter may name an alias or a model id
  const text = alias && !full ? `${alias[0].toUpperCase()}${alias.slice(1)}` : modelName(alias || id);
  return h("span", { class: `mb mb-${modelFamily(alias && !full ? `claude-${alias}-` : alias || id)}${plan ? " mb-plan" : ""}`, title: id }, text);
}

function modelBadge(m) {
  const info = routeInfo(m);
  const tip = [m.routed_model, m.route_reason].filter(Boolean).join("\n");
  if (info.kind === "human") return h("span", { class: "mb mb-human", title: tip }, "✎ ", L("確認者", "reviewer"));
  if (!info.models.length) return h("span", { class: "mb", title: tip }, m.routed_model || "—");
  const parts = [];
  info.models.forEach((id, i) => {
    if (i) parts.push(h("span", { class: "mb-arrow" }, "→"));
    parts.push(modelChip(id, info.kind === "plan"));
  });
  return h("span", { class: "mbs", title: tip },
    info.kind === "plan" ? h("span", { class: "mb mb-offline" }, "Offline Rule") : null, ...parts,
    info.revised ? h("span", { class: "mb-note" }, L("修正", "revision")) : null,
    info.kind === "plan" ? h("span", { class: "mb-note" }, L("計画", "plan")) : null);
}

function routeBlock(m) {
  const info = routeInfo(m);
  const chain = info.models.join(" → ");
  let what = m.routed_model || "—";
  if (info.kind === "plan" && liveFailure(S.run)) {
    what = L(`この欄の値はオフラインエンジンで作成しました（ライブ実行は途中で失敗。それまでの API 呼び出しは「Claude Code 連携」タブに記録）。ライブの auto モードでは ${chain} が担当します。`,
      `This value comes from the offline engine: the live run failed part-way (the API calls made before that are listed in the Claude Code tab). In live auto mode ${chain} would handle this field.`);
  } else if (info.kind === "plan") {
    what = L(`この欄はオフラインエンジンで処理しました（API 呼び出しなし）。ライブの auto モードでは ${chain} が担当します。`,
      `Processed by the offline engine — no API call. In live auto mode ${chain} would handle this field.`);
  } else if (info.kind === "live") {
    what = L(`この欄は ${m.routed_model} が処理しました（API が返したモデル ID）。`, `Served by ${m.routed_model} (model id returned by the API).`);
  } else if (info.kind === "human") {
    what = L("確認者が入力した値です（モデルは関与していません）。", "Entered by the reviewer — no model involved.");
  }
  const usage = (S.run.engine && S.run.engine.usage) || {};
  const thoughts = (usage.thinking_summaries || []).filter((t) => (t.slots || []).includes(m.slot_id));
  return h("div", { class: "route-box" },
    h("div", { class: "route-h" }, B(info.kind === "plan" ? "モデルの割り当て（ライブ auto の計画）" : "モデルの割り当て",
      info.kind === "plan" ? "Model routing (live auto plan)" : "Model routing"), modelBadge(m)),
    h("div", { class: "f-ctx" }, what),
    m.route_reason ? h("div", { class: "f-ctx", title: m.route_reason }, `${L("理由", "Why")}: ${routeReasonText(m.route_reason)}`) : null,
    ...thoughts.map((t) => h("details", { class: "thinking" },
      h("summary", {}, `${L("API が返した思考の要約", "API-returned thinking summary")} · ${t.model} · ${t.task}`),
      h("div", { class: "f-ctx" }, t.label), h("div", { class: "thinking-text" }, t.text))));
}

/* --------------------------------------------------------------- fields */

function statusChip(m) {
  if (m.status === "filled") return h("span", { class: "st st-filled" }, L("✓ 記入済み", "✓ filled"));
  if (m.status === "human_entered") return h("span", { class: "st st-human" }, L("✎ 確認者が入力", "✎ reviewer"));
  if (m.status === "recovered") return h("span", { class: "st st-pending" }, L("? 根拠未検証", "? not verified"));
  return h("span", { class: "st st-pending" }, L("⚠ 入力待ち", "⚠ needs input"));
}

function lineRef(m) {
  const lr = m.source_line_range;
  if (!lr) return "—";
  return lr[0] === lr[1] ? `L${lr[0]}` : `L${lr[0]}–L${lr[1]}`;
}

function renderFields() {
  const r = S.run;
  const maps = mapsById();
  $("#fieldRows").replaceChildren(...r.slots.map((s) => {
    const m = maps[s.slot_id];
    const val = m.filled_value || (m.suggested_value ? `${L("候補", "suggestion")}: ${m.suggested_value}` : L("（未記入 — 入力待ち）", "(empty — awaiting input)"));
    const n = cpLength(m.filled_value);
    const over = s.max_chars && n > s.max_chars;
    return h("tr", { dataset: { slot: s.slot_id }, onclick: () => selectSlot(s.slot_id, true) },
      h("td", {}, h("div", { class: "f-label" }, s.label), h("div", { class: "f-ctx" }, s.context_label || "")),
      h("td", { class: `f-value${m.filled_value ? "" : " pending"}` }, val),
      h("td", { class: `num${over ? " over" : ""}` }, s.max_chars ? `${n}/${s.max_chars}` : String(n || "")),
      h("td", {}, statusChip(m), ...m.normalizations.filter((f) => ATTENTION.has(f)).map((f) => h("div", { class: "f-ctx" }, `⚠ ${flagText(f)}`))),
      h("td", {}, modelBadge(m)),
      h("td", { class: "num" }, lineRef(m)));
  }));
  renderMemo();
  if (!S.sel) fill($("#fieldDetail"), h("p", { class: "muted" }, L("欄を選ぶか、プレビュー上の値をクリックしてください。", "Select a field — or click a highlighted value in the preview.")));
}

function renderMemo() {
  // start_char / end_char are code-point offsets (Python str); slice code points, not UTF-16 units
  const chars = Array.from(S.run.memo.text);
  const spans = [];
  S.run.mappings.forEach((m) => (m.source_spans || []).forEach((sp) => spans.push([sp.start_char, sp.end_char, m.slot_id])));
  const cuts = new Set([0, chars.length]);
  spans.forEach(([a, b]) => { cuts.add(a); cuts.add(b); });
  const bounds = [...cuts].sort((x, y) => x - y);
  const out = [];
  let line = 1;
  out.push(h("span", { class: "ln" }, `L${String(line).padStart(3, "0")}`));
  for (let i = 0; i < bounds.length - 1; i++) {
    const a = bounds[i];
    const b = bounds[i + 1];
    const ids = [...new Set(spans.filter(([s, e]) => s <= a && b <= e).map((x) => x[2]))];
    chars.slice(a, b).join("").split("\n").forEach((piece, k) => {
      if (k > 0) { line += 1; out.push("\n", h("span", { class: "ln" }, `L${String(line).padStart(3, "0")}`)); }
      if (!piece) return;
      out.push(ids.length ? h("mark", { dataset: { slots: ids.join("|") }, title: ids.join(", "), onclick: () => selectSlot(ids[0], true) }, piece) : piece);
    });
  }
  $("#memoView").replaceChildren(...out);
}

function selectSlot(id, scroll) {
  S.sel = id;
  $$("#fieldRows tr").forEach((tr) => tr.classList.toggle("sel", tr.dataset.slot === id));
  $$("#memoView mark").forEach((mk) => mk.classList.toggle("active", mk.dataset.slots.split("|").includes(id)));
  highlightSvg(id);
  renderDetail(id);
  if (scroll) {
    const mk = $("#memoView mark.active");
    if (mk) { const box = $("#memoView"); box.scrollTop = mk.offsetTop - box.offsetTop - 60; }
  }
}

function renderDetail(id) {
  const s = S.run.slots.find((x) => x.slot_id === id);
  const m = mapsById()[id];
  if (!s || !m) return;
  const cons = [];
  if (s.required_format && s.required_format !== "text") cons.push(`${L("形式", "format")}: ${fmtLabel(s.required_format)}`);
  if (s.max_chars) cons.push(L(`${s.max_chars}字以内`, `≤ ${s.max_chars} chars`));
  if (s.constraints.allowed_values) cons.push(`${L("選択肢", "options")}: ${s.constraints.allowed_values.join("／")}`);
  if (s.constraints.max_value) cons.push(`${L("上限", "cap")}: ${Number(s.constraints.max_value).toLocaleString()}円`);
  if (s.constraints.multiple_of) cons.push(L(`${s.constraints.multiple_of} の倍数`, `multiple of ${s.constraints.multiple_of}`));
  if (s.constraints.pattern && s.required_format !== "choice") cons.push(`${L("書式", "pattern")}: ${s.constraints.example || s.constraints.pattern}`);
  for (const r of s.constraints.advance_notice || []) cons.push(L(`${r.target}の${r.days}日前までに${r.action}`, `${r.days} days before ${r.target}`));
  const quotes = (m.source_spans || []).map((sp) => h("div", { class: "quote" },
    h("b", {}, sp.line_start === sp.line_end ? `L${sp.line_start} ` : `L${sp.line_start}–${sp.line_end} `), `「${sp.text}」`));
  const attempts = (m.attempts || []).map((a) => h("li", { class: a.passed ? "ok" : "err" },
    h("div", {}, h("b", {}, `#${a.attempt} `), a.action),
    a.value ? h("div", { class: "tv" }, `→ ${a.value}`) : null,
    ...(a.issues || []).filter((i) => i.severity === "error").map((i) => h("div", { class: "ti" }, `✗ ${i.code}: ${i.message}`)),
    ...(a.issues || []).filter((i) => i.severity === "warning").map((i) => h("div", { class: "ti", style: "color:var(--warn-ink)" }, `⚠ ${i.message}`))));
  fill($("#fieldDetail"),
    h("div", { class: "card-h" }, s.label, h("span", {}, s.location_path)),
    h("div", {}, statusChip(m), " ", ...m.normalizations.map((f) => h("span", { class: "chip" }, flagText(f)))),
    h("div", { class: "detail-value" }, m.filled_value || (m.suggested_value
      ? L(`（未検証の候補: ${m.suggested_value}）`, `（unverified suggestion: ${m.suggested_value}）`)
      : L("（メモに記載なし — 確認者が入力）", "（not stated in the memo — left for the reviewer）"))),
    cons.length ? h("div", { class: "f-ctx" }, `${L("様式に印刷されたルール", "Printed rules found on the form")}: ${cons.join(" · ")}`) : null,
    ...(m.warnings || []).map((w) => h("div", { class: "f-ctx", style: "color:var(--warn-ink)" }, `⚠ ${w}`)),
    ...(m.errors || []).map((w) => h("div", { class: "f-ctx", style: "color:var(--bad-ink)" }, `✗ ${w}`)),
    quotes.length ? h("div", { style: "margin-top:8px" }, h("b", {}, B("根拠（原文そのまま）", "Verbatim source")), ...quotes) : null,
    m.reasoning ? h("div", { class: "f-ctx" }, `${L("エンジンのメモ", "Engine note")}: ${m.reasoning}`) : null,
    routeBlock(m),
    attempts.length ? h("div", { style: "margin-top:8px" }, h("b", {}, B("自己修正の履歴", "Self-correction history")), h("ol", { class: "timeline" }, attempts)) : null,
    editControl(s, m),
  );
}

// Inline editor for ANY field — AI-filled, reviewer-entered or pending. Saving re-writes the file
// surgically, re-runs every check and clears any earlier approval (the server enforces this).
function editControl(s, m) {
  const current = m.filled_value || m.suggested_value || "";
  const inp = h("input", { type: "text", value: current, "aria-label": `Edit ${s.label}` });
  const count = h("span", { class: "f-ctx" });
  const update = () => {
    const n = cpLength(inp.value.trim());
    count.textContent = s.max_chars ? `${n}/${s.max_chars}` : L(`${n} 字`, `${n} chars`);
    count.classList.toggle("over", Boolean(s.max_chars && n > s.max_chars));
  };
  inp.addEventListener("input", update);
  update();
  const save = () => {
    const v = inp.value.trim();
    if (!v) { toast(L("値を入力してください。", "Type a value first.")); return; }
    if (v === m.filled_value) { toast(L("変更はありません。", "Unchanged.")); return; }
    submitValues({ [s.slot_id]: v }, L(`「${s.label}」を保存し再検査しました。承認は取り消されました。`, `Saved «${s.label}» — re-verified. Approval (if any) was cleared.`));
  };
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
  return h("div", { class: "edit-row" },
    h("label", { class: "f-ctx" }, m.status === "needs_human_input" ? L("値を入力", "Enter value") : L("値を修正", "Correct this value")),
    inp, count, h("button", { class: "btn btn-ghost", type: "button", onclick: save }, L("値を保存", "Save edit")));
}

/* ---------------------------------------------------------------- audit */

function renderAudit() {
  const r = S.run;
  const byCat = {};
  r.report.checks.forEach((c) => { (byCat[c.category] = byCat[c.category] || []).push(c); });
  $("#auditGroups").replaceChildren(...GROUPS.filter(([k]) => byCat[k]).map(([k, en, ja]) => {
    const items = byCat[k];
    const ok = items.filter((c) => c.passed).length;
    return h("div", { class: "ag" },
      h("h3", {}, B(ja, en), h("span", { class: ok === items.length ? "ok-i" : "bad-i" }, `${ok === items.length ? "✓" : "✗"} ${ok}/${items.length}`)),
      ...items.map((c) => h("div", { class: "ck", title: `${c.description} — ${c.details}` }, h("span", { class: c.passed ? "ok-i" : "bad-i" }, c.passed ? "✓" : "✗"),
        h("span", {}, h("b", {}, `${c.check_id} `), checkText(c)), h("span", { class: "d" }, detailText(c.details)))));
  }));
  $("#entryRows").replaceChildren(...r.report.entry_hashes.map((e) => {
    let res = h("span", { class: "res-bad" }, L("✗ 変更あり", "✗ changed"));
    if (e.role === "target") res = h("span", { class: "res-target" }, e.identical ? L("対象 · 変更なし", "target · unchanged") : L("対象 · 欄の文字のみ", "target · slot text only"));
    else if (e.identical && e.raw_identical) res = h("span", { class: "res-ok" }, L("✓ バイト単位で同一", "✓ byte-identical"));
    const role = { target: ["記入対象", "target"], untouched: ["対象外", "untouched"] }[e.role];
    return h("tr", {}, h("td", {}, e.name), h("td", {}, role ? L(...role) : e.role), h("td", {}, `${e.original_sha256.slice(0, 20)}…`), h("td", {}, res));
  }));
  $("#patchRows").replaceChildren(...r.patches.map((p) => h("tr", {}, h("td", {}, p.part_name), h("td", {}, p.slot_id),
    h("td", { class: "num" }, `${p.byte_start}–${p.byte_end}`),
    h("td", {}, p.kind === "attr" ? 'add xml:space="preserve"' : `«${p.old_text}» → «${p.new_text}»`))));
}

/* ----------------------------------------------------------------- HITL */

function renderHitl() {
  const r = S.run;
  const maps = mapsById();
  const pending = r.mappings.filter((m) => m.status === "needs_human_input");
  const box = $("#pendingBox");
  if (pending.length) {
    const inputs = {};
    fill(box, h("div", { class: "card pending-card" },
      h("div", { class: "card-h" }, B(`⚠ ${pending.length} 欄に人の入力が必要です`, `⚠ ${pending.length} field(s) need your input`),
        h("span", {}, L("メモに根拠が無い項目は推測せず、人が入力します", "Facts missing from the memo are never guessed"))),
      ...pending.map((m) => {
        const s = r.slots.find((x) => x.slot_id === m.slot_id);
        const inp = h("input", { type: "text", placeholder: m.suggested_value ? `${L("候補", "suggestion")}: ${m.suggested_value}` : (s.constraints.example || "") });
        inputs[m.slot_id] = inp;
        const why = m.errors && m.errors.length
          ? `${L("様式のルールで不合格", "Rejected by the form's rules")}: ${m.errors.join(" / ")}`
          : L("メモに記載が無いため、Form-Guard は推測しませんでした。", "Not stated in the memo, so Form-Guard did not guess it.");
        return h("div", { class: "pending-item" }, h("b", {}, s.label), inp,
          h("span", { class: "f-ctx" }, [s.constraints.digits ? L(`${s.constraints.digits}桁`, `${s.constraints.digits} digits`) : fmtLabel(s.required_format),
            s.max_chars ? `≤${s.max_chars}` : ""].filter(Boolean).join(" · ")),
          h("div", { class: "why" }, why));
      }),
      h("div", { style: "margin-top:8px" }, h("button", { class: "btn btn-primary", type: "button", onclick: () => applyOverrides(inputs) },
        B("反映して再検査", "Apply & re-verify")))));
  } else {
    box.replaceChildren();
  }
  const items = r.report.hitl_checklist;
  $("#checklist").replaceChildren(...items.map((it) => {
    const blocked = it.kind === "pending_input";
    const m = it.slot_id ? maps[it.slot_id] : null;
    const cb = h("input", { type: "checkbox", value: it.item_id, disabled: blocked || Boolean(r.approval) });
    cb.checked = S.checked.has(it.item_id);
    cb.addEventListener("change", () => { if (cb.checked) S.checked.add(it.item_id); else S.checked.delete(it.item_id); updateApprove(); });
    const flags = (it.flags || []).map((f) => h("span", { class: "chip", style: ATTENTION.has(f) ? "border-color:var(--warn);color:var(--warn-ink)" : "" }, flagText(f)));
    const slot = it.slot_id ? r.slots.find((x) => x.slot_id === it.slot_id) : null;
    return h("li", { class: `${it.kind === "global" ? "global" : ""}${blocked ? " blocked" : ""}` }, cb,
      h("div", {},
        it.slot_id ? h("div", {}, h("b", {}, it.label), " ＝ ", h("span", { class: "f-value" }, it.value || L("（未記入）", "（empty）"))) : null,
        h("div", { class: "q" }, L(it.prompt_ja || it.prompt, it.prompt_en || it.prompt)),
        it.source_quote ? h("div", { class: "quote" }, `「${it.source_quote}」`) : null,
        flags.length ? h("div", { class: "chips" }, flags) : null,
        m && it.slot_id ? h("a", { href: "#", onclick: (e) => { e.preventDefault(); selectSlot(it.slot_id, true); showTab("fields"); } }, L("根拠を見る →", "show source →")) : null,
        slot && m && it.kind === "field" ? h("details", { class: "edit-inline" }, h("summary", {}, L("✎ 値を修正", "✎ Edit value")), editControl(slot, m)) : null));
  }));
  renderApproval();
  updateApprove();
}

async function applyOverrides(inputs) {
  const values = {};
  Object.entries(inputs).forEach(([k, el]) => { if (el.value.trim()) values[k] = el.value.trim(); });
  if (!Object.keys(values).length) { toast(L("値を入力してください。", "Type a value first.")); return; }
  await submitValues(values);
}

async function submitValues(values, okMessage) {
  busy(true, L("入力値を外科的に書き込み、再検査しています…", "Writing your values surgically and re-verifying…"));
  try {
    const run = await api(`/api/runs/${S.run.run_id}/overrides`, {
      method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ values }),
    });
    setRun(run, false);
    const still = run.report.pending_slots;
    toast(still.length ? L(`まだ入力待ち: ${still.join(", ")}`, `Still awaiting: ${still.join(", ")}`)
      : run.report.overall_status === "FAIL"  // e.g. a longer value no longer fits its cell (VIS-01)
        ? L("保存しましたが、再検査で不合格です。", "Saved, but re-verification failed.") + failedChecksText(run)
        : (okMessage || L("全欄を記入し再検査しました。チェックリストを確認してください。", "All fields filled — re-verified. Review the checklist.")));
  } catch (e) { toast(L(`再検査に失敗: ${e.message}`, `Re-verify failed: ${e.message}`)); } finally { busy(false); }
}

// " VIS-01: …" — which deterministic checks failed and why, for the locked-download message and the toast.
function failedChecksText(r) {
  const failed = r.report.checks.filter((c) => !c.passed);
  return failed.length ? ` ${failed.map((c) => `${c.check_id}: ${detailText(c.details)}`).join(" · ")}` : "";
}

function updateApprove() {
  const r = S.run;
  if (!r) return;
  const required = r.report.hitl_checklist.filter((i) => i.required).map((i) => i.item_id);
  const allChecked = required.every((i) => S.checked.has(i));
  const reviewer = $("#reviewerInput").value.trim();
  const btn = $("#approveBtn");
  btn.disabled = Boolean(r.approval) || !r.can_approve || !allChecked || !reviewer;
  const msg = $("#approveMsg");
  if (r.approval) return;
  if (r.auto_preview) {
    msg.className = "msg err";
    msg.textContent = L("自動プレビューは承認できません。「↩ 空欄の選択に戻る」で空欄を確定してから承認してください。",
      "An auto-preview cannot be approved: go back to the cell selection, confirm the cells, then sign off that run.");
  } else if (!r.can_approve) {
    msg.className = "msg err";
    msg.textContent = r.report.pending_slots.length
      ? L(`ダウンロードはロック中: ${r.report.pending_slots.length} 欄がまだ入力待ちです。`, `Download locked: ${r.report.pending_slots.length} field(s) still need your input.`)
      : L("ダウンロードはロック中: 検査に合格していません。", "Download locked: verification did not pass.") + failedChecksText(r);
  } else if (!allChecked) {
    msg.className = "msg";
    msg.textContent = L(`${required.filter((i) => S.checked.has(i)).length}/${required.length} 項目を確認済み`,
      `${required.filter((i) => S.checked.has(i)).length}/${required.length} items confirmed.`);
  } else if (!reviewer) {
    msg.className = "msg"; msg.textContent = L("承認するには確認者名を入力してください。", "Enter the reviewer's name to approve.");
  } else {
    msg.className = "msg ok"; msg.textContent = L("承認できます。", "Ready to approve.");
  }
}

async function onApprove() {
  // Capture what is on screen now: the reviewer may open another document or revision while the request is in flight.
  const target = S.run;
  if (!target) return;
  const { run_id: runId, revision: rev } = target;
  const sha = target.filled && target.filled.sha256;
  const same = () => S.run && S.run.run_id === runId && S.run.revision === rev && S.run.filled && S.run.filled.sha256 === sha;
  try {
    const res = await api(`/api/runs/${runId}/approve`, {
      method: "POST", headers: JSON_HEADERS,
      // bind the approval to exactly what is on screen: the server refuses a stale revision or file hash
      body: JSON.stringify({ reviewer: $("#reviewerInput").value.trim(), checked: [...S.checked],
        filled_sha256: sha, revision: rev }),
    });
    if (same()) {  // never attach this approval to a run or revision that replaced it meanwhile
      S.run.approval = res;
      renderHitl();
    }
    toast(L("承認しました — 検査済みファイルのロックを解除しました。", "Approved — the verified file is unlocked."));
  } catch (e) {
    const missing = e.body && e.body.detail && e.body.detail.missing;
    toast(missing ? L(`未確認の項目: ${missing.length}`, `Unchecked items: ${missing.length}`) : L(`承認を拒否: ${e.message}`, `Approval refused: ${e.message}`));
    if (e.status === 409 && S.run && S.run.run_id === runId) {  // stale view: reload that run to show its current state
      try {
        const fresh = await api(`/api/runs/${runId}`);
        if (S.run && S.run.run_id === runId) setRun(fresh, false);
      } catch (_) { /* keep the old view */ }
    }
  }
}

// ISO timestamp → Asia/Tokyo (UTC+9, no daylight saving) with the weekday. Computed from the UTC offset, so the result
// never depends on the browser's time zone: 2026-11-02T21:11:00+00:00 → 2026-11-03（火）06:11 JST.
function fmtDateWithWeekday(iso) {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return String(iso || "");
  const d = new Date(t + 9 * 3600 * 1000);  // read back with the getUTC* methods = the wall clock in Tokyo
  const p = (n) => String(n).padStart(2, "0");
  const day = `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}`;
  const time = `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
  const wd = d.getUTCDay();  // 0 = Sunday
  return L(`${day}（${"日月火水木金土"[wd]}）${time} JST`, `${day} (${["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][wd]}) ${time} JST`);
}

// The download link always names the approved file and revision (filled_sha256 + revision are required query
// parameters: the server answers 422 without them and 409 for any other file or revision).
function downloadHref(r) {
  const a = r.approval;
  if (a.download_url) return a.download_url;
  return `/api/runs/${encodeURIComponent(r.run_id)}/download?filled_sha256=${encodeURIComponent(a.filled_sha256)}`
    + `&revision=${encodeURIComponent(a.revision)}`;
}

function renderApproval() {
  const r = S.run;
  const dl = $("#downloadBtn");
  const msg = $("#approveMsg");
  if (r.approval) {
    dl.classList.remove("disabled");
    dl.removeAttribute("aria-disabled");
    dl.href = downloadHref(r);
    dl.textContent = L(`⬇ 検査済み .${r.form.file_type} をダウンロード`, `⬇ Download verified .${r.form.file_type}`);
    msg.className = "msg ok";
    const when = fmtDateWithWeekday(r.approval.approved_at);
    fill(msg, h("div", {}, L(`✓ ${r.approval.reviewer} が ${when} に承認`, `✓ Approved by ${r.approval.reviewer} on ${when}`)),
      h("div", { class: "receipt" }, `SHA-256 ${r.approval.filled_sha256}`));
    $("#approveBtn").disabled = true;
  } else {
    dl.classList.add("disabled");
    dl.setAttribute("aria-disabled", "true");
    dl.removeAttribute("href");
    dl.textContent = L("⬇ 検査済みファイル（ロック中）", "⬇ Download verified file (locked)");
  }
}

/* ------------------------------------------------- Claude Code · Architecture */
// GET /api/claude-suite reads .claude/skills, .claude/agents, .claude/settings.json and .mcp.json from the
// repository, plus the routing policy and the measured workflow numbers; this run's routing comes from S.run.

async function loadSuite() {
  if (S.suite) return S.suite;
  if (!S.suitePromise) {
    S.suitePromise = api("/api/claude-suite").then((s) => { S.suite = s; return s; }).finally(() => { S.suitePromise = null; });
  }
  return S.suitePromise;
}

async function openSuite() {
  if (S.run) { showView("run"); showTab("claude"); return; }
  showView("suite");
  renderSuiteInto($("#suiteStandalone"), null);
}

function renderClaudeTab() {
  if (S.run) renderSuiteInto($("#tab-claude"), S.run);
}

function renderSuiteInto(el, run) {
  if (!S.suite) {
    fill(el, h("div", { class: "card" }, h("p", { class: "muted" },
      L("Claude Code の構成ファイルを読み込み、5つの様式で作業量を計測しています…", "Reading the Claude Code assets and measuring the five sample forms…"))));
    loadSuite().then(() => { if (!el.closest("[hidden]")) renderSuiteInto(el, run ? S.run : null); })
      .catch((e) => fill(el, h("div", { class: "card" }, `✗ ${e.message}`)));
    return;
  }
  fill(el, ...suiteSections(S.suite, run));
}

function section(ja, en, ...kids) {
  return h("section", { class: "suite-sec" }, h("h2", { class: "suite-h" }, B(ja, en)), ...kids);
}

function runLanes(run) {
  const lanes = { haiku: [], sonnet: [], opus: [], other: [] };
  const planned = run.engine.mode !== "live";
  run.mappings.forEach((m) => {
    const info = routeInfo(m);
    const fam = info.models.length ? modelFamily(info.models[0]) : "other";
    (lanes[fam] || lanes.other).push({ m, info });
  });
  const models = S.suite.routing.models;
  const failed = liveFailure(run);  // live calls were made before the run fell back to the offline engine
  const u = failed || run.engine.usage || {};
  const [calls, tin, tout, served] = [u.requests || 0, u.input_tokens || 0, u.output_tokens || 0, (u.models || []).join(", ")];
  const note = planned && failed
    ? L(`ライブ実行は ${calls} 回の API 呼び出し（入力 ${tin} / 出力 ${tout} トークン · 応答モデル ${served}）の後に失敗したため、値はオフラインエンジンで作成しました。下はライブの auto モードで各欄を担当するモデル（計画）です。`,
      `The live run failed after ${calls} API calls (${tin} input / ${tout} output tokens · served by ${served}), so the values come from the offline engine. Below is where live auto mode would send each field (the plan).`)
    : planned
      ? L("このランはオフラインエンジンで実行しました — モデルは呼び出していません。下はライブの auto モードで各欄を担当するモデル（計画）です。",
        "This run used the offline engine — no model was called. Below is where live auto mode would send each field (the plan).")
      : L(`ライブ実行：${calls} 回の API 呼び出し · 入力 ${tin} / 出力 ${tout} トークン · 応答モデル ${served}`,
        `Live run: ${calls} API calls · ${tin} input / ${tout} output tokens · served by ${served}`);
  const lane = (fam, title) => h("div", { class: `lane lane-${fam}` },
    h("div", { class: "lane-h" }, fam === "other" ? h("span", { class: "mb" }, title) : modelChip(models[fam], planned),
      h("span", { class: "mono f-ctx" }, fam === "other" ? "" : models[fam]), h("b", {}, String(lanes[fam].length))),
    h("div", { class: "chips" }, lanes[fam].map(({ m, info }) => h("span", { class: "chip", title: m.route_reason },
      run.slots.find((s) => s.slot_id === m.slot_id)?.label || m.slot_id,
      info.revised ? h("b", { class: "rev" }, " → Opus") : null))));
  return h("div", {},
    h("p", { class: planned ? "plan-note" : "f-ctx" }, note),
    h("div", { class: "lanes" }, lane("haiku"), lane("sonnet"), lane("opus"),
      lanes.other.length ? lane("other", L("人・その他", "human / other")) : null));
}

// "every check passed on every form", never "21/22" (which reads as 21 of 22 passed); the count per form as a range
function checksPerFormText(t) {
  const n = t.checks_per_form || [];  // sorted distinct check counts, e.g. [21] or [21, 22] (VIS-01 added on drift)
  const [ja, en] = n.length > 1 ? [`${n[0]}〜${n[n.length - 1]}`, `${n[0]}–${n[n.length - 1]}`] : [String(n[0] ?? "?"), String(n[0] ?? "?")];
  if (!t.all_checks_passed) return L(`不合格の検査あり（様式あたり ${ja} 項目）`, `some checks failed (${en} per form)`);
  return n.length > 1
    ? L(`全様式で全項目合格（様式により ${ja} 項目）`, `all checks passed on every form (${en} per form)`)
    : L(`全様式で全 ${ja} 項目合格`, `all ${en} checks passed on every form`);
}

function suiteSections(s, run) {
  const out = [];
  const nSkills = s.skills.length;
  const nAgents = s.agents.length;
  const nHooks = s.settings.hooks.length;
  const nTools = s.mcp.tools.length;
  out.push(h("div", { class: "suite-hero" },
    h("div", { class: "pitch-card" }, h("div", { class: "pitch-h" }, B("開発：Claude Code が中心", "Development — built with Claude Code")),
      h("p", {}, L(`設計・実装・テスト・ドキュメントを会期中に Claude Code で作成。リポジトリには Skills ${nSkills}・サブエージェント ${nAgents}・フック ${nHooks}・MCP ツール ${nTools} を同梱し、Claude Code から同じ検証エンジンを安全に使えます。`,
        `Designed, implemented, tested and documented with Claude Code during the hackathon. The repository ships ${nSkills} skills, ${nAgents} subagents, ${nHooks} hook${nHooks === 1 ? "" : "s"} and ${nTools} MCP tools, so Claude Code can drive the same verifier safely.`))),
    h("div", { class: "pitch-card" }, h("div", { class: "pitch-h" }, B("実行時：欄ごとにモデルを使い分け", "Runtime — per-field model routing")),
      h("p", {}, L("定型の欄は Haiku、通常の文章・金額・氏名は Sonnet、長文の要約と検証器に差し戻された後の自己修正は Opus。どの欄をどのモデルが担当したかは、監査ログ・レポート・この画面に残ります。",
        "Structured fields go to Haiku, standard text / amounts / names to Sonnet, long-text condensation and every self-correction after a validator rejection to Opus. Which model handled which field is kept in the audit log, the report and this screen.")))));
  if (run) out.push(section("この実行のモデル割り当て", "This run's model routing", runLanes(run)));
  out.push(section("ルーティング方針（--model-tier auto）", "Routing policy (--model-tier auto)",
    h("div", { class: "card table-card" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, B("作業", "Task")), h("th", {}, B("モデル", "Model")), h("th", {}, B("理由", "Why")))),
      h("tbody", {}, s.routing.policy.map((p) => h("tr", {}, h("td", {}, L(p.ja, p.en)),
        h("td", {}, modelChip(p.model, false), h("div", { class: "f-ctx mono" }, p.model)), h("td", { class: "f-ctx" }, L(p.why_ja, p.why_en)))))))));
  const KIND = { skill: ["Skill", "Skill"], agent: ["サブエージェント", "Subagent"], mcp: ["MCP", "MCP"], hook: ["フック", "Hook"], human: ["人", "Human"] };
  out.push(section("Claude Code 拡張の連携", "How the Claude Code suite works together",
    h("ol", { class: "flow" }, s.flow.map((f) => h("li", {},
      h("div", {}, L(f.ja, f.en)),
      h("div", { class: "chips" }, f.uses.map((u) => h("span", { class: `use use-${u.kind}${u.present ? "" : " missing"}` },
        `${L(...KIND[u.kind])} · ${u.name}${u.present ? "" : " ✗"}`))))))));
  const hooks = s.settings.hooks;  // every stage (PreToolUse before the write, PostToolUse after it)
  const hookCmd = (hk) => (hk.command && hk.command.length <= 80 ? hk.command : `${hk.runner} …`);
  const hookCard = h("div", { class: "card asset" }, h("div", { class: "card-h" },
    hooks.length > 1 ? L(`フック（${hooks.length} 段）`, `Hooks (${hooks.length} stages)`) : L("フック", "Hook"), h("span", {}, s.settings.path)),
    hooks.length ? hooks.map((hk) => h("div", { class: "asset-item" },
      h("b", { class: "mono" }, `${hk.event} · ${hk.matcher.split("|").join(" | ")}`),  // spaces let the matcher wrap
      h("div", { class: "f-ctx mono" }, `${hookCmd(hk)}${hk.timeout ? L(`（${hk.timeout}秒）`, ` (${hk.timeout} s)`) : ""}`),
      hk.when && (hk.when.ja || hk.when.en) ? h("div", { class: "f-ctx" }, L(hk.when.ja, hk.when.en)) : null,
      h("div", { class: "chips" }, hk.checks.map((c) => h("span", { class: "chip" }, L(c.ja, c.en))))))
      : h("p", { class: "muted" }, L("未設定", "not installed")),
    h("div", { class: "asset-item" }, h("b", {}, L("権限で読み取り禁止", "Denied reads")), h("div", { class: "chips" }, s.settings.deny.map((d) => h("span", { class: "chip mono" }, d))),
      h("div", { class: "f-ctx" }, s.settings.bypass_permissions ? "⚠ bypassPermissions" : L("bypassPermissions は不使用", "no bypassPermissions"))));
  const mcpCard = h("div", { class: "card asset" }, h("div", { class: "card-h" }, L("MCP サーバー", "MCP server"), h("span", {}, s.mcp.path)),
    ...s.mcp.servers.map((sv) => h("div", { class: "asset-item" }, h("b", { class: "mono" }, sv.name), h("div", { class: "f-ctx mono" }, `${sv.type}: ${sv.command} ${sv.args.join(" ")}`))),
    ...s.mcp.tools.map((t) => h("div", { class: "asset-item" }, h("b", { class: "mono" }, t.name), h("div", { class: "f-ctx" }, t.description.split(". ")[0]))));
  const agentCard = h("div", { class: "card asset" }, h("div", { class: "card-h" }, L("サブエージェント", "Subagents"), h("span", {}, ".claude/agents/")),
    ...s.agents.map((a) => h("div", { class: "asset-item" }, h("b", { class: "mono" }, a.name), " ",
      a.model ? modelChip(a.model, false, a.model) : null,
      h("div", { class: "chips" }, a.tools.map((t) => h("span", { class: "chip" }, t))), h("div", { class: "f-ctx" }, a.description))));
  const skillCard = h("div", { class: "card asset" }, h("div", { class: "card-h" }, "Skills", h("span", {}, ".claude/skills/")),
    ...s.skills.map((x) => h("div", { class: "asset-item" }, h("b", { class: "mono" }, x.name), h("div", { class: "f-ctx" }, x.description))));
  // the guard rails first (hooks before / after every write, the MCP tools), then who checks and the procedures
  out.push(section("リポジトリ同梱の Claude Code 資産（ファイルから読み込み）", "Claude Code assets in this repository (read from the files)",
    h("div", { class: "suite-grid" }, hookCard, mcpCard, agentCard, skillCard)));
  const w = s.workflow;
  if (w) {
    const t = w.totals;
    out.push(section("計測：5様式の作業量比較", "Measured workflow comparison (5 forms)",
      h("div", { class: "kpis" },
        tile(B("指定欄", "Designated fields", "stack"), String(t.fields), L(`${t.forms} 様式・${t.written} 件を記入`, `${t.forms} forms · ${t.written} values written`)),
        tile(B("記入した文字", "Characters written", "stack"), t.chars.toLocaleString(), L("人が転記するはずだった量", "what a person would transcribe")),
        tile(B("照合した XML タグ", "XML tags compared", "stack"), t.tags.toLocaleString(), L(`定型文 ${t.text_blocks} か所`, `${t.text_blocks} printed text blocks`)),
        tile(B("結合セル・罫線", "Merged cells · borders", "stack"), `${t.merges} · ${t.borders}`, L("結合セル・範囲 · 罫線定義", "merged cells/ranges · border definitions")),
        tile(B("1様式あたり", "Per form", "stack"), `${t.ms_min}–${t.ms_max} ms`, checksPerFormText(t), t.all_checks_passed ? "✓" : "✗", t.all_checks_passed ? "icon-good" : "icon-bad"),
        tile(B("人に残る作業", "Left for the human", "stack"), String(t.hitl), L(`確認項目 · 推測しない欄 ${t.pending}（${t.pending_slots.join(", ")}）`, `sign-off items · ${t.pending} unstated fact (${t.pending_slots.join(", ")})`))),
      h("div", { class: "card table-card" }, h("table", {},
        h("thead", {}, h("tr", {}, ...[["様式", "Form"], ["欄", "Fields"], ["文字", "Chars"], ["XML タグ", "XML tags"], ["定型文", "Text blocks"],
          ["結合", "Merges"], ["罫線", "Borders"], ["時間", "Time"], ["検査", "Checks"], ["確認項目", "Sign-off"]].map(([ja, en]) => h("th", {}, B(ja, en))))),
        h("tbody", {}, w.rows.map((r) => h("tr", {},
          h("td", {}, h("b", {}, r.form), h("div", { class: "f-ctx mono" }, r.file)),
          h("td", { class: "num" }, r.candidates ? `${r.fields} / ${r.candidates}` : String(r.fields)), h("td", { class: "num" }, String(r.chars)),
          h("td", { class: "num" }, r.tags.toLocaleString()), h("td", { class: "num" }, String(r.text_blocks)), h("td", { class: "num" }, String(r.merges)),
          h("td", { class: "num" }, String(r.borders)), h("td", { class: "num" }, `${r.ms} ms`),
          h("td", {}, `${r.passed}/${r.total} `, h("span", { class: `st ${r.status === "PASS" ? "st-filled" : "st-pending"}`, title: r.status }, statusText(r.status))),
          h("td", { class: "num" }, String(r.hitl) + (r.pending ? ` + ${r.pending}` : ""))))))),
      h("p", { class: "f-ctx" }, L("オフラインエンジンでこの PC 上で計測（ネットワークなし）。手作業の列はファイルから数えた作業量で、人の作業時間を計ったものではありません。マーカーなし様式の「欄」は 確定した欄 / 空欄候補 です。",
        "Measured on this machine with the offline engine (no network). The manual columns are counted from the files, not a timed study of a person. For unmarked forms “Fields” is confirmed cells / candidate cells."))));
  }
  return out;
}
