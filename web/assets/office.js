// 系辦端（A 組）：批次畢審、名單異常、資料上傳、學生明細與人工覆核、規則來源
let BATCH = [], statusFilter = null, flagFilter = "";
const FLAG_LABELS = { overflow: "超修學分", repeatable: "可重複修習", substitution_pending: "待辦抵修" };
const show = setupTabs("twin-office-view");

// ---------- 批次畢審 ----------
function renderBatch() {
  const counts = { PASS: 0, WARNING: 0, FAIL: 0, MANUAL_REVIEW: 0 };
  BATCH.forEach((s) => counts[s.status]++);
  $("#stats").innerHTML = Object.keys(counts).map((k) => `
    <div class="card stat ${statusFilter === k ? "selected" : ""}" data-status="${k}">
      <div class="small">${chip(k)}</div><div class="n">${counts[k]}</div>
      <div class="muted small">${{ PASS: "條件全部符合", WARNING: "可能有問題，需確認", FAIL: "明確缺少條件", MANUAL_REVIEW: "需人工判斷" }[k]}</div>
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
  $("#batch-filter-count").textContent = `顯示 ${rows.length} / ${BATCH.length} 位`;
  $("#batch-rows").innerHTML = rows.map((s) => {
    const pct = s.total_required ? Math.min(100, (s.total_credits / s.total_required) * 100) : 0;
    return `<tr data-id="${esc(s.id)}">
      <td><b>${esc(s.id)}</b></td>
      <td>${chip(s.status)}</td>
      <td><div>${s.total_credits}</div><div class="meter ${pct >= 100 ? "ok" : ""}"><div style="width:${pct}%"></div></div></td>
      <td class="hide-sm">${s.satisfied} / ${s.requirements}</td>
      <td class="small hide-sm">${(s.overflow || []).map((o) => `<div>${esc(o.category)} +${o.excess}<span class="muted">（可移入外系 ${o.movable}）</span></div>`).join("") || '<span class="muted">—</span>'}</td>
      <td class="small">${s.reasons.map(esc).join("<br>") || '<span class="muted">全部符合</span>'}${s.reviewed ? `<div class="muted">已人工覆核 ${s.reviewed} 筆</div>` : ""}</td>
      <td class="small">${(s.flags || []).map((f) => `<div><b>${esc(FLAG_LABELS[f.type] || f.type)}</b>：${esc(f.message)}</div>`).join("") || '<span class="muted">無疑似誤判提示</span>'}
        ${s.action_count ? `<div>需辦理 ${s.action_count} 項，點此列查看</div>` : ""}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="7" class="empty">沒有符合的學生</td></tr>`;
  document.querySelectorAll("#batch-rows tr[data-id]").forEach((tr) =>
    tr.addEventListener("click", () => { loadStudent(tr.dataset.id); show("student"); }));
}

$("#flag-filter").addEventListener("change", (e) => {
  flagFilter = e.target.value;
  renderBatch();
});

function renderRoster(rows) {
  $("#roster-rows").innerHTML = rows.map((a) =>
    `<tr><td><b>${a.id ? esc(a.id) : "—"}</b></td><td class="small">${esc(a.message)}</td></tr>`
  ).join("") || `<tr><td colspan="2" class="empty">名單比對無異常</td></tr>`;
}

// ---------- 學生明細 ----------
async function loadStudent(id) {
  $("#student-select").value = id;
  const s = await api(`/api/students/${encodeURIComponent(id)}`);
  $("#student-status").innerHTML = chip(s.status);
  $("#student-credits").textContent = s.total_credits;
  const target = s.requirements.find((r) => r.kind === "total_credits")?.need;
  $("#student-total-target").textContent = target == null ? "學分（未設定總學分門檻）" : `/ ${target} 學分`;
  $("#student-report").href = `/api/students/${encodeURIComponent(s.id)}/report`;
  $("#student-twin").href = `student.html?id=${encodeURIComponent(s.id)}`;
  renderBreakdown(s, target ?? 0);
  renderIssues(s);
  renderRequirements(s);
  renderRecords(s);
}

function renderIssues(s) {
  const iss = s.issues, out = [];
  iss.review.forEach((r) => out.push(`<div class="issue MANUAL_REVIEW">🔍 <b>${esc(r.code)} ${esc(r.name)}</b>（${r.term}，類別「${esc(r.category)}」）需人工判斷如何採計${reviewForm(r)}</div>`));
  iss.failed.forEach((r) => out.push(`<div class="issue FAIL">✘ <b>${esc(r.code)} ${esc(r.name)}</b>（${r.term}）${r.grade} 分不及格，尚未重修通過</div>`));
  iss.dept_unverified.forEach((r) => out.push(`<div class="issue WARNING">⚠ <b>${esc(r.code)} ${esc(r.name)}</b>（${r.term}）不在規則庫，暫以本系選修 ${r.credits} 學分計${reviewForm(r)}</div>`));
  s.flags.forEach((f) => out.push(`<div class="issue WARNING">⚑ 待確認提示：<b>${esc(FLAG_LABELS[f.type] || f.type)}</b>：${esc(f.message)}
    <div class="muted small">相關要求：${f.requirement_ids.map(esc).join("、") || "待確認"}；相關課程：${f.courses.map(esc).join("、") || "無"}</div></div>`));
  s.actions.forEach((a) => out.push(`<div class="issue WARNING"><b>需辦理：${esc(a.title)}</b>
    <div>承辦單位：${esc(a.office)}</div>
    <div class="muted small">相關要求：${a.requirement_ids.map(esc).join("、") || "待確認"}；相關課程：${a.courses.map(esc).join("、") || "無"}</div></div>`));
  s.reviews.forEach((r) => out.push(`<div class="issue reviewed">✓ <b>${esc(r.code)} ${esc(r.name)}</b>（${r.term}）已覆核：<b>${esc(r.label)}</b>
    <div class="muted small">${esc(r.reviewer)}・${esc(r.decided_at)}${r.note ? `・${esc(r.note)}` : ""}
      ${r.outdated ? `<span class="outdated">・決定時的規則版本為 ${esc(r.rule_version)}，規則庫已更新，建議再確認</span>` : ""}</div>
    <div class="review-form" data-code="${esc(r.code)}" data-term="${esc(r.term)}">
      <input class="note" placeholder="撤銷原因（選填）"><button class="ghost" data-action="revoke">撤銷決定</button></div></div>`));
  $("#issues").innerHTML = out.join("");
  $("#issues-card").style.display = out.length || s.review_history.length ? "" : "none";
  $("#reviewer-row").style.display = iss.review.length || iss.dept_unverified.length || s.reviews.length ? "" : "none";
  $("#review-msg").innerHTML = "";
  $("#review-history-box").style.display = s.review_history.length ? "" : "none";
  $("#review-history-count").textContent = s.review_history.length;
  $("#review-history").innerHTML = s.review_history.map((e) =>
    `<div style="margin-bottom: 4px">${esc(e.decided_at)}・${esc(e.reviewer)}：${esc(e.code)}（${esc(e.term)}）${esc(e.label)}${e.note ? `・${esc(e.note)}` : ""}</div>`).join("");
  document.querySelectorAll("#issues .review-form button").forEach((b) => b.addEventListener("click", () => submitReview(s.id, b)));
}

$("#student-select").addEventListener("change", (e) => loadStudent(e.target.value));

// ---------- 人工覆核 ----------
function reviewForm(r) {
  const options = [["dept_elective", "確認為本系選修"], ["outside", "外系認列"], ["exclude", "不採計"]]
    .concat(META.categories.map((c) => [`category:${c}`, `歸入「${c}」`]));
  return `<div class="review-form" data-code="${esc(r.code)}" data-term="${esc(r.term)}">
    <select><option value="">選擇採計方式…</option>${options.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("")}</select>
    <input class="note" placeholder="依據或備註（選填）">
    <button class="primary" data-action="decide">確認</button></div>`;
}

async function submitReview(sid, button) {
  const form = button.closest(".review-form"), msg = $("#review-msg");
  const reviewer = $("#reviewer").value.trim();
  const choice = button.dataset.action === "revoke" ? "revoke" : form.querySelector("select").value;
  const fail = (text) => { msg.innerHTML = `<div class="small" style="color: var(--fail); margin-top: 6px">${esc(text)}</div>`; };
  if (!reviewer) return fail("請先填寫覆核人。");
  if (!choice) return fail("請選擇採計方式。");
  try { localStorage.setItem("twin-reviewer", reviewer); } catch {}
  const [action, category = ""] = choice.split(/:(.*)/);
  try {
    await api(`/api/students/${encodeURIComponent(sid)}/reviews`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: form.dataset.code, term: form.dataset.term, action, category, reviewer,
                             note: form.querySelector("input.note").value }),
    });
    BATCH = await api("/api/students");
    renderBatch();
    fillStudentSelect();
    await loadStudent(sid);
  } catch (e) {
    fail(e.message);
  }
}
try { $("#reviewer").value = localStorage.getItem("twin-reviewer") || ""; } catch {}

// ---------- 規則來源 ----------
function renderRules() {
  $("#sources").innerHTML = Object.entries(META.sources).map(([k, v]) => `<li>${esc(v)} <span class="muted small">(${k})</span></li>`).join("");
  $("#assumptions").innerHTML = META.assumptions.map((a) => `<li>${esc(a)}</li>`).join("");
}

// ---------- 資料上傳 ----------
function renderSource(src) {
  $("#data-source").textContent =
    `修課紀錄 ${src.records}（${src.students} 位、${src.records_count} 筆）・應屆名單 ${src.roster}（${src.roster_count} 位）`;
}

function fillStudentSelect() {
  $("#student-select").innerHTML = BATCH.map((s) => `<option value="${esc(s.id)}">${esc(s.id)}（${STATUS[s.status]}）</option>`).join("");
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
  const msg = $("#upload-msg");
  msg.innerHTML = `<div class="muted small" style="margin-top: 8px">處理中…</div>`;
  try {
    await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    await loadData();
    msg.innerHTML = `<div class="small" style="margin-top: 8px; color: var(--pass)">已套用，審查結果已更新。</div>`;
    $("#records-file").value = $("#roster-file").value = "";
  } catch (e) {
    msg.innerHTML = `<div class="small" style="margin-top: 8px; color: var(--fail)"><b>${esc(e.message)}</b>，目前資料維持不變。</div>`
      + (e.details?.length ? `<ul class="upload-errors">${e.details.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>` : "");
  }
}

const readFile = async (input) => {
  const f = input.files[0];
  return f ? { name: f.name, text: await f.text() } : null;
};

$("#upload-btn").addEventListener("click", async () => {
  const records = await readFile($("#records-file")), roster = await readFile($("#roster-file"));
  if (!records && !roster) {
    $("#upload-msg").innerHTML = `<div class="small" style="margin-top: 8px; color: var(--fail)">請先選擇至少一個檔案。</div>`;
    return;
  }
  await sendData("/api/upload", { records, roster });
});
$("#reset-btn").addEventListener("click", () => sendData("/api/reset", {}));

// ---------- AI 解析儀表板 ----------
let DRAFT = null;
const DASH_FIELDS = ["rule_code", "term", "code", "name", "credits", "grade"];
const DASH_LABELS = { rule_code: "規則代碼", term: "學期", code: "課號", name: "課名", credits: "學分", grade: "成績" };

function dashMsg(el, html, color = "var(--muted)") {
  $(el).innerHTML = html ? `<div class="small" style="margin-top: 8px; color: ${color}">${html}</div>` : "";
}

function invalidateDraftConfirmation() {
  $("#dash-checked").checked = $("#dash-ack").checked = false;
  dashMsg("#dash-confirm-msg", "");
}

// 雙模型交叉驗證摘要：一致不代表正確，只用來排出優先核對的列
function crossCheckSummary(cc) {
  if (!cc) return "";
  if (cc.status !== "ok") return `<div class="issue WARNING">⚠ 未交叉驗證：${esc(cc.note)}；請逐列對照原文</div>`;
  const flagged = cc.flagged_rows + cc.missing_in_primary;
  return `<div class="issue ${flagged ? "WARNING" : "PASS"}">${flagged ? "⚠" : "✔"} 雙模型交叉驗證（${esc(cc.primary_model)}／${esc(cc.check_model)}）：`
    + `一致 ${cc.agreed_rows} 列、需優先核對 ${cc.flagged_rows} 列`
    + (cc.missing_in_primary ? `、第二模型多擷取 ${cc.missing_in_primary} 列（可能漏列）` : "")
    + `。兩模型一致不代表正確，送出前仍須逐列對照原文。</div>`;
}

function renderDraft(fresh = false) {
  const d = DRAFT;
  $("#dash-draft").hidden = false;
  $("#dash-meta").textContent = `模型 ${d.model}・${d.generated_at}・提示 ${d.prompt_version}・版型 ${d.format}`
    + (d.fallback_from ? `・已改用備援（${d.fallback_from}）` : "");
  if (fresh) $("#dash-cohort").value = d.cohort ?? "";
  $("#dash-declared").textContent = `文件標示：應修 ${d.declared_required ?? "—"}、實得 ${d.declared_earned ?? "—"} 學分`
    + `；系統有 ${d.cohorts.join("、")} 學年度規則；空白時沿用名單年度，名單也未填則用 ${META.cohort}`;
  $("#dash-exists").textContent = d.exists ? `；${d.student_id} 已有修課紀錄，確認後將以本次紀錄取代` : "";
  const docIssues = d.issues.filter((i) => i.row === null);
  const rowIssues = (i) => d.issues.filter((x) => x.row === i);
  $("#dash-cross").innerHTML = crossCheckSummary(d.cross_check);
  $("#dash-issues").innerHTML = docIssues.map((i) =>
    `<div class="issue ${i.level === "error" ? "FAIL" : "WARNING"}">${i.level === "error" ? "✘" : "⚠"} ${esc(i.message)}</div>`).join("");
  const cats = ["", ...d.categories];
  $("#dash-rows").innerHTML = d.rows.map((r, i) => {
    const iss = rowIssues(i);
    const cls = iss.some((x) => x.level === "error") ? "has-error" : iss.length ? "has-warning" : "";
    return `<tr class="${cls}" data-i="${i}">
      <td>${i + 1}</td>
      ${DASH_FIELDS.map((f) => `<td><input data-f="${f}" aria-label="第 ${i + 1} 列${DASH_LABELS[f]}" title="${esc(r[f] ?? "")}" value="${esc(r[f] ?? "")}"></td>`).join("")}
      <td><select data-f="category" aria-label="第 ${i + 1} 列類別">${cats.map((c) => `<option ${c === r.category ? "selected" : ""} value="${esc(c)}">${esc(c || "依課號判斷")}</option>`).join("")}</select></td>
      <td class="src"><label class="source-page">頁碼 <input type="number" min="1" step="1" data-f="source_page" aria-label="第 ${i + 1} 列來源頁碼" value="${esc(r.source_page ?? "")}"></label>
        <textarea data-f="source_text" rows="2" aria-label="第 ${i + 1} 列來源原文" placeholder="請填 PDF 上該列原文">${esc(r.source_text || "")}</textarea>
        ${iss.map((x) => `<div class="row-issue">${x.level === "error" ? "✘" : "⚠"} ${esc(x.message)}</div>`).join("")}</td>
      <td><button class="ghost small" data-del="${i}">刪除</button></td>
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
  $("#dash-ack-row").hidden = !d.issues.some((i) => i.level === "warning");
  invalidateDraftConfirmation();
}

$("#dash-cohort").addEventListener("input", invalidateDraftConfirmation);

$("#dash-extract").addEventListener("click", async () => {
  const file = $("#dash-file").files[0], sid = $("#dash-sid").value.trim();
  if (!file || !sid) return dashMsg("#dash-msg", "請選擇 PDF 並填寫學號。", "var(--fail)");
  DRAFT = null;
  $("#dash-draft").hidden = true;
  invalidateDraftConfirmation();
  const started = Date.now();
  const waiting = () => dashMsg("#dash-msg", `AI 解析中，已等待 ${Math.round((Date.now() - started) / 1000)} 秒。`
    + "主模型預設最多等 90 秒，逾時自動改用備援模型（最多 120 秒）；確認前不會改動審查資料。");
  waiting();
  const timer = setInterval(waiting, 1000);
  $("#dash-extract").disabled = true;
  $("#dash-file").disabled = $("#dash-sid").disabled = true;
  try {
    const bytes = new Uint8Array(await file.arrayBuffer());
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    DRAFT = await api("/api/dashboard/extract", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ student_id: sid, data: btoa(bin) }) });
    const errors = DRAFT.issues.filter((i) => i.level === "error").length, warnings = DRAFT.issues.length - errors;
    dashMsg("#dash-msg", `擷取 ${DRAFT.rows.length} 列；錯誤 ${errors}、警示 ${warnings}。結果為待確認草稿，尚未進入審查。`);
    renderDraft(true);
  } catch (e) {
    dashMsg("#dash-msg", `<b>${esc(e.message)}</b>`, "var(--fail)");
  } finally {
    clearInterval(timer);
    $("#dash-extract").disabled = false;
    $("#dash-file").disabled = $("#dash-sid").disabled = false;
  }
});

