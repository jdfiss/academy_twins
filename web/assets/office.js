// 系辦端（A 組）：監控台（分流、批次放行與抽查、AI 處理流程、指標）、收件匣、學生明細與覆核、規則、工具
let BATCH = [], statusFilter = null, JOBS = null, jobFilter = null;
const FLAG_LABELS = { overflow: "超修學分", repeatable: "可重複修習", substitution_pending: "待辦抵修" };
const show = setupTabs("twin-office-view");
// 網址可直接指定分頁（如 office.html#inbox），方便錄影與評審開啟；舊網址 #ingest、#batch 導向監控台
const ALIASES = { ingest: "monitor", batch: "monitor" };
const viewFromHash = () => {
  const raw = location.hash.slice(1), v = ALIASES[raw] || raw;
  if (v && document.getElementById(`view-${v}`)) show(v);
};
viewFromHash();
window.addEventListener("hashchange", viewFromHash);
document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () =>
  history.replaceState(null, "", `#${b.dataset.view}`)));
const go = (view) => { show(view); history.replaceState(null, "", `#${view}`); };

// ---------- 共用：監控者與訊息 ----------
// 不再要求填姓名：紀錄上的操作者固定為「教務處」（後端仍要求非空，紀錄格式不變）
const officer = () => "教務處";

function msg(el, html, kind = "") {
  $(el).innerHTML = html ? `<div class="msg ${kind}">${html}</div>` : "";
}

// 檔案欄位：瀏覽器內建的「沒有選擇檔案」沒有資訊量，改成一個「選擇 PDF／CSV」按鈕，選了才顯示檔名。
// 原本的 <input type=file> 與 id 保留（隱藏），其餘程式照舊讀 .files
function fileName(input) {
  const span = input.nextElementSibling?.nextElementSibling;
  if (!span?.classList.contains("file-name")) return;
  const files = [...input.files];
  span.textContent = files.length > 1 ? `${files.length} 個檔案` : files[0]?.name || "";
}
document.querySelectorAll("input[type=file]").forEach((input) => {
  const btn = document.createElement("button"), span = document.createElement("span");
  btn.type = "button";
  btn.className = "ghost file-btn";
  btn.textContent = /pdf/i.test(input.accept) ? "選擇 PDF" : "選擇 CSV";
  btn.addEventListener("click", () => input.click());
  span.className = "file-name hint";
  input.after(btn, span);
  input.addEventListener("change", () => fileName(input));
  new MutationObserver(() => { btn.disabled = input.disabled; }).observe(input, { attributes: true, attributeFilter: ["disabled"] });
});

