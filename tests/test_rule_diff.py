"""規則啟用前影響預覽（差異測試）：虛構規則變更、虛構學生，判定一律由規則引擎產生。（A 組）

測試中的規則變更（最低畢業學分 128→130、院訂必修增減一門、國文 5→6 學分、新增一條要求）
皆為虛構，不代表正式規則。
"""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from academic_twin import knowledge_base, rule_diff, rule_draft, rule_engine, rule_versions
from academic_twin.api import KB_PATH, AcademicTwinService

RAW = json.loads(KB_PATH.read_text(encoding="utf-8"))
DRAFT_ID = "c" * 32


def variant(**changes) -> knowledge_base.KnowledgeBase:
    """以 114 規則庫為底的虛構版本；changes 為 {要求編號: 要改的欄位}。"""
    raw = copy.deepcopy(RAW)
    for req in raw["requirements"]:
        req.update(changes.pop(req["id"], {}))
    raw["requirements"] += [{"id": rid, **fields} for rid, fields in changes.items()]
    return knowledge_base.from_dict(raw)


def requirement(rid: str) -> dict:
    return next(r for r in RAW["requirements"] if r["id"] == rid)


def group(result: dict, ids: list[str]) -> dict:
    return next(d for d in result["differences"] if d["requirement_ids"] == ids)


class RuleDiffTest(unittest.TestCase):
    old = knowledge_base.from_dict(copy.deepcopy(RAW))

    def test_total_credit_threshold_change_finds_boundary_student(self):
        result = rule_diff.search(self.old, variant(R10={"min_credits": 130}))
        d = group(result, ["R10"])
        change = d["changes"][0]
        self.assertTrue(change["old"]["satisfied"])
        self.assertFalse(change["new"]["satisfied"])
        self.assertIn(change["old"]["have"], (128, 129))
        self.assertEqual((change["old"]["need"], change["new"]["need"]), (128, 130))
        self.assertEqual((d["old_status"], d["new_status"]), ("PASS", "FAIL"))
        self.assertEqual(len(d["case"]["edits"]), 1)  # 最精簡：只差總學分
        self.assertIn("舊規則符合", change["reason"])
        self.assertEqual([r["id"] for r in result["changed_requirements"]], ["R10"])
        self.assertEqual(result["changed_without_observed_difference"], [])
        self.assertIn("找到", result["message"])

    def test_required_list_removal_and_addition(self):
        courses = requirement("R2")["courses"]
        result = rule_diff.search(self.old, variant(R2={"courses": courses[:-1]}))
        d = group(result, ["R2"])
        self.assertEqual((d["changes"][0]["old"]["satisfied"], d["changes"][0]["new"]["satisfied"]), (False, True))
        self.assertEqual(d["changes"][0]["old"]["missing"], [courses[-1]])
        self.assertEqual(d["case"]["edits"], [f"未修 {courses[-1]} 經濟學(下)（3 學分）"])
        key = {c["code"]: c["grade"] for c in d["case"]["key_courses"]}
        self.assertIsNone(key[courses[-1]])  # 關鍵課程中該門未修

        extra = "IM1013"  # 規則庫內課程；虛構地把它加入院訂必修
        result = rule_diff.search(self.old, variant(R2={"courses": courses + [extra]}))
        d = group(result, ["R2"])
        self.assertEqual((d["changes"][0]["old"]["satisfied"], d["changes"][0]["new"]["satisfied"]), (True, False))
        self.assertEqual(d["changes"][0]["new"]["missing"], [extra])

    def test_category_threshold_and_added_requirement(self):
        result = rule_diff.search(self.old, variant(R4={"min_credits": 6}))
        d = group(result, ["R4"])
        self.assertEqual(d["case"]["edits"], ["國文 5 學分"])
        self.assertEqual((d["changes"][0]["old"]["have"], d["changes"][0]["new"]["need"]), (5, 6))

        added = {"kind": "all_of", "title": "虛構新增必修", "courses": ["IM1013"],
                 "source": {"doc": "IM-114-TABLE", "ref": "虛構"}}
        result = rule_diff.search(self.old, variant(R99=added))
        d = group(result, ["R99"])
        self.assertIsNone(d["changes"][0]["old"])
        self.assertIn("沒有這條要求", d["changes"][0]["reason"])
        self.assertFalse(d["changes"][0]["new"]["satisfied"])
        self.assertEqual(result["changed_requirements"][0]["change"], "added")

    def test_identical_rules_report_no_difference_without_guarantee(self):
        result = rule_diff.search(self.old, variant())
        self.assertEqual((result["difference_count"], result["differences"]), (0, []))
        self.assertGreater(result["searched"], 100)
        self.assertIn("在搜尋範圍內未發現差異", result["message"])
        self.assertIn("不代表", result["message"])
        self.assertNotIn("保證", result["message"])

    def test_title_only_change_is_reported_as_changed_without_difference(self):
        result = rule_diff.search(self.old, variant(R10={"title": "虛構改名"}))
        self.assertEqual(result["differences"], [])
        self.assertEqual([r["id"] for r in result["changed_without_observed_difference"]], ["R10"])

    def test_results_are_reproducible(self):
        new = variant(R10={"min_credits": 130}, R4={"min_credits": 6})
        first = rule_diff.search(self.old, new)
        self.assertEqual(json.dumps(first, ensure_ascii=False), json.dumps(rule_diff.search(self.old, new),
                                                                          ensure_ascii=False))
        self.assertEqual(first["seed"], rule_diff.SEED)
        self.assertGreater(first["random_cases"], 0)

    def test_cases_are_fictional_and_judged_only_by_engine(self):
        new = variant(R10={"min_credits": 130})
        seen = []

        def spy(kb, records, *args, **kwargs):
            seen.append(records)
            return rule_engine.audit(kb, records, *args, **kwargs)

        with mock.patch.object(rule_diff, "audit", side_effect=spy):
            result = rule_diff.search(self.old, new)
        self.assertGreaterEqual(len(seen), 2 * result["searched"])
        known = set(self.old.courses)
        for records in seen:
            for r in records:
                self.assertEqual(r.term, rule_diff.TERM)
                self.assertTrue(r.code in known or r.code.startswith(("ZZ", "IM9")), r.code)

        # 兩版規則不同，但若引擎對兩次審查給出相同結果，就不應回報差異：判定完全來自引擎。
        with mock.patch.object(rule_diff, "audit", side_effect=lambda kb, recs: rule_engine.audit(self.old, recs)):
            self.assertEqual(rule_diff.search(self.old, new)["differences"], [])


