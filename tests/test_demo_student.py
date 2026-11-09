"""學生端 Demo 腳本（docs/DEMO_STUDENT.md）的預期數字（B 組）。

影片旁白引用的學分、畢業學期與延後學期數都在這裡檢查；規則、規劃器或示範資料改動使數字變了，
這裡會失敗，提醒先更新腳本再錄影。只用 A 組的虛構示範批次，不是實證數據。
"""
import unittest

from tests.test_demo_batch import confirmed_service

SID = "DEMO-D03"
# 腳本「本學期修課」輸入的 4 門課（117-1）
CURRENT = [
    {"code": "IM4001"},
    {"code": "IM4048"},
    {"code": "IM5099", "name": "雲端運算", "credits": 3, "category": ""},
    {"code": "GE-3", "name": "通識：社會與文化", "credits": 3, "category": "通識"},
]


def without_im4048():
    return [dict(c, counted=False) if c["code"] == "IM4048" else c for c in CURRENT]


class DemoStudentScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = confirmed_service()
        # 與系辦端段落相同的覆核：他校抵免認列為本系選修
        cls.service.add_review(SID, "XFER-01", "114-1", "dept_elective", "示範承辦人")

    def test_progress(self):
        s = self.service.twin(SID)
        self.assertEqual((s["status"], s["total_credits"], s["next_term"]), ("FAIL", 100, "117-1"))
        unmet = {r["id"]: r for r in s["requirements"] if not r["satisfied"]}
        self.assertEqual(set(unmet), {"R1", "R6", "R11", "R10"})
        self.assertEqual({m["code"] for m in unmet["R1"]["missing"]}, {"IM3031", "IM4001", "IM4048"})
        self.assertEqual([(unmet[r]["have"], unmet[r]["need"]) for r in ("R6", "R11", "R10")],
                         [(9, 14), (9, 16), (100, 128)])
        self.assertEqual([(f["type"], f["credits"]) for f in s["flags"]], [("overflow", 2)])
        self.assertEqual([(a["type"], a["office"]) for a in s["actions"]], [("manual_recognition", "待確認")])

    def test_current_term(self):
        sim = self.service.simulate(SID, current=CURRENT)
        cur = sim["current"]
        self.assertEqual((cur["term"], sim["start"]), ("117-1", "117-2"))
        self.assertEqual((cur["official_total_credits"], cur["trial_total_credits"]), (100, 112))
        self.assertEqual(sim["base"]["graduation"], "117-2")

    def test_current_term_without_im4048(self):
        sim = self.service.simulate(SID, current=without_im4048())
        self.assertEqual(sim["current"]["trial_total_credits"], 109)
        self.assertEqual(sim["base"]["graduation"], "118-1")
        placed = {c["code"]: t["term"] for t in sim["base"]["terms"] for c in t["courses"]}
        self.assertEqual(placed["IM4048"], "118-1")

    def test_exchange_whatif(self):
        sc = self.service.simulate(SID, away=["117-2"], away_credits=15, current=CURRENT)["scenario"]
        self.assertEqual((sc["graduation"], sc["delay_terms"]), ("118-2", 2))
        self.assertIn("IM3031 電子商務：117-2 → 118-2", sc["impact"])

    def test_exchange_after_graduation_is_not_called_no_delay(self):
        # 組員回報：「明年整年交換」落在預估畢業（117-2）之後，不能顯示「不延畢」
        sc = self.service.simulate(SID, away=["118-1", "118-2"], current=CURRENT)["scenario"]
        self.assertTrue(sc["away_after_graduation"])
        self.assertIsNone(sc["delay_terms"])
        self.assertEqual(len(sc["impact"]), 1)
        self.assertIn("117-2 之後", sc["impact"][0])
        # 腳本的大四下交換在畢業前，照常算延畢
        demo = self.service.simulate(SID, away=["117-2"], current=CURRENT)["scenario"]
        self.assertFalse(demo["away_after_graduation"])

    def test_official_result_unchanged(self):
        self.service.simulate(SID, away=["117-2"], current=CURRENT)
        self.assertEqual(self.service.twin(SID)["total_credits"], 100)


if __name__ == "__main__":
    unittest.main()