$("#dash-add").addEventListener("click", () => {
  DRAFT.rows.push({ rule_code: "", term: "", code: "", name: "", credits: "", grade: "", category: "",
    source_page: null, source_text: "" });
  renderDraft();
});

$("#dash-confirm").addEventListener("click", async () => {
  if (!DRAFT || $("#dash-confirm").disabled) return;
  if (!$("#dash-checked").checked) return dashMsg("#dash-confirm-msg", "請先逐列對照原文並勾選確認。", "var(--fail)");
  $("#dash-confirm").disabled = true;
  try {
    const s = await api("/api/dashboard/confirm", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft_id: DRAFT.draft_id, rows: DRAFT.rows, reviewer: $("#dash-reviewer").value,
        cohort: $("#dash-cohort").value.trim(), acknowledge_warnings: $("#dash-ack").checked }) });
    DRAFT = null;
    $("#dash-draft").hidden = true;
    dashMsg("#dash-msg", "");
    await loadData();
    await loadStudent(s.id);
    show("student");
  } catch (e) {
    if ([e.message, ...(e.details || [])].some((m) => m.includes("警示"))) $("#dash-ack-row").hidden = false;
    dashMsg("#dash-confirm-msg", `<b>${esc(e.message)}</b>`
      + (e.details?.length ? `<ul class="upload-errors">${e.details.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>` : ""), "var(--fail)");
  } finally {
    $("#dash-confirm").disabled = false;
  }
});