class DraftPreviewTest(unittest.TestCase):
    """已確認草稿的啟用前預覽：經由 rule_versions_status 提供，不啟用、不留紀錄。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        kb_dir, self.versions, drafts = root / "kb", root / "versions", root / "drafts"
        kb_dir.mkdir()
        drafts.mkdir()
        self.base = kb_dir / KB_PATH.name
        shutil.copy(KB_PATH, self.base)
        for target, name, value in ((rule_versions, "KB_PATH", self.base), (rule_versions, "VERSIONS_DIR", self.versions),
                                    (rule_draft, "DRAFT_DIR", drafts)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        row = {**requirement("R10"), "min_credits": 130, "applicable_year": 114, "source_page": 1,
               "source_quote": "虛構引文", "applicability": "虛構", "exceptions": "未載明", "review_notes": "虛構"}
        draft = {"draft_id": DRAFT_ID, "state": "confirmed", "year": 114, "requirements": [row],
                 "confirmation": {"reviewer": "虛構確認人", "confirmed_at": "2026-10-27T00:00:00+00:00",
                                  "baseline_sha256": rule_draft._hash(RAW)}}
        (drafts / f"{DRAFT_ID}.confirmed.json").write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    def test_status_includes_preview_and_does_not_activate(self):
        service = AcademicTwinService(self.base)
        version = service.rule_version
        draft = service.rule_versions_status()["confirmed_drafts"][0]
        preview = draft["preview"]
        self.assertEqual((preview["replaced"], preview["added"]), (["R10"], []))
        self.assertEqual(group(preview, ["R10"])["changes"][0]["new"]["need"], 130)
        self.assertEqual(preview["label"], rule_diff.LABEL)
        self.assertEqual(rule_versions.history(), [])
        self.assertEqual(rule_versions.active_paths()[114], self.base)
        self.assertEqual(service.rule_version, version)

        # 啟用仍須明確操作；啟用後目前版本已等同草稿，預覽回報未發現差異。
        service.rule_activate(DRAFT_ID, "虛構承辦人", True)
        preview = service.rule_versions_status()["confirmed_drafts"][0]["preview"]
        self.assertEqual(preview["differences"], [])

    def test_preview_errors_do_not_break_status(self):
        with mock.patch.object(rule_diff, "search", side_effect=ValueError("虛構錯誤")):
            draft = AcademicTwinService(self.base).rule_versions_status()["confirmed_drafts"][0]
        self.assertEqual(draft["preview"], {"error": "虛構錯誤"})


if __name__ == "__main__":
    unittest.main()
