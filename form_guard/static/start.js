// Form-Guard かんたん5ステップ: a guided front end over the existing API (discover → fill → review → approve).
// "おまかせ" mode chains discover → fill → self-check automatically; a person still confirms every checklist item.
"use strict";
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const LOCAL = ["127.0.0.1", "localhost", "[::1]", "::1"];
const state = { step: 1, run: null, discoveryId: null, discoveryKey: "", busy: false, seq: 0, modeTouched: false };

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function msg(id, text, err) { const el = $(id); el.textContent = text || ""; el.className = "msg" + (err ? " err" : ""); }
const JA = {
  "forbidden origin": "別のサイトからの操作は受け付けません。",
  "passphrase required": "合言葉が必要です。合言葉付きのリンクから開き直してください。",
  "fields still awaiting human input": "まだ入力待ちの欄があります。",
  "verification did not pass — download stays blocked": "機械検査が不合格のため承認できません。",
  "every checklist item must be confirmed": "確認していない項目があります。",
  "reviewer name is required": "確認した人の名前を入れてください。",
};
async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...opts });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = body.detail;
    const raw = typeof d === "string" ? d : (d && d.message) || "";
    if (/stale|modified/.test(raw)) throw new Error("内容が更新されています。もう一度「反映して再検査」からやり直してください。");
    if (res.status === 422 && /0 confirmed|no candidate|confirmed/.test(raw))
      throw new Error("記入できる欄を見つけられませんでした。詳しい画面（右上のリンク）で欄を選んでください。");
    throw new Error(JA[raw] || raw || `エラー（${res.status}）`);
  }
  return body;
}
function setBusy(on) {
  state.busy = on;
  $$("button").forEach((b) => { if (b.id !== "toStep2" || !on) b.disabled = on; });
  $("#sample").disabled = on; $("#file").disabled = on;
  $$("#fields input[data-slot]").forEach((i) => { i.disabled = on; });
  if (!on && state.fromChat) $("#runBtn").disabled = true;
  if (!on) $("#approveBtn").disabled = !(state.run && state.run.can_approve && state.run.report.overall_status === "PASS");
}
function go(n) {
  state.step = n;
  $$("section.card").forEach((c) => c.classList.toggle("on", +c.dataset.s === n));
  $$("#steps li").forEach((li) => { const s = +li.dataset.s; li.className = s === n ? "on" : s < n ? "done" : ""; });
  window.scrollTo(0, 0);
}
function markAuto(n) { $$("#steps li").forEach((li) => { if (+li.dataset.s === n) li.className = "auto"; }); }
const mode = () => (document.querySelector("input[name=mode]:checked") || {}).value || "demo";
const src = () => document.querySelector("input[name=src]:checked").value;
const auto = () => $("#autoMode").checked;
$$("[data-back]").forEach((b) => b.addEventListener("click", () => { if (!state.busy) go(+b.dataset.back); }));

// public tunnel notice: fictional samples only
if (!LOCAL.includes(location.hostname)) {
  $("#banner").innerHTML = '<div class="banner">⚠️ 一時公開URLで開いています。架空のサンプル様式で試してください。患者さんの情報・個人情報・未公開の資料は入れないでください。</div>';
  $("#srcSample").checked = true;
}

// STEP 1 — the user's own choice is never overwritten by a late health reply
$$("input[name=mode]").forEach((r) => r.addEventListener("change", () => { state.modeTouched = true; }));
(async () => {
  try {
    const h = await api("/api/health");
    if (h.live_available) {
      $("#aiStatus").innerHTML = '<span class="pill ok">API キー設定あり（実際の接続は記入時に確認します）</span>';
      if (!state.modeTouched) $("#modeLive").checked = true;
    } else {
      $("#aiStatus").innerHTML = '<span class="pill warn">API キー未設定のため、お試しモードで動きます</span>';
      $("#modeLive").disabled = true;
      $("#modeDemo").checked = true;
    }
  } catch (e) {
    $("#aiStatus").innerHTML = `<span class="pill ng">サーバーに接続できません：${esc(e.message)}</span>`;
    $("#modeDemo").checked = true; $("#modeLive").disabled = true;
  }
  $("#toStep2").disabled = false;
})();
$("#toStep2").addEventListener("click", () => go(2));