// 伺服器時間（ISO）顯示為本地「YYYY-MM-DD HH:MM」
function when(iso) {
  const d = new Date(iso);
  if (!iso || isNaN(d)) return esc(iso || "");
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// 清單用的短時間（像信箱）：今天只顯示「HH:MM」，今年顯示「MM/DD」，更早顯示「YYYY/MM/DD」
function shortWhen(iso) {
  const d = new Date(iso), now = new Date();
  if (!iso || isNaN(d)) return esc(iso || "");
  const p = (n) => String(n).padStart(2, "0");
  if (d.toDateString() === now.toDateString()) return `${p(d.getHours())}:${p(d.getMinutes())}`;
  return d.getFullYear() === now.getFullYear() ? `${p(d.getMonth() + 1)}/${p(d.getDate())}`
    : `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())}`;
}

// AI 錯誤原文（常是 Gemini 的英文 JSON）在清單只顯示短的中文原因；完整原文放在信件詳細內容
function shortError(err) {
  if (/429|quota|RESOURCE_EXHAUSTED/i.test(err)) return "額度已用完，稍後重試";
  if (/逾時|timeout/i.test(err)) return "AI 回應逾時";
  return "AI 擬稿失敗";
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
      <div><span class="chip ${k}">${STATUS[k].replace(/^\S+\s/, "")}</span></div><div class="n">${counts[k]}</div>
    </div>`).join("");
  document.querySelectorAll(".stat").forEach((el) => el.addEventListener("click", () => {
    statusFilter = statusFilter === el.dataset.status ? null : el.dataset.status;
    renderBatch();
  }));
  const rows = BATCH.filter((s) => !statusFilter || s.status === statusFilter);
  // 沒篩選時人數就是整張表，不重複顯示
  $("#batch-filter-count").textContent = statusFilter ? `${rows.length} / ${BATCH.length} 位` : "";
  $("#batch-rows").innerHTML = rows.map((s) => {
    const pct = s.total_required ? Math.min(100, (s.total_credits / s.total_required) * 100) : 0;
    return `<tr data-id="${esc(s.id)}">
      <td><b>${esc(s.id)}</b></td>
      <td>${chip(s.status)}</td>
      <td><div>${s.total_credits}</div><div class="meter ${pct >= 100 ? "ok" : ""}"><div style="width:${pct}%"></div></div></td>
      <td class="hide-sm">${s.satisfied} / ${s.requirements}</td>
      <td>${reasonList(s.reasons)}${s.reviewed ? `<div class="hint">已覆核 ${s.reviewed} 筆</div>` : ""}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="5" class="empty">沒有符合的學生</td></tr>`;
  document.querySelectorAll("#batch-rows tr[data-id]").forEach((tr) =>
    tr.addEventListener("click", () => { loadStudent(tr.dataset.id); go("student"); }));
}

// 原因最多列 3 條，其餘在學生明細看
function reasonList(reasons) {
  if (!reasons.length) return '<span class="hint">全部符合</span>';
  const more = reasons.length - 3;
  return reasons.slice(0, 3).map(esc).join("<br>") + (more > 0 ? `<div class="hint">另 ${more} 項</div>` : "");
}

function renderRoster(rows) {
  $("#roster-card").hidden = !rows.length;
  $("#roster-count").textContent = rows.length ? `${rows.length} 筆` : "";
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
  foldSatisfied();
  renderRecords(s);
}

// 系辦主要看沒過的條件：已符合的收進一行「已符合 N 項」，點開才看細節（只搬動畫面，不改判定）
function foldSatisfied() {
  const box = $("#requirements"), done = [...box.querySelectorAll(":scope > .req.ok")];
  if (!done.length) return;
  const group = document.createElement("details");
  group.className = "req-ok-group";
  group.innerHTML = `<summary>✔ 已符合 ${done.length} 項</summary><div class="req-ok-list"></div>`;
  done.forEach((el) => group.lastElementChild.appendChild(el));
  box.appendChild(group);
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

// ---------- 資料（工具頁） ----------
function renderSource(src) {
  $("#data-source").textContent = `${src.students} 位學生・${src.records_count} 筆紀錄`;
  $("#data-source").title = `修課紀錄 ${src.records}・應屆名單 ${src.roster}（${src.roster_count} 位）`;
  $("#judge-banner").hidden = src.replay_mode !== "only";
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
  renderBatch();
  renderEmpty();
  const first = BATCH.find((s) => s.status === "FAIL") || BATCH[0];
  if (first) await loadStudent(first.id);
}

async function sendData(path, payload, el = "#upload-msg") {
  msg(el, "處理中…");
  try {
    await postJSON(path, payload);
    await loadData();
    msg(el, "已更新審查結果。", "ok");
    $("#records-file").value = $("#roster-file").value = "";
    fileName($("#records-file")); fileName($("#roster-file"));
    return true;
  } catch (e) {
    msg(el, errorList(e) + "<div>目前資料維持不變。</div>", "err");
    return false;
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
async function loadDemo(el) {
  if (await sendData("/api/demo-batch", {}, el)) {
    DRAFT = null;
    $("#dash-draft").hidden = true;
    await Promise.all([refreshJobs(), refreshInbox(), loadMetrics()]);
    go("monitor");
    $("#jobs-files").nextElementSibling.focus();
  }
}
$("#demo-btn").addEventListener("click", () => loadDemo("#upload-msg"));
$("#empty-demo").addEventListener("click", () => loadDemo("#jobs-msg"));

function renderEmpty() {
  $("#monitor-empty").hidden = !(JOBS && !JOBS.jobs.length && !BATCH.length);
}

// ---------- 核對 AI 解析結果（逐份） ----------
let DRAFT = null;
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

// 執行軌跡：AI 做了什麼、哪裡被程式檢查擋下、哪裡交給承辦人（只有計數，沒有課程內容）
const TRACE_STEPS = {
  extract: (s) => s.ok ? `${s.model} 擷取 ${s.rows} 列${s.fallback ? "（改用備援）" : ""}` : "主模型擷取失敗",
  cross_check: (s) => s.ok ? `${s.model} 同時擷取供交叉比對` : "第二模型失敗，未交叉驗證",
  check: (s) => s.ok ? "程式檢查通過" : `程式檢查擋下 ${s.errors} 個錯誤`
    + (s.kept === "first" ? "（修正後沒有變好，保留第一次結果）" : ""),
  retry: (s) => s.ok ? "只把檢查理由回饋給 AI，重新擷取 1 次" : "自我修正時 AI 失敗",
};
function traceSummary(t) {
  if (!t) return "";
  const end = t.to_manual ? "仍有錯誤，交給承辦人逐列修正" : t.corrected ? "自我修正成功" : "交給承辦人確認";
  const replayed = t.replay ? ` <span class="replay">重播：${esc(t.replay.model)}・${esc(when(t.replay.generated_at))}產生`
    + `（${esc(t.replay.reason)}）</span>` : "";
  const src = t.source_check ? `<li class="${t.source_check.ok ? "" : "bad"}">${!t.source_check.available ? "PDF 抽不出文字，無法自動核對原文"
    : t.source_check.warnings ? `PDF 原文核對：${t.source_check.warnings} 項與原文不符` : "PDF 原文核對：全部相符"}</li>` : "";
  return `<div class="trace"><b>AI 做了什麼</b>${replayed}
    <ol>${t.steps.map((s) => `<li class="${s.ok ? "" : "bad"}">${esc((TRACE_STEPS[s.step] || (() => s.step))(s))}</li>`).join("")}
    ${src}<li class="${t.to_manual ? "bad" : ""}">${end}</li></ol>
    <span class="hint">共 ${t.wait_seconds} 秒・提示 ${esc(t.prompt_version)}</span></div>`;
}

function renderDraft(fresh = false) {
  const d = DRAFT;
  $("#dash-draft").hidden = false;
  $("#dash-sid-label").textContent = d.student_id;
  $("#dash-meta").textContent = `模型 ${d.model}${d.cross_check ? `／${d.cross_check.check_model}` : ""}・${when(d.generated_at)}・`
    + `提示 ${d.prompt_version}・版型 ${d.format}` + (d.fallback_from ? `・已改用備援（${d.fallback_from}）` : "")
    + (d.replay ? `・重播（${d.replay.reason}），非即時呼叫` : "");
  if (fresh) $("#dash-cohort").value = d.cohort ?? "";
  $("#dash-declared").textContent = `文件標示應修 ${d.declared_required ?? "—"}、實得 ${d.declared_earned ?? "—"} 學分`
    + (d.exists ? `；${d.student_id} 已有紀錄，確認後取代` : "");
  const sampling = JOBS?.release?.active?.sampled?.includes(d.student_id);
  $("#dash-sample-note").innerHTML = sampling ? `<div class="issue WARNING">🎲 這份是批次放行的<b>隨機抽查</b>：請逐列對照原文。
    有任何修改，本批取消、其餘全部改為逐份核對。</div>` : "";
  const rowIssues = (i) => d.issues.filter((x) => x.row === i);
  $("#dash-trace").innerHTML = traceSummary(d.trace);
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
$("#dash-close").addEventListener("click", () => { DRAFT = null; $("#dash-draft").hidden = true; msg("#dash-msg", ""); });

$("#dash-extract").addEventListener("click", async () => {
  const file = $("#dash-file").files[0], sid = $("#dash-sid").value.trim();
  if (!file || !sid) return msg("#dash-single-msg", "請選擇 PDF 並填寫學號。", "err");
  DRAFT = null;
  $("#dash-draft").hidden = true;
  invalidateDraftConfirmation();
  const started = Date.now();
  const waiting = () => msg("#dash-single-msg", `解析中，已等待 ${Math.round((Date.now() - started) / 1000)} 秒…`);
  waiting();
  const timer = setInterval(waiting, 1000);
  $("#dash-extract").disabled = $("#dash-file").disabled = $("#dash-sid").disabled = true;
  try {
    DRAFT = await postJSON("/api/dashboard/extract", { student_id: sid, data: await pdfBase64(file) });
    msg("#dash-single-msg", "");
    go("monitor");
    renderDraft(true);
    $("#dash-draft").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    msg("#dash-single-msg", `<b>${esc(e.message)}</b>`, "err");
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
  $("#dash-confirm").disabled = true;
  try {
    // 單一勾選同時代表「已對照原文」與「已確認警示」；伺服器仍會重新檢查，錯誤一律擋下
    const s = await postJSON("/api/dashboard/confirm", { draft_id: DRAFT.draft_id, rows: DRAFT.rows,
      reviewer: officer(), cohort: $("#dash-cohort").value.trim(), acknowledge_warnings: true });
    DRAFT = null;
    $("#dash-draft").hidden = true;
    await Promise.all([loadData(), refreshJobs(), loadMetrics()]);
    msg("#jobs-msg", `${esc(s.id)} 已確認送入審查。`, "ok");
    $("#release-panel").hidden || $("#release-panel").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    $("#dash-checked").checked = false;
    msg("#dash-confirm-msg", errorList(e), "err");
  } finally {
    $("#dash-confirm").disabled = false;
  }
});

// ---------- 監控台：分流、AI 處理流程、批次放行 ----------
const JOB_BUTTON = { fix: "修正", review: "核對", sampling: "抽查核對" };
let jobsTimer = null;

function cp(kind, text) {
  return `<span class="cp ${kind}"><i></i>${esc(text)}</span>`;
}
function checkpoints(j) {
  if (j.state === "queued") return [cp("", "排隊中"), "", "", ""];
  if (j.state === "running") return [cp("", "解析中…"), "", "", ""];
  if (j.state === "failed" || j.state === "stopped") return [cp("bad", j.error || "失敗"), "", "", ""];
  const extract = cp("ok", `${j.rows} 列${j.replay ? "・重播" : ""}`);
  const cross = !j.cross_checked ? cp("warn", "未交叉驗證") : j.flagged_rows ? cp("warn", `不一致 ${j.flagged_rows} 列`) : cp("ok", "兩模型一致");
  const s = j.source_check;
  const source = !s ? cp("", "—") : !s.available ? cp("warn", "無法核對") : s.warnings ? cp("warn", `${s.warnings} 項不符`) : cp("ok", "相符");
  const rule = j.errors ? cp("bad", `錯誤 ${j.errors}`) : j.corrected ? cp("warn", "AI 重讀後通過") : cp("ok", "通過");
  return [extract, cross, source, rule];
}

const WORK_DASH = [["failed", "失敗", "need"], ["fix", "需修正", "need"], ["review", "需核對", "check"],
  ["sampling", "抽查中", "check"], ["release", "建議放行", "ai"], ["pending", "處理中", "ai"], ["confirmed", "已確認", "ok"]];
const WORK_INBOX = [["manual", "轉人工", "need"], ["failed", "AI 失敗", "need"], ["drafted", "待核准", "check"],
  ["new", "新進", "ai"], ["replied", "已回覆", "ok"]];

function renderWork() {
  const b = JOBS?.buckets || {};
  $("#work-dash").innerHTML = WORK_DASH.map(([k, label, cls]) => {
    const n = (b[k] || 0) + (k === "sampling" ? b.held || 0 : 0);
    return `<button class="cell ${cls} ${n ? "" : "zero"} ${jobFilter === k ? "selected" : ""}" data-bucket="${k}">
      <div class="n">${n}</div><div class="l">${label}${k === "sampling" && b.held ? `（等待 ${b.held}）` : ""}</div></button>`;
  }).join("");
  document.querySelectorAll("#work-dash .cell").forEach((el) => el.addEventListener("click", () => {
    jobFilter = jobFilter === el.dataset.bucket ? null : el.dataset.bucket;
    renderJobs(JOBS);
  }));
  const c = INBOX?.counts || {};
  $("#work-inbox").innerHTML = WORK_INBOX.map(([k, label, cls]) => {
    const n = (c[k] || 0) + (k === "new" ? c.processing || 0 : 0);
    return `<button class="cell ${cls} ${n ? "" : "zero"}" data-state="${k}"><div class="n">${n}</div><div class="l">${label}</div></button>`;
  }).join("");
  document.querySelectorAll("#work-inbox .cell").forEach((el) => el.addEventListener("click", () => {
    inboxFilter = el.dataset.state;
    renderInbox();
    go("inbox");
  }));
}

function renderJobs(st) {
  JOBS = st;
  const c = st.counts, pending = c.queued + c.running;
  renderAiSwitch(st.paused);
  $("#jobs-workers").textContent = `（同時 ${st.workers} 份）`;
  $("#jobs-add").disabled = st.paused;
  $("#jobs-retry-all").hidden = st.paused || !(c.failed + c.stopped);
  $("#jobs-progress").textContent = st.jobs.length ? `共 ${st.jobs.length} 份・處理中 ${pending}` : "";
  const order = Object.keys(st.bucket_labels);
  const jobs = st.jobs.filter((j) => !jobFilter || j.bucket === jobFilter || (jobFilter === "sampling" && j.bucket === "held"))
    .sort((a, b) => order.indexOf(a.bucket) - order.indexOf(b.bucket) || a.student_id.localeCompare(b.student_id));
  $("#jobs-card").hidden = !st.jobs.length;
  $("#jobs-filter-label").textContent = jobFilter ? `只顯示：${st.bucket_labels[jobFilter]}` : "";
  $("#jobs-filter-clear").hidden = !jobFilter;
  $("#jobs-rows").innerHTML = jobs.map((j) => {
    const [extract, cross, source, rule] = checkpoints(j);
    const label = JOB_BUTTON[j.bucket];
    const button = label ? `<button class="primary" data-open="${esc(j.student_id)}">${label}</button>`
      : j.state === "done" ? `<button class="ghost" data-open="${esc(j.student_id)}">查看</button>`
        : j.state === "confirmed" ? `<button class="ghost" data-student="${esc(j.student_id)}">審查結果</button>`
          : (j.state === "failed" || j.state === "stopped") && !st.paused ? `<button class="ghost" data-retry="${esc(j.student_id)}">重試</button>` : "";
    return `<tr><td><b>${esc(j.student_id)}</b></td><td><span class="bucket ${j.bucket}">${esc(st.bucket_labels[j.bucket])}</span></td>
      <td>${extract}</td><td>${cross}</td><td>${source}</td><td>${rule}</td>
      <td class="hide-sm">${j.seconds == null ? "" : `${j.seconds}`}</td><td>${button}</td></tr>`;
  }).join("") || `<tr><td colspan="8" class="empty">這一類沒有文件</td></tr>`;
  document.querySelectorAll("#jobs-rows [data-open]").forEach((b) => b.addEventListener("click", () => openJob(b.dataset.open)));
  document.querySelectorAll("#jobs-rows [data-student]").forEach((b) => b.addEventListener("click", () => {
    loadStudent(b.dataset.student); go("student");
  }));
  document.querySelectorAll("#jobs-rows [data-retry]").forEach((b) => b.addEventListener("click", () =>
    jobsAction("retry", { student_id: b.dataset.retry })));
  renderRelease(st);
  renderWork();
  renderEmpty();
  clearTimeout(jobsTimer);
  if (pending) jobsTimer = setTimeout(async () => {
    await refreshJobs();
    if (!(JOBS.counts.queued + JOBS.counts.running)) await Promise.all([loadData(), loadMetrics()]);
  }, 2000);
}
$("#jobs-filter-clear").addEventListener("click", () => { jobFilter = null; renderJobs(JOBS); });

function renderRelease(st) {
  const r = st.release, n = st.buckets.release || 0, a = r.active, el = $("#release-panel");
  const k = Math.min(n, Math.max(1, Math.ceil(n * r.sample_rate)));
  const last = r.last ? `<p class="hint" style="margin: 10px 0 0">上一批：${{ released: `抽查 ${r.last.sampled} 份無修改，放行 ${r.last.released} 份`
    + (r.last.failed.length ? `；${r.last.failed.length} 份重新檢查未通過，改為需修正` : ""),
  cancelled_by_sample: `抽查發現修改，${r.last.candidates} 份改為逐份核對`,
  cancelled_by_monitor: "監控者取消" }[r.last.outcome]}（${when(r.last.at)}）</p>` : "";
  let body = "";
  if (!r.enabled) {
    body = `<h2>批次放行已停用</h2><p>${esc(r.note)}。這一批請逐份核對；確認問題排除後再重新開啟。</p>
      <button class="ghost" data-release="reopen">重新開啟批次放行</button>`;
  } else if (a) {
    const done = a.sampled.filter((s) => a.results[s]);
    body = `<h2>批次放行：${a.status === "ready" ? "抽查完成" : "隨機抽查中"}</h2>
      <p>${a.candidates} 份建議放行，伺服器隨機抽出 ${a.sampled.length} 份（${done.length} / ${a.sampled.length} 已核對）。
        抽查有任何修改，本批取消、其餘全部改為逐份核對。</p>
      <ol>${a.sampled.map((s) => `<li><b>${esc(s)}</b> ${a.results[s] ? "✔ 已核對，無修改"
        : `<button class="primary" data-open="${esc(s)}">核對</button>`}</li>`).join("")}</ol>
      <div class="row">${a.status === "ready" ? `<button class="primary" data-release="finish">放行其餘 ${a.held.length} 份</button>` : ""}
        <button class="ghost" data-release="cancel">取消本批</button></div>`;
  } else if (n) {
    body = `<h2>${n} 份建議放行</h2>
      <p>兩個模型一致、原文核對相符、規則檢查與學分合計都沒有問題，也沒有經過 AI 重讀。
        按下後伺服器隨機抽出 ${k} 份讓你逐列核對，其餘 ${n - k} 份等抽查結果。</p>
      <button class="primary" data-release="start">批次確認（先抽查 ${k} 份）</button>`;
  }
  el.hidden = !body && !last;
  el.classList.toggle("off", !r.enabled);
  el.innerHTML = body + last + '<div id="release-msg"></div>';
  el.querySelectorAll("[data-open]").forEach((b) => b.addEventListener("click", () => openJob(b.dataset.open)));
  el.querySelectorAll("[data-release]").forEach((b) => b.addEventListener("click", () => releaseAction(b.dataset.release)));
}

async function releaseAction(action) {
  try {
    const st = await postJSON(action === "start" ? "/api/dashboard/release" : `/api/dashboard/release/${action}`,
      { reviewer: officer() });
    renderJobs(st);
    if (action === "finish") await Promise.all([loadData(), loadMetrics()]);
  } catch (e) {
    msg("#release-msg", esc(e.message), "err");
  }
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
  msg("#dash-msg", "");
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
  fileName($("#jobs-files"));
  $("#jobs-add").disabled = false;
  const lines = [`已加入佇列 ${added.length} 份`];
  if (unmatched.length) lines.push(`學號不在應屆名單、未解析 ${unmatched.length} 份：${unmatched.map(esc).join("、")}（已列入名單異常）`);
  msg("#jobs-msg", lines.join("<br>") + (failed.length
    ? `<ul class="upload-errors">${failed.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""),
    failed.length || unmatched.length ? "err" : "ok");
  renderRoster(await api("/api/roster-anomalies"));
  await refreshJobs();
});