// ---------- AI 草擬規則（確認僅存檔，永不啟用） ----------
let RULE_DRAFT = null, ruleNeedsCheck = false, ruleSourceUrl = null;
const RULE_DIFF_LABELS = { same: "相同（仍須核對原文）", different: "欄位不同・需人工確認",
  missing_in_draft: "草稿漏列・需人工確認", extra_in_draft: "草稿多列・需人工確認" };
const rulePost = (action, body) => api('/api/rule-drafts/' + action, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});

function renderRuleDraft(d) {
  RULE_DRAFT = d;
  ruleNeedsCheck = false;
  $("#rule-draft-panel").hidden = false;
  $("#rule-meta").textContent = '模型 ' + d.model + '・' + d.generated_at + '・提示 ' + d.prompt_version;
  $("#rule-issues").innerHTML = d.issues.map((i) => '<div class="issue FAIL">' + esc(i.message) + '</div>').join("")
    + (d.baseline_changed ? '<div class="issue WARNING">對照 JSON 在草擬後已改變，請重新核對全部差異。</div>' : "");
  $("#rule-requirements").innerHTML = (Array.isArray(d.requirements) ? d.requirements : []).map((r) => {
    if (!r || typeof r !== 'object') return '<div class="issue FAIL">要求格式錯誤，請修正。</div>';
    return '<div class="issue WARNING"><b>' + esc(r.id) + ' ' + esc(r.title) + '</b>・AI 草稿、未啟用'
      + '<div>適用年度：' + esc(r.applicable_year) + '；PDF 第 ' + esc(r.source_page) + ' 頁；位置：' + esc(r.source?.ref) + '</div>'
      + '<blockquote style="white-space: pre-wrap">' + esc(r.source_quote) + '</blockquote>'
      + '<div>門檻／清單：' + esc(JSON.stringify(r.courses ?? r.categories ?? [])) + '；最低學分：' + esc(r.min_credits ?? '依清單') + '</div>'
      + '<div>適用條件：' + esc(r.applicability) + '</div><div>例外：' + esc(r.exceptions) + '</div>'
      + '<div>核對事項：' + esc(r.review_notes) + '</div></div>';
  }).join("");
  $("#rule-diff").innerHTML = d.diff.map((r) => '<details class="small" style="margin-bottom: 8px"><summary>'
    + esc(r.id) + '：' + esc(RULE_DIFF_LABELS[r.status]) + '</summary>'
    + r.fields.map((f) => '<div style="overflow-wrap: anywhere">' + esc(f.field) + '：'
      + esc(f.change === 'added' ? '（原無欄位）' : JSON.stringify(f.before)) + ' → '
      + esc(f.change === 'removed' ? '（草稿無欄位）' : JSON.stringify(f.after)) + '</div>').join("") + '</details>').join("");
  $("#rule-editor").value = JSON.stringify(d.requirements, null, 2);
  $("#rule-checked").checked = false;
  const confirmed = d.state === 'confirmed';
  $("#rule-editor").disabled = $("#rule-check").disabled = $("#rule-confirm").disabled = confirmed;
  $("#rule-checked").disabled = confirmed;
}

