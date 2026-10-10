# 現有實作與待建置項目

更新日期：2026-10-11。依目前程式整理；測試 72 項通過。

## 現有能力

| 項目 | 現況 |
|---|---|
| 知識庫 | 課程、正式／建議先修、要求、類別、採計政策、來源及假設 |
| 審查引擎 | 必修、選修、類別學分／門數、總學分、四種狀態及逐條結果 |
| 紀錄處理 | 不及格、重修、同課號去重、本系未核對課、類別與外系課 |
| 課程圖 | 前後依賴、瓶頸及循環檢查 |
| 規劃器 | 貪婪排課，考慮季節、先修、學分上限及不修課／交換情境 |
| 命令列 | 單人查詢及依應屆名單的批次文字審查 |
| 名單比對 | 應屆名單對修課紀錄：無紀錄、不在名單、重複、學號空白、入學年度與規則庫不符（選填 `cohort` 欄）列入異常清單；選填 `english_passed` 欄供英文門檻判定（`academic_twin/roster.py`） |
| 審查明細 | 單一學生可列印的 HTML 明細：判定、逐條結果與出處、學分採計、覆核紀錄、修課紀錄，附規則版本與審查時間；網頁「列印審查明細」、命令列 `--report`（`academic_twin/report.py`） |
| 資料上傳 | 網頁「資料來源」可上傳修課紀錄與應屆名單（可只換一份），命令列 `--records`／`--roster`；修課紀錄逐列檢查欄位、學分、成績、學期格式，有錯整份不收並列出行號（`academic_twin/records.py`） |
| 人工覆核 | 學生頁對 Manual Review／Warning 紀錄選擇採計方式（歸入類別、本系選修、外系認列、不採計），需填覆核人，存檔後立即重審；可撤銷，歷程只增不改；規則庫版本變動時標示「建議再確認」。規則庫能判定的紀錄不接受覆核（`academic_twin/reviews.py`，存 `data/reviews/decisions.json`） |
| 結果匯出 | 批次審查結果與名單異常匯出 CSV，每列附規則版本（學年度＋規則庫雜湊）與審查時間；命令列 `--export`、網頁「匯出 CSV」 |
| 測試程式 | `tests/test_rule_engine.py`、`tests/test_planner.py`、`tests/test_curriculum_114.py`、`tests/test_roster.py`、`tests/test_export.py`、`tests/test_records.py`、`tests/test_reviews.py` |

## 資料與基本操作

- `data/curriculum_im_114.json`：114 年度規則，含來源連結與待確認假設。
- `data/sources/im_114_table.pdf`、`data/sources/im_114_coursemap.pdf`：114 應修科目表與課程地圖。
- `data/sources/im_requirements_slide.jpg`：應修科目表說明投影片（必選課定義、本系／外系選修學分）。
- `data/sources/im_111_audit_guide.pdf`：111 學年畢業審核表說明（中文字無法直接抽取，需轉成圖片閱讀）。
- `data/sources/im_113_coursemap.pdf`：113 學年度課程地圖。
- `data/students/sample_students.csv`：匿名示例紀錄。
- `data/students/sample_roster.csv`：示例應屆名單，含一位無修課紀錄的 S005 以示範異常清單。
- `tests/fixtures/curriculum_sample.json`：測試示例規則。

規則庫固定使用上述 114 年度 JSON，尚無逐人年度切換；修課紀錄與名單預設為示例 CSV，可從網頁上傳或用命令列指定。CSV 欄位如下，`category` 可空白：

```text
student_id,code,name,credits,grade,term,category
```

在專案根目錄執行：

```powershell
python app.py
python app.py --roster 名單.csv
python app.py S002
python app.py S002 --plan
python app.py --bottlenecks
```

依序為批次審查、指定名單的批次審查、單人查詢、修課規劃及瓶頸分析；虛擬環境可使用 `.venv\Scripts\python.exe`。

## 待建置

- 系辦單人查詢、批次審查、明細及篩選介面。
- 校務系統匯出欄位對接（目前需先轉成本專案 CSV 格式）；上傳資料持久保存。
- AI 文件解析確認與依審查結果生成說明。
- 按年度、學制選用規則及版本管理。
- 舊課號、替代課、抵免與特殊核准的認列模型。
- 覆核人身分驗證（目前覆核人為自填姓名，無登入）。
- 資料儲存、系辦權限與存取方式。

## 規則待確認事項

現有 JSON 已引用 114 年度應修科目表與課程地圖，並非只有早期示意資料；但 `assumptions` 保留以下假設：

- 必選課 IM3029、MA1006 暫設 3 學分；停修是否算「修過」待確認。
- 開課學期部分依建議學期推定，不是實際開課保證。
- 建議先修由順序推論，只影響排課，不影響畢業判定。
- 服務學習門數為暫定解讀；體育 5 學期含大一體育 2 學期，以課名含「大一體育」辨識。
- 通識核心領域要求採簡化判定，細節待補。
- 非 IM 課號可否認列本系選修待確認，目前以人工覆核處理。
- 國文、外文超修及第二外語暫計入外系 12 學分，待確認。

2026-10-10 依說明投影片、111 畢審說明與 113 課程地圖重新審閱，並已改入規則庫：

- R3 必選課改為「修過即可」（`taken_all`）；通過的學分 IM1013、IM3029 計入本系選修，MA1006 計入外系選修並受 12 學分上限（課程 `counts_as`）。
- 新增 R11 本系選修至少 16 學分（`dept_elective_credits`）。
- 不在規則庫的 IM 課號直接算本系選修，不再標 Warning（`dept_prefix_is_elective`）。未開啟此設定的規則庫仍沿用 Warning 機制。

已從文件得知、但尚未改入規則庫：

- 體育 5 學期須含大一體育上下兩學期（需先知道如何辨認大一體育）。
- 111 版另有學生學習護照（服務學習 100 小時）、操性等條件，需確認 114 是否適用。

2026-10-11 依語言中心公告新增 R12 英文畢業門檻（`english_threshold`）：名單 `english_passed` 欄（Y／N）判定；未提供時進修英文及格滿 4 學分即符合，否則為「待確認」並使判定成為 Manual Review（已有明確缺漏者仍為 Fail）。

正式使用前應由系辦確認其餘假設與規則完整性；問題清單與文件出處見 [畢業規則確認清單](OFFICE_QUESTIONS.md)。

## 限制

系統目前只有 114 學年度規則庫；名單 `cohort` 欄為其他年度的學生列為異常、不審查，空白視為 114。尚未區分學制。網頁上傳的資料只存在伺服器記憶體，重開即回到示例資料。

排課最多模擬 16 學期，使用貪婪法，畢業學期為估計且不保證全域最早。非指定課號需求以「其他學分」補足，尚未逐門安排，也未處理時間衝突、名額及實際開課異動。

來源引用不等於完整覆核紀錄；介面、AI、版本、覆核、匯出依 [主要流程](PROJECT_DESIGN.md) 後續建置。

