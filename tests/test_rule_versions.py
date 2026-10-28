"""規則版本啟用：已確認草稿經明確啟用才用於審查，正式規則庫檔案不變，可還原。

草稿內容（最低畢業學分改為 130）為測試虛構，不代表正式規則。
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from academic_twin import rule_draft, rule_versions
from academic_twin.api import KB_PATH, AcademicTwinService

DRAFT_ID = "a" * 32


class RuleVersionsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.kb_dir, self.versions, self.drafts = root / "kb", root / "versions", root / "drafts"
        self.kb_dir.mkdir()
        self.base = self.kb_dir / KB_PATH.name
        shutil.copy(KB_PATH, self.base)
        for name, value in (("KB_PATH", self.base), ("VERSIONS_DIR", self.versions)):
            patcher = mock.patch.object(rule_versions, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(rule_draft, "DRAFT_DIR", self.drafts)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_draft()

    def write_draft(self, state="confirmed", min_credits=130, baseline=None, **changes):
        base = json.loads(self.base.read_text(encoding="utf-8"))
        rule = {**next(r for r in base["requirements"] if r["kind"] == "total_credits"),
                "min_credits": min_credits, **changes}
        row = {**rule, "applicable_year": 114, "source_page": 1, "source_quote": "虛構引文",
               "applicability": "虛構", "exceptions": "未載明", "review_notes": "虛構"}
        draft = {"draft_id": DRAFT_ID, "state": state, "year": 114, "requirements": [row],
                 "confirmation": {"reviewer": "虛構確認人", "confirmed_at": "2026-10-22T00:00:00+00:00",
                                  "baseline_sha256": baseline or rule_draft._hash(base)}}
        self.drafts.mkdir(exist_ok=True)
        (self.drafts / f"{DRAFT_ID}.confirmed.json").write_text(json.dumps(draft, ensure_ascii=False),
                                                                 encoding="utf-8")
        return rule["id"]

    def total_need(self, path):
        kb = json.loads(Path(path).read_text(encoding="utf-8"))
        return next(r["min_credits"] for r in kb["requirements"] if r["kind"] == "total_credits")

    def test_activation_needs_activator_and_acknowledgement(self):
        with self.assertRaises(ValueError):
            rule_versions.activate(DRAFT_ID, " ", True)
        with self.assertRaises(ValueError):
            rule_versions.activate(DRAFT_ID, "虛構承辦人", False)
        with self.assertRaises(KeyError):
            rule_versions.activate("b" * 32, "虛構承辦人", True)
        self.assertEqual(rule_versions.history(), [])

    def test_activate_replaces_only_draft_requirements_and_keeps_base_file(self):
        before = self.base.read_bytes()
        rid = self.write_draft()
        entry = rule_versions.activate(DRAFT_ID, "虛構承辦人", True)
        self.assertEqual((entry["replaced"], entry["added"]), ([rid], []))
        active = rule_versions.active_paths()[114]
        self.assertNotEqual(active, self.base)
        self.assertEqual(self.total_need(active), 130)
        self.assertEqual(self.base.read_bytes(), before)
        base, new = (json.loads(p.read_text(encoding="utf-8")) for p in (self.base, active))
        self.assertEqual([r for r in new["requirements"] if r["id"] != rid],
                         [r for r in base["requirements"] if r["id"] != rid])
        self.assertNotIn("source_quote", json.dumps(new["requirements"], ensure_ascii=False))

    def test_unconfirmed_changed_base_or_invalid_rules_are_rejected(self):
        self.write_draft(state="draft")
        with self.assertRaises(ValueError):
            rule_versions.activate(DRAFT_ID, "虛構承辦人", True)
        self.write_draft(baseline="0" * 64)
        with self.assertRaises(ValueError):
            rule_versions.activate(DRAFT_ID, "虛構承辦人", True)
        self.write_draft(kind="all_of", courses=["NOPE999"])
        with self.assertRaises((ValueError, KeyError)):
            rule_versions.activate(DRAFT_ID, "虛構承辦人", True)
        self.assertEqual(rule_versions.history(), [])
        self.assertEqual(rule_versions.active_paths()[114], self.base)

    def test_revert_returns_to_base_and_keeps_history(self):
        with self.assertRaises(ValueError):
            rule_versions.revert(114, "虛構承辦人")
        rule_versions.activate(DRAFT_ID, "虛構承辦人", True)
        rule_versions.revert(114, "虛構承辦人")
        self.assertEqual(rule_versions.active_paths()[114], self.base)
        self.assertEqual([e["type"] for e in rule_versions.history()], ["activate", "revert"])
        default, extra = rule_versions.startup_paths()
        self.assertEqual((default, extra), (self.base, []))

    def test_service_audits_with_activated_version_and_flags_old_reviews(self):
        service = AcademicTwinService(self.base)
        old_version = service.rule_version
        status = service.rule_versions_status()
        self.assertEqual(status["confirmed_drafts"][0]["draft_id"], DRAFT_ID)
        self.assertIsNone(status["years"][0]["activated"])

        status = service.rule_activate(DRAFT_ID, "虛構承辦人", True)
        self.assertEqual(status["years"][0]["activated"]["draft_id"], DRAFT_ID)
        self.assertNotEqual(service.rule_version, old_version)
        s = service.student("S001")
        self.assertEqual(next(r["need"] for r in s["requirements"] if r["kind"] == "total_credits"), 130)
        self.assertEqual(s["rule_version"], service.rule_version)

        service.rule_revert(114, "虛構承辦人")
        self.assertEqual(service.rule_version, old_version)
        self.assertEqual(service.rule_versions_status()["history"][0]["type"], "revert")

    def test_web_starts_with_activated_version(self):
        rule_versions.activate(DRAFT_ID, "虛構承辦人", True)
        default, _ = rule_versions.startup_paths()
        self.assertEqual(default.parent, self.versions)


if __name__ == "__main__":
    unittest.main()
