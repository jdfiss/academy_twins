# 交接文件 — 系辦端「監控者模式」改版（批次放行＋抽查、監控台、學生問題收件匣 AI）

> 寫於：2026-11-07 15:25
> 上一個 session：講解系辦端 → 做系辦端設計書 → 與「郭家瑞」反方討論出改版計畫 → 實作 M1–M5；改了 9 個檔案、新增 10 個檔案，全部**尚未 commit**
> 接班人請從「立即動作」開始讀

---

## 🎯 使用者原始需求

使用者（專案負責人兼 A 組對接人）看完系辦端說明後提出六點（原話）：
1. 「建資料庫時 人工需要手動匯入每位學生的已修總學分???」→ 答案：不用，總學分由引擎從逐門課紀錄加總（`academic_twin/rule_engine.py` 的 `audit()`）。
2. 「人工應該升級為監控者 覺得目前人工工作太多太雜 可能系統作為監管儀表板的程度要再加強」
3. 功能要分層：核心／偶爾用／輔助／小功能，只呈現核心。
4. 「主要還是覺得整體亮點不夠乾淨俐落」
5. 「若能更凸顯ai亮點會更好」
6. 「系辦端會常常處理學生發來的小問題 能自動全部處理好 給監控者確認修改否決」

之後使用者說：「不管時間 沒差 做的完 /郭家瑞 一起跟他協作 詳細計畫」→ 產出 `docs/OFFICE_MONITOR_PLAN.md`；再說「做」→ 實作；最後「收尾」「交接優先」。

---

## 📍 現在進度到哪

詳細計畫與狀態表：`D:\temppp\Academic-Twin\docs\OFFICE_MONITOR_PLAN.md`（開頭有〈實作狀態（2026-11-07）〉）。

### 已完成 ✅
- **M1 工作狀態存檔**：`academic_twin/office_state.py`（新）；`OfficeMixin.restore_office_state()`、`_save_state()`、覆寫 `load_data()`（`academic_twin/office_api.py`）；`DashboardBatch` 加 `on_change`、`to_state()`、`restore()`（`academic_twin/dashboard_batch.py`）。存在 `data/office_state/state.json`（已加入 `.gitignore`）。`web.py` 的 `main()` 啟動時呼叫 `service.restore_office_state()`。
- **M2 分流＋批次確認＋隨機抽查**：`academic_twin/monitor.py`（新，`MonitorMixin`、`triage()`、`sample_size()`）。四桶：失敗／需修正／需核對／建議放行；曾自我修正的份放「需核對」；抽查用 `secrets.SystemRandom`，`max(1, ceil(10%))`；抽查有修改 → 整批改「需核對」並停用批次放行，需手動 `release_reopen`。`dashboard_confirm()` 新增 `mode` 參數（single／sampled／batch）並寫入 log。名單異常新增 `dashboard_pending`。
- **M3 指標**：`academic_twin/metrics.py`（新）；`OfficeMixin.office_metrics()`；GET `/api/metrics`。每項 `{k, n}`，n<5 只顯示件數。
- **M4 介面**：`web/office.html`、`web/assets/office.js` 重寫：分頁「① 監控台 ② 收件匣 ③ 學生 ④ 規則 ＋ 工具」，頁首有 AI 總開關；舊網址 `#ingest`、`#batch` 導向監控台。
- **M5 學生問題收件匣**：`academic_twin/inbox.py`（新，`InboxMixin`、`draft_reply()`、`Tools`、`check_reply()`、`check_proposals()`、`evaluate()`、CLI `python -m academic_twin.inbox`）。示範信件 `data/samples/inbox_demo.json`（10 封）、評測集 `data/samples/inbox_eval.json`（30 題，dev／validation）。
- `web.py` 新增路由（不改既有路由）：`/api/dashboard/release[/finish|/cancel|/reopen]`、`/api/inbox`、`/api/inbox/<id>`、`/api/inbox/demo|import|process|approve-many`、`/api/inbox/<id>/decide`、`/api/inbox/<id>/proposals/<i>`。
- `academic_twin/dashboard.py` 的 `log_run()` 加鎖（多執行緒同時寫曾經少一行，造成測試偶發失敗）。
- 測試：新增 `tests/test_monitor.py`（18 項）、`tests/test_inbox.py`（22 項）。加入評測程式前，完整測試連跑 3 次全過（346 項）；加入評測後 `tests.test_inbox` 通過。
- 畫面：用評審模式跑過 6 份示範 PDF → 5 份建議放行、DEMO-D07 需核對（未交叉驗證）→ 抽查 1 份放行 4 份。已用無頭 Edge 截圖確認監控台與收件匣版面。
- 文件：`docs/TEAM_PLAN.md`（〈AI 輸入與啟用驗收〉儀表板列、A 組選配列改為收件匣）、`docs/IMPLEMENTATION_STATUS.md`（新增 4 列能力）、`docs/OFFICE_MONITOR_PLAN.md`（實作狀態）。