$("#jobs-retry-all").addEventListener("click", () => jobsAction("retry"));

// ---------- AI 總開關（解析、擬稿、草擬規則一起停） ----------
let aiPaused = false;
function renderAiSwitch(paused) {
  aiPaused = paused;
  $("#ai-toggle").classList.toggle("off", paused);
  $("#ai-label").textContent = paused ? "AI 已停用（按此恢復）" : "AI 運作中";
}
$("#ai-toggle").addEventListener("click", () => jobsAction(aiPaused ? "resume" : "stop"));

// ---------- 指標 ----------
function pct(r, minN) {
  if (!r || !r.n) return '<span class="hint">—</span>';
  return r.n < minN ? `${r.k} / ${r.n}` : `${Math.round((r.k / r.n) * 100)}%<span class="hint">（${r.k} / ${r.n}）</span>`;
}
async function loadMetrics() {
  let m;
  try { m = await api("/api/metrics"); } catch (e) { return msg("#metrics-note", esc(e.message), "err"); }
  const p = (r) => pct(r, m.min_n);
  const both = (rows, key) => `<table><thead><tr><th>${key === "dashboard" ? "儀表板解析" : "學生來信"}</th><th>本批次</th><th>累計</th></tr></thead><tbody>`
    + rows.map(([label, f]) => `<tr><td>${label}</td><td>${f(m.batch[key])}</td><td>${f(m.total[key])}</td></tr>`).join("")
    + "</tbody></table>";
  const dash = [
    ["AI 解析次數", (d) => d.runs],
    ["完成率", (d) => p(d.completed)],
    ["轉人工率（仍有錯誤）", (d) => p(d.to_manual)],
    ["模型失敗", (d) => p(d.model_failed)],
    ["AI 重讀後修好", (d) => p(d.self_corrected)],
    ["等待秒數（中位）", (d) => d.wait_median ?? "—"],
    ["確認時有修改的份", (d) => p(d.docs_edited)],
    ["人工修改落在標示列", (d) => p(d.edits_in_flagged_rows)],
    ["抽查有修改", (d) => p(d.sampled_docs_edited)],
    ["批次放行份數", (d) => d.released_by_batch],
  ];
  const inbox = [
    ["AI 擬稿次數", (d) => d.runs],
    ["擬稿完成（有依據）", (d) => p(d.drafted)],
    ["轉人工", (d) => p(d.to_manual)],
    ["依據檢查後反思", (d) => p(d.reflected)],
    ["未修改即核准", (d) => p(d.approved_unedited)],
    ["修改後核准", (d) => p(d.approved_edited)],
    ["否決", (d) => p(d.rejected)],
    ["採用覆核建議", (d) => d.proposals_applied],
  ];
  const b = m.benchmark;
  const bench = b ? `<table><thead><tr><th colspan="2">虛構基準集量測（${esc(b.date)}）</th></tr></thead><tbody>
      <tr><td>案例／執行次數</td><td>${b.cases} 份／${b.runs} 次</td></tr>
      <tr><td>主模型欄位正確率</td><td>${p(b.field_accuracy)}</td></tr>
      <tr><td>整份全對</td><td>${p(b.fully_correct_docs)}</td></tr>
      <tr><td>交叉比對抓到的錯誤列</td><td>${p(b.catch_rate)}</td></tr>
      <tr><td>兩模型一致但仍錯的列</td><td>${p(b.agreed_error_rate)}</td></tr>
      <tr><td>需人工優先看的列</td><td>${p(b.review_share)}</td></tr>
    </tbody></table><p class="hint">${esc(b.disclaimer)}</p>`
    : `<table><thead><tr><th>虛構基準集量測</th></tr></thead><tbody><tr><td class="hint">尚未量測：python -m academic_twin.benchmark --runs 3</td></tr></tbody></table>`;
  const g = m.ai_budget || {};
  const budget = g.error ? `<td class="hint">${esc(g.error)}</td>`
    : `<td>${(g.used || 0).toLocaleString()} ／ ${g.budget ? g.budget.toLocaleString() : "未設上限"}${g.exceeded ? "（已達預算，AI 暫停）" : ""}</td>`;
  const cost = `<table><thead><tr><th colspan="2">AI 費用上限（${esc(g.month || "")}）</th></tr></thead><tbody>
      <tr><td>本月 token 已用／預算</td>${budget}</tr></tbody></table>
    <p class="hint">達到預算即停止呼叫模型，改走人工流程；可用 ACADEMIC_TWIN_TOKEN_BUDGET 調整。</p>`;
  $("#metrics").innerHTML = `<div>${both(dash, "dashboard")}</div><div>${both(inbox, "inbox")}</div><div>${bench}${cost}</div>`;
  $("#metrics-note").textContent = `${m.note}；樣本少於 ${m.min_n} 時只顯示件數。`;
}
$("#metrics-refresh").addEventListener("click", loadMetrics);

