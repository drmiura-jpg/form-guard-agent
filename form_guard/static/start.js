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
    state.discoveryId = d.discovery_id; state.discoveryKey = key; resetRun();
    return `記入欄の候補を ${d.candidates.length} か所見つけました。`;
  }
  const id = $("#sample").value, key = `sample:${id}:${m}`;
  if (state.discoveryId && state.discoveryKey === key) return null;
  const d = await api("/api/discover/demo", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ demo_id: id, mode: m }),
  });
  if (my !== state.seq) return "stale";
  state.discoveryId = d.discovery_id; state.discoveryKey = key; resetRun();
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
    if (auto() && $("#memo").value.trim()) { setBusy(false); await runFill(); return; }
  } catch (e) { msg("#msg2", "読み込めませんでした：" + e.message, true); }
  finally { setBusy(false); }
});

// STEP 3 — fill (おまかせ: then self-check and jump to the human checks)
async function runFill() {
  const memo = $("#memo").value.trim();
  if (!memo) return msg("#msg3", "材料の文章を貼り付けてください。", true);
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

// STEP 5 — one checkbox per required checklist item; only the ticked ids are sent
function prepareStep5() {
  const r = state.run, rep = r.report || {};
  const items = (rep.hitl_checklist || []).filter((h) => h.required);
  const ok = rep.overall_status === "PASS" && r.can_approve;
  $("#finalSummary").innerHTML =
    `<p class="big">${ok ? "✅ 記入完了・機械検査に合格" : "❌ このままでは承認できません"}</p>
     <p class="hint">様式：${esc(r.display_name || "")}　/　記入した欄：${(r.mappings || []).length} か所　/
     <a href="/api/runs/${encodeURIComponent(r.run_id)}/report" target="_blank" rel="noopener">検査レポート（指定欄以外の比較表示）を見る</a></p>`;
  $("#checklist").innerHTML = items.map((h) => {
    const att = (h.flags || []).length || h.importance === "high";
    const flags = (h.flags || []).map((f) => `<span class="pill warn">${esc(f)}</span>`).join("");
    return `<li class="${att ? "attention" : ""}"><label><input type="checkbox" class="ck" value="${esc(h.item_id)}">
      <span>${esc(h.prompt_ja || h.prompt)} ${flags}${h.source_quote ? `<br><span class="hint">根拠：「${esc(h.source_quote)}」</span>` : ""}</span></label></li>`;
  }).join("");
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
  if (!runId || !/^\d{8}-\d{6}-[0-9a-f]{6}$/.test(runId)) return;
  setBusy(true);
  try {
    state.run = await api(`/api/runs/${encodeURIComponent(runId)}`);
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
