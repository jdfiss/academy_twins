# Demo 錄影使用說明

更新日期：2026-11-09。給要錄比賽 Demo 影片的組員，以及協助他們的 AI agent。兩段都有自動播放：網頁會自己點選、捲動、打字並疊上字幕，錄影的人只要開螢幕錄影。分鏡、旁白與預期數字以 [系辦端 Demo 腳本](DEMO_OFFICE.md) 和 [學生端 Demo 腳本](DEMO_STUDENT.md) 為準，本文只講怎麼操作。

| 段落 | 網址 | 程式 | 長度 |
|---|---|---|---|
| 教務處端 | `http://localhost:8000/autopilot.html` | `web/autopilot.html`（A 組） | 約 2 分鐘 |
| 學生端 | `http://localhost:8000/student.html?id=DEMO-D03&tour` | `web/assets/student-tour.js`（B 組） | 約 2 分鐘，AI 等待另計 |

全部使用虛構示範資料（DEMO-D03 等），不需要也不可以用真實學生資料。

## 一、事前準備（每台電腦一次）

1. 取得最新程式並確認測試通過：
   ```
   git checkout main
   git pull
   python -m unittest discover -s tests -t .
   ```
   測試沒有全部通過就先不要錄，數字可能跟腳本對不上。
2. 不需要 Gemini 金鑰。兩段都用**評審模式**重播已錄好的 AI 回應（`data/replay/`），不呼叫模型、不花額度，每次結果都一樣。

## 二、啟動伺服器（評審模式）

在 repo 根目錄執行：

- Windows PowerShell：
  ```powershell
  $env:ACADEMIC_TWIN_RECORD=''; $env:ACADEMIC_TWIN_REPLAY='only'; python web.py
  ```
- macOS／Linux／Git Bash：
  ```bash
  ACADEMIC_TWIN_REPLAY=only python web.py
  ```

開 `http://localhost:8000/office.html`，頁首出現「評審模式」橫幅就是對的（學生頁沒有這個橫幅）。伺服器保持開著，錄完再按 Ctrl+C 關掉。

## 三、瀏覽器與錄影工具

- 用 Chrome 或 Edge，縮放 100%，按 F11 全螢幕，或至少關掉書籤列。建議畫面寬 1440 以上，學生端上排五塊摘要才會排成一列。
- 關掉通知、聊天軟體跳出視窗。
- 錄影工具擇一：
  - Windows：Xbox Game Bar，按 Win+Alt+R 開始／停止，錄目前視窗；或 Win+Shift+S 剪取工具的「錄製」，可框選範圍。
  - macOS：Cmd+Shift+5 →「錄製選取的部分」。
  - 需要剪接或疊旁白：OBS Studio。

## 四、錄教務處端（先錄這段）

1. 開 `http://localhost:8000/autopilot.html`。
2. 右上角面板選速度、字幕：要配旁白就把「顯示字幕」關掉。
3. 開始螢幕錄影，按面板的「開始」。
4. 這段會從示範批次開始，解析 DEMO-D03–D08 儀表板、抽查放行，並在收件匣採用「轉學抵免的課」的覆核建議。跑完 DEMO-D03 的資料就準備好了，學生端可以直接接著錄。

這段的操作細節由 A 組維護，見 [系辦端 Demo 腳本](DEMO_OFFICE.md)。

## 五、錄學生端

1. 開 `http://localhost:8000/student.html?id=DEMO-D03&tour`，按 Ctrl+F5 確認是最新版。
2. 頁面停在「學生端自動導覽」開始畫面。先開始螢幕錄影，再按**空白鍵**或點一下開始。
3. 導覽中：
   - **空白鍵**：暫停／繼續。暫停時右上角顯示「已暫停」，錄影裡會看到，剪掉即可。
   - **Esc**：中止。
4. 跑完或中止後出現結束畫面：
   - **重新錄影**：先停止目前的螢幕錄影，再按這個按鈕或 R 鍵。畫面回到起點，再次停在開始畫面。
   - **關閉**：右下角留一顆「↻ 重新錄影」按鈕，之後隨時可以按。
5. 本學期修課的 4 門課，導覽開始前會自動設好，不用手動輸入。

畫面順序：我的進度（點三塊摘要、畢業條件與規章依據）→ 本學期修課（100 → 112、117-2；取消勾選管理資訊系統後延到 118-1）→ 一句話 What-if（AI 轉成「117-2 出國交換 1 學期」，確認後模擬）→ 結果（117-2 → 118-2，延後 2 學期）。字幕一個斷句一個框，評審模式會多一句「這是預先錄下的模型回應」。

### 沒有先錄教務處端時

