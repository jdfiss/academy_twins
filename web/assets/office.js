// 系辦端（A 組）：畢審總表、學生明細與人工覆核、AI 解析儀表板、規則（草擬、版本啟用、來源）
let BATCH = [], statusFilter = null, flagFilter = "";
const FLAG_LABELS = { overflow: "超修學分", repeatable: "可重複修習", substitution_pending: "待辦抵修" };
const show = setupTabs("twin-office-view");

// ---------- 共用：承辦人（全頁填一次）與訊息 ----------
const officer = () => $("#officer").value.trim();
try { $("#officer").value = localStorage.getItem("twin-reviewer") || ""; } catch {}
$("#officer").addEventListener("change", () => { try { localStorage.setItem("twin-reviewer", officer()); } catch {} });

function msg(el, html, kind = "") {
  $(el).innerHTML = html ? `<div class="msg ${kind}">${html}</div>` : "";
}

function needOfficer(el) {
  if (officer()) return true;
  msg(el, "請先在右上角填寫承辦人。", "err");
  $("#officer").focus();
  return false;
}

// 伺服器時間（ISO）顯示為本地「YYYY-MM-DD HH:MM」
function when(iso) {
  const d = new Date(iso);
  if (!iso || isNaN(d)) return esc(iso || "");
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

const errorList = (e) => `<b>${esc(e.message)}</b>`
  + (e.details?.length ? `<ul class="upload-errors">${e.details.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>` : "");

const postJSON = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body) });