// ---------- 收件匣 ----------
let INBOX = null, inboxFilter = "", inboxSel = null, inboxTimer = null;
const TOOL_LABELS = { get_progress: "查進度", get_flags: "查提示與待辦", find_record: "查修課紀錄", get_requirement: "查畢業要求" };
const GROUNDING = { passed: "依據檢查通過", reflected_passed: "依據檢查未過，AI 反思 1 次後通過", failed: "依據檢查未通過（無法確認）",
  skipped: "此類型不由 AI 回覆" };

async function refreshInbox() {
  try {
    INBOX = await api("/api/inbox");
  } catch (e) {
    return msg("#inbox-msg", esc(e.message), "err");
  }
  renderInbox();
  renderWork();
  clearTimeout(inboxTimer);
  if (INBOX.counts.processing) inboxTimer = setTimeout(async () => {
    await refreshInbox();
    if (!INBOX.counts.processing) loadMetrics();
  }, 2000);
}

function renderInbox() {
  if (!INBOX) return;
  const c = INBOX.counts;
  $("#nav-inbox-count").textContent = c.drafted + c.manual ? `(${c.drafted + c.manual})` : "";
  $("#inbox-filter").innerHTML = `<option value="">全部（${INBOX.messages.length}）</option>`
    + Object.entries(INBOX.states).map(([k, v]) => `<option value="${k}" ${k === inboxFilter ? "selected" : ""}>${v}（${c[k]}）</option>`).join("");
  const safe = INBOX.messages.filter((m) => m.batch_ok);
  $("#inbox-approve-many").hidden = !safe.length;
  $("#inbox-approve-many").textContent = `批次核准 ${safe.length} 封`;
  $("#inbox-approve-many").title = "只限依據檢查通過、沒有覆核建議的擬稿";
  const list = INBOX.messages.filter((m) => !inboxFilter || m.state === inboxFilter
    || (inboxFilter === "new" && m.state === "processing"));
  $("#inbox-list").innerHTML = list.map((m) => `<div class="mail ${m.id === inboxSel ? "sel" : ""}" data-id="${m.id}">
      <div class="top"><span class="mstate ${m.state}">${esc(INBOX.states[m.state])}</span>
        <span class="subj">${esc(m.subject)}</span><span class="time">${shortWhen(m.received_at)}</span></div>
      <div class="meta"><span class="who">${m.type ? `${esc(INBOX.types[m.type])}・` : ""}${esc(m.student_id)}</span>
        <span class="marks">${m.error ? `<span class="m-err">${esc(shortError(m.error))}</span>` : ""}
          ${m.grounded === false ? '<span class="m-warn">⚠ 需核對</span>' : ""}${m.stale ? '<span class="m-warn">需重新擬稿</span>' : ""}
          ${m.proposals ? `<span>覆核建議 ${m.proposals}</span>` : ""}</span></div></div>`).join("")
    || '<p class="empty">沒有信件。按「匯入示範信件」載入虛構範例。</p>';
  document.querySelectorAll("#inbox-list .mail").forEach((el) => el.addEventListener("click", () => openMail(el.dataset.id)));
  if (inboxSel && !INBOX.messages.some((m) => m.id === inboxSel)) {
    inboxSel = null;
    $("#inbox-detail").innerHTML = '<p class="hint">選一封信查看 AI 擬稿。</p>';
  }
}
$("#inbox-filter").addEventListener("change", (e) => { inboxFilter = e.target.value; renderInbox(); });

