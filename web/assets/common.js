// 兩端共用：API 呼叫、分頁、審查結果的共同呈現（變更需通知另一組，見 docs/TEAM_PLAN.md）
const STATUS = {
  PASS: "✅ Pass", WARNING: "⚠️ Warning", FAIL: "❌ Fail", MANUAL_REVIEW: "🔍 Manual Review",
};
const BREAKDOWN_COLORS = ["var(--c-required)", "var(--c-selective)", "var(--c-college)", "#8a6fd1", "#d16f9e", "#6fa8d1", "#9aa3b5"];
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const chip = (status) => `<span class="chip ${status}">${STATUS[status]}</span>`;
const courseTag = (c, cls = "") => `<span class="course ${cls}"><span class="code">${esc(c.code)}</span>${esc(c.name)}</span>`;

async function api(path, options) {
  const res = await fetch(path, options);
  const data = await res.json();
  if (!res.ok) throw Object.assign(new Error(data.error || res.statusText), { details: data.errors || [] });
  return data;
}

let META = null;

async function loadMeta() {
  META = await api("/api/meta");
  $("#program").textContent = `${META.program}・${META.cohort} 學年度入學・規則庫依官方應修科目表`;
  return META;
}

// ---------- 分頁（各頁記住自己上次的分頁） ----------
function setupTabs(storageKey) {
  const show = (view) => {
    document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
    document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
    try { localStorage.setItem(storageKey, view); } catch {}
  };
  document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));
  let saved = null;
  try { saved = localStorage.getItem(storageKey); } catch {}
  if (saved && document.getElementById(`view-${saved}`)) show(saved);
  return show;
}

// ---------- 審查結果（交接格式 student()／twin()） ----------
function renderBreakdown(s, target = 128) {
  const entries = Object.entries(s.credit_breakdown);
  const total = Math.max(target, s.total_credits, 1);
  $("#breakdown-bar").innerHTML = entries.map(([k, v], i) =>
    `<div title="${esc(k)} ${v}" style="width:${(v / total) * 100}%;background:${BREAKDOWN_COLORS[i % BREAKDOWN_COLORS.length]}"></div>`).join("");
  $("#breakdown-legend").innerHTML = entries.map(([k, v], i) =>
    `<span><i style="background:${BREAKDOWN_COLORS[i % BREAKDOWN_COLORS.length]}"></i>${esc(k)} ${v}</span>`).join("")
    + (s.total_credits < target ? `<span><i style="background:var(--line)"></i>尚缺 ${target - s.total_credits}</span>` : "");
}

// extra(r)：各端在每條要求下加的內容（例如學生端的白話說明）
function renderRequirements(s, extra = () => "") {
  const ok = s.requirements.filter((r) => r.satisfied).length;
  $("#req-summary").textContent = `${ok} / ${s.requirements.length} 符合`;
  $("#requirements").innerHTML = s.requirements
    .slice().sort((a, b) => a.satisfied - b.satisfied || b.pending - a.pending)
    .map((r) => {
      const pct = r.need ? Math.min(100, (r.have / r.need) * 100) : 100;
      const unit = r.kind.includes("credits") ? "學分" : "門";
      let detail = "";
      if (r.pending) {
        detail = `<div class="small" style="margin-top:6px">修課紀錄無法判斷，請確認是否已通過英檢（應屆名單 english_passed 欄）</div>`;
      } else if (!r.satisfied) {
        detail = r.missing.length
          ? `<div class="small" style="margin-top:6px">缺：</div><div class="courses">${r.missing.map((c) => courseTag(c)).join("")}</div>`
          : `<div class="small" style="margin-top:6px">尚缺 ${r.shortfall} ${unit}</div>`;
      }
      const terms = [r.system_code, r.regulation_term].filter(Boolean).map(esc).join("・");
      const state = r.satisfied ? "ok" : r.pending ? "pending" : "bad";
      return `<div class="req ${state}">
        <div class="req-top"><b>${{ ok: "✔", pending: "?", bad: "✘" }[state]} ${esc(r.title)}${terms ? `<span class="terms">${terms}</span>` : ""}</b><span class="muted small">${r.have} / ${r.need}${r.detail ? `・${esc(r.detail)}` : ""}</span></div>
        <div class="meter ${state === "pending" ? "" : state}" style="margin-top:6px"><div style="width:${pct}%"></div></div>
        ${detail}
        ${extra(r)}
        <div class="cite">依據：${esc(r.citation)}</div>
      </div>`;
    }).join("");
}

function renderRecords(s) {
  $("#record-count").textContent = s.records.length;
  $("#records").innerHTML = s.records.map((r) => `<tr style="cursor:default">
    <td>${r.term}</td><td>${esc(r.code)}</td><td>${esc(r.name)}</td><td>${r.credits}</td>
    <td style="color:${r.grade < 60 ? "var(--fail)" : "inherit"}">${r.grade}</td><td class="muted">${esc(r.category)}</td></tr>`).join("");
}
