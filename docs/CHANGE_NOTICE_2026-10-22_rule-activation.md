# 共用模組變更通知：規則版本換用

- 日期：2026-10-22
- 改動者：yangsunny0830（A 組）
- 分支：`a/rule-activation`（疊在 `a/office-flow` 上），尚未合併到 `main`
- 對象：B 組（依 [兩組分工計畫](TEAM_PLAN.md#檔案歸屬)，`academic_twin/api.py` 為「A（共用，變更需通知 B）」）

## 為什麼改

A 組優先 1「AI 草擬規則庫」：已確認的規則草稿須由承辦人明確啟用，才會用於審查（見 [實作狀態〈規則版本啟用〉](IMPLEMENTATION_STATUS.md#規則版本啟用)）。啟用或還原後，伺服器要立刻換用該年度的新規則庫，不必重開。

## 改了什麼

| 項目 | 改動前 | 改動後 |
|---|---|---|
| `CoreService.use_ruleset(path)` | — | 新方法：載入規則庫檔案並取代同一年度已載入的 `RuleSet`；預設年度時一併更新 `self.kb`、`self.graph`、`self.rule_version`。只能換已載入的年度 |
| `web.py` 啟動 | 載入 `data/` 下各年度 `curriculum_im_<年度>.json` | 同左，但某年度有已啟用的規則版本時改載入該版本（`rule_versions.startup_paths()`） |

**交接格式沒有改**：`student()` 欄位不變，`tests/test_contract.py` 照常通過。

## 對 B 組的影響

- 承辦人啟用或還原規則版本後，該年度學生的 `rule_version` 會改變，`requirements`、`missing`、`remaining_courses` 依新版本產生。學生端依 `rule_version` 顯示版本即可，不需另外處理。
- `student_api.py` 若直接持有 `self.kb`、`self.graph` 的參照（例如存在其他變數），換用後可能仍指向舊版本；建議每次需要時從 `self.kb` 或 `self.ruleset(sid)` 重新取得。

## 驗證

- 新增 `tests/test_rule_versions.py`：啟用阻擋條件、只取代草稿要求、正式規則庫檔案不變、還原、伺服器啟動沿用、服務換用後審查改變。
- 完整測試 179 項通過。
