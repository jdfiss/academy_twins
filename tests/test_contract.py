"""交接格式與兩端一致性（docs/TEAM_PLAN.md）：學生端與系辦端讀同一份審查結果。"""
import json
import unittest
from pathlib import Path

from academic_twin.api import AcademicTwinService
from academic_twin.rule_engine import FLAG_KINDS

DEMO = Path(__file__).parent.parent / "web" / "assets" / "demo-twin.json"
FLAG_KEYS = {"type", "requirement_ids", "courses", "credits", "message"}
ACTION_KEYS = {"type", "title", "office", "requirement_ids", "courses", "from_flag"}


class ContractTest(unittest.TestCase):
    def setUp(self):
        self.service = AcademicTwinService()

    def test_student_carries_contract_fields(self):
        s = self.service.student("S002")
        self.assertEqual(s["cohort"], "114")
        self.assertEqual(s["rule_version"], self.service.rule_version)
        self.assertIs(s["trial"], False)
        self.assertIsInstance(s["flags"], list)
        self.assertIsInstance(s["actions"], list)
        for r in s["requirements"]:
            self.assertIn("system_code", r)
            self.assertIn("regulation_term", r)

    def test_twin_is_trial_without_review_history(self):
        s = self.service.twin("S002")
        self.assertIs(s["trial"], True)
        self.assertNotIn("review_history", s)

    def test_twin_ids_cover_every_student_with_records(self):
        self.assertEqual(self.service.twin_ids(), sorted(self.service.students))

    def test_demo_file_matches_real_output(self):
        """學生端以 demo 檔開發畫面；欄位要跟真實輸出一致，交接格式改了就要一起更新 demo。"""
        demo = json.loads(DEMO.read_text(encoding="utf-8"))
        real = self.service.twin(demo["id"])
        self.assertEqual(set(demo) - {"_demo"}, set(real))
        self.assertEqual(set(demo["requirements"][0]), set(real["requirements"][0]))
        self.assertTrue(demo["flags"] and demo["actions"])
        for f in demo["flags"]:
            self.assertEqual(set(f), FLAG_KEYS)
            self.assertIn(f["type"], FLAG_KINDS)
        for a in demo["actions"]:
            self.assertEqual(set(a), ACTION_KEYS)


class ConsistencyTest(unittest.TestCase):
    """同一份資料在學生端與系辦端得到相同判定。"""

    def test_both_sides_agree_for_every_audited_student(self):
        service = AcademicTwinService()
        for row in service.batch():
            office, twin = service.student(row["id"]), service.twin(row["id"])
            with self.subTest(student=row["id"]):
                self.assertEqual(row["status"], twin["status"])
                self.assertEqual(row["total_credits"], twin["total_credits"])
                for key in ("status", "requirements", "credit_breakdown", "flags", "actions", "rule_version"):
                    self.assertEqual(office[key], twin[key], key)

    def test_both_sides_agree_on_detected_flags(self):
        """示例學生在正式 114 規則下沒有 flags；改用 A 組虛構示範資料，確認提示真的出現時兩端仍一致。"""
        root = Path(__file__).parent.parent
        service = AcademicTwinService(root / "data" / "curriculum_demo_flags.json",
                                      root / "data" / "students" / "demo_flags_records.csv",
                                      root / "data" / "students" / "demo_flags_roster.csv")
        flagged = service.twin("DEMO-FLAGS")
        self.assertEqual({f["type"] for f in flagged["flags"]}, set(FLAG_KINDS))
        for sid in service.twin_ids():
            office, twin = service.student(sid), service.twin(sid)
            with self.subTest(student=sid):
                self.assertEqual(office["flags"], twin["flags"])
                self.assertEqual(office["actions"], twin["actions"])
                self.assertEqual(office["status"], twin["status"])

    def test_flags_and_actions_point_to_existing_requirements(self):
        """學生頁依 requirement_ids 找條件名稱、依 from_flag 找提示名稱，引用要能對上。"""
        root = Path(__file__).parent.parent
        service = AcademicTwinService(root / "data" / "curriculum_demo_flags.json",
                                      root / "data" / "students" / "demo_flags_records.csv",
                                      root / "data" / "students" / "demo_flags_roster.csv")
        cases = [service.twin(sid) for sid in service.twin_ids()] + [json.loads(DEMO.read_text(encoding="utf-8"))]
        for s in cases:
            ids = {r["id"] for r in s["requirements"]}
            with self.subTest(student=s["id"]):
                for x in s["flags"] + s["actions"]:
                    self.assertLessEqual(set(x["requirement_ids"]), ids)
                for a in s["actions"]:
                    self.assertIn(a["from_flag"], (None, *FLAG_KINDS))

    def test_both_sides_agree_after_review(self):
        service = AcademicTwinService()
        service.add_review("S003", "XFER-01", "114-1", "outside", "王助教")
        self.assertEqual(service.student("S003")["status"], service.twin("S003")["status"])
        self.assertEqual(service.student("S003")["requirements"], service.twin("S003")["requirements"])


if __name__ == "__main__":
    unittest.main()