async function openMail(id) {
  inboxSel = id;
  renderInbox();
  let m;
  try { m = await api(`/api/inbox/${encodeURIComponent(id)}`); } catch (e) { return msg("#inbox-msg", esc(e.message), "err"); }
  const t = m.trace;
  const open = m.state === "drafted" || m.state === "manual";
  // 日常核准用不到細節：收成一行，點開才看；有被擋下的步驟時預設展開
  const traceBad = t && (t.invalid_tools || t.grounding === "failed" || t.proposals_dropped);
  const trace = t ? `<details class="trace" ${traceBad ? "open" : ""}><summary><b>AI 做了什麼</b>
      <span class="hint">（${t.rounds} 輪・${Math.round(t.wait_seconds)} 秒）</span></summary>${t.replay ? ` <span class="replay">重播：${esc(t.replay.model)}・${esc(when(t.replay.generated_at))}產生</span>` : ""}
      <ol>${t.tools.map((x) => `<li>${esc(TOOL_LABELS[x] || x)}</li>`).join("")}
        ${t.invalid_tools ? `<li class="bad">要求白名單外的工具 ${t.invalid_tools} 次，已拒絕</li>` : ""}
        <li class="${t.grounding === "failed" ? "bad" : ""}">${esc(GROUNDING[t.grounding] || t.grounding)}</li>
        ${t.proposals_dropped ? `<li class="bad">丟棄不合法的覆核建議 ${t.proposals_dropped} 項</li>` : ""}</ol>
      <span class="hint">${t.rounds} 輪・${t.wait_seconds} 秒・${esc(t.model || "")}</span></details>` : "";
  const proposals = m.proposals.map((p, i) => `<div class="issue MANUAL_REVIEW">🔍 AI 覆核建議：<b>${esc(p.code)}</b>（${esc(p.term)}）
      → ${esc(META.review_actions[p.action] || p.action)}${p.category ? `「${esc(p.category)}」` : ""}
      <div class="hint">理由：${esc(p.reason || "—")}</div>
      ${p.applied ? `<div class="hint">${p.applied.accepted ? "已採用" : "已拒絕"}・${esc(p.applied.by)}・${when(p.applied.at)}</div>`
        : `<div class="row" style="margin-top:6px"><button class="primary" data-prop="${i}" data-accept="1">採用（寫入人工覆核並重審）</button>
           <button class="ghost" data-prop="${i}" data-accept="0">不採用</button></div>`}</div>`).join("");
  $("#inbox-detail").innerHTML = `<div class="stack-gap">
    <div class="row"><span class="mstate ${m.state}">${esc(INBOX.states[m.state])}</span><b>${esc(m.subject)}</b>
      <span class="spacer"></span><span class="hint">${esc(m.student_id)}・${when(m.received_at)}</span></div>
    <div class="letter">${esc(m.body)}</div>
    ${m.type ? `<div>問題類型：<b>${esc(INBOX.types[m.type])}</b></div>` : ""}
    ${m.notes.map((n) => `<div class="issue WARNING">⚠ ${esc(n)}</div>`).join("")}
    ${m.stale ? '<div class="issue WARNING">⚠ 審查結果已因採用覆核建議而改變，建議重新擬稿。</div>' : ""}
    ${m.error ? `<div class="issue FAIL">✘ ${esc(shortError(m.error))}
      <details class="fold" style="margin-top: 6px"><summary>錯誤詳情</summary><div class="hint" style="overflow-wrap: anywhere">${esc(m.error)}</div></details></div>` : ""}
    ${trace}
    ${proposals}
    ${m.state === "new" || m.state === "processing" ? '<p class="hint">尚未擬稿。</p>' : `<div>
      <label for="mail-reply"><b>${m.state === "replied" ? "已送出的回覆" : "回覆草稿"}</b>${m.grounded === false ? " <span style='color:var(--fail)'>（無法確認，請逐句核對或改寫）</span>" : ""}</label>
      <textarea id="mail-reply" class="reply ${m.grounded === false ? "unsure" : ""}" ${open ? "" : "readonly"}>${esc(m.reply)}</textarea></div>`}
    <div class="row">
      ${open ? `<button class="primary" data-act="approve">核准送出</button>
        <select id="mail-reason" aria-label="否決原因"><option value="">否決原因…</option>
          ${Object.entries(INBOX.reject_reasons).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select>
        <button class="ghost" data-act="reject">否決</button>
        ${m.state === "drafted" ? '<button class="ghost" data-act="self">轉給自己處理</button>' : ""}` : ""}
      ${["drafted", "manual", "failed", "new"].includes(m.state) ? '<button class="ghost" data-act="redraft">重新擬稿</button>' : ""}
      ${m.state === "replied" ? '<button class="ghost" data-act="copy">複製回覆</button>' : ""}
    </div>
    <div id="mail-msg"></div>
    ${m.history.length ? `<details class="fold"><summary>處理紀錄（${m.history.length}）</summary>${m.history.map((h) =>
      `<div class="hint">${when(h.at)}・${esc(h.by)}・${esc({ approve: "核准送出", reject: "否決", self: "轉給自己", apply_proposal: "採用覆核建議",
        decline_proposal: "不採用覆核建議" }[h.action] || h.action)}${h.edited_chars ? `（修改 ${h.edited_chars} 字）` : ""}</div>`).join("")}</details>` : ""}
  </div>`;
  $("#inbox-detail").querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => mailAction(m, b.dataset.act)));
  $("#inbox-detail").querySelectorAll("[data-prop]").forEach((b) => b.addEventListener("click", () =>
    applyProposal(m, Number(b.dataset.prop), b.dataset.accept === "1")));
}

