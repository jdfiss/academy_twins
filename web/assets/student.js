// 學生端（B 組）：個人進度試算、誤判提示、需辦理事項、修課規劃與 What-if、先修關係圖
// 網址加 ?demo 會改讀 assets/demo-twin.json（交接格式範例，含 flags、actions），A 組偵測完成前用來做畫面
let CURRENT = null, BASE_PLAN = null;
const params = new URLSearchParams(location.search);
const DEMO = params.has("demo");
setupTabs("twin-student-view");

// 白話說明（B 組待建置）：依 r.kind 與審查結果組成學生看得懂的說明；只能引用審查結果，不得自行計算或改判
function explain(r) {
  return "";
}

// ---------- 我的進度 ----------
async function loadStudent(id) {
  const s = DEMO ? await api("assets/demo-twin.json") : await api(`/api/twin/${encodeURIComponent(id)}`);
  CURRENT = s;
  $("#student-select").value = s.id;
  $("#student-status").innerHTML = chip(s.status);
  $("#trial-badge").style.display = s.trial ? "" : "none";
  $("#student-credits").textContent = s.total_credits;
  $("#student-next").textContent = `下一學期：${s.next_term}`;
  try { history.replaceState(null, "", `?${DEMO ? "demo&" : ""}id=${encodeURIComponent(s.id)}`); } catch {}
  renderBreakdown(s);
  renderFlags(s);
  renderActions(s);
  renderIssues(s);
  renderRequirements(s, explain);
  renderRecords(s);
  await loadPlan(s.id);
}

function renderFlags(s) {
  $("#flags").innerHTML = s.flags.map((f) => `<div class="issue WARNING">⚑ ${esc(f.message)}
      ${f.courses.length ? `<div class="muted small">相關課程：${f.courses.map(esc).join("、")}</div>` : ""}</div>`).join("")
    || `<div class="muted small">沒有偵測到校務系統可能誤判的項目。（超修、可重複修習、待辦抵修的偵測建置中）</div>`;
}

function renderActions(s) {
  $("#actions").innerHTML = s.actions.map((a) => `<div class="issue">📋 <b>${esc(a.title)}</b>
      <div class="muted small">承辦：${esc(a.office)}${a.courses.length ? `・相關課程：${a.courses.map(esc).join("、")}` : ""}</div></div>`).join("")
    || `<div class="muted small">目前沒有需要辦理的事項。</div>`;
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
  const ids = await api("/api/twin");
  $("#student-select").innerHTML = ids.map((id) => `<option value="${esc(id)}">${esc(id)}</option>`).join("");
  const id = params.get("id");
  if (ids.length) await loadStudent(ids.includes(id) ? id : ids[0]);
})();
