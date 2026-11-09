// 學生端（B 組）：個人進度試算、誤判提示、需辦理事項、修課規劃與 What-if、先修關係圖
// 網址加 ?demo 會改讀 assets/demo-twin.json（交接格式範例，含 flags、actions），A 組偵測完成前用來做畫面
let CURRENT = null, BASE_PLAN = null;
const params = new URLSearchParams(location.search);
const DEMO = params.has("demo");
setupTabs("twin-student-view");

// ---------- 我的進度 ----------
async function loadStudent(id) {
  const s = DEMO ? await api("assets/demo-twin.json") : await api(`/api/twin/${encodeURIComponent(id)}`);
  CURRENT = s;
  $("#student-select").value = s.id;
  $("#student-status").innerHTML = chip(s.status);
  $("#trial-badge").style.display = s.trial ? "" : "none";
  $("#student-credits").textContent = s.total_credits;
  const target = s.requirements.find((r) => r.kind === "total_credits")?.need;
  $("#student-total-target").textContent = target == null ? "學分" : `/ ${target} 學分`;
  $("#student-next").textContent = `下一學期：${s.next_term}`;
  try { history.replaceState(null, "", `?${DEMO ? "demo&" : ""}id=${encodeURIComponent(s.id)}`); } catch {}
  renderBreakdown(s, target ?? 0);
  const summary = renderSummary(s);
  renderFlags(s);
  renderActions(s);
  renderIssues(s);
  renderTiles(s, summary, target);
  // 待確認（英檢）不顯示系辦用語
  renderRequirements(s, (r) => flagNote(s, r), "");
  foldSatisfied();
  renderRecords(s);
  await loadPlan(s.id);
}

// 比照系辦端：已符合的條件收進一行「已符合 N 項」，點開才看細節（只搬動畫面，不改判定）
function foldSatisfied() {
  const box = $("#requirements"), done = [...box.querySelectorAll(":scope > .req.ok")];
  if (!done.length) return;
  const group = document.createElement("details");
  group.className = "req-ok-group";
  group.innerHTML = `<summary>✔ 已符合 ${done.length} 項</summary><div class="req-ok-list"></div>`;
  done.forEach((el) => group.lastElementChild.appendChild(el));
  box.appendChild(group);
}

// ---------- 疑似誤判與需辦理事項（flags、actions 由審查核心產生，這裡只組成白話說明） ----------
// overflow、repeatable：本試算已依規則採計，但校務系統可能不計；substitution_pending：辦理抵修前兩邊都仍列缺課
const FLAG_INFO = {
  overflow: {
    label: "超修學分",
    text: (f) => `你在某一類課程修得比需求多，其中 ${f.credits} 學分可能可以移到其他類別採計。校務系統「一個蘿蔔一個坑」，不會自動移動多修的學分，所以儀表板上的學分可能比這裡少；能認列多少由承辦人確認。`,
  },
  repeatable: {
    label: "可重複修習",
    text: (f) => `這門課允許重複修習，你修了不只一次，第二次起的 ${f.credits} 學分依規則可以採計。但校務系統第二次起不計學分，儀表板可能顯示不合格；這是系統限制，不一定是真的缺學分，請找承辦人確認。`,
  },
  substitution_pending: {
    label: "待辦抵修",
    text: () => "原本的課程已停開，你已經修過可以替代的課程，但還沒辦理抵修。辦理前，校務系統和這裡的試算都會顯示缺這門課；辦好抵修後才會算符合。",
  },
};
const flagLabel = (type) => FLAG_INFO[type]?.label ?? type;
const reqTitle = (s, id) => s.requirements.find((r) => r.id === id)?.title ?? id;
// 課號 → 「課號 課名」；課名只取審查結果裡已有的資料
function courseLabel(s, code) {
  const c = [...s.records, ...s.remaining_courses, ...s.requirements.flatMap((r) => r.missing)].find((x) => x.code === code);
  return c ? `${c.code} ${c.name}` : code;
}
const related = (s, x) => [
  x.requirement_ids.length ? `相關條件：${x.requirement_ids.map((id) => esc(reqTitle(s, id))).join("、")}` : "",
  x.courses.length ? `相關課程：${x.courses.map((c) => esc(courseLabel(s, c))).join("、")}` : "",
].filter(Boolean).join("<br>");
const ACTION_TYPES = { substitution: "抵修", transfer_credit: "抵免", manual_recognition: "人工認列" };
const officeText = (a) => a.office === "待確認" ? "待確認（尚未取得正式承辦單位）" : esc(a.office);