async function mailAction(m, act) {
  if (act === "copy") {
    try { await navigator.clipboard.writeText($("#mail-reply").value); msg("#mail-msg", "已複製。", "ok"); }
    catch { $("#mail-reply").select(); msg("#mail-msg", "請按 Ctrl+C 複製。"); }
    return;
  }
  if (act === "redraft") {
    try { INBOX = await postJSON("/api/inbox/process", { id: m.id }); } catch (e) { return msg("#mail-msg", esc(e.message), "err"); }
    await refreshInbox();
    return openMail(m.id);
  }
  const body = { action: act, reviewer: officer() };
  if (act === "approve") body.reply = $("#mail-reply").value;
  if (act === "reject") body.reason = $("#mail-reason").value;
  try {
    await postJSON(`/api/inbox/${encodeURIComponent(m.id)}/decide`, body);
  } catch (e) {
    return msg("#mail-msg", esc(e.message), "err");
  }
  await Promise.all([refreshInbox(), loadMetrics()]);
  await openMail(m.id);
}

async function applyProposal(m, index, accept) {
  try {
    await postJSON(`/api/inbox/${encodeURIComponent(m.id)}/proposals/${index}`, { reviewer: officer(), accept });
  } catch (e) {
    return msg("#mail-msg", esc(e.message), "err");
  }
  await Promise.all([loadData(), refreshInbox(), loadMetrics()]);
  await openMail(m.id);
}

