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

const FLAG = {
  wareki: "和暦に変換 · to 和暦", halfwidth: "半角に統一 · half-width", yen: "金額表記を統一 · amount format",
  condensed: "抽出要約 · condensed by deletion", re_extracted: "制約違反→再抽出 · re-extracted", order: "様式の順に整列 · reordered",
  name: "敬称・注記を除去 · name cleaned", note: "注記を除去 · note removed", year_inferred: "年は推定 · year inferred",
  weekday_ok: "曜日一致 · weekday checked", human: "人が入力 · reviewer entry",
  re_anchored: "引用を付け替え · quote re-anchored — check the wording",
  low_conf: "低信頼度 · low confidence", needs_input: "要入力 · needs input", has_suggestion: "候補あり · has suggestion",
  unsafe_condensation: "要約で条件が欠ける · condition would be lost", weekday: "曜日を付記 · weekday added",
  recovered_unverified: "根拠未検証 · not verified",
};
const ATTENTION = new Set(["year_inferred", "low_conf", "needs_input", "re_extracted", "condensed", "unsafe_condensation", "re_anchored"]);

// Python counts characters as Unicode code points; JS .length counts UTF-16 units (𠮷 or 🧪 = 2).
// Every offset / length that comes from the server is therefore applied to Array.from(text).
const cpLength = (s) => Array.from(s || "").length;
const GROUPS = [
  ["zip", "ZIP package", "パッケージ"], ["xml", "XML", "XML構造"], ["layout", "Layout", "罫線・結合・書式"],
  ["text", "Text", "定型文"], ["grounding", "Grounding", "根拠"], ["constraints", "Constraints", "記入ルール"],
  ["application", "Application", "読込確認"], ["visual", "Visual", "見た目"],
];

const S = { demos: [], run: null, sel: null, cmp: "naive", layout: "split", checked: new Set(), health: null, activeDemo: null };