function renderFlags(s) {
  $("#flags").innerHTML = s.flags.length
    ? s.flags.map((f) => `<div class="issue WARNING">⚑ <b>${esc(flagLabel(f.type))}</b>
        <div class="small" style="margin-top:4px">${FLAG_INFO[f.type] ? FLAG_INFO[f.type].text(f) : esc(f.message)}</div>
        <div class="muted small" style="margin-top:4px">${related(s, f)}</div></div>`).join("")

    : `<div class="muted small">目前沒有偵測到可能被校務系統誤判的項目。</div>`;
}

function renderActions(s) {
  $("#actions").innerHTML = s.actions.map((a) => `<div class="issue">📋 <b>${esc(a.title)}</b>${ACTION_TYPES[a.type] ? ` <span class="terms">${ACTION_TYPES[a.type]}</span>` : ""}
      <div class="small">承辦單位：${officeText(a)}${a.from_flag ? `・來自提示「${esc(flagLabel(a.from_flag))}」` : ""}</div>
      <div class="muted small">${related(s, a)}</div></div>`).join("")
    || `<div class="muted small">目前沒有需要辦理的事項。</div>`;
}

// 畢業條件下標出相關提示與需辦理事項，讓學生分得出「真的缺」與「可能是系統限制」
function flagNote(s, r) {
  const types = [...new Set(s.flags.filter((f) => f.requirement_ids.includes(r.id)).map((f) => f.type))];
  const actions = s.actions.filter((a) => a.requirement_ids.includes(r.id));
  const lines = [
    types.length ? `⚑ 相關提示：${types.map((t) => esc(flagLabel(t))).join("、")}（見「可能是校務系統誤判」）` : "",
    ...actions.map((a) => `📋 需辦理：${esc(a.title)}（承辦單位：${officeText(a)}）`),
  ].filter(Boolean);
  return lines.length ? `<div class="small" style="margin-top:4px;color:var(--warn)">${lines.join("<br>")}</div>` : "";
}

// 一眼看出：真正缺修、可辦抵修、校務系統可能顯示不同、待確認；只分類審查結果，不改判定
function renderSummary(s) {
  const unmet = s.requirements.filter((r) => !r.satisfied && !r.pending);
  // 缺的課全部都在待辦抵修提示裡，才算「辦理抵修後可能符合」；只有部分可抵修的仍列為還沒達到
  const substitutable = new Set(s.flags.filter((f) => f.type === "substitution_pending").flatMap((f) => f.courses));
  const viaSubstitution = unmet.filter((r) => r.missing.length && r.missing.every((c) => substitutable.has(c.code)));
  const real = unmet.filter((r) => !viaSubstitution.includes(r));
  const systemGap = s.flags.filter((f) => f.type !== "substitution_pending");
  const pending = s.requirements.filter((r) => r.pending);
  const unsure = pending.length + s.issues.review.length + s.issues.dept_unverified.length;
  const titles = (rs) => rs.map((r) => esc(r.title)).join("、");
  const rows = [
    [real.length, "FAIL", "✘ 還沒達到的條件", titles(real)],
    [viaSubstitution.length, "WARNING", "🛠 辦理抵修後可能符合", titles(viaSubstitution)],
    [systemGap.length, "WARNING", "⚑ 校務系統可能顯示不同", systemGap.map((f) => esc(flagLabel(f.type))).join("、")],
    [unsure, "MANUAL_REVIEW", "? 待確認", [titles(pending), s.issues.review.length + s.issues.dept_unverified.length
      ? `${s.issues.review.length + s.issues.dept_unverified.length} 筆修課紀錄待承辦人確認採計方式` : ""].filter(Boolean).join("、")],
  ].filter(([n]) => n);
  $("#summary").innerHTML = (rows.length
    ? rows.map(([n, cls, label, text]) => `<div class="issue ${cls}"><b>${label}（${n}）</b><div class="small">${text}</div></div>`).join("")
    : `<div class="issue PASS"><b>✔ 目前的修課紀錄符合所有已建檔的畢業條件</b></div>`);
  return { real, viaSubstitution, systemGap, unsure };
}

