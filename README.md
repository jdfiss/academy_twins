# NCU Academic Twin

為每位學生建立「學業分身」：即時知道目前的畢業進度、模擬未來修課路徑、提前預警延畢，並協助系辦做畢業資格初審。

目前範圍（MVP）：**中央大學資訊管理學系 114 學年度入學**，規則庫依官方應修科目表建立。

## 設計原則
- **AI 不負責最終資格判定**：所有判定由規則引擎處理
- **每個判定都能回溯到規章條文**
- **規則庫看不懂的不猜**：不確定的紀錄標為 Warning 或 Manual Review，交由系辦確認

## 快速開始
只需要 Python 3.10+，不用安裝任何套件。

```bash
python web.py                              # 網頁介面 http://localhost:8000
python app.py                              # 系辦批次畢審（含應屆名單比對）
python app.py --roster 名單.csv            # 指定應屆名單（需有 student_id 欄）
python app.py --records 修課紀錄.csv       # 指定修課紀錄（先做格式檢查，有錯整份不收）
python app.py --export 結果.csv            # 批次畢審結果匯出 CSV（附規則版本與審查時間）
python app.py S002                         # 單一學生畢業進度
python app.py S002 --plan                  # 排出到畢業的修課路徑
python app.py S002 --skip IM2011@116-1     # What-if：116-1 不修資料與檔案結構
python app.py S002 --away 116-2            # What-if：116-2 出國交換
python app.py --course IM2011              # 這門不修會卡到哪些課
python app.py --bottlenecks                # 瓶頸課排行
python app.py --assumptions                # 規則庫待確認的假設
python -m unittest discover -s tests -t .  # 測試
```

## 架構
```
data/curriculum_im_114.json   規則庫：課程、畢業規則（附條文出處）、學分認列政策
data/students/                範例學生修課紀錄（虛構）
data/sources/                 官方應修科目表與課程地圖 PDF
academic_twin/
  knowledge_base.py           載入並驗證規則庫
  rule_engine.py              畢業資格判定：Pass / Warning / Fail / Manual Review
  graph.py                    先修關係 DAG：影響範圍、瓶頸課
  planner.py                  學期排課與 What-if 延畢分析
  api.py                      引擎結果 → JSON（給網頁與之後的 LLM 層）
app.py                        命令列介面
web.py, web/index.html        網頁介面
```

## 已知限制
- 官方文件沒有正式擋修規定；圖中的先修關係是由建議修課學期推論的「建議先修」，只用於排課，不影響畢業判定
- 本系選修課程目錄（課號、學分）尚未建檔，不在規則庫的 IM 課號暫以本系選修計並標為 Warning
- 其他待確認的解讀見 `python app.py --assumptions` 或網頁「規則來源」分頁

## 資料來源
- [資管系 114 學年度應修科目表](https://im.mgt.ncu.edu.tw/download/course/rules/114學年應修科目表.pdf)
- [資管系大學部課程地圖（114 學年度）](https://im.mgt.ncu.edu.tw/course/courseMap)