class ApiError extends Error {
  constructor(status, body) {
    const d = body && body.detail;
    super(typeof d === "string" ? d : (d && d.message) || `HTTP ${status}`);
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
  toast._t = setTimeout(() => t.classList.remove("show"), 4200);
}

function busy(on, step) {
  $("#busy").hidden = !on;
  if (step) $("#busyStep").textContent = step;
}

/* ------------------------------------------------------------------ init */

document.addEventListener("DOMContentLoaded", async () => {
  const theme = localStorage.getItem("fg-theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.documentElement.dataset.theme = theme;
  $("#themeToggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("fg-theme", next);
  });
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  $$("#cmpMode button").forEach((b) => b.addEventListener("click", () => { S.cmp = b.dataset.v; renderVisual(); }));
  $$("#cmpLayout button").forEach((b) => b.addEventListener("click", () => { S.layout = b.dataset.v; renderVisual(); }));
  $("#sliderRange").addEventListener("input", (e) => $("#sliderStage").style.setProperty("--pos", `${e.target.value}%`));
  bindSliderDrag();
  $("#uploadForm").addEventListener("submit", onUpload);
  $("#fileInput").addEventListener("change", (e) => {
    const f = e.target.files[0];
    $("#fileLabel").textContent = f ? `${f.name} (${Math.round(f.size / 1024)} KB)` : "Choose a .docx / .xlsx";
  });
  $("#checkAll").addEventListener("click", () => {
    $$("#checklist input[type=checkbox]:not(:disabled)").forEach((c) => { c.checked = true; S.checked.add(c.value); });
    updateApprove();
  });
  $("#reviewerInput").addEventListener("input", updateApprove);
  $("#approveBtn").addEventListener("click", onApprove);
  try {
    S.health = await api("/api/health");
    $("#modeSelect").value = "auto";
    const b = $("#engineBadge");
    b.textContent = S.health.live_available ? `Claude ready · ${S.health.default_model}` : "Offline demo engine (no API key)";
    b.className = `pill ${S.health.live_available ? "pill-live" : "pill-neutral"}`;
    if (!S.health.live_available) $("#modeSelect option[value=live]").textContent = "Live Claude (needs ANTHROPIC_API_KEY)";
  } catch (e) { toast(`Health check failed: ${e.message}`); }
  try {
    S.demos = await api("/api/demos");
    renderDemos();
  } catch (e) { toast(`Could not load demos: ${e.message}`); }
  // Deep links for demos/recordings: /#demo=form2&tab=hitl&slot=法人番号&layout=slider
  const params = new URLSearchParams(location.hash.slice(1));
  if (params.get("layout") === "slider") S.layout = "slider";
  if (params.get("demo") && S.demos.some((d) => d.id === params.get("demo"))) {
    await runDemo(params.get("demo"));
    if (params.get("slot")) selectSlot(params.get("slot"), true);
    if (params.get("tab")) showTab(params.get("tab"));
  }
});

function showTab(name) {
  $$(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  ["visual", "fields", "audit", "hitl"].forEach((t) => { $(`#tab-${t}`).hidden = t !== name; });
}

/* ---------------------------------------------------------------- demos */

function renderDemos() {
  const list = $("#demoList");
  list.replaceChildren(...S.demos.map((d) => h("button", {
    class: `demo${S.activeDemo === d.id ? " active" : ""}`, type: "button", onclick: () => runDemo(d.id),
  },
  h("div", { class: "demo-top" }, h("span", { class: "ftype" }, `.${d.file_type}`), h("span", { class: "demo-title" }, d.title)),
  h("div", { class: "demo-org" }, `${d.org} · ${d.title_en}`),
  h("div", { class: "chips" }, d.highlights.map((x) => h("span", { class: "chip" }, x))))));
}

async function runDemo(id) {
  S.activeDemo = id;
  renderDemos();
  busy(true, "Scan → Map & self-correct → Surgical write → Verify → Visual diff");
  try {
    const run = await api("/api/runs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ demo_id: id, mode: $("#modeSelect").value }),
    });
    setRun(run, true);
  } catch (e) {
    toast(e.status === 422 && $("#modeSelect").value === "live"
      ? `Live mode failed: ${e.message}. Use Auto or Offline demo.` : `Run failed: ${e.message}`);
  } finally { busy(false); }
}

async function onUpload(ev) {
  ev.preventDefault();
  const f = $("#fileInput").files[0];
  const memo = $("#memoInput").value;
  if (!f || !memo.trim()) { toast("Choose a form and paste a memo first."); return; }
  const fd = new FormData();
  fd.append("file", f);
  fd.append("memo_text", memo);
  fd.append("mode", $("#modeSelect").value);
  const spec = $("#specInput").files[0];
  if (spec) fd.append("slot_spec", spec);
  S.activeDemo = null;
  renderDemos();
  busy(true, "Uploading and running the pipeline…");
  try {
    setRun(await api("/api/runs/upload", { method: "POST", body: fd }), true);
  } catch (e) { toast(`Upload failed: ${e.message}`); } finally { busy(false); }
}

/* ------------------------------------------------------------------ run */

function setRun(run, fresh) {
  S.run = run;
  S.checked = new Set(run.approval ? run.approval.checked : []);
  if (fresh) { S.sel = null; S.cmp = run.previews.naive ? "naive" : "blank"; }
  $("#emptyState").hidden = true;
  $("#runView").hidden = false;
  renderHeader();
  renderKpis();
  renderVisual();
  renderFields();
  renderAudit();
  renderHitl();
  if (S.sel) selectSlot(S.sel, false);
  if (fresh) showTab("visual");
}

function mapsById() {
  return Object.fromEntries(S.run.mappings.map((m) => [m.slot_id, m]));
}

function statusInfo(st) {
  if (st === "PASS") return { cls: "st-pass", text: "✓ PASS — ready for human sign-off" };
  if (st === "NEEDS_HUMAN_INPUT") return { cls: "st-need", text: "⚠ Needs human input" };
  return { cls: "st-fail", text: "✗ FAIL — do not use" };
}

function renderHeader() {
  const r = S.run;
  const si = statusInfo(r.report.overall_status);
  const st = $("#runStatus");
  st.className = `status ${si.cls}`;
  st.textContent = si.text;
  $("#runTitle").textContent = r.form.title;
  const eng = r.engine || {};
  $("#runSub").textContent = `${r.form.title_en ? r.form.title_en + " · " : ""}${r.display_name} → ${r.filled.name} · engine: ${eng.label || "?"}`
    + (eng.fallback_reason ? ` (fallback: ${eng.fallback_reason})` : "");
  $("#reportLink").href = r.links.report;
  $("#auditLink").href = r.links.audit;
  const b = $("#engineBadge");
  b.textContent = eng.mode === "live" ? `Claude · ${eng.model === "auto" ? "auto model routing" : eng.model}` : "Offline demo engine";
  b.className = `pill ${eng.mode === "live" ? "pill-live" : "pill-neutral"}`;
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
  const filled = r.mappings.filter((m) => m.status === "filled" || m.status === "human_entered").length;
  const pending = rep.pending_slots.length;
  const total = rep.passed_count + rep.failed_count;
  const untouchedAll = rep.entry_hashes.filter((e) => e.role === "untouched").length;
  const untouched = rep.entry_hashes.filter((e) => e.role === "untouched" && e.identical && e.raw_identical).length;
  const vis = r.visual || {};
  $("#kpis").replaceChildren(
    tile("Fields filled", `${filled}/${r.mappings.length}`, "記入済み（根拠付き）"),
    tile("Awaiting human input", String(pending), pending ? "not in the memo — never guessed" : "none", pending ? "⚠" : "✓", pending ? "icon-warn" : "icon-good"),
    tile("Checks passed", `${rep.passed_count}/${total}`, "structural + grounding", rep.failed_count ? "✗" : "✓", rep.failed_count ? "icon-bad" : "icon-good"),
    tile("Parts byte-identical", untouched === untouchedAll ? String(untouched) : `${untouched}/${untouchedAll}`,
      untouched === untouchedAll ? "ZIP entries copied verbatim" : `${untouchedAll - untouched} non-target part(s) changed`,
      untouched === untouchedAll ? null : "✗", "icon-bad"),
    tile("Layout drift outside fields", `${vis.max_shift_px ?? 0} px`, `${vis.boxes_compared ?? 0} boxes compared`, vis.identical_outside_slots ? "✓" : "✗", vis.identical_outside_slots ? "icon-good" : "icon-bad"),
    tile("Ungrounded claims", String(rep.ungrounded_claims_count), "根拠のない記入", rep.ungrounded_claims_count ? "✗" : "✓", rep.ungrounded_claims_count ? "icon-bad" : "icon-good"),
  );
}

/* --------------------------------------------------------------- visual */

function captionNode(kind) {
  const r = S.run;
  if (kind === "naive") {
    const n = r.naive_report;
    return [h("span", {}, "Naive AI regeneration (simulated) · 作り直し版"),
      h("span", { class: "st st-pending", style: "background:var(--bad-soft);color:var(--bad-ink)" }, `✗ ${n ? n.failed_count : "?"} checks failed`)];
  }
  if (kind === "blank") return [h("span", {}, "Blank original · 原本"), h("span", { class: "st st-human" }, "▭ designated fields")];
  const rep = r.report;
  return [h("span", {}, "Form-Guard Agent · 指定欄だけ記入"),
    h("span", { class: `st ${rep.failed_count ? "st-pending" : "st-filled"}` }, `${rep.failed_count ? "✗" : "✓"} ${rep.passed_count}/${rep.passed_count + rep.failed_count} checks`)];
}

function renderVisual() {
  const r = S.run;
  const hasNaive = Boolean(r.previews.naive);
  if (!hasNaive && S.cmp === "naive") S.cmp = "blank";
  $$("#cmpMode button").forEach((b) => {
    b.classList.toggle("on", b.dataset.v === S.cmp);
    b.disabled = b.dataset.v === "naive" && !hasNaive;
    b.title = b.disabled ? "No naive regeneration is available for uploaded forms" : "";
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
    $("#sliderCapA").textContent = leftKind === "naive" ? "◀ Naive AI regeneration" : "◀ Blank original";
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
    fill(nv, h("h3", {}, `✗ What a from-scratch regeneration broke (${fails.length})`),
      h("ul", {}, fails.map((c) => h("li", {}, h("span", { class: "bad-i" }, "✗ "), h("b", {}, c.description), ` — ${c.details}`))));
  } else {
    nv.hidden = false;
    nv.replaceChildren(h("h3", {}, "No naive comparison for uploaded forms"),
      h("p", { class: "muted" }, "Demo forms include a scripted naive regeneration that reproduces the usual failure modes."));
  }
  const keep = ["ZIP-04", "XML-03", "LAYOUT-01", "LAYOUT-02", "TEXT-01", "GRD-01", "GRD-02", "CON-03"];
  const checks = r.report.checks.filter((c) => keep.includes(c.check_id) || !c.passed);
  $("#fgVerdict").replaceChildren(h("h3", {}, "✓ What Form-Guard proved for this file"),
    h("ul", {}, checks.map((c) => h("li", {}, h("span", { class: c.passed ? "ok-i" : "bad-i" }, c.passed ? "✓ " : "✗ "),
      h("b", {}, c.description), ` — ${c.details}`))));
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

/* --------------------------------------------------------------- fields */

function statusChip(m) {
  if (m.status === "filled") return h("span", { class: "st st-filled" }, "✓ filled");
  if (m.status === "human_entered") return h("span", { class: "st st-human" }, "✎ reviewer");
  if (m.status === "recovered") return h("span", { class: "st st-pending" }, "? not verified");
  return h("span", { class: "st st-pending" }, "⚠ needs input");
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
    const val = m.filled_value || (m.suggested_value ? `suggestion: ${m.suggested_value}` : "（未記入 — awaiting input）");
    const n = cpLength(m.filled_value);
    const over = s.max_chars && n > s.max_chars;
    return h("tr", { dataset: { slot: s.slot_id }, onclick: () => selectSlot(s.slot_id, true) },
      h("td", {}, h("div", { class: "f-label" }, s.label), h("div", { class: "f-ctx" }, s.context_label || "")),
      h("td", { class: `f-value${m.filled_value ? "" : " pending"}` }, val),
      h("td", { class: `num${over ? " over" : ""}` }, s.max_chars ? `${n}/${s.max_chars}` : String(n || "")),
      h("td", {}, statusChip(m), ...m.normalizations.filter((f) => ATTENTION.has(f)).map((f) => h("div", { class: "f-ctx" }, `⚠ ${FLAG[f] || f}`))),
      h("td", { class: "num" }, lineRef(m)));
  }));
  renderMemo();
  if (!S.sel) fill($("#fieldDetail"), h("p", { class: "muted" }, "Select a field — or click a highlighted value in the preview."));
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
  if (s.required_format && s.required_format !== "text") cons.push(`format: ${s.required_format}`);
  if (s.max_chars) cons.push(`≤ ${s.max_chars} chars`);
  if (s.constraints.allowed_values) cons.push(`options: ${s.constraints.allowed_values.join("／")}`);
  if (s.constraints.max_value) cons.push(`cap: ${Number(s.constraints.max_value).toLocaleString()}円`);
  if (s.constraints.multiple_of) cons.push(`multiple of ${s.constraints.multiple_of}`);
  if (s.constraints.pattern && s.required_format !== "choice") cons.push(`pattern: ${s.constraints.example || s.constraints.pattern}`);
  const quotes = (m.source_spans || []).map((sp) => h("div", { class: "quote" },
    h("b", {}, sp.line_start === sp.line_end ? `L${sp.line_start} ` : `L${sp.line_start}–${sp.line_end} `), `「${sp.text}」`));
  const attempts = (m.attempts || []).map((a) => h("li", { class: a.passed ? "ok" : "err" },
    h("div", {}, h("b", {}, `#${a.attempt} `), a.action),
    a.value ? h("div", { class: "tv" }, `→ ${a.value}`) : null,
    ...(a.issues || []).filter((i) => i.severity === "error").map((i) => h("div", { class: "ti" }, `✗ ${i.code}: ${i.message}`)),
    ...(a.issues || []).filter((i) => i.severity === "warning").map((i) => h("div", { class: "ti", style: "color:var(--warn-ink)" }, `⚠ ${i.message}`))));
  fill($("#fieldDetail"), 
    h("div", { class: "card-h" }, s.label, h("span", {}, s.location_path)),
    h("div", {}, statusChip(m), " ", ...m.normalizations.map((f) => h("span", { class: "chip" }, FLAG[f] || f))),
    h("div", { class: "detail-value" }, m.filled_value || (m.suggested_value ? `（unverified suggestion: ${m.suggested_value}）` : "（not stated in the memo — left for the reviewer）")),
    cons.length ? h("div", { class: "f-ctx" }, `Printed rules found on the form: ${cons.join(" · ")}`) : null,
    ...(m.warnings || []).map((w) => h("div", { class: "f-ctx", style: "color:var(--warn-ink)" }, `⚠ ${w}`)),
    ...(m.errors || []).map((w) => h("div", { class: "f-ctx", style: "color:var(--bad-ink)" }, `✗ ${w}`)),
    quotes.length ? h("div", { style: "margin-top:8px" }, h("b", {}, "Verbatim source · 根拠（原文そのまま）"), ...quotes) : null,
    m.reasoning ? h("div", { class: "f-ctx" }, `Engine note: ${m.reasoning}`) : null,
    attempts.length ? h("div", { style: "margin-top:8px" }, h("b", {}, "Self-correction history · 自己修正の履歴"), h("ol", { class: "timeline" }, attempts)) : null,
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
    count.textContent = s.max_chars ? `${n}/${s.max_chars}` : `${n} chars`;
    count.classList.toggle("over", Boolean(s.max_chars && n > s.max_chars));
  };
  inp.addEventListener("input", update);
  update();
  const save = () => {
    const v = inp.value.trim();
    if (!v) { toast("Type a value first."); return; }
    if (v === m.filled_value) { toast("Unchanged."); return; }
    submitValues({ [s.slot_id]: v }, `Saved «${s.label}» — re-verified. Approval (if any) was cleared.`);
  };
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
  return h("div", { class: "edit-row" },
    h("label", { class: "f-ctx" }, m.status === "needs_human_input" ? "Enter value · 値を入力" : "Correct this value · 値を修正"),
    inp, count, h("button", { class: "btn btn-ghost", type: "button", onclick: save }, "Save edit / 値を修正"));
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
      h("h3", {}, h("span", {}, `${en} · ${ja}`), h("span", { class: ok === items.length ? "ok-i" : "bad-i" }, `${ok === items.length ? "✓" : "✗"} ${ok}/${items.length}`)),
      ...items.map((c) => h("div", { class: "ck" }, h("span", { class: c.passed ? "ok-i" : "bad-i" }, c.passed ? "✓" : "✗"),
        h("span", {}, h("b", {}, `${c.check_id} `), c.description), h("span", { class: "d" }, c.details))));
  }));
  $("#entryRows").replaceChildren(...r.report.entry_hashes.map((e) => {
    let res = h("span", { class: "res-bad" }, "✗ changed");
    if (e.role === "target") res = h("span", { class: "res-target" }, e.identical ? "target · unchanged" : "target · slot text only");
    else if (e.identical && e.raw_identical) res = h("span", { class: "res-ok" }, "✓ byte-identical");
    return h("tr", {}, h("td", {}, e.name), h("td", {}, e.role), h("td", {}, `${e.original_sha256.slice(0, 20)}…`), h("td", {}, res));
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
      h("div", { class: "card-h" }, `⚠ ${pending.length} field(s) need your input`, h("span", {}, "メモに根拠が無い項目は推測せず、人が入力します")),
      ...pending.map((m) => {
        const s = r.slots.find((x) => x.slot_id === m.slot_id);
        const inp = h("input", { type: "text", placeholder: m.suggested_value ? `suggestion: ${m.suggested_value}` : (s.constraints.example || "") });
        inputs[m.slot_id] = inp;
        const why = m.errors && m.errors.length ? `Rejected by the form's rules: ${m.errors.join(" / ")}` : "Not stated in the memo, so Form-Guard did not guess it.";
        return h("div", { class: "pending-item" }, h("b", {}, s.label), inp,
          h("span", { class: "f-ctx" }, [s.constraints.digits ? `${s.constraints.digits}桁 digits` : s.required_format,
            s.max_chars ? `≤${s.max_chars}` : ""].filter(Boolean).join(" · ")),
          h("div", { class: "why" }, why));
      }),
      h("div", { style: "margin-top:8px" }, h("button", { class: "btn btn-primary", type: "button", onclick: () => applyOverrides(inputs) }, "Apply & re-verify"))));
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
    const flags = (it.flags || []).map((f) => h("span", { class: "chip", style: ATTENTION.has(f) ? "border-color:var(--warn);color:var(--warn-ink)" : "" }, FLAG[f] || f));
    const slot = it.slot_id ? r.slots.find((x) => x.slot_id === it.slot_id) : null;
    return h("li", { class: `${it.kind === "global" ? "global" : ""}${blocked ? " blocked" : ""}` }, cb,
      h("div", {},
        it.slot_id ? h("div", {}, h("b", {}, it.label), " ＝ ", h("span", { class: "f-value" }, it.value || "（未記入）")) : null,
        h("div", { class: "q" }, it.prompt),
        it.source_quote ? h("div", { class: "quote" }, `「${it.source_quote}」`) : null,
        flags.length ? h("div", { class: "chips" }, flags) : null,
        m && it.slot_id ? h("a", { href: "#", onclick: (e) => { e.preventDefault(); selectSlot(it.slot_id, true); showTab("fields"); } }, "show source →") : null,
        slot && m && it.kind === "field" ? h("details", { class: "edit-inline" }, h("summary", {}, "✎ 値を修正 / Edit value"), editControl(slot, m)) : null));
  }));
  renderApproval();
  updateApprove();
}