function invalidateRuleCheck() {
  ruleNeedsCheck = true;
  $("#rule-checked").checked = false;
  dashMsg("#rule-confirm-msg", "內容已修改，請重新檢查後核對差異，再勾選確認。");
}
$("#rule-editor").addEventListener("input", invalidateRuleCheck);
$("#rule-file").addEventListener("change", () => {
  RULE_DRAFT = null;
  $("#rule-draft-panel").hidden = true;
  dashMsg("#rule-msg", "");
  if (ruleSourceUrl) URL.revokeObjectURL(ruleSourceUrl);
  const file = $("#rule-file").files[0];
  ruleSourceUrl = file ? URL.createObjectURL(file) : null;
  $("#rule-source").hidden = !file;
  if (file) $("#rule-source").href = ruleSourceUrl;
});

$("#rule-extract").addEventListener("click", async () => {
  const file = $("#rule-file").files[0];
  if (!file) return dashMsg("#rule-msg", "請選擇公開 114 應修科目表 PDF。", "var(--fail)");
  RULE_DRAFT = null;
  $("#rule-draft-panel").hidden = true;
  dashMsg("#rule-confirm-msg", "");
  $("#rule-extract").disabled = $("#rule-file").disabled = true;
  const start = Date.now();
  const waiting = () => dashMsg("#rule-msg", 'AI 草擬中，已等待 ' + Math.round((Date.now() - start) / 1000) + ' 秒；結果為 AI 草稿、未啟用。');
  waiting();
  const timer = setInterval(waiting, 1000);
  try {
    const bytes = new Uint8Array(await file.arrayBuffer());
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    const d = await rulePost('extract', { data: btoa(bin), year: Number($("#rule-year").value) });
    renderRuleDraft(d);
    dashMsg("#rule-msg", '已保存 AI 草稿、未啟用；結構錯誤 ' + d.issues.length + ' 項。請對照原文。');
  } catch (e) {
    dashMsg("#rule-msg", esc(e.message), "var(--fail)");
  } finally {
    clearInterval(timer);
    $("#rule-extract").disabled = $("#rule-file").disabled = false;
  }
});