學生端需要 DEMO-D03 的資料，而且 XFER-01 要已經覆核為本系選修。沒有先跑 `autopilot.html` 的話，照 [學生端 Demo 腳本〈錄影前準備〉](DEMO_STUDENT.md#錄影前準備) 第 2 步「單獨錄學生端」準備：在教務處端「工具」上傳 `data/students/demo_batch_roster.csv` 與 `data/samples/demo_batch/DEMO-D03_answer.csv`，到「學生」分頁把 114-1 的 `XFER-01` 覆核為本系選修。

## 六、錄完檢查

學生端畫面要看到以下數字（由 `tests/test_demo_student.py` 鎖定）：

| 畫面 | 應該是 |
|---|---|
| 頁首 | 未通過（Fail）、100 / 128 學分、下一學期 117-1、「試算」標示 |
| 摘要 | 4 條未達成、可能誤判 1 項（超修學分）、要辦理 1 件 |
| 本學期修課 | 100 → 112，預估 117-2 畢業；取消勾選後 109，延到 118-1 |
| What-if 結果 | 117-2 → 118-2，延畢 2 學期，受影響 IM3031 電子商務 |

## 七、常見問題

| 狀況 | 原因與處理 |
|---|---|
| 開始畫面出現紅字「目前學生是 …」 | 網址少了 `id=DEMO-D03`，或伺服器沒有 DEMO-D03 的資料。依第四節或〈沒有先錄教務處端時〉準備 |
| 學分是 97、狀態是待人工判斷 | XFER-01 還沒覆核。先跑教務處端，或手動覆核 |
| AI 轉換失敗，字幕說「AI 服務無法使用時，學生一樣可以手動選情境」 | 沒有對上錄製檔：問題、資料或本學期修課任一處不同就會轉手動。這是設計好的備案，結果一樣是 117-2 → 118-2，可以照用；要拍 AI 轉換那段，先確認是用最新 `main`、DEMO-D03、評審模式 |
| 畫面還是舊版（沒有五塊摘要） | Ctrl+F5 強制重新整理 |
| 字幕或游標停住 | 可能按到空白鍵暫停了，再按一次空白鍵 |
| 跑測試出現大量失敗 | 同一個終端機設了 `ACADEMIC_TWIN_REPLAY=only` 或 `ACADEMIC_TWIN_RECORD`。測試要在沒有這兩個變數的新終端機跑（PowerShell 可用 `Remove-Item Env:ACADEMIC_TWIN_REPLAY`） |
| 想調字幕內容或速度 | 字幕在 `web/assets/student-tour.js` 的 `run()`；速度是同檔的 `CPS` 與 `say()`。旁白要與 `DEMO_STUDENT.md` 的分鏡一致，改完跑測試 |

## 八、重錄 AI 重播檔（只有提示、模型或示範資料改動時才需要）

學生端 What-if 的重播檔是 `data/replay/c84928ee44c3f3d44a16e99bfc4c2580.json`。`academic_twin/ask.py` 的提示、DEMO-D03 資料或腳本問題改了，檔名（提示雜湊）就會變，評審模式會對不上而轉手動，這時要重錄：

1. 設好 `GEMINI_API_KEY`（環境變數，或 repo 根目錄未進版控的 `.env`）。
2. 用錄製模式啟動：PowerShell `$env:ACADEMIC_TWIN_REPLAY=''; $env:ACADEMIC_TWIN_RECORD='1'; python web.py`。
3. 準備好 DEMO-D03（第四節），跑一次學生端導覽。AI 那一步會呼叫模型並存檔。
4. 確認 `data/replay/` 新檔的 `data` 是 `away_start: "117-2"`、`away_count: 1`，而且 `fallback_from` 為空（備援模型的回應不會存）。開 `b/` 分支 commit 這個檔案並開 PR。

## 給 AI agent 的注意事項

- 被問到「怎麼錄 Demo」時，依本文第二至五節帶使用者操作；不需要金鑰，也不要叫使用者輸入真實資料。
- 字幕與旁白不得說「保證畢業」「系統發現誤判」「AI 算出延畢」，也不能宣稱準確率，見 [學生端 Demo 腳本〈旁白不能說的話〉](DEMO_STUDENT.md#旁白不能說的話)。
- 數字不對時先跑 `python -m unittest tests.test_demo_student`。測試失敗表示程式或資料改了，要更新腳本；測試通過則是畫面資料沒準備好，例如 XFER-01 還沒覆核。
- `student-tour.js` 屬 B 組，`autopilot.html` 屬 A 組；要改對方的檔案，依 [兩組分工計畫](TEAM_PLAN.md) 先告知。