async function applyOverrides(inputs) {
  const values = {};
  Object.entries(inputs).forEach(([k, el]) => { if (el.value.trim()) values[k] = el.value.trim(); });
  if (!Object.keys(values).length) { toast("Type a value first."); return; }
  await submitValues(values);
}

async function submitValues(values, okMessage) {
  busy(true, "Writing your values surgically and re-verifying…");
  try {
    const run = await api(`/api/runs/${S.run.run_id}/overrides`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ values }),
    });
    setRun(run, false);
    const still = run.report.pending_slots;
    toast(still.length ? `Still awaiting: ${still.join(", ")}` : (okMessage || "All fields filled — re-verified. Review the checklist."));
  } catch (e) { toast(`Re-verify failed: ${e.message}`); } finally { busy(false); }
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
  if (!r.can_approve) {
    msg.className = "msg err";
    msg.textContent = r.report.pending_slots.length
      ? `Download locked: ${r.report.pending_slots.length} field(s) still need your input.`
      : "Download locked: verification did not pass.";
  } else if (!allChecked) {
    msg.className = "msg";
    msg.textContent = `${required.filter((i) => S.checked.has(i)).length}/${required.length} items confirmed.`;
  } else if (!reviewer) {
    msg.className = "msg"; msg.textContent = "Enter the reviewer's name to approve.";
  } else {
    msg.className = "msg ok"; msg.textContent = "Ready to approve.";
  }
}

