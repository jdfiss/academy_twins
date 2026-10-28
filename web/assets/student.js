// 學生端（B 組）：個人進度試算、誤判提示、需辦理事項、修課規劃與 What-if、先修關係圖
// 網址加 ?demo 會改讀 assets/demo-twin.json（交接格式範例，含 flags、actions），A 組偵測完成前用來做畫面
let CURRENT = null, BASE_PLAN = null;
const params = new URLSearchParams(location.search);
const DEMO = params.has("demo");
setupTabs("twin-student-view");

// 白話說明：依 r.kind 與審查結果組成學生看得懂的說明；只引用 have、need、shortfall、detail、satisfied、pending，不自行計算或改判
const EXPLAIN = {
  all_of: (r) => r.satisfied
    ? `這 ${r.need} 門課每一門都要及格才算數，你都已經通過了。`
    : `這 ${r.need} 門課每一門都要及格才算數。你已通過 ${r.have} 門，還有 ${r.shortfall} 門沒通過（見上方「缺」）。`,
  taken_all: (r) => r.satisfied
    ? `必選課只要「有修過」就算，不需要及格；這 ${r.need} 門你都修過了。及格的話，學分另外算進本系選修。`
    : `必選課只要「有修過」就算，不需要及格。你修過 ${r.have} 門，還有 ${r.shortfall} 門沒修過（見上方「缺」）。`,
  n_of: (r) => r.satisfied
    ? `從清單中任選 ${r.need} 門及格即可，你已經達到。`
    : `從清單中任選 ${r.need} 門及格即可。你已通過 ${r.have} 門，還差 ${r.shortfall} 門。`,
  credits_from: (r) => r.satisfied
    ? `從指定課程中修滿 ${r.need} 學分即可，你已經達到。`
    : `從指定課程中修滿 ${r.need} 學分即可。目前採計 ${r.have} 學分，還差 ${r.shortfall} 學分。`,
  category_credits: (r) => r.satisfied
    ? `這一類課要修滿 ${r.need} 學分，你已有 ${r.have} 學分算進這一類。`
    : `這一類課要修滿 ${r.need} 學分。目前算進這一類的有 ${r.have} 學分，還差 ${r.shortfall} 學分。`,
  category_count: (r) => r.satisfied
    ? `這一項要修 ${r.need} 門，你已修 ${r.have} 門${r.detail ? `（${r.detail}）` : ""}。`
    : r.shortfall
      ? `這一項要修 ${r.need} 門。你已修 ${r.have} 門，還差 ${r.shortfall} 門${r.detail ? `（${r.detail}）` : ""}。`
      : `門數已經夠了，但還有附帶條件沒達成${r.detail ? `：${r.detail}` : ""}，請對照上方標題。`,
  dept_elective_credits: (r) => r.satisfied
    ? `本系選修要修滿 ${r.need} 學分，目前採計 ${r.have} 學分，已經達到。`
    : `本系選修要修滿 ${r.need} 學分。目前採計 ${r.have} 學分，還差 ${r.shortfall} 學分。`,
  english_threshold: (r) => r.satisfied
    ? `你已達到英文畢業門檻。`
    : r.pending
      ? `修課紀錄看不出你是否通過英檢。已通過英檢的話，這項就符合；還沒通過的話，可以改修進修英文，及格 ${r.need} 學分即可（目前 ${r.have} 學分）。`
      : `目前紀錄顯示尚未通過英檢。可以改修進修英文，及格 ${r.need} 學分即可；目前 ${r.have} 學分，還差 ${r.shortfall} 學分。`,
  total_credits: (r) => r.satisfied
    ? `畢業總共要 ${r.need} 學分，你目前可採計 ${r.have} 學分，已經達到。`
    : `畢業總共要 ${r.need} 學分，你目前可採計 ${r.have} 學分，還差 ${r.shortfall} 學分。`,
};

function explain(r) {
  const text = EXPLAIN[r.kind]?.(r);
  return text ? `<div class="small" style="margin-top:6px">💬 ${esc(text)}</div>` : "";
}

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
  renderSummary(s);
  renderFlags(s);
  renderActions(s);
  renderIssues(s);
  renderRequirements(s, (r) => explain(r) + flagNote(s, r));
  renderRecords(s);
  await loadPlan(s.id);
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
      + `<div class="muted small">以上是校務系統可能顯示有誤的項目，和「畢業條件」裡真正缺修的項目分開看；處理後是否符合，以承辦人審查為準。</div>`
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
    : `<div class="issue PASS"><b>✔ 目前的修課紀錄符合所有已建檔的畢業條件</b></div>`)
    + `<div class="muted small" style="margin-top:6px">這是試算：只依目前的修課紀錄與已建檔的規則計算，不代表一定能或一定不能畢業，正式結果以系辦審查為準。</div>`;
}