function renderIssues(s) {
  const iss = s.issues, out = [];
  iss.failed.forEach((r) => out.push(`<div class="issue FAIL">✘ <b>${esc(r.code)} ${esc(r.name)}</b>（${r.term}）${r.grade} 分不及格，尚未重修通過</div>`));
  iss.review.forEach((r) => out.push(`<div class="issue MANUAL_REVIEW">🔍 <b>${esc(r.code)} ${esc(r.name)}</b>（${r.term}）採計方式待承辦人確認</div>`));
  iss.dept_unverified.forEach((r) => out.push(`<div class="issue WARNING">⚠ <b>${esc(r.code)} ${esc(r.name)}</b>（${r.term}）暫以本系選修 ${r.credits} 學分計，待承辦人確認</div>`));
  $("#issues").innerHTML = out.join("") || `<div class="muted small">目前沒有需要注意的修課紀錄。</div>`;
}

// ---------- 上排摘要磚：只數審查結果裡的項目（件數），不計算學分、不改判定 ----------
let TILE_PANE = "summary";
function renderTiles(s, sum, target) {
  const set = (key, cls, value, sub = "") => {
    const tile = document.querySelector(`.tile[data-pane="${key}"]`);
    tile.className = `tile ${cls}`;
    tile.querySelector(".t-value").innerHTML = value;
    const subEl = tile.querySelector(".t-sub");
    if (subEl) subEl.textContent = sub;
  };
  const unit = (n, u) => `${n}<small>${u}</small>`;
  // 目前狀態：真正缺修為主數字，其餘類別列在說明
  const others = [
    [sum.viaSubstitution.length, "抵修後可能符合"], [sum.systemGap.length, "系統可能顯示不同"], [sum.unsure, "待確認"],
  ].filter(([n]) => n).map(([n, t]) => `${t} ${n}`).join("・");
  if (sum.real.length) set("summary", "fail", `✘ ${unit(sum.real.length, " 條未達成")}`, others);
  else if (others) set("summary", "warn", "⚑ 需確認", others);
  else set("summary", "pass", "✔ 全部符合", "符合所有已建檔的畢業條件");
  const flagTypes = [...new Set(s.flags.map((f) => flagLabel(f.type)))].join("、");
  set("flags", s.flags.length ? "warn" : "none", unit(s.flags.length, " 項"), s.flags.length ? flagTypes : "沒有偵測到");
  set("actions", s.actions.length ? "warn" : "none", unit(s.actions.length, " 件"),
    s.actions.length ? s.actions.map((a) => a.title).join("、") : "沒有要辦理的事項");
  const credits = $("#tile-credits");
  credits.innerHTML = target == null ? unit(s.total_credits, " 學分") : `${s.total_credits}<small> / ${target} 學分</small>`;
  credits.parentElement.className = `tile ${target != null && s.total_credits >= target ? "pass" : "info"}`;
  const iss = s.issues, nIssues = iss.failed.length + iss.review.length + iss.dept_unverified.length;
  const issueSub = [[iss.failed.length, "不及格"], [iss.review.length + iss.dept_unverified.length, "採計待確認"]]
    .filter(([n]) => n).map(([n, t]) => `${t} ${n}`).join("・");
  set("issues", nIssues ? (iss.failed.length ? "fail" : "review") : "none", unit(nIssues, " 筆"), issueSub || "沒有");
  showPane(TILE_PANE);
}

function showPane(key) {
  TILE_PANE = key;
  document.querySelectorAll(".tile").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.pane === key)));
  document.querySelectorAll(".tile-detail .pane").forEach((p) => { p.hidden = p.dataset.pane !== key; });
}
document.querySelectorAll(".tile").forEach((t) => t.addEventListener("click", () => showPane(t.dataset.pane)));

$("#student-select").addEventListener("change", (e) => loadStudent(e.target.value));

// ---------- 修課規劃與 What-if ----------
// 本學期修課：學生自行輸入（學校選課系統沒有公開介接），只存在瀏覽器；每次規劃與 What-if 都帶給後端試算
// 每項 {code, name, credits, category, counted}；勾選的課由後端交給審查引擎假設及格重算，這裡不計算學分
let CURRENT_COURSES = [];
const curKey = (id) => `twin-current-courses:${id}`;

function saveCurrent() {
  try { localStorage.setItem(curKey(CURRENT.id), JSON.stringify(CURRENT_COURSES)); } catch {}
}

function currentParams(params = new URLSearchParams()) {
  if (CURRENT_COURSES.length) params.set("current", JSON.stringify(CURRENT_COURSES));
  return params;
}