async function pdfBase64(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

// ---------- 畢審總表 ----------
function renderBatch() {
  const counts = { PASS: 0, WARNING: 0, FAIL: 0, MANUAL_REVIEW: 0 };
  BATCH.forEach((s) => counts[s.status]++);
  $("#stats").innerHTML = Object.keys(counts).map((k) => `
    <div class="card stat ${statusFilter === k ? "selected" : ""}" data-status="${k}">
      <div>${chip(k)}</div><div class="n">${counts[k]}</div>
    </div>`).join("");
  document.querySelectorAll(".stat").forEach((el) => el.addEventListener("click", () => {
    statusFilter = statusFilter === el.dataset.status ? null : el.dataset.status;
    renderBatch();
  }));
  const rows = BATCH.filter((s) => {
    if (statusFilter && s.status !== statusFilter) return false;
    const flags = s.flags || [];
    if (flagFilter === "any") return flags.length > 0;
    if (flagFilter === "actions") return s.action_count > 0;
    if (flagFilter === "none") return !flags.length && !s.action_count;
    return !flagFilter || flags.some((f) => f.type === flagFilter);
  });
  $("#batch-filter-count").textContent = `${rows.length} / ${BATCH.length} 位`;
  $("#batch-rows").innerHTML = rows.map((s) => {
    const pct = s.total_required ? Math.min(100, (s.total_credits / s.total_required) * 100) : 0;
    const types = [...new Set((s.flags || []).map((f) => f.type))];
    return `<tr data-id="${esc(s.id)}">
      <td><b>${esc(s.id)}</b></td>
      <td>${chip(s.status)}</td>
      <td><div>${s.total_credits}</div><div class="meter ${pct >= 100 ? "ok" : ""}"><div style="width:${pct}%"></div></div></td>
      <td class="hide-sm">${s.satisfied} / ${s.requirements}</td>
      <td>${reasonList(s.reasons)}${s.reviewed ? `<div class="hint">已覆核 ${s.reviewed} 筆</div>` : ""}</td>
      <td><div class="flags">${types.map((t) => `<span class="flag">${esc(FLAG_LABELS[t] || t)}</span>`).join("")}
        ${s.action_count ? `<span class="flag">待辦 ${s.action_count}</span>` : ""}</div>
        ${!types.length && !s.action_count ? '<span class="hint">—</span>' : ""}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="6" class="empty">沒有符合的學生</td></tr>`;
  document.querySelectorAll("#batch-rows tr[data-id]").forEach((tr) =>
    tr.addEventListener("click", () => { loadStudent(tr.dataset.id); show("student"); }));
}

// 原因最多列 3 條，其餘在學生明細看
function reasonList(reasons) {
  if (!reasons.length) return '<span class="hint">全部符合</span>';
  const more = reasons.length - 3;
  return reasons.slice(0, 3).map(esc).join("<br>") + (more > 0 ? `<div class="hint">另 ${more} 項</div>` : "");
}

$("#flag-filter").addEventListener("change", (e) => {
  flagFilter = e.target.value;
  renderBatch();
});

function renderRoster(rows) {
  $("#roster-card").hidden = !rows.length;
  $("#roster-rows").innerHTML = rows.map((a) =>
    `<tr><td><b>${a.id ? esc(a.id) : "—"}</b></td><td>${esc(a.message)}</td></tr>`).join("");
}

// ---------- 學生明細 ----------
async function loadStudent(id) {
  $("#student-select").value = id;
  const s = await api(`/api/students/${encodeURIComponent(id)}`);
  $("#student-status").innerHTML = chip(s.status);
  $("#student-credits").textContent = s.total_credits;
  const target = s.requirements.find((r) => r.kind === "total_credits")?.need;
  $("#student-total-target").textContent = target == null ? "學分" : `/ ${target} 學分`;
  $("#student-report").href = `/api/students/${encodeURIComponent(s.id)}/report`;
  $("#student-twin").href = `student.html?id=${encodeURIComponent(s.id)}`;
  renderBreakdown(s, target ?? 0);
  renderIssues(s);
  renderRequirements(s);
  renderRecords(s);
}

function renderIssues(s) {
  const iss = s.issues, out = [];
  const related = (x) => `<div class="hint">相關要求：${x.requirement_ids.map(esc).join("、") || "待確認"}；課程：${x.courses.map(esc).join("、") || "無"}</div>`;
  iss.review.forEach((r) => out.push(`<div class="issue MANUAL_REVIEW">🔍 <b>${esc(r.code)} ${esc(r.name)}</b>（${r.term}，類別「${esc(r.category)}」）需人工判斷如何採計${reviewForm(r)}</div>`));
  iss.failed.forEach((r) => out.push(`<div class="issue FAIL">✘ <b>${esc(r.code)} ${esc(r.name)}</b>（${r.term}）${r.grade} 分不及格，尚未重修通過</div>`));
  iss.dept_unverified.forEach((r) => out.push(`<div class="issue WARNING">⚠ <b>${esc(r.code)} ${esc(r.name)}</b>（${r.term}）不在規則庫，暫以本系選修 ${r.credits} 學分計${reviewForm(r)}</div>`));
  s.flags.forEach((f) => out.push(`<div class="issue WARNING">⚑ <b>${esc(FLAG_LABELS[f.type] || f.type)}</b>：${esc(f.message)}${related(f)}</div>`));
  s.actions.forEach((a) => out.push(`<div class="issue WARNING"><b>需辦理：${esc(a.title)}</b>（${esc(a.office)}）${related(a)}</div>`));
  s.reviews.forEach((r) => out.push(`<div class="issue reviewed">✓ <b>${esc(r.code)} ${esc(r.name)}</b>（${r.term}）已覆核：<b>${esc(r.label)}</b>
    <div class="hint">${esc(r.reviewer)}・${when(r.decided_at)}${r.note ? `・${esc(r.note)}` : ""}
      ${r.outdated ? `<span class="outdated">・規則已更新，建議再確認</span>` : ""}</div>
    <div class="review-form" data-code="${esc(r.code)}" data-term="${esc(r.term)}">
      <input class="note" placeholder="撤銷原因（選填）"><button class="ghost" data-action="revoke">撤銷</button></div></div>`));
  $("#issues").innerHTML = out.join("");
  $("#issues-card").style.display = out.length || s.review_history.length ? "" : "none";
  $("#review-msg").innerHTML = "";
  $("#review-history-box").style.display = s.review_history.length ? "" : "none";
  $("#review-history-count").textContent = s.review_history.length;
  $("#review-history").innerHTML = s.review_history.map((e) =>
    `<div>${when(e.decided_at)}・${esc(e.reviewer)}：${esc(e.code)}（${esc(e.term)}）${esc(e.label)}${e.note ? `・${esc(e.note)}` : ""}</div>`).join("");
  document.querySelectorAll("#issues .review-form button").forEach((b) => b.addEventListener("click", () => submitReview(s.id, b)));
}

$("#student-select").addEventListener("change", (e) => loadStudent(e.target.value));

// ---------- 人工覆核 ----------
function reviewForm(r) {
  const options = [["dept_elective", "確認為本系選修"], ["outside", "外系認列"], ["exclude", "不採計"]]
    .concat(META.categories.map((c) => [`category:${c}`, `歸入「${c}」`]));
  return `<div class="review-form" data-code="${esc(r.code)}" data-term="${esc(r.term)}">
    <select><option value="">採計方式…</option>${options.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("")}</select>
    <input class="note" placeholder="依據（選填）">
    <button class="primary" data-action="decide">確認</button></div>`;
}

async function submitReview(sid, button) {
  const form = button.closest(".review-form");
  const choice = button.dataset.action === "revoke" ? "revoke" : form.querySelector("select").value;
  if (!needOfficer("#review-msg")) return;
  if (!choice) return msg("#review-msg", "請選擇採計方式。", "err");
  const [action, category = ""] = choice.split(/:(.*)/);
  try {
    await postJSON(`/api/students/${encodeURIComponent(sid)}/reviews`, { code: form.dataset.code, term: form.dataset.term,
      action, category, reviewer: officer(), note: form.querySelector("input.note").value });
    BATCH = await api("/api/students");
    renderBatch();
    fillStudentSelect();
    await loadStudent(sid);
  } catch (e) {
    msg("#review-msg", esc(e.message), "err");
  }
}

// ---------- 規則來源 ----------
function renderRules() {
  $("#sources").innerHTML = Object.values(META.sources).map((v) => `<li>${esc(v)}</li>`).join("");
  $("#assumptions").innerHTML = META.assumptions.map((a) => `<li>${esc(a)}</li>`).join("");
}

// ---------- 資料上傳 ----------
function renderSource(src) {
  $("#data-source").textContent = `${src.students} 位學生・${src.records_count} 筆紀錄`;
  $("#data-source").title = `修課紀錄 ${src.records}・應屆名單 ${src.roster}（${src.roster_count} 位）`;
}

function fillStudentSelect() {
  $("#student-select").innerHTML = BATCH.map((s) => `<option value="${esc(s.id)}">${esc(s.id)}</option>`).join("");
}

async function loadData() {
  let roster, src;
  [BATCH, roster, src] = await Promise.all([api("/api/students"), api("/api/roster-anomalies"), api("/api/data-source")]);
  renderSource(src);
  renderRoster(roster);
  fillStudentSelect();
  statusFilter = null;
  flagFilter = "";
  $("#flag-filter").value = "";
  renderBatch();
  const first = BATCH.find((s) => s.status === "FAIL") || BATCH[0];
  if (first) await loadStudent(first.id);
}

async function sendData(path, payload) {
  msg("#upload-msg", "處理中…");
  try {
    await postJSON(path, payload);
    await loadData();
    msg("#upload-msg", "已更新審查結果。", "ok");
    $("#records-file").value = $("#roster-file").value = "";
  } catch (e) {
    msg("#upload-msg", errorList(e) + "<div>目前資料維持不變。</div>", "err");
  }
}

const readFile = async (input) => {
  const f = input.files[0];
  return f ? { name: f.name, text: await f.text() } : null;
};

$("#upload-btn").addEventListener("click", async () => {
  const records = await readFile($("#records-file")), roster = await readFile($("#roster-file"));
  if (!records && !roster) return msg("#upload-msg", "請先選擇檔案。", "err");
  await sendData("/api/upload", { records, roster });
});
$("#reset-btn").addEventListener("click", () => sendData("/api/reset", {}));

// ---------- AI 解析儀表板 ----------
let DRAFT = null, draftFromBatch = false;
const DASH_FIELDS = ["rule_code", "term", "code", "name", "credits", "grade"];
const DASH_LABELS = { rule_code: "規則代碼", term: "學期", code: "課號", name: "課名", credits: "學分", grade: "成績" };

function invalidateDraftConfirmation() {
  $("#dash-checked").checked = false;
  msg("#dash-confirm-msg", "");
}

// 雙模型交叉驗證摘要：一致不代表正確，只用來排出優先核對的列
function crossCheckSummary(cc) {
  if (!cc) return "";
  if (cc.status !== "ok") return `<div class="issue WARNING">⚠ 未交叉驗證：${esc(cc.note)}</div>`;
  const parts = [`兩模型一致 ${cc.agreed_rows} 列`];
  if (cc.flagged_rows) parts.push(`需核對 ${cc.flagged_rows} 列`);
  if (cc.missing_in_primary) parts.push(`可能漏列 ${cc.missing_in_primary} 列`);
  const flagged = cc.flagged_rows + cc.missing_in_primary;
  return `<div class="issue ${flagged ? "WARNING" : "PASS"}">${flagged ? "⚠" : "✔"} ${parts.join("・")}</div>`;
}

function renderDraft(fresh = false) {
  const d = DRAFT;
  $("#dash-draft").hidden = false;
  $("#dash-meta").textContent = `模型 ${d.model}${d.cross_check ? `／${d.cross_check.check_model}` : ""}・${when(d.generated_at)}・`
    + `提示 ${d.prompt_version}・版型 ${d.format}` + (d.fallback_from ? `・已改用備援（${d.fallback_from}）` : "");
  if (fresh) $("#dash-cohort").value = d.cohort ?? "";
  $("#dash-declared").textContent = `文件標示應修 ${d.declared_required ?? "—"}、實得 ${d.declared_earned ?? "—"} 學分`
    + (d.exists ? `；${d.student_id} 已有紀錄，確認後取代` : "");
  const rowIssues = (i) => d.issues.filter((x) => x.row === i);
  $("#dash-cross").innerHTML = crossCheckSummary(d.cross_check);
  $("#dash-issues").innerHTML = d.issues.filter((i) => i.row === null).map((i) =>
    `<div class="issue ${i.level === "error" ? "FAIL" : "WARNING"}">${i.level === "error" ? "✘" : "⚠"} ${esc(i.message)}</div>`).join("");
  const cats = ["", ...d.categories];
  $("#dash-rows").innerHTML = d.rows.map((r, i) => {
    const iss = rowIssues(i);
    const cls = iss.some((x) => x.level === "error") ? "has-error" : iss.length ? "has-warning" : "";
    return `<tr class="${cls}" data-i="${i}">
      <td>${i + 1}</td>
      ${DASH_FIELDS.map((f) => `<td><input data-f="${f}" aria-label="第 ${i + 1} 列${DASH_LABELS[f]}" title="${esc(r[f] ?? "")}" value="${esc(r[f] ?? "")}"></td>`).join("")}
      <td><select data-f="category" aria-label="第 ${i + 1} 列類別">${cats.map((c) => `<option ${c === r.category ? "selected" : ""} value="${esc(c)}">${esc(c || "依課號")}</option>`).join("")}</select></td>
      <td><label class="page">第 <input type="number" min="1" step="1" data-f="source_page" aria-label="第 ${i + 1} 列來源頁碼" value="${esc(r.source_page ?? "")}"> 頁</label>
        <textarea data-f="source_text" rows="1" aria-label="第 ${i + 1} 列來源原文" placeholder="PDF 上該列原文">${esc(r.source_text || "")}</textarea>
        ${iss.map((x) => `<div class="row-issue">${x.level === "error" ? "✘" : "⚠"} ${esc(x.message)}</div>`).join("")}</td>
      <td><button class="ghost del" data-del="${i}" aria-label="刪除第 ${i + 1} 列" title="刪除這一列">✕</button></td>
    </tr>`;
  }).join("");
  document.querySelectorAll("#dash-rows input, #dash-rows select, #dash-rows textarea").forEach((el) => el.addEventListener("input", () => {
    DRAFT.rows[el.closest("tr").dataset.i][el.dataset.f] = el.dataset.f === "source_page"
      ? (el.value.trim() ? Number(el.value) : null) : el.value;
    invalidateDraftConfirmation();
  }));
  document.querySelectorAll("#dash-rows [data-del]").forEach((b) => b.addEventListener("click", () => {
    DRAFT.rows.splice(Number(b.dataset.del), 1);
    DRAFT.issues = [];  // 列號已變動，確認時由伺服器重新檢查
    renderDraft();
  }));
  invalidateDraftConfirmation();
}

$("#dash-cohort").addEventListener("input", invalidateDraftConfirmation);

$("#dash-extract").addEventListener("click", async () => {
  const file = $("#dash-file").files[0], sid = $("#dash-sid").value.trim();
  if (!file || !sid) return msg("#dash-msg", "請選擇 PDF 並填寫學號。", "err");
  DRAFT = null;
  draftFromBatch = false;
  $("#dash-draft").hidden = true;
  invalidateDraftConfirmation();
  const started = Date.now();
  const waiting = () => msg("#dash-msg", `解析中，已等待 ${Math.round((Date.now() - started) / 1000)} 秒…`);
  waiting();
  const timer = setInterval(waiting, 1000);
  $("#dash-extract").disabled = $("#dash-file").disabled = $("#dash-sid").disabled = true;
  try {
    DRAFT = await postJSON("/api/dashboard/extract", { student_id: sid, data: await pdfBase64(file) });
    const errors = DRAFT.issues.filter((i) => i.level === "error").length, warnings = DRAFT.issues.length - errors;
    msg("#dash-msg", `擷取 ${DRAFT.rows.length} 列・錯誤 ${errors}・警示 ${warnings}`);
    renderDraft(true);
  } catch (e) {
    msg("#dash-msg", `<b>${esc(e.message)}</b>`, "err");
  } finally {
    clearInterval(timer);
    $("#dash-extract").disabled = $("#dash-file").disabled = $("#dash-sid").disabled = false;
  }
});

$("#dash-add").addEventListener("click", () => {
  DRAFT.rows.push({ rule_code: "", term: "", code: "", name: "", credits: "", grade: "", category: "",
    source_page: null, source_text: "" });
  renderDraft();
});

$("#dash-confirm").addEventListener("click", async () => {
  if (!DRAFT || $("#dash-confirm").disabled) return;
  if (!$("#dash-checked").checked) return msg("#dash-confirm-msg", "請先對照原文並勾選確認。", "err");
  if (!needOfficer("#dash-confirm-msg")) return;
  $("#dash-confirm").disabled = true;
  try {
    // 單一勾選同時代表「已對照原文」與「已確認警示」；伺服器仍會重新檢查，錯誤一律擋下
    const s = await postJSON("/api/dashboard/confirm", { draft_id: DRAFT.draft_id, rows: DRAFT.rows,
      reviewer: officer(), cohort: $("#dash-cohort").value.trim(), acknowledge_warnings: true });
    DRAFT = null;
    $("#dash-draft").hidden = true;
    msg("#dash-msg", "");
    await loadData();
    await loadStudent(s.id);
    if (draftFromBatch) {  // 批次：留在本頁繼續核對下一份
      await refreshJobs();
      msg("#jobs-msg", `${esc(s.id)} 已確認送入審查。`, "ok");
      $("#jobs-table").scrollIntoView({ behavior: "smooth" });
    } else show("student");
  } catch (e) {
    $("#dash-checked").checked = false;
    msg("#dash-confirm-msg", errorList(e), "err");
  } finally {
    $("#dash-confirm").disabled = false;
  }
});

// ---------- 批次解析：背景排隊，解析完逐份核對確認 ----------
const JOB_LABELS = { queued: "排隊中", running: "解析中", done: "待確認", failed: "失敗", stopped: "已停止", confirmed: "已確認" };
const JOB_ORDER = { failed: 0, stopped: 1, done: 2, running: 3, queued: 4, confirmed: 5 };
let jobsTimer = null, aiPaused = false;

function renderJobs(st) {
  const c = st.counts, pending = c.queued + c.running;
  aiPaused = st.paused;
  $("#jobs-workers").textContent = `（同時 ${st.workers} 份）`;
  $("#jobs-stop").textContent = st.paused ? "恢復 AI" : "停止全部";
  $("#jobs-stop").classList.toggle("primary", st.paused);
  $("#jobs-add").disabled = st.paused;
  $("#jobs-retry-all").hidden = st.paused || !(c.failed + c.stopped);
  $("#jobs-progress").textContent = st.jobs.length
    ? `已確認 ${c.confirmed}・待確認 ${c.done}・處理中 ${pending}・失敗 ${c.failed + c.stopped}`
      + (st.paused ? "・AI 已停用" : "") : (st.paused ? "AI 已停用" : "");
  const jobs = [...st.jobs].sort((a, b) => JOB_ORDER[a.state] - JOB_ORDER[b.state]
    || b.errors - a.errors || b.warnings - a.warnings || a.student_id.localeCompare(b.student_id));
  $("#jobs-table").hidden = !jobs.length;
  $("#jobs-rows").innerHTML = jobs.map((j) => {
    const check = j.state === "done" || j.state === "confirmed"
      ? `錯誤 ${j.errors}・警示 ${j.warnings}` + (j.cross_checked ? `・需核對 ${j.flagged_rows} 列` : "・未交叉驗證")
      : j.error ? `<span class="hint">${esc(j.error)}</span>` : "";
    const button = j.state === "done" ? `<button class="primary" data-open="${esc(j.student_id)}">核對</button>`
      : (j.state === "failed" || j.state === "stopped") && !st.paused
        ? `<button class="ghost" data-retry="${esc(j.student_id)}">重試</button>` : "";
    return `<tr><td><b>${esc(j.student_id)}</b></td>
      <td><span class="flag">${JOB_LABELS[j.state]}</span></td>
      <td>${j.state === "done" || j.state === "confirmed" ? j.rows : ""}</td>
      <td>${check}</td><td class="hide-sm">${j.seconds == null ? "" : `${j.seconds} 秒`}</td><td>${button}</td></tr>`;
  }).join("");
  document.querySelectorAll("#jobs-rows [data-open]").forEach((b) => b.addEventListener("click", () => openJob(b.dataset.open)));
  document.querySelectorAll("#jobs-rows [data-retry]").forEach((b) => b.addEventListener("click", () =>
    jobsAction("retry", { student_id: b.dataset.retry })));
  clearTimeout(jobsTimer);
  if (pending) jobsTimer = setTimeout(refreshJobs, 2000);
}

async function refreshJobs() {
  try {
    renderJobs(await api("/api/dashboard/batch"));
  } catch (e) {
    msg("#jobs-msg", esc(e.message), "err");
  }
}

async function jobsAction(action, body = {}) {
  try {
    renderJobs(await postJSON(`/api/dashboard/batch/${action}`, body));
    msg("#jobs-msg", "");
  } catch (e) {
    msg("#jobs-msg", esc(e.message), "err");
  }
}

async function openJob(sid) {
  try {
    DRAFT = await api(`/api/dashboard/batch/${encodeURIComponent(sid)}`);
  } catch (e) {
    return msg("#jobs-msg", esc(e.message), "err");
  }
  draftFromBatch = true;
  msg("#dash-msg", `核對 ${esc(sid)}：擷取 ${DRAFT.rows.length} 列`);
  renderDraft(true);
  $("#dash-draft").scrollIntoView({ behavior: "smooth" });
}

$("#jobs-add").addEventListener("click", async () => {
  const files = [...$("#jobs-files").files];
  if (!files.length) return msg("#jobs-msg", "請選擇 PDF（可多選）。", "err");
  $("#jobs-add").disabled = true;
  const added = [], unmatched = [], failed = [];
  for (const [i, f] of files.entries()) {
    msg("#jobs-msg", `上傳中 ${i + 1} / ${files.length}…`);
    try {
      const r = await postJSON("/api/dashboard/batch", { filename: f.name, data: await pdfBase64(f) });
      (r.unmatched ? unmatched : added).push(r.unmatched?.student_id || r.student_id);
    } catch (e) {
      failed.push(`${f.name}：${e.message}`);
    }
  }
  $("#jobs-files").value = "";
  $("#jobs-add").disabled = false;
  const lines = [`已加入佇列 ${added.length} 份`];
  if (unmatched.length) lines.push(`學號不在應屆名單、未解析 ${unmatched.length} 份：${unmatched.map(esc).join("、")}（已列入名單異常）`);
  msg("#jobs-msg", lines.join("<br>") + (failed.length
    ? `<ul class="upload-errors">${failed.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""),
    failed.length || unmatched.length ? "err" : "ok");
  renderRoster(await api("/api/roster-anomalies"));
  await refreshJobs();
});

$("#jobs-stop").addEventListener("click", () =>
  jobsAction(aiPaused ? "resume" : "stop"));
$("#jobs-retry-all").addEventListener("click", () => jobsAction("retry"));

// ---------- AI 草擬規則（確認僅存檔，啟用另行操作） ----------
let RULE_DRAFT = null, ruleEdited = false, ruleSourceUrl = null;
const RULE_DIFF_LABELS = { same: "相同", different: "欄位不同", missing_in_draft: "草稿漏列", extra_in_draft: "草稿多列" };

function renderRuleDraft(d) {
  RULE_DRAFT = d;
  ruleEdited = false;
  $("#rule-draft-panel").hidden = false;
  $("#rule-meta").textContent = `模型 ${d.model}・${when(d.generated_at)}`;
  $("#rule-issues").innerHTML = d.issues.map((i) => `<div class="issue FAIL">✘ ${esc(i.message)}</div>`).join("")
    + (d.warnings || []).map((w) => `<div class="issue WARNING">⚠ ${esc(w.message)}</div>`).join("")
    + (d.baseline_changed ? '<div class="issue WARNING">⚠ 現有規則在草擬後已改變，請重新核對差異。</div>' : "");
  $("#rule-requirements").innerHTML = (Array.isArray(d.requirements) ? d.requirements : []).map((r) => {
    if (!r || typeof r !== "object") return '<div class="issue FAIL">要求格式錯誤，請修正。</div>';
    const rule = r.min_credits != null ? `最低 ${esc(r.min_credits)} 學分` : esc(JSON.stringify(r.courses ?? r.categories ?? []));
    return `<div class="req"><div class="req-top"><b>${esc(r.id)} ${esc(r.title)}</b><span class="hint">第 ${esc(r.source_page)} 頁</span></div>
      <blockquote>${esc(r.source_quote)}</blockquote>
      <div>${rule}</div>
      <div class="hint">適用：${esc(r.applicability)}・例外：${esc(r.exceptions)}</div></div>`;
  }).join("");
  $("#rule-diff").innerHTML = d.diff.map((r) => `<div style="margin-bottom: 8px"><b>${esc(r.id)}</b>：${esc(RULE_DIFF_LABELS[r.status])}`
    + r.fields.map((f) => `<div class="hint" style="overflow-wrap: anywhere">${esc(f.field)}：`
      + esc(f.change === "added" ? "（原無）" : JSON.stringify(f.before)) + " → "
      + esc(f.change === "removed" ? "（草稿無）" : JSON.stringify(f.after)) + "</div>").join("") + "</div>").join("");
  $("#rule-editor").value = JSON.stringify(d.requirements, null, 2);
  $("#rule-checked").checked = false;
  const confirmed = d.state === "confirmed";
  $("#rule-editor").disabled = $("#rule-confirm").disabled = $("#rule-checked").disabled = confirmed;
}

$("#rule-editor").addEventListener("input", () => {
  ruleEdited = true;
  $("#rule-checked").checked = false;
});
$("#rule-file").addEventListener("change", () => {
  RULE_DRAFT = null;
  $("#rule-draft-panel").hidden = true;
  msg("#rule-msg", "");
  if (ruleSourceUrl) URL.revokeObjectURL(ruleSourceUrl);
  const file = $("#rule-file").files[0];
  ruleSourceUrl = file ? URL.createObjectURL(file) : null;
  $("#rule-source").hidden = !file;
  if (file) $("#rule-source").href = ruleSourceUrl;
});

$("#rule-extract").addEventListener("click", async () => {
  const file = $("#rule-file").files[0];
  if (!file) return msg("#rule-msg", "請選擇應修科目表 PDF。", "err");
  RULE_DRAFT = null;
  $("#rule-draft-panel").hidden = true;
  msg("#rule-confirm-msg", "");
  $("#rule-extract").disabled = $("#rule-file").disabled = true;
  const start = Date.now();
  const waiting = () => msg("#rule-msg", `草擬中，已等待 ${Math.round((Date.now() - start) / 1000)} 秒…`);
  waiting();
  const timer = setInterval(waiting, 1000);
  try {
    renderRuleDraft(await postJSON("/api/rule-drafts/extract", { data: await pdfBase64(file), year: 114 }));
    msg("#rule-msg", "");
  } catch (e) {
    msg("#rule-msg", esc(e.message), "err");
  } finally {
    clearInterval(timer);
    $("#rule-extract").disabled = $("#rule-file").disabled = false;
  }
});

// 內容改過時先重新檢查並刷新差異，要求重新核對；沒改過才存檔（伺服器存檔前仍會再驗證）
$("#rule-confirm").addEventListener("click", async () => {
  if (!RULE_DRAFT || $("#rule-confirm").disabled) return;
  let requirements;
  try { requirements = JSON.parse($("#rule-editor").value); } catch { return msg("#rule-confirm-msg", "JSON 格式錯誤。", "err"); }
  const lock = (on) => { $("#rule-confirm").disabled = $("#rule-editor").disabled = $("#rule-extract").disabled = on; };
  lock(true);
  try {
    if (ruleEdited) {
      renderRuleDraft(await postJSON("/api/rule-drafts/check", { draft_id: RULE_DRAFT.draft_id, requirements }));
      return msg("#rule-confirm-msg", "已依修改內容重新檢查，請核對後再勾選確認。");
    }
    if (!$("#rule-checked").checked) return msg("#rule-confirm-msg", "請先核對並勾選確認。", "err");
    if (!needOfficer("#rule-confirm-msg")) return;
    const d = await postJSON("/api/rule-drafts/confirm", { draft_id: RULE_DRAFT.draft_id, requirements,
      reviewer: officer(), acknowledge: true });
    renderRuleDraft(d);
    msg("#rule-confirm-msg", "已確認存檔，可在下方啟用。", "ok");
    await loadRuleVersions();
  } catch (e) {
    $("#rule-checked").checked = false;
    msg("#rule-confirm-msg", esc(e.message), "err");
  } finally {
    lock(RULE_DRAFT?.state === "confirmed");
    $("#rule-extract").disabled = false;
  }
});

// ---------- 規則版本（已確認草稿經承辦人明確啟用；可還原） ----------
function renderRuleVersions(v) {
  $("#rule-versions-years").innerHTML = v.years.map((y) => `<div class="row" style="margin-bottom: 8px">
    <b>${y.year} 學年度</b>
    ${y.activated
      ? `<span class="flag">啟用草稿 ${esc(y.activated.draft_id.slice(0, 8))}</span>
         <span class="hint">${esc(y.activated.by)}・${when(y.activated.at)}</span>
         <button class="ghost" data-revert="${y.year}">還原正式規則</button>`
      : '<span class="hint">正式規則庫</span>'}</div>`).join("");
  $("#rule-versions-drafts").innerHTML = v.confirmed_drafts.map((d) => `<div class="req">
    <div class="req-top"><b>${d.year} 學年度・${esc(d.requirement_ids.join("、"))}</b>
      <button class="primary" data-activate="${esc(d.draft_id)}">啟用</button></div>
    <div class="hint">${esc(d.reviewer)} 確認・${when(d.confirmed_at)}</div>
    ${renderRulePreview(d.preview)}</div>`).join("")
    || '<p class="hint">尚無已確認的草稿</p>';
  $("#rule-versions-history").innerHTML = v.history.map((e) => `<div>${when(e.at)}・${e.year} 學年度・`
    + (e.type === "activate" ? `啟用草稿 ${esc(e.draft_id.slice(0, 8))}` : "還原正式規則") + `・${esc(e.by)}</div>`).join("")
    || '<span class="hint">尚無紀錄</span>';
  document.querySelectorAll("[data-activate]").forEach((b) => b.addEventListener("click", () =>
    changeRuleVersion("activate", { draft_id: b.dataset.activate, acknowledge: $("#rule-activate-ack").checked,
      activated_by: officer() })));
  document.querySelectorAll("[data-revert]").forEach((b) => b.addEventListener("click", () =>
    changeRuleVersion("revert", { year: Number(b.dataset.revert), reverted_by: officer() })));
}

// 啟用前影響預覽：虛構學生由規則引擎以目前規則與套用草稿後的規則各審查一次，列出判定不同的案例。
function renderRulePreview(p) {
  if (!p) return "";
  if (p.error) return `<div class="issue WARNING">⚠ 無法產生影響預覽：${esc(p.error)}</div>`;
  const verdict = (r) => !r ? "無此要求" : r.pending ? "待確認" : r.satisfied ? "符合" : "未符合";
  const side = (r) => r ? `${verdict(r)}（${r.have}／${r.need}）` : verdict(r);
  const groups = p.differences.map((d) => `<div class="issue WARNING">
    ${d.changes.map((c) => `<div><b>${esc(c.id)} ${esc(c.title)}</b>：${side(c.old)} → ${side(c.new)}</div>`).join("")}
    <div>${chip(d.old_status)} → ${chip(d.new_status)}・同類 ${d.cases_found} 個</div>
    <div class="hint">例：${esc(d.case.edits.join("；"))}</div></div>`).join("");
  const quiet = p.changed_without_observed_difference.length
    ? `<div class="hint">有改但未見判定差異：${esc(p.changed_without_observed_difference.map((r) => r.id).join("、"))}</div>` : "";
  return `<details class="fold" ${p.differences.length ? "open" : ""}>
    <summary>啟用前影響預覽：${esc(p.message)}</summary>
    <div style="margin-top: 8px">${groups}${quiet}</div></details>`;
}

async function loadRuleVersions() {
  try {
    renderRuleVersions(await api("/api/rule-versions"));
  } catch (e) {
    msg("#rule-versions-msg", esc(e.message), "err");
  }
}

async function changeRuleVersion(action, body) {
  if (!needOfficer("#rule-versions-msg")) return;
  if (action === "activate" && !$("#rule-activate-ack").checked)
    return msg("#rule-versions-msg", "請先勾選了解啟用的影響。", "err");
  try {
    renderRuleVersions(await postJSON(`/api/rule-versions/${action}`, body));
    $("#rule-activate-ack").checked = false;
    msg("#rule-versions-msg", action === "activate" ? "已啟用，審查結果已更新。" : "已還原，審查結果已更新。", "ok");
    await loadMeta();
    renderRules();
    await loadData();
  } catch (e) {
    msg("#rule-versions-msg", esc(e.message), "err");
  }
}

// ---------- 啟動 ----------
(async function init() {
  await loadMeta();
  renderRules();
  await loadData();
  await loadRuleVersions();
  await refreshJobs();
})();
