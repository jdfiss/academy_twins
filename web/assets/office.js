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
      <td class="small">${s.reasons.map(esc).join("<br>") || '<span class="muted">全部符合</span>'}${s.reviewed ? `<div class="muted">已人工覆核 ${s.reviewed} 筆</div>` : ""}</td>
      <td class="small">${(s.flags || []).map((f) => `<div><b>${esc(FLAG_LABELS[f.type] || f.type)}</b>：${esc(f.message)}</div>`).join("") || '<span class="muted">無疑似誤判提示</span>'}
        ${s.action_count ? `<div>需辦理 ${s.action_count} 項，點此列查看</div>` : ""}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="6" class="empty">沒有符合的學生</td></tr>`;
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

// ---------- 啟動 ----------
(async function init() {
  await loadMeta();
  renderRules();
  await loadData();
})();