async function loadPlan(id) {
  CURRENT_COURSES = [];
  try { CURRENT_COURSES = JSON.parse(localStorage.getItem(curKey(id)) || "[]"); } catch {}
  if (!Array.isArray(CURRENT_COURSES)) CURRENT_COURSES = [];
  $("#current-term").textContent = CURRENT?.next_term ?? "";
  await loadOfferings(CURRENT?.next_term);
  fillCurrentOptions();
  try {
    await refreshPlan(id);
  } catch (err) {
    // 存在瀏覽器的舊輸入不合規則庫時清掉重來，不讓整個規劃頁壞掉
    CURRENT_COURSES = []; saveCurrent();
    await refreshPlan(id);
    $("#current-msg").innerHTML = `<div class="issue WARNING">先前輸入的本學期課程無法使用，已清除：${esc(err.message)}</div>`;
  }
}

async function refreshPlan(id) {
  const sim = await api(`/api/students/${encodeURIComponent(id)}/simulate?${currentParams()}`);
  BASE_PLAN = sim.base;
  $("#current-msg").innerHTML = "";
  renderCurrent(sim.current);
  $("#base-plan").innerHTML = renderPlan(sim.base);
  fillWhatIf(sim.base);
  $("#ask-result").innerHTML = "";
  $("#sim-result").innerHTML = "";
}

// 學分數字全部來自 simulate 的 current（審查引擎結果），這裡只負責排版
function renderCurrent(cur) {
  if (!cur) {
    $("#current-courses").innerHTML = "";
    $("#current-total").innerHTML = "";
    return;
  }
  $("#current-courses").innerHTML = cur.courses.map((c) => `
    <div class="small" style="display:flex;align-items:center;gap:8px;margin:2px 0">
      <label style="flex:1"><input type="checkbox" data-code="${esc(c.code)}" ${c.counted ? "checked" : ""}>
        ${esc(c.code)} ${esc(c.name)}（${c.credits} 學分${c.category ? `・${esc(c.category)}` : ""}）</label>
      <button class="ghost" data-remove="${esc(c.code)}" style="padding:2px 10px">移除</button>
    </div>`).join("");
  const n = cur.courses.filter((c) => c.counted).length;
  $("#current-total").innerHTML = `可採計學分：目前 <b>${cur.official_total_credits}</b>；若勾選的 ${n} 門都及格 → <b>${cur.trial_total_credits}</b>。規劃從 ${esc(nextTerm(cur.term))} 開始。`;
}

// 本學期開課清單（academic_twin/offerings.py 從學校課程查詢系統抓取的公開資料），依學生的本學期讀取；
// 沒有該學期的檔案時為 null，退回只列規則庫課程。清單只方便挑課，學分與判定仍以規則庫、審查引擎為準
let OFFERINGS = null;

async function loadOfferings(term) {
  OFFERINGS = null;
  if (!term || DEMO) return;
  try {
    const res = await fetch(`assets/offerings/${encodeURIComponent(term)}.json`);
    if (res.ok) OFFERINGS = await res.json();
  } catch {}
}

function offeringOf(code) {
  return OFFERINGS?.courses.find((c) => c.code === code) ?? null;
}

function sectionsText(o) {
  const s = o.sections.map((x) => `${x.time.split("/")[0]} ${x.teacher}`.trim());
  return s.length > 2 ? `${s.slice(0, 2).join("；")}…共 ${s.length} 班` : s.join("；");
}

// 課程選項：審查結果中還沒通過的課（remaining_courses，不及格可重修的課仍在內）＋本學期開課清單；已通過的不列
function fillCurrentOptions() {
  const remaining = CURRENT?.remaining_courses ?? [];
  const passed = new Set(META.courses.map((c) => c.code).filter((code) => !remaining.some((r) => r.code === code)));
  const offered = (OFFERINGS?.courses ?? []).filter((o) => !passed.has(o.code));
  const notOffered = remaining.filter((c) => !offeringOf(c.code));
  const option = (code, name, label) => `<option value="${esc(code)} ${esc(name)}" label="${esc(label)}"></option>`;
  $("#cur-options").innerHTML = [
    ...offered.map((o) => option(o.code, remaining.find((r) => r.code === o.code)?.name ?? o.name,
      `${o.credits} 學分・${OFFERINGS.simulated ? "模擬開課" : "本學期開課"}：${sectionsText(o)}`)),
    ...notOffered.map((c) => option(c.code, c.name, `${c.credits} 學分`)),
  ].join("");
}

function fillCategoryOptions() {
  $("#cur-o-category").innerHTML = `<option value="">（外系／不確定）</option>` +
    META.categories.map((c) => `<option>${esc(c)}</option>`).join("");
}

