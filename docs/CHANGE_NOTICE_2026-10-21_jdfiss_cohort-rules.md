# 共用模組變更通知：規則依入學年度取用

- 日期：2026-10-21（程式於 2026-10-20 完成）
- 改動者：jdfiss（A 組）
- 分支：`a/cohort-rules`（commit `c391fd8`），尚未合併到 `main`
- 對象：B 組（依 [兩組分工計畫](TEAM_PLAN.md#檔案歸屬)，`academic_twin/api.py` 為「A（共用，變更需通知 B）」）

## 為什麼改

A 組優先 1「規則依入學年度取用（最小版）」：同一批學生可能來自不同入學年度，必須各自用自己年度的規則審查；沒有該年度規則時列為異常，不能拿別的年度頂替。

## 改了什麼

| 項目 | 改動前 | 改動後 |
|---|---|---|
| 規則庫載入 | `CoreService` 只載入一份規則庫 | 可同時載入多個年度：`kb_path` 為預設年度，新參數 `extra_kb_paths` 為其他年度；同一年度重複載入直接報錯 |
| 學生適用的規則 | 全部用 `self.kb` | 新方法 `CoreService.ruleset(sid)` 依名單 `cohort` 回傳該生的 `RuleSet`（`kb`、`graph`、`rule_version`）；名單沒填年度時用預設年度；沒有該年度規則庫時丟 `KeyError` |
| 名單異常 | 入學年度與「唯一的規則庫年度」不同就列 `other_cohort` | 入學年度不在「已載入的年度」才列 `other_cohort`，不審查、不套其他年度 |
| `course(code)` | 只查預設規則庫 | 新增選填參數 `kb`，可指定查哪一年度的規則庫 |
| `meta()` | — | 新增 `cohorts`：已載入規則庫的年度清單 |

`self.kb`、`self.graph`、`self.rule_version` 保留，仍指向預設年度。目前 `web.py` 與 `app.py` 只載入 114，所以實際畫面行為還沒有改變；多年度只在測試中用虛構規則庫驗證（`tests/test_cohort_rules.py`）。

## 對 B 組的影響

**交接格式沒有改**：`student()` 欄位不變，`tests/test_contract.py` 照常通過。只有兩個欄位的**值**改成依該生年度：

- `cohort`、`rule_version`：該生適用規則庫的年度與版本，不再固定是預設年度。
- `requirements[].missing`、`remaining_courses`：從該生年度的規則庫取課程資料。

**學生端需要跟進的地方**（屬 B 組「規劃器改用入學年度規則」，時間由 B 組決定）：

- `student_api.py` 的 `course_impact`、`twin`、`simulate` 目前用 `self.kb`、`self.graph`，也就是預設年度。載入多個年度後，非預設年度的學生會用錯年度的課程與先修關係推演。
- 建議改用 `rs = self.ruleset(sid)`，再以 `rs.kb`、`rs.graph` 取代 `self.kb`、`self.graph`；查課程用 `self.course(code, rs.kb)`。
- `ruleset(sid)` 遇到沒有規則庫的年度會丟 `KeyError`，學生端要顯示「目前沒有你入學年度的規則」，不要退回預設年度。

在 `web.py` 載入第二個年度之前，學生端不改也不會出錯。

## 驗證

- 新增 `tests/test_cohort_rules.py`：同批不同年度各用自己的規則、缺年度列異常不審查、`rule_version` 帶出年度、114 原有行為不變。
- 在本分支（從 `main` 分出）完整測試 117 項通過。

## 相關文件

- [現有實作與待建置項目〈規則依入學年度取用〉](IMPLEMENTATION_STATUS.md#規則依入學年度取用)
- 有疑問請在 `a/cohort-rules` 合併的 PR 留言，或直接找 jdfiss。
