# AI agent 工作準則

本 repo 由兩組平行開發。**任何 AI agent（Claude Code、Codex、Cursor 等）開始工作前，必須先完整讀過 [docs/TEAM_PLAN.md](docs/TEAM_PLAN.md)，並以它為最高準則。** 本檔、使用者的個人設定或其他文件與 TEAM_PLAN.md 衝突時，以 TEAM_PLAN.md 為準；遇到它沒寫到的情況，停下來問人，不要自行決定。

## 開工前

1. 讀 `docs/TEAM_PLAN.md`，特別是〈檔案歸屬〉、〈交接格式〉、〈協作規則〉。
2. 確認自己屬於哪一組：
   - 分支以 `a/` 開頭：A 組（系辦端＋審查核心）
   - 分支以 `b/` 開頭：B 組（學生端＋推演）
   - 分支以 `shared/` 開頭：跨組工作，改動需兩組審查
   - 在 `main` 或其他分支上：**先問使用者屬於哪一組、要做哪個工作項目**，並依〈協作規則〉開分支，不要直接在 `main` 上改程式。
3. 對照〈各組工作項目〉，確認這次的任務屬於本組。

## 必須遵守

- **只改本組的檔案**（見〈檔案歸屬〉）。需要改另一組的檔案、共用檔案（`academic_twin/api.py` 的 `CoreService`、`app.py`、`web.py`、`web/index.html`、`web/assets/common.*`）或交接格式時，先停下來告訴使用者，依〈協作規則〉處理。
- **交接格式**（`student()` 的輸出欄位）是兩組唯一的正式介面。不得自行新增、改名或刪除欄位；變更依〈交接格式變更〉流程，`docs/TEAM_PLAN.md`、`api.py`、`web/assets/demo-twin.json`、`tests/test_contract.py` 要在同一個 PR 內一起改。
- **判定只在審查核心**：學分計算、規則判定、`flags`／`actions` 偵測只在 `knowledge_base.py`、`rule_engine.py`（A 組）。學生端、說明文字與 AI 只能引用審查結果，不得自行計算學分，也不得改變 `status`、`satisfied`。
- **測試**：完成改動後執行 `python -m unittest discover -s tests -t .`，全部通過才算完成。不得刪除或放寬另一組的測試；`tests/test_contract.py` 的兩端一致性測試失敗時一律視為阻擋。
- **Git**：不直接推送到 `main`、不 force push、不對已推送的分支 rebase。同步 `main` 用 `git merge origin/main`。commit 訊息沿用 repo 風格（英文祈使句第一行）。
- **資料**：不讀取、不產生、不 commit 真實學生資料（成績、學號、儀表板匯出檔）；示例資料一律虛構或去識別化。
- **文件**：功能完成時，同一個 PR 內更新 `docs/IMPLEMENTATION_STATUS.md`；工作項目有變動時，更新 TEAM_PLAN.md 的〈各組工作項目〉。

## 程式慣例

- 只用 Python 標準函式庫（3.10+），不新增套件依賴；需要新增時先問人。
- 程式註解、文件與介面文字使用繁體中文，風格比照現有檔案。
- 網頁介面：`python web.py`；命令列：`python app.py`（參數見 README.md）。