async function changeCurrent(next) {
  const before = CURRENT_COURSES;
  CURRENT_COURSES = next;
  try {
    await refreshPlan(CURRENT.id);
    saveCurrent();
  } catch (err) {
    CURRENT_COURSES = before;
    $("#current-msg").innerHTML = `<div class="issue FAIL">${esc(err.message)}</div>`;
  }
}

function addCurrent(course) {
  if (CURRENT_COURSES.some((c) => c.code === course.code)) {
    $("#current-msg").innerHTML = `<div class="issue WARNING">${esc(course.code)} 已經在清單裡。</div>`;
    return;
  }
  changeCurrent([...CURRENT_COURSES, { ...course, counted: true }]);
}

$("#cur-add").addEventListener("click", () => {
  const text = $("#cur-search").value.trim();
  const code = text.split(/\s+/)[0].toUpperCase();
  const options = CURRENT?.remaining_courses ?? [];
  const c = options.find((x) => x.code === code) ?? options.find((x) => text && x.name === text);
  // 規則庫沒有、但本學期有開的課（如本系選修、管院課程）：課名學分取自開課清單，類別留給審查引擎依規則判斷
  const o = c ? null : (offeringOf(code) ?? OFFERINGS?.courses.find((x) => text && x.name === text));
  const passed = META.courses.some((x) => x.code === (o?.code ?? code) || (!o && text && x.name === text));
  if (!c && (!o || passed)) {
    $("#current-msg").innerHTML = passed
      ? `<div class="issue WARNING">「${esc(text)}」你已經修過並通過了，不用再加入。</div>`
      : `<div class="issue WARNING">課程清單裡找不到「${esc(text)}」；通識或外系課請用「其他課程」加入。</div>`;
    return;
  }
  $("#cur-search").value = "";
  const pick = c ?? o;
  addCurrent({ code: pick.code, name: pick.name, credits: pick.credits, category: "" });
});
$("#cur-search").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#cur-add").click(); });

$("#cur-o-add").addEventListener("click", () => {
  const name = $("#cur-o-name").value.trim();
  const code = $("#cur-o-code").value.trim().toUpperCase() || `自填-${name}`;
  if (!name) {
    $("#current-msg").innerHTML = `<div class="issue WARNING">請填課名。</div>`;
    return;
  }
  $("#cur-o-code").value = ""; $("#cur-o-name").value = "";
  addCurrent({ code, name, credits: Number($("#cur-o-credits").value || 0), category: $("#cur-o-category").value });
});

$("#current-courses").addEventListener("change", (e) => {
  const code = e.target.dataset.code;
  if (!code) return;
  changeCurrent(CURRENT_COURSES.map((c) => (c.code === code ? { ...c, counted: e.target.checked } : c)));
});
$("#current-courses").addEventListener("click", (e) => {
  const code = e.target.dataset.remove;
  if (code) changeCurrent(CURRENT_COURSES.filter((c) => c.code !== code));
});

// 學期字串的下一學期，同 planner.py 的 Term.next()（"115-1" → "115-2" → "116-1"）
function nextTerm(term) {
  const [year, sem] = term.split("-").map(Number);
  return sem === 1 ? `${year}-2` : `${year + 1}-1`;
}

function renderPlan(plan, moved = new Set(), away = []) {
  if (!plan.terms.length) return `<div class="empty">已符合所有畢業條件</div>`;
  const cols = plan.terms.map((t) => {
    const grad = t.term === plan.graduation;
    const isAway = away.includes(t.term);
    return `<div class="term ${isAway ? "away" : ""} ${grad ? "grad" : ""}">
      <div class="term-head"><span>${t.term}${grad ? " 🎓" : ""}</span><span class="muted small">${t.credits} 學分</span></div>
      ${isAway ? '<div class="small" style="margin-bottom:4px">✈ 出國交換</div>' : ""}
      ${t.courses.map((c) => courseTag(c, moved.has(c.code) ? "moved" : "")).join("")}
      ${t.other_credits ? `<div class="other">其他學分 ${t.other_credits}</div>` : ""}
      ${!t.courses.length && !t.other_credits ? '<div class="muted small">（無本系課程）</div>' : ""}
    </div>`;
  }).join("");
  const warn = plan.unplaceable.length
    ? `<div class="issue FAIL" style="margin-top:8px">排不進去：${plan.unplaceable.map((c) => courseTag(c)).join(" ")}</div>` : "";
  return cols + warn;
}