async function onApprove() {
  try {
    const res = await api(`/api/runs/${S.run.run_id}/approve`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      // bind the approval to exactly what is on screen: the server refuses a stale revision or file hash
      body: JSON.stringify({ reviewer: $("#reviewerInput").value.trim(), checked: [...S.checked],
        filled_sha256: S.run.filled.sha256, revision: S.run.revision }),
    });
    S.run.approval = res;
    renderHitl();
    toast("Approved — the verified file is unlocked.");
  } catch (e) {
    const missing = e.body && e.body.detail && e.body.detail.missing;
    toast(missing ? `Unchecked items: ${missing.length}` : `Approval refused: ${e.message}`);
    if (e.status === 409 && S.run) {  // stale view: reload the run so the reviewer sees the current state
      try { setRun(await api(`/api/runs/${S.run.run_id}`), false); } catch (_) { /* keep the old view */ }
    }
  }
}

function renderApproval() {
  const r = S.run;
  const dl = $("#downloadBtn");
  const msg = $("#approveMsg");
  if (r.approval) {
    dl.classList.remove("disabled");
    dl.removeAttribute("aria-disabled");
    dl.href = r.links.download;
    dl.textContent = `⬇ Download verified .${r.form.file_type}`;
    msg.className = "msg ok";
    fill(msg, h("div", {}, `✓ Approved by ${r.approval.reviewer} at ${r.approval.approved_at}`),
      h("div", { class: "receipt" }, `SHA-256 ${r.approval.filled_sha256}`));
    $("#approveBtn").disabled = true;
  } else {
    dl.classList.add("disabled");
    dl.setAttribute("aria-disabled", "true");
    dl.removeAttribute("href");
    dl.textContent = "⬇ Download verified file (locked)";
  }
}