// STEP 2 — picking a file / sample also selects its radio; memo samples never leak into an upload
$("#sample").addEventListener("change", () => { $("#srcSample").checked = true; });
$("#file").addEventListener("change", () => { $("#srcUpload").checked = true; });
$("#memo").addEventListener("input", () => { $("#memo").dataset.sample = "0"; });
$("#makeTemplate").addEventListener("click", () => {
  if (state.busy) return;
  if ($("#memo").value.trim() && $("#memo").dataset.template !== "1" && !confirm("いまのメモを、書式から作ったひな形に置き換えますか？")) return;
  const ok = fillTemplate();
  msg("#msg3", ok ? "📝 書式の記入欄からメモのひな形を作りました。「：」の右側に書いてください。" : "先に STEP 2 で書式を入れてください。", !ok);
});

const WD = "日月火水木金土";
function todayJa() { const d = new Date(); return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日（${WD[d.getDay()]}）`; }
function templateText() {
  const rows = (state.candidates || []).filter((c) => c.recommended);
  const hint = (c) => {
    const f = c.required_format || "";
    if (/申請日|届出日|提出日|記入日/.test(c.label) && /date/.test(f)) return todayJa();
    return "";
  };
  const note = (c) => {
    const f = c.required_format || "", parts = [];
    if (f === "wareki_date") parts.push("例：2026年11月10日（火）");
    if (f === "wareki_period") parts.push("例：2026年11月16日（月）から2026年11月27日（金）まで");
    if (f === "yen") parts.push("例：48,000円");
    if (f === "person_name") parts.push("氏名だけ");
    if (f === "choice") parts.push("様式の選択肢から1つ");
    if (c.max_chars) parts.push(`${c.max_chars}字以内`);
    return parts.length ? `　←（${parts.join("・")}）` : "";
  };
  return "【記入メモ】このメモの「：」の右側に書いてください。書かなかった欄は空欄のまま、あとで入力できます。\n\n" +
    rows.map((c) => `${c.label.replace(/\s+/g, "")}：${hint(c)}${hint(c) ? "" : note(c)}`).join("\n") + "\n";
}
function fillTemplate() {
  if (!(state.candidates || []).some((c) => c.recommended)) return false;
  $("#memo").value = templateText(); $("#memo").dataset.sample = "0"; $("#memo").dataset.template = "1";
  return true;
}
function resetRun() { state.run = null; $("#fields").innerHTML = ""; $("#checklist").innerHTML = ""; }

async function discover() {
  const my = ++state.seq, m = mode();
  if (src() === "upload") {
    const f = $("#file").files[0];
    if (!f) throw new Error("様式ファイルを選んでください。");
    if ($("#memo").dataset.sample === "1") { $("#memo").value = ""; $("#memo").dataset.sample = "0"; }
    const key = `upload:${f.name}:${f.size}:${f.lastModified}:${m}`;
    if (state.discoveryId && state.discoveryKey === key) return null;
    const fd = new FormData(); fd.append("file", f); fd.append("mode", m);
    const d = await api("/api/discover", { method: "POST", body: fd });
    if (my !== state.seq) return "stale";
    state.discoveryId = d.discovery_id; state.discoveryKey = key; state.candidates = d.candidates || []; resetRun();
    if (!$("#memo").value.trim()) fillTemplate();
    return `記入欄の候補を ${d.candidates.length} か所見つけました。`;
  }
  const id = $("#sample").value, key = `sample:${id}:${m}`;
  if (state.discoveryId && state.discoveryKey === key) return null;
  const d = await api("/api/discover/demo", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ demo_id: id, mode: m }),
  });
  if (my !== state.seq) return "stale";
  state.discoveryId = d.discovery_id; state.discoveryKey = key; state.candidates = d.candidates || []; resetRun();
  if (!$("#memo").value.trim() || $("#memo").dataset.sample === "1") { $("#memo").value = d.memo_text || ""; $("#memo").dataset.sample = "1"; }
  return `「${d.title}」を読み込みました。記入欄の候補 ${d.candidates.length} か所。`;
}

$("#toStep3").addEventListener("click", async () => {
  if (state.busy) return;
  msg("#msg2", "書式を読み込んで、記入欄を探しています…");
  setBusy(true);
  try {
    const r = await discover();
    if (r === "stale") return;
    msg("#msg2", r || "");
    go(3);
    if ($("#memo").dataset.template === "1") {
      msg("#msg3", "📝 書式から記入メモのひな形を作りました。「：」の右側に書いて「AIに記入してもらう」を押してください。");
      $("#memo").focus(); return;
    }
    if (auto() && $("#memo").value.trim()) { setBusy(false); await runFill(); return; }
  } catch (e) { msg("#msg2", "読み込めませんでした：" + e.message, true); }
  finally { setBusy(false); }
});

// STEP 3 — fill (おまかせ: then self-check and jump to the human checks)
// the template's guide text (instruction line and "←（例：…）" notes) is never sent as material
// and a template line left empty ("所属機関：") is dropped, so the next line can never be read as its value
const cleanMemo = (s) => s.replace(/^【記入メモ】.*$/m, "").replace(/[ \u3000]*←（[^\n]*$/gm, "")
  .split("\n").filter((ln) => !/^[^：:\n]{1,40}[：:][ \u3000]*$/.test(ln)).join("\n").trim();
async function runFill() {
  if (state.fromChat || !state.discoveryId) return msg("#msg3", state.fromChat ? "材料を変えて記入し直すときは、元のチャットで依頼してください。" : "先に STEP 2 で書式を入れてください。", true);
  const memo = cleanMemo($("#memo").value);
  if (!memo || !/：\s*\S/.test(memo) && $("#memo").dataset.template === "1")
    return msg("#msg3", "メモの「：」の右側に、少なくとも1つ書いてください。", true);
  if (state.busy) return;
  setBusy(true); msg("#msg3", "");
  const my = ++state.seq;
  try {
    if (auto()) { markAuto(3); $("#progress").textContent = "🤖 AI が記入しています（指定欄だけを差し替え、ほかは触りません）…"; }
    else $("#progress").textContent = "AI が記入しています…";
    const run = await api("/api/runs/from-discovery", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ discovery_id: state.discoveryId, confirmed_targets: "recommended", memo_text: memo, mode: mode() }),
    });
    if (my !== state.seq) return;
    state.run = run; renderReview();
    const rep = run.report || {};
    if (auto() && !(rep.pending_slots || []).length && rep.overall_status === "PASS") {
      $("#progress").textContent = "🤖 自己チェック完了：機械検査に合格。人の確認項目へ進みます。";
      markAuto(4); prepareStep5(); go(5); markAuto(3); markAuto(4);
    } else {
      $("#progress").textContent = auto() ? "🤖 材料にない欄があるため、ここで人の入力をお願いします。" : "";
      go(4);
    }
  } catch (e) { $("#progress").textContent = ""; msg("#msg3", "記入できませんでした：" + e.message, true); }
  finally { setBusy(false); }
}
$("#runBtn").addEventListener("click", runFill);

// STEP 4 — the shown value is what the file contains; only edited cells are sent
const needsAttention = (m) => m.status !== "filled" || (m.confidence ?? 1) < 0.8 || (m.warnings || []).length || (m.errors || []).length;
function renderReview() {
  const r = state.run, rep = r.report || {};
  const pending = (rep.pending_slots || []).length;
  const status = rep.overall_status;
  const failed = (rep.checks || []).filter((c) => !c.passed);
  $("#checkSummary").innerHTML =
    (status === "PASS" ? '<span class="pill ok">✅ 機械検査に合格（最後は人が確認）</span>'
      : status === "NEEDS_HUMAN_INPUT" ? '<span class="pill warn">✏️ 人の入力待ち</span>'
      : `<span class="pill ng">❌ 機械検査で不合格 ${failed.length} 件</span>`) +
    `<span class="pill">検査 ${rep.passed_count ?? "?"} 項目合格</span>` +
    (pending ? `<span class="pill warn">入力待ち ${pending} 欄</span>` : "") +
    failed.map((c) => `<br><span class="pill ng">${esc(c.check_id)}：${esc(c.description)}（${esc(c.details)}）</span>`).join("");
  const labels = Object.fromEntries((r.slots || []).map((s) => [s.slot_id, s.label || s.slot_id]));
  const rows = [...(r.mappings || [])].sort((a, b) => needsAttention(b) - needsAttention(a));
  $("#fields").innerHTML = rows.map((m) => {
    const val = m.filled_value ?? "";
    const att = needsAttention(m);
    const notes = [...(m.warnings || []), ...(m.errors || [])].map((w) => `<span class="pill warn">${esc(w)}</span>`).join("");
    const conf = m.confidence != null ? `<span class="pill ${m.confidence < 0.8 ? "warn" : "ok"}">自信 ${Math.round(m.confidence * 100)}%</span>` : "";
    return `<tr class="${att ? "attention" : ""}"><td>${att ? "🟧 " : ""}${esc(labels[m.slot_id] || m.slot_id)}${m.status !== "filled" && !val ? '<br><span class="pill warn">入力してください</span>' : ""}${m.status === "human_entered" ? '<br><span class="pill">人が入力</span>' : ""}</td>
      <td><input type="text" data-slot="${esc(m.slot_id)}" data-orig="${esc(val)}" value="${esc(val)}" placeholder="${esc(m.suggested_value || "（材料に記載なし）")}"></td>
      <td class="src">${m.source_quote ? "「" + esc(m.source_quote) + "」" : "—"} ${conf} ${notes}</td></tr>`;
  }).join("");
  $$("#fields input[data-slot]").forEach((i) => i.addEventListener("input", () => i.classList.toggle("dirty", isDirty(i))));
  $("#checklist").innerHTML = ""; // a new run or revision always needs fresh human checks
}
const isDirty = (i) => i.value.trim() !== (i.dataset.orig || "").trim();
const dirtyInputs = () => $$("#fields input[data-slot]").filter(isDirty);

$("#saveEdits").addEventListener("click", async () => {
  if (state.busy) return;
  const values = {};
  dirtyInputs().forEach((i) => { values[i.dataset.slot] = i.value.trim(); });
  if (!Object.keys(values).length) return msg("#msg4", "書き換えた欄がありません。", true);
  setBusy(true); msg("#msg4", "反映して、検査し直しています…");
  const my = ++state.seq, runId = state.run.run_id;
  try {
    const run = await api(`/api/runs/${encodeURIComponent(runId)}/overrides`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ values }),
    });
    if (my !== state.seq || run.run_id !== runId) return;
    state.run = run; renderReview(); msg("#msg4", "反映しました。");
  } catch (e) { msg("#msg4", "反映できませんでした：" + e.message, true); }
  finally { setBusy(false); }
});

// STEP 5 — 詳細レポートでチェック: plain-language verdict, machine checks, before/after images, one big card per
// required checklist item (only the ticked ids are sent)
const CATS = {
  zip: ["📦 ファイルが壊れていない", "書類ファイルの中身が欠けたり壊れたりしていないか"],
  xml: ["🧩 ファイルの中の形が正しい", "Word・Excel 用の正しい形のままか"],
  layout: ["📐 罫線・枠・書式が変わっていない", "表の枠、罫線、文字の大きさ、余白の設定が白紙と同じか"],
  text: ["📝 印刷された文字が変わっていない", "注意書き・誓約文・項目名が1文字も変わっていないか"],
  grounding: ["🔎 値の根拠が材料にある", "AI が書いた値の根拠（引用）が材料にそのまま見つかるか（和暦・金額の書き方は直すことがあります）。人が入力した値は対象外です"],
  constraints: ["📏 書き方のルールを守っている", "文字数の上限、和暦の日付、金額の書き方などのルール"],
  application: ["💻 別の読み込み用プログラムで開き直せる", "別の仕組みで読み直しても正しく読めるか（Word・Excel の実際の表示は、ダウンロード後に人が確認します）"],
  visual: ["👀 簡易表示で見た目のずれがない", "内蔵の簡易表示で、記入欄の外がずれていないか（実際の表示・印刷はダウンロード後に確認）"],
};
const CATS_PDF = {
  zip: ["📦 PDF が壊れていない", "白紙の PDF がそのまま先頭に残り、記入分だけが後ろに足されているか"],
  xml: ["🧩 元のページの中身がそのまま", "白紙のページの描画がそのままで、記入は指定の枠の中だけか"],
  application: ["💻 別の読み方でも開ける", "別の仕組みで PDF を読み直しても正しく読めるか（実際の表示はダウンロード後に確認）"],
};
function catOf(c) { return (c.category || c.check_id || "").toLowerCase(); }
const svgImg = (svg, alt) => svg ? `<img alt="${esc(alt)}" src="data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}">` : "<p class='hint'>（画像なし）</p>";
let previewView = "filled";
function renderPreview() {
  const pv = (state.run && state.run.previews) || {};
  const blank = `<figure><figcaption>記入前（白紙）</figcaption>${svgImg(pv.blank, "記入前")}</figure>`;
  const filled = `<figure><figcaption>記入後（AI が書いたもの）</figcaption>${svgImg(pv.filled, "記入後")}</figure>`;
  $("#preview").innerHTML = previewView === "both" ? blank + filled : previewView === "blank" ? blank : filled;
  $$(".rp-tab").forEach((b) => b.classList.toggle("on", b.dataset.view === previewView));
}
$$(".rp-tab").forEach((b) => b.addEventListener("click", () => { previewView = b.dataset.view; renderPreview(); }));
function updateProgress() {
  const all = $$("#checklist .ck"), done = all.filter((c) => c.checked);
  all.forEach((c) => c.closest("li").classList.toggle("done", c.checked));
  $("#ckProgress").textContent = `（${done.length} / ${all.length} 確認済み）`;
  $("#ckBar").style.width = all.length ? `${(100 * done.length) / all.length}%` : "0";
}
function plainPrompt(h, labels) {
  if (h.item_id === "global:visual") return ["👀 書類の見た目", "上の「② 書類の見た目を比べる」で、色の枠の外（罫線・注意書き・誓約文）が白紙と同じことを目で確かめた"];
  if (h.item_id === "global:responsibility") return ["🙋 最終判断は自分がする", "AI の記入は下書きです。内容が正しいか、提出してよいかは自分が判断します"];
  const label = labels[h.slot_id] || h.label || h.slot_id || "";
  return [`「${label}」の欄`, String(h.prompt_ja || h.prompt || "").replace(/原文\s*L(\d+)(?:[–-]L?(\d+))?/g,
    (_, a, b) => `材料の${a}${b ? "〜" + b : ""}行目`)];
}
const FLAG_JA = {
  wareki: "西暦を和暦に直して書きました", weekday_ok: "曜日が日付と合っているのを確認済み", weekday: "曜日を付け足しました",
  halfwidth: "全角の英数字を半角に直しました", yen: "金額を「〇〇円」の書き方にそろえました（例：48万円→480,000円）",
  order: "選んだ項目を様式の順に並べました", name: "氏名の後ろの役職・かっこ書きを外しました",
  note: "値の後ろの注記（かっこ書き）を外しました", checkbox: "□ を ■ にして選びました", human: "人が入力した値です",
  year_inferred: "⚠️ 年が書いていなかったので AI が推定しました。年が正しいか見てください",
  condensed: "⚠️ 文字数の上限に収めるため、材料の文を一部けずりました。大事な言葉が消えていないか見てください",
  re_extracted: "⚠️ 様式のルール（上限・選択肢など）に合わなかったので、材料から別の値を選び直しました。材料と合っているか見てください",
  re_anchored: "⚠️ 根拠の引用を材料の別の行に付け替えました。言葉が合っているか見てください",
  unsafe_condensation: "⚠️ 短くすると大事な条件が消えるため、AI は書かずに人の判断に回しました",
  advance_notice: "⚠️ 期限（〇日前までに提出など）のルールに日付が合っていません。日付を見てください",
  low_conf: "⚠️ AI の自信が低い欄です。特によく見てください",
  recovered_unverified: "⚠️ ファイルから読み取った値で、根拠は未確認です。材料と照らしてください",
};
const flagPill = (f) => {
  const t = FLAG_JA[f] || `⚠️ 確認の印（${f}）`;
  return `<span class="pill ${t.startsWith("⚠️") ? "warn" : "ok"}">${esc(t)}</span>`;
};
function prepareStep5() {
  const r = state.run, rep = r.report || {}, vis = r.visual || {};
  const items = (rep.hitl_checklist || []).filter((h) => h.required);
  const ok = rep.overall_status === "PASS" && r.can_approve;
  const checks = rep.checks || [], failed = checks.filter((c) => !c.passed);
  const autoN = (r.mappings || []).filter((m) => m.status === "filled" && m.filled_value).length;
  const humanN = (r.mappings || []).filter((m) => m.status === "human_entered" && m.filled_value).length;
  const isPdf = (r.form || {}).file_type === "pdf";
  const compared = vis.boxes_compared != null ? `記入欄の外 ${vis.boxes_compared} か所を比べて、ずれ ${vis.drift_count ?? 0} か所` : "";
  $("#verdict").innerHTML = ok
    ? `<div class="rp-verdict ok">✅ 機械の検査にすべて合格しました。<br>記入したのは指定の欄だけです（AI ${autoN} 項目${humanN ? `・人が入力 ${humanN} 項目` : ""}）。<small>機械の検査 ${checks.length} 項目に合格。${esc(compared)}。内容と実際の表示・印刷は、あなたが確認してください。下の確認項目をすべて押すと完成です。</small></div>`
    : `<div class="rp-verdict ${failed.length ? "ng" : "warn"}">${failed.length ? `❌ 機械の検査で ${failed.length} 項目が不合格です` : "✏️ まだ入力待ちの欄があります"}<small>このままでは承認できません。「記入内容の修正に戻る」から直してください。</small></div>`;
  const groups = {};
  checks.forEach((c) => { const k = catOf(c); (groups[k] = groups[k] || []).push(c); });
  if (!groups.visual && typeof vis.identical_outside_slots === "boolean")  // never turns a server failure green
    groups.visual = [{ passed: vis.identical_outside_slots && !(vis.overflow_count > 0), check_id: "VIS",
                       details: vis.identical_outside_slots ? "" : `記入欄の外のずれ ${vis.drift_count ?? "?"} か所` }];
  $("#machineCards").innerHTML = Object.entries(groups).map(([k, list]) => {
    const bad = list.filter((c) => !c.passed).length, [title, sub] = (isPdf && CATS_PDF[k]) || CATS[k] || [k, ""];
    return `<div class="rp-card ${bad ? "ng" : "ok"}"><b>${bad ? "❌" : "✅"} ${esc(title)}</b><span>${esc(sub)}</span>
      ${bad ? `<br><span>不合格 ${bad} 件：${list.filter((c) => !c.passed).map((c) => esc(c.details || c.check_id)).join(" / ")}</span>` : ""}</div>`;
  }).join("");
  renderPreview();
  const labels = Object.fromEntries((r.slots || []).map((s) => [s.slot_id, s.label || s.slot_id]));
  $("#checklist").innerHTML = items.map((h) => {
    const att = (h.flags || []).length || h.importance === "high";
    const flags = (h.flags || []).map(flagPill).join("");
    const [title, text] = plainPrompt(h, labels);
    const value = h.kind === "field" ? `<span class="val">${esc(h.value || "（空欄）")}</span>` : "";
    const why = h.source_quote ? `<span class="why">材料に書いてあったこと：「${esc(h.source_quote)}」</span>` : "";
    return `<li class="${att ? "attention" : ""}"><label><input type="checkbox" class="ck" value="${esc(h.item_id)}">
      <span><b>${esc(title)}</b>${value}<span class="why">${esc(text)}</span>${why}${flags}</span></label></li>`;
  }).join("");
  $$("#checklist .ck").forEach((c) => c.addEventListener("change", updateProgress));
  updateProgress();
  $("#techReport").href = `/api/runs/${encodeURIComponent(r.run_id)}/report`;
  $("#finalSummary").textContent = r.display_name || "";
  $("#checklist").dataset.run = `${r.run_id}:${r.revision}:${r.filled.sha256}`;
  $("#approveBtn").disabled = !ok;
  msg("#msg5", ok ? "" : "機械検査に合格していないか、入力待ちの欄があります。STEP 4 に戻って直してください。", !ok);
}

$("#toStep5").addEventListener("click", () => {
  if (state.busy || !state.run) return;
  if (dirtyInputs().length) return msg("#msg4", "書き換えた値がまだ反映されていません。「反映して再検査」を押してから進んでください。", true);
  const pending = ((state.run.report || {}).pending_slots || []).length;
  if (pending) return msg("#msg4", `まだ入力待ちの欄が ${pending} つあります。値を入れて「反映して再検査」を押してください。`, true);
  msg("#msg4", ""); prepareStep5(); go(5);
});

$("#approveBtn").addEventListener("click", async () => {
  if (state.busy) return;
  const r = state.run, rep = r.report || {};
  if ($("#checklist").dataset.run !== `${r.run_id}:${r.revision}:${r.filled.sha256}`) {
    prepareStep5(); return msg("#msg5", "内容が更新されたので、確認項目をもう一度チェックしてください。", true);
  }
  const reviewer = $("#reviewer").value.trim();
  if (!reviewer) return msg("#msg5", "確認した人の名前を入れてください。", true);
  const checked = $$("#checklist .ck:checked").map((c) => c.value);
  const required = (rep.hitl_checklist || []).filter((h) => h.required).length;
  if (checked.length < required) return msg("#msg5", `確認していない項目があと ${required - checked.length} つあります。`, true);
  setBusy(true);
  try {
    const a = await api(`/api/runs/${encodeURIComponent(r.run_id)}/approve`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reviewer, checked, filled_sha256: r.filled.sha256, revision: r.revision }),
    });
    if (state.run !== r) return;
    msg("#msg5", "承認しました。ダウンロードを始めます。");
    window.location.href = a.download_url;
  } catch (e) { msg("#msg5", "承認できませんでした：" + e.message, true); }
  finally { setBusy(false); }
});

go(1);

// Opened from a Claude chat (MCP form_guard_fill → /start?run_id=…): the AI already filled and verified the draft;
// load it and go straight to the human checks (STEP 4 when something is missing, otherwise STEP 5).
(async () => {
  const runId = new URLSearchParams(location.search).get("run_id");
  if (runId === null) return;
  if (!/^\d{8}-\d{6}-[0-9a-f]{6}$/.test(runId)) {
    $("#aiStatus").insertAdjacentHTML("beforeend", '<span class="pill ng">下書きリンクの形式が正しくありません。元のチャットからリンクをコピーし直してください。</span>');
    return;
  }
  setBusy(true);
  try {
    state.run = await api(`/api/runs/${encodeURIComponent(runId)}`);
    state.fromChat = true;
    $("#memo").value = ((state.run.memo || {}).text) || ""; $("#memo").readOnly = true; $("#memo").dataset.sample = "0";
    $("#runBtn").disabled = true;
    msg("#msg3", "この下書きは Claude とのチャットで作られました。材料を変えて記入し直すときは、元のチャットで依頼してください（欄ごとの修正は STEP 4 でできます）。");
    renderReview();
    [1, 2, 3].forEach(markAuto);
    const rep = state.run.report || {};
    $("#progress").textContent = "🤖 Claude がチャットから記入・自己チェックした下書きを読み込みました。";
    if (!(rep.pending_slots || []).length && rep.overall_status === "PASS" && state.run.can_approve) { prepareStep5(); go(5); }
    else go(4);
    [1, 2, 3].forEach(markAuto);
  } catch (e) {
    $("#aiStatus").insertAdjacentHTML("beforeend", `<span class="pill ng">下書き ${esc(runId)} を読み込めません：${esc(e.message)}</span>`);
  } finally { setBusy(false); }
})();