function fillWhatIf(plan) {
  const planned = plan.terms.flatMap((t) => t.courses.map((c) => ({ ...c, term: t.term })));
  $("#skip-course").innerHTML = `<option value="">（不設定）</option>` +
    planned.map((c) => `<option value="${c.code}" data-term="${c.term}">${c.code} ${esc(c.name)}（原排 ${c.term}）</option>`).join("");
  const terms = plan.terms.map((t) => t.term);
  $("#skip-term").innerHTML = terms.map((t) => `<option>${t}</option>`).join("");
  $("#away-term").innerHTML = `<option value="">（不設定）</option>` + terms.map((t) => `<option>${t}</option>`).join("");
}

$("#skip-course").addEventListener("change", (e) => {
  const opt = e.target.selectedOptions[0];
  if (opt && opt.dataset.term) $("#skip-term").value = opt.dataset.term;
});

$("#reset-sim").addEventListener("click", () => {
  $("#skip-course").value = ""; $("#away-term").value = ""; $("#away-count").value = "1";
  $("#ask-result").innerHTML = "";
  $("#base-plan").innerHTML = renderPlan(BASE_PLAN);
  $("#sim-result").innerHTML = "";
});

$("#run-sim").addEventListener("click", () => {
  const code = $("#skip-course").value, start = $("#away-term").value;
  // 從起始學期連續往後排；simulate 的 away 可重複帶多個學期，可抵學分逐學期計算
  const away = [];
  for (let t = start, i = 0; start && i < Number($("#away-count").value); i++, t = nextTerm(t)) away.push(t);
  if (!code && !away.length) {
    $("#sim-result").innerHTML = `<div class="muted small">請先選一門課，或選一個交換學期。</div>`;
    return;
  }
  runSim(code ? [`${code}@${$("#skip-term").value}`] : [], away, $("#away-credits").value || "0");
});

// ---------- 自然語言 What-if ----------
// AI 只回傳待確認的 simulate 參數；學生按確認後才呼叫 simulate，結果與手動選項同一套呈現
let ASK = null;