$("#rule-check").addEventListener("click", async () => {
  if (!RULE_DRAFT) return;
  $("#rule-check").disabled = $("#rule-confirm").disabled = $("#rule-editor").disabled = true;
  try {
    const requirements = JSON.parse($("#rule-editor").value);
    const d = await rulePost('check', { draft_id: RULE_DRAFT.draft_id, requirements });
    renderRuleDraft(d);
    dashMsg("#rule-confirm-msg", '已重新檢查；錯誤 ' + d.issues.length + ' 項，請重新核對並勾選確認。');
  } catch (e) {
    dashMsg("#rule-confirm-msg", esc(e.message), "var(--fail)");
  } finally {
    $("#rule-check").disabled = $("#rule-confirm").disabled = $("#rule-editor").disabled = false;
  }
});

$("#rule-confirm").addEventListener("click", async () => {
  if (!RULE_DRAFT || $("#rule-confirm").disabled) return;
  if (ruleNeedsCheck || !$("#rule-checked").checked) return dashMsg("#rule-confirm-msg", "請重新檢查、核對原文與差異，再勾選確認。", "var(--fail)");
  if (!$("#rule-reviewer").value.trim()) return dashMsg("#rule-confirm-msg", "請填寫確認人。", "var(--fail)");
  $("#rule-confirm").disabled = $("#rule-check").disabled = $("#rule-editor").disabled = $("#rule-extract").disabled = $("#rule-file").disabled = true;
  try {
    const d = await rulePost('confirm', { draft_id: RULE_DRAFT.draft_id, requirements: JSON.parse($("#rule-editor").value),
      reviewer: $("#rule-reviewer").value, acknowledge: true });
    renderRuleDraft(d);
    dashMsg("#rule-confirm-msg", '已確認存檔（AI 草稿、未啟用）：' + esc(d.confirmation.reviewer) + '・' + esc(d.confirmation.confirmed_at)
      + '；修改 ' + d.confirmation.edited_field_count + ' 欄。檔案：data/eval/rule_drafts/' + esc(d.draft_id) + '.confirmed.json；可在下方「規則版本與啟用」明確啟用。');
    await loadRuleVersions();
  } catch (e) {
    $("#rule-checked").checked = false;
    dashMsg("#rule-confirm-msg", esc(e.message), "var(--fail)");
  } finally {
    const confirmed = RULE_DRAFT?.state === 'confirmed';
    $("#rule-confirm").disabled = $("#rule-check").disabled = $("#rule-editor").disabled = confirmed;
    $("#rule-extract").disabled = $("#rule-file").disabled = false;
  }
});

