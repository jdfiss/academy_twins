// 學生端自動導覽（B 組）：網址加 ?tour 才啟動，照 docs/DEMO_STUDENT.md 分鏡自己點選、捲動、打字並疊字幕，供錄 Demo 影片。
// 只操作畫面上既有的按鈕與輸入框，數字一律讀畫面上的審查與規劃結果，不自行計算；AI 轉換失敗時照腳本備案改走手動選項並照實說明。
// 操作：空白鍵暫停／繼續，Esc 結束。
(function () {
  // 先讀參數：student.js 載入學生後會改寫網址（只留 id），之後就讀不到 tour
  const MODE = new URLSearchParams(location.search).get("tour");
  if (MODE === null) return;

  // 腳本的本學期修課（同 tests/test_demo_student.py）；本系課的課名學分取自審查結果，與手動「本系課程搜尋」加入的內容相同
  const OTHER_COURSES = [
    { code: "IM5099", name: "雲端運算", credits: 3, category: "" },
    { code: "GE-3", name: "通識：社會與文化", credits: 3, category: "通識" },
  ];
  const QUESTION = "如果我大四下去交換一學期，會延畢嗎？";
  const CPS = 5.2; // 字幕每秒字數；與下方停留、游標、打字時間一起調到全程約 2 分鐘（不含 AI 等待）

  // ---------- 樣式與覆蓋層 ----------
  const css = document.createElement("style");
  css.textContent = `
    .tour-caption { position: fixed; left: 50%; bottom: 28px; transform: translateX(-50%); z-index: 1000; max-width: min(1240px, 94vw);
      background: rgba(15, 18, 26, .88); color: #fff; font-size: 24px; line-height: 1.5; font-weight: 500; padding: 12px 26px;
      border-radius: 12px; text-align: center; letter-spacing: .02em; transition: opacity .25s; pointer-events: none; }
    .tour-caption:empty { opacity: 0; }
    .tour-pointer { position: fixed; z-index: 1001; width: 26px; height: 26px; margin: -13px 0 0 -13px; border-radius: 50%;
      background: rgba(31, 78, 140, .35); border: 3px solid #1f4e8c; pointer-events: none; opacity: 0;
      transition: left .5s ease, top .5s ease, opacity .3s, transform .15s; }
    .tour-pointer.press { transform: scale(.7); }
    .tour-ring { outline: 3px solid #e0a020 !important; outline-offset: 4px; border-radius: 10px; transition: outline-color .3s; }
    .student #requirements .req.tour-hover > .cite { display: block; }
    .tour-start { position: fixed; inset: 0; z-index: 1002; background: rgba(15, 18, 26, .55); display: grid; place-items: center; cursor: pointer; }
    .tour-start > div { background: var(--panel); color: var(--ink); border-radius: 14px; padding: 28px 34px; max-width: 560px; font-size: 17px; line-height: 1.7; }
    .tour-start h2 { margin: 0 0 8px; }
    .tour-start .warn { color: var(--fail); font-weight: 600; }
    .tour-paused::after { content: "⏸ 已暫停（空白鍵繼續）"; position: fixed; top: 16px; right: 16px; z-index: 1003;
      background: rgba(15, 18, 26, .85); color: #fff; padding: 6px 14px; border-radius: 8px; font-size: 15px; }`;
  document.head.appendChild(css);
  const caption = Object.assign(document.createElement("div"), { className: "tour-caption" });
  const pointer = Object.assign(document.createElement("div"), { className: "tour-pointer" });
  document.body.append(caption, pointer);

  // ---------- 控制：暫停、結束 ----------
  let paused = false, stopped = false;
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { stopped = true; caption.textContent = ""; pointer.style.opacity = 0; document.body.classList.remove("tour-paused"); }
    if (e.key === " " && !document.querySelector(".tour-start") && document.activeElement?.tagName !== "INPUT") {
      e.preventDefault(); paused = !paused; document.body.classList.toggle("tour-paused", paused);
    }
  });
  class Stop extends Error {}
  async function sleep(ms) {
    const end = Date.now() + ms;
    while (Date.now() < end || paused) {
      if (stopped) throw new Stop();
      await new Promise((r) => setTimeout(r, 50));
    }
  }
  async function waitFor(test, timeout = 15000) {
    const end = Date.now() + timeout;
    while (!test()) {
      if (stopped) throw new Stop();
      if (Date.now() > end) return false;
      await new Promise((r) => setTimeout(r, 100));
    }
    return true;
  }

  // ---------- 動作 ----------
  // 一個斷句一個字幕框，依序播放；每框停留依字數（每秒 CPS 字）加 0.2 秒換框，至少 1.2 秒
  async function say(...phrases) {
    for (const p of phrases) {
      caption.textContent = p;
      await sleep(Math.max(1200, (p.length / CPS) * 1000 + 200));
    }
  }
  // 畫面文字（AI 轉出的條件）放進字幕前，標點一律改成逗號
  const clean = (t) => t.replace(/[。？！：；、／「」『』（）()—]+/g, "，").replace(/，+/g, "，").replace(/^，|，$/g, "").trim();
  function center(el) {
    const r = el.getBoundingClientRect();
    return [r.left + r.width / 2, r.top + Math.min(r.height / 2, 40)];
  }
  // 平滑捲到畫面中央，等位置不再變動（捲動結束）才回傳，游標才會指在對的地方
  async function show(el) {
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    let last = null;
    for (let i = 0; i < 40; i++) {
      await sleep(100);
      const top = Math.round(el.getBoundingClientRect().top);
      if (top === last) break;
      last = top;
    }
  }
  async function point(el) {
    await show(el);
    const [x, y] = center(el);
    pointer.style.opacity = 1; pointer.style.left = `${x}px`; pointer.style.top = `${y}px`;
    await sleep(550);
  }
  async function click(el) {
    await point(el);
    pointer.classList.add("press"); await sleep(150); pointer.classList.remove("press");
    el.click();
    await sleep(300);
  }
  let ringed = [];
  function ring(...els) { unring(); ringed = els.filter(Boolean); ringed.forEach((e) => e.classList.add("tour-ring")); }
  function unring() { ringed.forEach((e) => e.classList.remove("tour-ring")); ringed = []; }
  async function type(input, text) {
    await click(input);
    input.value = "";
    for (const ch of text) { input.value += ch; await sleep(80); }
    await sleep(400);
  }
  const $$ = (sel) => document.querySelector(sel);
  const reqCard = (title) => [...document.querySelectorAll("#requirements .req")].find((r) => r.querySelector(".req-top b")?.textContent.includes(title));
  const totals = () => [...document.querySelectorAll("#current-total b")].map((b) => b.textContent);
  const simStats = () => [...document.querySelectorAll("#sim-result .card")].map((c) => c.lastElementChild.textContent.trim());

  // ---------- 準備：本學期修課設成腳本內容（與手動輸入相同） ----------
  function expectedCourses() {
    const pick = (code) => {
      const c = CURRENT.remaining_courses.find((x) => x.code === code);
      return c ? { code: c.code, name: c.name, credits: c.credits, category: "", counted: true } : null;
    };
    const mine = [pick("IM4001"), pick("IM4048")];
    if (mine.includes(null)) return null;
    return [...mine, ...OTHER_COURSES.map((c) => ({ ...c, counted: true }))];
  }

  async function prepare() {
    await waitFor(() => typeof CURRENT !== "undefined" && CURRENT && BASE_PLAN, 30000);
    const notes = [];
    if (CURRENT.id !== "DEMO-D03") notes.push(`目前學生是 ${CURRENT.id}，字幕依 DEMO-D03 撰寫，數字可能對不上。`);
    const want = expectedCourses();
    if (want && JSON.stringify(CURRENT_COURSES) !== JSON.stringify(want)) {
      await changeCurrent(want);
      await waitFor(() => totals().length === 2);
    }
    if (!want) notes.push("找不到 IM4001／IM4048，無法自動填本學期修課。");
    $$("#ask-input").value = "";
    return notes;
  }

  function startScreen(notes) {
    return new Promise((resolve) => {
      const box = document.createElement("div");
      box.className = "tour-start";
      box.innerHTML = `<div><h2>學生端自動導覽</h2>
        先開始螢幕錄影，再按<b>空白鍵</b>或點一下開始。<br>導覽中：空白鍵暫停／繼續，Esc 結束。
        ${notes.map((n) => `<div class="warn">⚠ ${esc(n)}</div>`).join("")}</div>`;
      const go = () => { box.remove(); document.removeEventListener("keydown", key); setTimeout(resolve, 600); };
      const key = (e) => { if (e.key === " ") { e.preventDefault(); go(); } };
      box.addEventListener("click", go);
      document.addEventListener("keydown", key);
      document.body.appendChild(box);
    });
  }

  // ---------- 分鏡（docs/DEMO_STUDENT.md） ----------
  async function run() {
    const t0 = performance.now();
    // 0:00–0:10 我的進度
    $$('nav button[data-view="progress"]').click();
    window.scrollTo({ top: 0 });
    showPane("summary");
    await sleep(500);
    ring($$(".student-head"));
    await say("系辦審完了", "學生自己也看得到同一份結果", "這位大四學生看到校務系統寫未通過", "第一個反應是", "我是不是畢不了業");
    unring();

    // 0:10–0:35 摘要磚：目前狀態 → 可能誤判 → 找承辦人
    await click($$('.tile[data-pane="summary"]'));
    ring($$(".tile-detail"));
    await say("學生端把兩件事分開", "真正缺的是三門必修", "還有通識和本系選修學分", "這些要自己修");
    await click($$('.tile[data-pane="flags"]'));
    await say("另一件是國文多修了 2 學分", "校務系統不會自動移到別的類別", "但可能可以由承辦人認列", "這是系統的限制", "不是真的缺");
    await click($$('.tile[data-pane="actions"]'));
    await say("旁邊直接寫出要辦什麼", "還有要找哪個單位");
    unring();

    // 0:35–0:45 畢業條件 R1 與規章依據
    const r1 = reqCard("系訂必修");
    if (r1) { await point(r1); r1.classList.add("tour-hover"); ring(r1); }
    await say("每一條都列出缺什麼", "附上規章出處", "學分和判定全部來自系辦同一套審查引擎", "學生端不自己算");
    r1?.classList.remove("tour-hover"); unring();

    // 0:45–1:10 本學期修課
    await click($$('nav button[data-view="plan"]'));
    window.scrollTo({ top: 0 });
    ring($$("#current-card"));
    await show($$("#current-card"));
    const [now, trial] = totals();
    await say("現在是大四上", "成績還沒出來", "學生輸入這學期修的課", "假設都會過");
    ring($$("#current-total"), $$("#base-plan .term.grad"));
    await say(`學分從 ${now} 變 ${trial}`, `預估 ${BASE_PLAN.graduation} 畢業`);
    const box = () => $$('#current-courses input[data-code="IM4048"]');
    if (box()) {
      unring();
      await say("如果管理資訊系統沒過", "取消勾選");
      // 勾選改變後頁面會重新試算並重畫清單，等學分數字變了再往下
      const before = totals().join();
      await click(box());
      await waitFor(() => totals().join() !== before);
      await sleep(600);
      ring($$("#current-total"), $$("#base-plan .term.grad"));
      await say("它只在上學期開", `畢業就延到 ${BASE_PLAN.graduation}`);
      unring();
      const after = totals().join();
      await click(box());
      await waitFor(() => totals().join() !== after);
      await sleep(400);
    }

    // 1:10–1:45 一句話 What-if
    const input = $$("#ask-input");
    await show(input);
    await say("規劃問題也可以直接用一句話問");
    await type(input, QUESTION);
    await click($$("#ask-run"));
    caption.textContent = "AI 轉換中";
    await waitFor(() => !$$("#ask-run").disabled, 150000);
    const confirm = $$("#ask-confirm");
    if (confirm) {
      ring($$("#ask-result"));
      const conds = [...document.querySelectorAll("#ask-result .impact li")].map((li) => clean(li.textContent));
      await say("AI 只做一件事", "把問題轉成模擬條件", ...conds, "而且要學生確認才算");
      await say("下面是執行軌跡", "用了哪個模型", "檢核幾次", "有沒有自我修正");
      if ($$("#ask-result").textContent.includes("重播")) await say("這是預先錄下的模型回應");
      unring();
      await click(confirm);
    } else {
      // 腳本備案：AI 服務無法使用時改用手動選項，不提 AI 轉換
      ring($$("#ask-result"));
      await say("AI 服務無法使用時", "學生一樣可以手動選情境");
      unring();
      $$("#away-term").value = "117-2"; $$("#away-count").value = "1"; $$("#away-credits").value = "15";
      await click($$("#run-sim"));
    }

    // 1:45–2:00 模擬結果
    await waitFor(() => simStats().length >= 3, 30000);
    await sleep(500);
    const result = $$("#sim-result .verdict");
    await show($$("#sim-result"));
    ring(result);
    const [base, scen, delay] = simStats();
    await say("結果由規劃器計算", "電子商務只在下學期開", "交換一學期", `畢業從 ${base} 延到 ${scen}`, `延後 ${delay}`);
    await say("學生在出國前就知道", "要先找系上討論怎麼調整");
    unring();
    await say("學生端為試算", "正式結果以系辦審查為準");
    caption.textContent = ""; pointer.style.opacity = 0;
    console.info(`學生端導覽結束：共 ${((performance.now() - t0) / 1000).toFixed(1)} 秒（含 AI 等待）`);
  }

  (async () => {
    try {
      const notes = await prepare();
      // ?tour=auto 直接開始（自動截圖檢查用），一般錄影用 ?tour 會先停在開始畫面
      if (MODE !== "auto") await startScreen(notes);
      await run();
    } catch (err) {
      if (!(err instanceof Stop)) { caption.textContent = `導覽中斷：${err.message}`; console.error(err); }
    }
  })();
})();