$("#ask-run").addEventListener("click", async () => {
  const question = $("#ask-input").value.trim();
  if (!question) return;
  ASK = null;
  // 模型回應約需 10–30 秒；等待時鎖住按鈕與輸入框，避免重複送出
  $("#ask-run").disabled = $("#ask-input").disabled = true;
  $("#ask-result").innerHTML = `<div class="muted small">AI 轉換中，約需 10–30 秒…</div>`;
  try {
    const res = await api(`/api/twin/${encodeURIComponent(CURRENT.id)}/ask`, { method: "POST", body: JSON.stringify({ question, current: CURRENT_COURSES }) });
    $("#ask-result").innerHTML = renderAsk(res);
    if (res.status === "proposed") ASK = res.params;
  } catch (err) {
    $("#ask-result").innerHTML = `<div class="issue FAIL">${esc(err.message)}；請改用下方手動選項。</div>`;
  } finally {
    $("#ask-run").disabled = $("#ask-input").disabled = false;
  }
});
$("#ask-input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !$("#ask-run").disabled) $("#ask-run").click(); });

function renderAsk(res) {
  const t = res.trace;
  const steps = t.steps.filter((s) => s.step === "check").length;
  // 重播（評審模式或 AI 服務失敗時改用錄製結果）一律標示原本的模型與產生時間
  const played = t.replay ? `<b>重播</b>：模型 ${esc(t.replay.model)}、產生時間 ${esc(t.replay.generated_at)}・` : "";
  const trace = `<div class="muted small">執行軌跡：${played}模型 ${esc(t.model ?? "—")}・提示 ${esc(t.prompt_version)}・檢核 ${steps} 次${t.corrected ? "（自我修正 1 次後通過）" : ""}${t.to_manual ? "・轉手動" : ""}・${t.wait_seconds} 秒</div>`;
  if (res.status === "proposed") {
    return `<div class="issue WARNING">
        <div><b>AI 理解的模擬條件</b>（${esc(res.label)}）：</div>
        <ul class="impact">${res.conditions.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>
        <button class="primary" id="ask-confirm">確認並模擬</button>
        <button class="ghost" id="ask-cancel">不對，改用手動</button>
      </div>${trace}`;
  }
  const errors = res.errors.length ? `<ul class="impact">${res.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : "";
  const head = res.status === "unsupported" ? "目前無法模擬這個問題" : "AI 無法轉成條件";
  return `<div class="issue ${res.status === "unsupported" ? "WARNING" : "FAIL"}"><b>${head}</b>：${esc(res.reason)}${errors}
      <div class="small">支援的情境：某門課某學期不修、出國交換（可連續多學期）。請改用下方手動選項。</div></div>${trace}`;
}

$("#ask-result").addEventListener("click", (e) => {
  if (e.target.id === "ask-cancel") { ASK = null; $("#ask-result").innerHTML = ""; return; }
  if (e.target.id !== "ask-confirm" || !ASK) return;
  // 單一條件時同步到手動選項，方便學生微調
  const [code, term] = ASK.skip.length === 1 ? ASK.skip[0].split("@") : ["", ""];
  $("#skip-course").value = code; if (term) $("#skip-term").value = term;
  $("#away-term").value = ASK.away[0] ?? ""; $("#away-count").value = String(ASK.away.length || 1);
  if (ASK.away.length) $("#away-credits").value = ASK.away_credits;
  runSim(ASK.skip, ASK.away, String(ASK.away_credits ?? 0));
});

async function runSim(skip, away, awayCredits) {
  const params = currentParams();
  skip.forEach((s) => params.append("skip", s));
  away.forEach((t) => params.append("away", t));
  if (away.length) params.append("away_credits", awayCredits);
  try {
    const sim = await api(`/api/students/${encodeURIComponent(CURRENT.id)}/simulate?${params}`);
    const sc = sim.scenario;
    const movedCodes = new Set(sim.base.terms.flatMap((t) => t.courses.map((c) => [c.code, t.term]))
      .filter(([c, t]) => { const n = sc.terms.find((x) => x.courses.some((y) => y.code === c)); return !n || n.term !== t; })
      .map(([c]) => c));
    const delayed = sc.delay_terms > 0 || !sc.feasible;
    // 數字全部來自 simulate 的結果（graduation、delay_terms、impact），這裡只負責排版
    const stat = (label, value, color = "inherit") =>
      `<div class="card" style="padding:10px 14px;flex:1 1 140px"><div class="muted small">${label}</div>
        <div style="font-size:22px;font-weight:700;color:${color}">${value}</div></div>`;
    // 交換全在預估畢業之後：impact 只有一行說明，不顯示延畢數字
    const after = sc.away_after_graduation;
    const moved = after ? [] : sc.impact.slice(0, -1);
    $("#sim-result").innerHTML = `
      <div class="small muted">條件：${sc.conditions.map(esc).join("；")}</div>
      <div style="display:flex;flex-wrap:wrap;gap:8px;margin:12px 0">
        ${stat("原本最早畢業", esc(sim.base.graduation ?? "已符合"))}
        ${stat("模擬後最早畢業", after ? "—" : sc.feasible ? esc(sc.graduation ?? "已符合") : "排不完", after ? "inherit" : delayed ? "var(--fail)" : "var(--pass)")}
        ${stat("延畢", after || !sc.feasible ? "—" : sc.delay_terms > 0 ? `${sc.delay_terms} 學期` : "不會延畢", after ? "inherit" : delayed ? "var(--fail)" : "var(--pass)")}
        ${stat("被往後推的課", `${moved.length} 門`, moved.length ? "var(--warn)" : "inherit")}
      </div>
      <div class="verdict ${delayed ? "delay" : "same"}">
        ${after || !sc.feasible ? esc(sc.impact.at(-1)) : delayed ? `會延畢 ${sc.delay_terms} 學期：${sim.base.graduation} → ${sc.graduation}` : `不影響畢業：仍可在 ${sc.graduation} 畢業`}
        ${moved.length ? `<ul class="impact">${moved.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>` : ""}
      </div>
      <div class="timeline">${renderPlan(sc, movedCodes, away)}</div>`;
  } catch (err) {
    $("#sim-result").innerHTML = `<div class="issue FAIL">${esc(err.message)}</div>`;
  }
}

// ---------- 先修關係圖 ----------
function renderGraph() {
  const { courses, edges } = META;
  const linked = new Set(edges.flatMap((e) => [e.from, e.to]));
  const nodes = courses.filter((c) => linked.has(c.code));
  // 層級 = 最長先修鏈深度（courses 已是拓樸順序）
  const layer = {};
  nodes.forEach((c) => {
    const parents = edges.filter((e) => e.to === c.code).map((e) => layer[e.from] ?? 0);
    layer[c.code] = parents.length ? Math.max(...parents) + 1 : 0;
  });
  const cols = {};
  nodes.forEach((c) => (cols[layer[c.code]] ||= []).push(c.code));
  const W = 140, H = 44, GX = 26, GY = 12, PAD = 6;
  const pos = {};
  Object.entries(cols).forEach(([l, codes]) => codes.forEach((code, i) => {
    pos[code] = { x: PAD + l * (W + GX), y: PAD + i * (H + GY) };
  }));
  const width = PAD * 2 + Object.keys(cols).length * (W + GX) - GX;
  const height = PAD * 2 + Math.max(...Object.values(cols).map((c) => c.length)) * (H + GY) - GY;
  const svg = $("#dag");
  svg.setAttribute("width", width); svg.setAttribute("height", height);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const typeColor = { required: "var(--c-required)", college: "var(--c-college)", selective: "var(--c-selective)" };
  svg.innerHTML = edges.map((e) => {
    const a = pos[e.from], b = pos[e.to];
    const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2, mx = (x1 + x2) / 2;
    return `<path class="edge ${e.soft ? "soft" : ""}" data-from="${e.from}" data-to="${e.to}" d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}"/>`;
  }).join("") + nodes.map((c) => {
    const p = pos[c.code];
    return `<g class="node" data-code="${c.code}" transform="translate(${p.x},${p.y})">
      <rect width="${W}" height="${H}"></rect>
      <rect width="4" height="${H}" style="fill:${typeColor[c.type] || "var(--muted)"};stroke:none"></rect>
      <text x="12" y="17" class="tcode">${c.code}・${c.terms.map((t) => (t === "fall" ? "上" : "下")).join("/")}</text>
      <text x="12" y="34">${esc(c.name)}</text></g>`;
  }).join("");
  svg.querySelectorAll(".node").forEach((g) => g.addEventListener("click", () => selectCourse(g.dataset.code)));
  $("#isolated").innerHTML = courses.filter((c) => !linked.has(c.code)).map((c) => courseTag(c)).join("");
  $("#bottlenecks").innerHTML = META.bottlenecks.map((b) => `<tr data-code="${b.code}">
    <td>${courseTag(b)}</td><td class="small muted">鏈長 ${b.depth}・卡住 ${b.blocked} 門</td></tr>`).join("");
  document.querySelectorAll("#bottlenecks tr").forEach((tr) => tr.addEventListener("click", () => selectCourse(tr.dataset.code)));
}