$("#inbox-demo").addEventListener("click", async () => {
  // 按鈕在「工具」分頁，訊息顯示在同一張卡片
  try { INBOX = await postJSON("/api/inbox/demo", {}); msg("#upload-msg", "已匯入虛構示範信件，到「收件匣」查看。", "ok"); }
  catch (e) { return msg("#upload-msg", esc(e.message), "err"); }
  inboxSel = null;
  await refreshInbox();
});
$("#inbox-process").addEventListener("click", async () => {
  try { await postJSON("/api/inbox/process", {}); msg("#inbox-msg", "AI 擬稿中，完成後自動更新。"); }
  catch (e) { return msg("#inbox-msg", esc(e.message), "err"); }
  await refreshInbox();
});
$("#inbox-approve-many").addEventListener("click", async () => {
  const ids = INBOX.messages.filter((m) => m.batch_ok).map((m) => m.id);
  try { await postJSON("/api/inbox/approve-many", { ids, reviewer: officer() }); msg("#inbox-msg", `已核准 ${ids.length} 封。`, "ok"); }
  catch (e) { return msg("#inbox-msg", esc(e.message), "err"); }
  await Promise.all([refreshInbox(), loadMetrics()]);
});
$("#inbox-import").addEventListener("click", async () => {
  let messages;
  try { messages = JSON.parse($("#inbox-json").value); } catch { return msg("#inbox-import-msg", "JSON 格式錯誤。", "err"); }
  try { await postJSON("/api/inbox/import", { messages }); msg("#inbox-import-msg", `已匯入 ${messages.length} 封。`, "ok"); }
  catch (e) { return msg("#inbox-import-msg", esc(e.message), "err"); }
  await refreshInbox();
});

// ---------- AI 草擬規則（確認僅存檔，啟用另行操作） ----------
let RULE_DRAFT = null, ruleEdited = false, ruleSourceUrl = null;
const RULE_DIFF_LABELS = { same: "相同", different: "欄位不同", missing_in_draft: "草稿漏列", extra_in_draft: "草稿多列" };

function renderRuleDraft(d) {
  RULE_DRAFT = d;
  ruleEdited = false;
  $("#rule-draft-panel").hidden = false;
  $("#rule-meta").textContent = `模型 ${d.model}・${when(d.generated_at)}`
    + (d.replay ? `・重播（${d.replay.reason}），非即時呼叫` : "");
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
  await Promise.all([refreshJobs(), refreshInbox()]);
  // 收件匣預先打開第一封待處理的信（待核准或轉人工）
  const first = INBOX?.messages.find((m) => m.state === "drafted" || m.state === "manual");
  if (first) openMail(first.id);
  await loadData();
  await Promise.all([loadRuleVersions(), loadMetrics()]);
})();