function renderIssues(s) {
  const iss = s.issues, out = [];
  iss.failed.forEach((r) => out.push(`<div class="issue FAIL">✘ <b>${esc(r.code)} ${esc(r.name)}</b>（${r.term}）${r.grade} 分不及格，尚未重修通過</div>`));
  iss.review.forEach((r) => out.push(`<div class="issue MANUAL_REVIEW">🔍 <b>${esc(r.code)} ${esc(r.name)}</b>（${r.term}）採計方式待承辦人確認</div>`));
  iss.dept_unverified.forEach((r) => out.push(`<div class="issue WARNING">⚠ <b>${esc(r.code)} ${esc(r.name)}</b>（${r.term}）暫以本系選修 ${r.credits} 學分計，待承辦人確認</div>`));
  $("#issues").innerHTML = out.join("");
  $("#issues-card").style.display = out.length ? "" : "none";
}

$("#student-select").addEventListener("change", (e) => loadStudent(e.target.value));

// ---------- 修課規劃與 What-if ----------
async function loadPlan(id) {
  const sim = await api(`/api/students/${encodeURIComponent(id)}/simulate`);
  BASE_PLAN = sim.base;
  $("#base-plan").innerHTML = renderPlan(sim.base);
  fillWhatIf(sim.base);
  $("#sim-result").innerHTML = `<div class="muted small">選一門課與學期，或選擇交換學期，看看會不會延畢。</div>`;
}

function renderPlan(plan, moved = new Set(), away = "") {
  if (!plan.terms.length) return `<div class="empty">已符合所有畢業條件</div>`;
  const cols = plan.terms.map((t) => {
    const grad = t.term === plan.graduation;
    return `<div class="term ${t.term === away ? "away" : ""} ${grad ? "grad" : ""}">
      <div class="term-head"><span>${t.term}${grad ? " 🎓" : ""}</span><span class="muted small">${t.credits} 學分</span></div>
      ${t.term === away ? '<div class="small" style="margin-bottom:4px">✈ 出國交換</div>' : ""}
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
  $("#skip-course").value = ""; $("#away-term").value = "";
  $("#base-plan").innerHTML = renderPlan(BASE_PLAN);
  $("#sim-result").innerHTML = `<div class="muted small">選一門課與學期，或選擇交換學期，看看會不會延畢。</div>`;
});

$("#run-sim").addEventListener("click", async () => {
  const params = new URLSearchParams();
  const code = $("#skip-course").value, away = $("#away-term").value;
  if (code) params.append("skip", `${code}@${$("#skip-term").value}`);
  if (away) { params.append("away", away); params.append("away_credits", $("#away-credits").value || "0"); }
  if (!code && !away) {
    $("#sim-result").innerHTML = `<div class="muted small">請先選一門課，或選一個交換學期。</div>`;
    return;
  }
  try {
    const sim = await api(`/api/students/${encodeURIComponent(CURRENT.id)}/simulate?${params}`);
    const sc = sim.scenario;
    const movedCodes = new Set(sim.base.terms.flatMap((t) => t.courses.map((c) => [c.code, t.term]))
      .filter(([c, t]) => { const n = sc.terms.find((x) => x.courses.some((y) => y.code === c)); return !n || n.term !== t; })
      .map(([c]) => c));
    const delayed = sc.delay_terms > 0 || !sc.feasible;
    $("#sim-result").innerHTML = `
      <div class="small muted">條件：${sc.conditions.map(esc).join("；")}</div>
      <div class="verdict ${delayed ? "delay" : "same"}">
        ${!sc.feasible ? "⚠ 此方案排不完" : delayed ? `會延畢 ${sc.delay_terms} 學期：${sim.base.graduation} → ${sc.graduation}` : `不影響畢業：仍可在 ${sc.graduation} 畢業`}
        <ul class="impact">${sc.impact.slice(0, -1).map((l) => `<li>${esc(l)}</li>`).join("")}</ul>
      </div>
      <div class="muted small" style="margin-bottom:6px">模擬後的路徑（紅框＝被往後推的課）</div>
      <div class="timeline">${renderPlan(sc, movedCodes, away)}</div>`;
  } catch (err) {
    $("#sim-result").innerHTML = `<div class="issue FAIL">${esc(err.message)}</div>`;
  }
});

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
    <div class="courses">${info.descendants.map((c) => courseTag(c, "moved")).join("") || '<span class="muted small">無</span>'}</div>
    ${info.soft ? '<div class="muted small" style="margin-top:10px">＊含建議先修：依建議修課學期推論，非正式擋修。</div>' : ""}`;
}

// ---------- 啟動 ----------
(async function init() {
  await loadMeta();
  $("#cap").textContent = META.max_credits_per_term;
  renderGraph();
  // demo 模式只有範例檔這一位，選單不列其他學生，以免切換後仍顯示範例資料
  const ids = DEMO ? [(await api("assets/demo-twin.json")).id] : await api("/api/twin");
  $("#student-select").innerHTML = ids.map((id) => `<option value="${esc(id)}">${esc(id)}</option>`).join("");
  const id = params.get("id");
  if (ids.length) await loadStudent(ids.includes(id) ? id : ids[0]);
})();