// ---------- 規則版本（已確認草稿經承辦人明確啟用；可還原） ----------
function renderRuleVersions(v) {
  $("#rule-versions-years").innerHTML = v.years.map((y) => `<div class="issue ${y.activated ? "WARNING" : "PASS"}">
    <b>${y.year} 學年度</b>：${y.activated
      ? `啟用版本（草稿 ${esc(y.activated.draft_id.slice(0, 8))}，取代 ${esc(y.activated.replaced.join("、") || "—")}`
        + `${y.activated.added.length ? `、新增 ${esc(y.activated.added.join("、"))}` : ""}；${esc(y.activated.by)}・${esc(y.activated.at)}）`
        + ` <button class="ghost small" data-revert="${y.year}">還原為正式規則庫</button>`
      : "正式規則庫"}<div class="muted small">規則版本 ${esc(y.rule_version)}</div></div>`).join("");
  $("#rule-versions-drafts").innerHTML = v.confirmed_drafts.map((d) => `<div class="small" style="margin-bottom: 6px">
    ${d.year} 學年度・${esc(d.requirement_ids.join("、"))}・確認人 ${esc(d.reviewer)}・${esc(d.confirmed_at)}
    <button class="primary small" data-activate="${esc(d.draft_id)}">啟用</button></div>`).join("")
    || '<span class="muted small">尚無已確認的草稿</span>';
  $("#rule-versions-history").innerHTML = v.history.map((e) => `<div>${esc(e.at)}・${e.year} 學年度・`
    + (e.type === "activate" ? `啟用草稿 ${esc(e.draft_id.slice(0, 8))}` : "還原為正式規則庫") + `・${esc(e.by)}</div>`).join("")
    || '<span class="muted">尚無紀錄</span>';
  document.querySelectorAll("[data-activate]").forEach((b) => b.addEventListener("click", () =>
    changeRuleVersion("activate", { draft_id: b.dataset.activate, acknowledge: $("#rule-activate-ack").checked,
      activated_by: $("#rule-activator").value })));
  document.querySelectorAll("[data-revert]").forEach((b) => b.addEventListener("click", () =>
    changeRuleVersion("revert", { year: Number(b.dataset.revert), reverted_by: $("#rule-activator").value })));
}

async function loadRuleVersions() {
  try {
    renderRuleVersions(await api("/api/rule-versions"));
  } catch (e) {
    dashMsg("#rule-versions-msg", esc(e.message), "var(--fail)");
  }
}

async function changeRuleVersion(action, body) {
  if (!$("#rule-activator").value.trim()) return dashMsg("#rule-versions-msg", "請填寫啟用／還原人。", "var(--fail)");
  if (action === "activate" && !$("#rule-activate-ack").checked)
    return dashMsg("#rule-versions-msg", "請先勾選了解啟用的影響。", "var(--fail)");
  try {
    renderRuleVersions(await api(`/api/rule-versions/${action}`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }));
    $("#rule-activate-ack").checked = false;
    dashMsg("#rule-versions-msg", action === "activate" ? "已啟用，審查結果已依新版本更新。" : "已還原為正式規則庫，審查結果已更新。");
    await loadMeta();
    renderRules();
    await loadData();
  } catch (e) {
    dashMsg("#rule-versions-msg", esc(e.message), "var(--fail)");
  }
}

// ---------- 啟動 ----------
(async function init() {
  await loadMeta();
  renderRules();
  await loadData();
  await loadRuleVersions();
})();