### 進行中 🚧
- ✅ 最後一次完整測試：347 項全過（改完文件後執行）。
- 🚧 **M6 實證被額度卡住**：2026-11-07 約 15:19，Gemini 回 429「You exceeded your current quota」。原因是同時跑的基準量測（`python -m academic_twin.benchmark --runs 3 --workers 2`）把額度用完：68 次執行中 65 次失敗。量測已手動中止，**沒有產生報告檔**。收件匣錄製也因 429 全部失敗，沒有留下錄製檔；失敗的 log `data/eval/inbox_runs.jsonl` 已刪除。

### 還沒做 ⏳
- [ ] 重跑完整測試並確認全過
- [ ] 請使用者決定是否 commit（尚未 commit；使用者沒有明確說要 commit）。commit 時不要包含 `docs/MERGE_KIT_2026-10-21.md`、`docs/OFFICE_EMAIL_2026-10-22.md`，這兩份是舊的未追蹤檔，與本次無關
- [ ] M0：TEAM_PLAN 是兩組共用準則、`web.py` 是共用檔 → PR 需要 B 組對接人 Goldaniga（GitHub `Goldaniga1`）同意；由使用者 push 與合併
- [ ] M6 額度恢復後：①`python -m academic_twin.benchmark --runs 3`（不夠就 `--runs 1`）②`python -m academic_twin.inbox --split validation` ③錄製收件匣評審模式（見下）
- [ ] M7 Demo 影片腳本（計畫內有 2 分鐘腳本表）
- [ ] `docs/OFFICE_SYSTEM_DESIGN.html` 還是改版前的內容，需要補監控台、收件匣兩張流程圖（已發布為 artifact：https://claude.ai/artifact/J64U8SgpGD5WaTkhtfuwK6 ，同一路徑重新發布即可更新）

---

## 🗂️ 動過的檔案（皆在 `D:\temppp\Academic-Twin\`，分支 `a/monitor-mode`，自 `main` 開出）
| 檔案 | 改了什麼 | 狀態 |
|---|---|---|
| `academic_twin/office_state.py` | 新：存檔讀寫、Extraction 序列化、名單／紀錄轉 CSV | ✅ 有測試 |
| `academic_twin/monitor.py` | 新：分流與批次放行抽查 | ✅ 有測試 |
| `academic_twin/metrics.py` | 新：監控台指標 | ⚠️ 只有經 UI 間接使用，沒有專屬單元測試 |
| `academic_twin/inbox.py` | 新：收件匣 agent、評測 | ✅ 替身測試；🚧 真實模型未跑過 |
| `academic_twin/office_api.py` | 繼承 Monitor／Inbox Mixin、存檔、`source_check` 寫入 trace、confirm 的 mode | ✅ |
| `academic_twin/dashboard_batch.py` | on_change／to_state／restore；summary 加 `source_check`、`replay` | ✅ |
| `academic_twin/dashboard.py` | `log_run` 加鎖 | ✅ |
| `web.py`（共用） | 新路由、啟動時接續存檔、收件匣錯誤訊息 | ✅ 匯入正常、實際跑過 |
| `web/office.html`、`web/assets/office.js` | 監控者模式介面重寫 | ✅ 截圖檢查；⚠️ 收件匣詳細頁的按鈕沒有實際點擊測試 |
| `.gitignore` | 加 `data/office_state/` | ✅ |
| `data/samples/inbox_demo.json`、`inbox_eval.json` | 虛構信件與評測集 | ✅ |
| `tests/test_monitor.py`、`tests/test_inbox.py` | 新測試 | ✅ |
| `docs/TEAM_PLAN.md`、`IMPLEMENTATION_STATUS.md`、`OFFICE_MONITOR_PLAN.md`、`OFFICE_SYSTEM_DESIGN.html` | 文件 | ✅（設計書尚未更新到改版後） |

---