async function selectCourse(code) {
  const info = await api(`/api/courses/${encodeURIComponent(code)}`);
  const anc = new Set(info.ancestors.map((c) => c.code)), desc = new Set(info.descendants.map((c) => c.code));
  document.querySelectorAll("#dag .node").forEach((g) => {
    const c = g.dataset.code;
    g.classList.toggle("sel", c === code);
    g.classList.toggle("anc", anc.has(c));
    g.classList.toggle("desc", desc.has(c));
    g.classList.toggle("dim", c !== code && !anc.has(c) && !desc.has(c));
  });
  const related = new Set([code, ...anc, ...desc]);
  document.querySelectorAll("#dag .edge").forEach((e) =>
    e.classList.toggle("hot", related.has(e.dataset.from) && related.has(e.dataset.to)));
  $("#impact-card").innerHTML = `
    <h3>${esc(info.code)} ${esc(info.name)}</h3>
    <div class="muted small">${info.credits} 學分・開課：${info.terms.map((t) => (t === "fall" ? "上學期" : "下學期")).join("、")}</div>
    <div style="margin-top:12px"><b>要先修</b>（${info.ancestors.length}）</div>
    <div class="courses">${info.ancestors.map((c) => courseTag(c)).join("") || '<span class="muted small">無</span>'}</div>
    <div style="margin-top:12px"><b>沒修會卡到</b>（${info.descendants.length}）</div>
    <div class="courses">${info.descendants.map((c) => courseTag(c, "moved")).join("") || '<span class="muted small">無</span>'}</div>`;
}

// ---------- 啟動 ----------
(async function init() {
  await loadMeta();
  fillCategoryOptions();
  renderGraph();
  // demo 模式只有範例檔這一位，選單不列其他學生，以免切換後仍顯示範例資料
  const ids = DEMO ? [(await api("assets/demo-twin.json")).id] : await api("/api/twin");
  $("#student-select").innerHTML = ids.map((id) => `<option value="${esc(id)}">${esc(id)}</option>`).join("");
  const id = params.get("id");
  if (ids.length) await loadStudent(ids.includes(id) ? id : ids[0]);
})();