## 🧠 關鍵發現 / 決策脈絡
- **沒有「無人放行」**：郭家瑞指出「自動通過」違反 TEAM_PLAN「所有擷取結果均須供承辦人確認」，所以改成「AI 建議放行＋監控者一鍵確認＋強制隨機抽查」。**抽查是閘門，不是量測**，準確率由基準集提供。
- **收件匣邊界**：分 6 類，correction／other 一律轉人工並用固定文字（AI 只負責分類）；工具沒有學號參數，所以結構上查不到別人；回覆經 grounding 檢查，未過則反思 1 次，仍未過標「無法確認」。
- **grounding 的限度**：數字只檢查「是否出現在工具結果中」，不檢查是否用在對的地方（例如 12 常常出現），所以擬稿仍須人核准。測試裡要用 999 這種不會出現的數字。已寫在 `inbox.py` 開頭說明。
- **防蓋章**：帶覆核建議、grounding 未過或需重新擬稿的信不能批次核准；指標公開「未修改即核准率」。刻意**不做**故意埋錯稿的測驗（會弄髒資料，對承辦人也不誠實）。
- `source_check` 放在 trace 的獨立欄位，**不放進 `steps`**：既有 `tests/test_dashboard.py` 斷言 steps 的最後一步是 retry／check。
- 收件匣提示內含 JSON 範例的大括號 → 用 `.replace()` 組提示，不能用 `str.format`。
- **評審模式錄製的正確做法**（重要）：收件匣提示包含工具結果，而工具結果來自確認後的修課紀錄。所以錄製時儀表板必須用 `ACADEMIC_TWIN_REPLAY=only` 的重播結果，6 份都確認且**不修改**，之後才切到 `ACADEMIC_TWIN_REPLAY=fallback ACADEMIC_TWIN_RECORD=1` 處理示範信件。可以用這個 session 寫好的腳本（在 scratchpad，可能已被清掉；邏輯如上，重寫很快）：載入示範批次 → 加入 6 份 PDF → 全部 `dashboard_confirm`（cohort 用草稿的 114）→ 設環境變數 → `inbox_load_demo()` → `inbox_process()` → `inbox_wait()` → 對「轉學抵免的課」那封採用覆核建議並重新擬稿（也要錄）。Demo 時監控者若改了任何紀錄，收件匣就會對不上錄製。
- 評審模式示範批次的實際結果：DEMO-D03 MANUAL_REVIEW、D04 PASS、D05 PASS（通識超修）、D06 FAIL（R12 英文門檻）、D07 MANUAL_REVIEW（IM9001 重複通過）、D08 PASS。

---

## 🔧 環境 / 上下文
- 專案：`D:\temppp\Academic-Twin`；分支 `a/monitor-mode`（自 `main` 01bf1b2 開出）。另一個分支 `a/fix-d07-timeout` 有未合併的 commit 90a8625，與本次無關。
- 背景程序：測試 task `bteoo4gey` 可能還在跑或已結束；伺服器都已停止。
- `data/office_state/` 已清空（截圖用的替身擬稿已刪）。
- 規則：只有使用者能 `git push`（hook 會擋）；PR 動到共用檔要 Goldaniga 審。只用標準函式庫；只用虛構資料。
- 相關 memory：`project-roles-and-push.md`、`competition-plan-2026-10.md`、`office-overhaul-2026-11-06.md`、`save-deliverables-in-repo.md`
- 比賽：115-11-11 23:59 繳交報告與影片；11-27 決賽

---

## ▶️ 立即動作（給接班人）
1. 讀本文件與 `docs/OFFICE_MONITOR_PLAN.md`（開頭實作狀態），以及 `AGENTS.md` → `docs/TEAM_PLAN.md`
2. `git checkout a/monitor-mode`，跑 `python -m unittest discover -s tests -t .`，確認全過（預期約 347 項）
3. `ACADEMIC_TWIN_REPLAY=only python web.py`，開 `office.html#monitor`，從「載入示範批次」實際點一次：丟 6 份 PDF → 批次確認 → 抽查 → 放行 → 收件匣（評審模式下收件匣會失敗，因為還沒錄製）
4. 問使用者：要不要 commit／開 PR（commit 訊息英文祈使句，例如 `Add monitor mode: release sampling, office metrics, student inbox agent`）
5. Gemini 額度恢復後做 M6（見〈還沒做〉）

---

## 🧰 建議使用的 skills
- `principle-prove-it-works`：宣告完成前用實際畫面與 API 驗證
- `run`：啟動 web.py 實測
- `code-review` 或 `thermos`：開 PR 前審查這次大改動
- `artifact-design`／`diagram-design`：更新 `OFFICE_SYSTEM_DESIGN.html` 的流程圖

---

## ⚠️ 切記
- 不要讓 AI 直接改判定；不要做「無人放行」；收件匣不得真的寄信
- 不要在 Gemini 額度不足時跑全量 benchmark：會產生大量失敗，報告沒有意義
- 改了 `inbox.PROMPT`、`dashboard.PROMPT` 或模型名稱，評審模式錄製就會失效，需要重錄
- 示範批次與收件匣只用虛構資料；不讀、不產生真實學生資料
