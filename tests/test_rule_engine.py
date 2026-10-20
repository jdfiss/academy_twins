import unittest
import json
import tempfile
from dataclasses import replace
from pathlib import Path

from academic_twin import knowledge_base
from academic_twin.knowledge_base import CreditPolicy, Requirement, Source
from academic_twin.rule_engine import Decision, Record, Status, audit

KB = knowledge_base.load(Path(__file__).parent / "fixtures" / "curriculum_sample.json")
ALL_REQUIRED = ["IM1001", "IM1002", "IM1003", "IM1004", "IM1005", "IM2001",
                "IM2002", "IM2003", "IM2004", "IM3001", "IM3002", "GE0001", "GE0002", "GE0003"]


def rec(code, grade=80, credits=None, term="113-1"):
    credits = credits if credits is not None else (KB.courses[code].credits if code in KB.courses else 3)
    return Record(code, code, credits, grade, term)


def result(report, rid):
    return next(r for r in report.results if r.requirement.id == rid)


class RuleEngineTest(unittest.TestCase):
    def graduating(self, electives, free=0):
        records = [rec(c) for c in ALL_REQUIRED + electives]
        if free:
            records.append(rec("FREE", credits=free))
        return records

    def test_complete_record_passes(self):
        electives = ["IM3101", "IM3102", "IM3201", "IM3301", "IM3302", "IM3303"]
        report = audit(KB, self.graduating(electives, free=128 - 57))
        self.assertEqual(report.status, Status.PASS)

    def test_core_elective_not_double_counted(self):
        # 只修 4 門選修：2 門被 R3 吃掉，R4 只剩 6 學分
        report = audit(KB, self.graduating(["IM3101", "IM3102", "IM3201", "IM3301"], free=200))
        self.assertTrue(result(report, "R3").satisfied)
        r4 = result(report, "R4")
        self.assertFalse(r4.satisfied)
        self.assertEqual(r4.have, 6)
        self.assertEqual(report.status, Status.FAIL)

    def test_failed_then_retaken_counts_once(self):
        report = audit(KB, [rec("IM1002", grade=50, term="111-1"), rec("IM1002", grade=70, term="112-1")])
        self.assertIn("IM1002", result(report, "R1").used)
        self.assertEqual(report.transcript.failed, [])
        self.assertEqual(report.total_credits, 3)

    def test_failed_course_not_counted(self):
        report = audit(KB, [rec("IM1002", grade=59)])
        self.assertIn("IM1002", result(report, "R1").missing)
        self.assertEqual(len(report.transcript.failed), 1)

    def test_unknown_dept_code_counted_but_unverified(self):
        report = audit(KB, [rec("IM0999")])
        self.assertEqual([r.code for r in report.transcript.dept_unverified], ["IM0999"])
        self.assertEqual(result(report, "R5").have, 3)

    def test_unverified_dept_course_turns_pass_into_warning(self):
        electives = ["IM3101", "IM3102", "IM3201", "IM3301", "IM3302", "IM3303"]
        records = self.graduating(electives, free=128 - 57 - 3) + [rec("IM0999")]
        self.assertEqual(audit(KB, records).status, Status.WARNING)

    def test_unknown_category_needs_manual_review(self):
        report = audit(KB, [Record("X1", "抵免", 3, 80, "113-1", "抵免")])
        self.assertEqual(report.status, Status.MANUAL_REVIEW)

    def test_other_dept_course_counts_as_free_credits(self):
        report = audit(KB, [rec("EC1001", credits=3)])
        self.assertEqual(report.status, Status.FAIL)
        self.assertEqual(result(report, "R5").have, 3)

    def test_every_result_has_citation(self):
        report = audit(KB, [])
        for r in report.results:
            self.assertTrue(r.citation.strip())

    def test_invalid_kb_reference_rejected(self):
        import json, tempfile
        raw = json.loads((Path(__file__).parent / "fixtures" / "curriculum_sample.json").read_text(encoding="utf-8"))
        raw["requirements"][0]["courses"].append("NOPE")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(raw, f)
        with self.assertRaises(ValueError):
            knowledge_base.load(f.name)
        Path(f.name).unlink()


class MisjudgmentFlagsTest(unittest.TestCase):
    """全部使用虛構規則；這些案例不代表已確認的校務系統誤判。"""

    def setUp(self):
        self.source = Source("IM-REG", "虛構測試條文")
        self.kb = replace(KB, requirements=[
            Requirement("M", "all_of", "虛構必修", self.source, courses=("IM3001",)),
            Requirement("G", "category_credits", "虛構通識", self.source,
                        categories=("通識",), min_credits=4),
            Requirement("T", "total_credits", "虛構總學分", self.source, min_credits=10),
        ], categories={"通識": "", "核心": "通識", "體育": ""},
            credit_policy=CreditPolicy(outside_cap=5, category_caps={"通識": 2}, excluded=("體育",)))

    def repeatable(self, *codes):
        return replace(self.kb, repeatable_courses=tuple(codes))

    def stopped(self):
        courses = dict(self.kb.courses)
        courses["IM3001"] = replace(courses["IM3001"], discontinued=True,
                                   replacement_courses=("IM3303",), substitution_office="虛構系辦")
        return replace(self.kb, courses=courses)

    def test_overflow_uses_category_cap_without_changing_credits(self):
        records = [Record("G1", "虛構通識", 4, 80, "113-1", "通識"),
                   Record("G2", "虛構核心", 5, 80, "113-2", "核心")]
        report = audit(self.kb, records)
        [flag] = report.flags
        self.assertEqual((flag.type, flag.credits), ("overflow", 2))
        self.assertEqual(set(flag.courses), {"G1", "G2"})
        self.assertIn("G", flag.requirement_ids)
        self.assertEqual(report.total_credits, 6)
        self.assertEqual(report.credit_breakdown, {"院系必修": 0, "通識": 4, "外系認列": 2})
        self.assertEqual(report.status, Status.FAIL)
        self.assertEqual(report.actions[0].from_flag, "overflow")

    def test_overflow_is_pool_amount_even_when_outside_cap_is_full(self):
        report = audit(self.kb, [Record("G1", "虛構通識", 7, 80, "113-1", "通識"),
                                 rec("EC1", credits=5)])
        self.assertEqual(report.flags[0].credits, 2)
        self.assertEqual(report.credit_breakdown["外系認列"], 5)
        self.assertIn("上限", report.flags[0].message)

    def test_no_overflow_for_exact_requirement_excluded_or_failed(self):
        report = audit(self.kb, [Record("G1", "虛構通識", 4, 80, "113-1", "通識"),
                                 Record("G2", "虛構通識", 5, 20, "113-2", "通識"),
                                 Record("PE", "虛構體育", 3, 80, "113-1", "體育")])
        self.assertEqual(report.flags, [])

    def test_zero_category_transfer_cap_does_not_create_action(self):
        kb = replace(self.kb, credit_policy=CreditPolicy(category_caps={"通識": 0}))
        report = audit(kb, [Record("G1", "虛構通識", 8, 80, "113-1", "通識")])
        self.assertEqual(report.flags, [])
        self.assertEqual(report.actions, [])

    def test_repeatable_course_counts_each_pass_and_flags_extra_credits(self):
        kb = self.repeatable("IM3303")
        report = audit(kb, [rec("IM3303", term="112-1"), rec("IM3303", term="113-1")])
        self.assertEqual(report.total_credits, 6)
        [flag] = report.flags
        self.assertEqual((flag.type, flag.courses, flag.credits), ("repeatable", ("IM3303",), 3))
        self.assertEqual(report.transcript.review, [])
        self.assertEqual(report.actions[0].type, "manual_recognition")

    def test_repeatable_category_counts_each_term_including_zero_credits(self):
        kb = self.repeatable("PE")
        kb = replace(kb, requirements=[Requirement("PE", "category_count", "虛構體育", self.source,
                                                  categories=("體育",), n=2)])
        records = [Record("PE", "虛構體育", 0, 80, term, "體育") for term in ("112-1", "112-2")]
        report = audit(kb, records)
        self.assertTrue(report.results[0].satisfied)
        self.assertEqual(report.status, Status.PASS)
        self.assertEqual(report.flags[0].credits, 0)

    def test_repeatable_failure_and_single_pass_do_not_create_flag(self):
        records = [rec("IM3303", grade=40, term="112-1"), rec("IM3303", term="113-1")]
        report = audit(self.repeatable("IM3303"), records)
        self.assertEqual(report.total_credits, 3)
        self.assertEqual(report.flags, [])
        self.assertEqual(report.transcript.failed, [])

    def test_unmarked_repeat_is_visible_for_review_instead_of_discarded(self):
        records = [rec("IM3303", term="112-1"), rec("IM3303", term="113-1")]
        report = audit(self.kb, records)
        self.assertEqual(report.total_credits, 3)
        self.assertEqual(report.status, Status.MANUAL_REVIEW)
        self.assertEqual([r.term for r in report.transcript.review], ["113-1"])
        self.assertEqual(report.flags, [])
        self.assertEqual(report.actions[0].type, "manual_recognition")
        self.assertIsNone(report.actions[0].from_flag)
        self.assertEqual(audit(self.kb, list(reversed(records))), report)

    def test_reviewed_duplicate_can_be_excluded_without_losing_first_pass(self):
        records = [rec("IM3303", term="112-1"), rec("IM3303", term="113-1")]
        report = audit(self.kb, records, {("IM3303", "113-1"): Decision("exclude")})
        self.assertEqual(report.total_credits, 3)
        self.assertEqual(report.transcript.review, [])
        self.assertEqual(len(report.transcript.reviewed), 1)
        self.assertEqual(report.actions, [])

    def test_same_course_and_term_cannot_inflate_repeatable_credits(self):
        with self.assertRaisesRegex(ValueError, "重複"):
            audit(self.repeatable("IM3303"), [rec("IM3303"), rec("IM3303")])
        with self.assertRaisesRegex(ValueError, "重複"):
            audit(self.repeatable("IM3303"), [rec("IM3303", term="99-1"), rec("IM3303", term="099-1")])

    def test_repeatable_course_with_only_one_counted_attempt_does_not_flag(self):
        kb = self.repeatable("X1")
        records = [Record("X1", "虛構待確認課程", 3, 80, "113-1", "未知類別"),
                   Record("X1", "虛構外系課程", 3, 80, "113-2")]
        report = audit(kb, records)
        self.assertEqual(report.flags, [])
        self.assertEqual(report.total_credits, 3)

    def test_repeatable_passes_can_supply_separate_exclusive_requirements(self):
        kb = replace(self.repeatable("IM3303"), requirements=[
            Requirement("N", "n_of", "虛構選一門", self.source, courses=("IM3303", "IM3302"), n=1),
            Requirement("C", "credits_from", "虛構另三學分", self.source,
                        courses=("IM3303", "IM3302"), min_credits=3),
        ])
        records = [rec("IM3303", term="112-1"), rec("IM3303", term="113-1")]
        self.assertTrue(all(r.satisfied for r in audit(kb, records).results))
        kb = replace(kb, requirements=[replace(kb.requirements[0], n=2)])
        self.assertFalse(audit(kb, records).results[0].satisfied)

    def test_substitution_pending_keeps_original_requirement_missing(self):
        report = audit(self.stopped(), [rec("IM3303")])
        [flag] = report.flags
        self.assertEqual(flag.type, "substitution_pending")
        self.assertEqual(flag.requirement_ids, ("M",))
        self.assertEqual(set(flag.courses), {"IM3001", "IM3303"})
        self.assertIn("IM3001", report.results[0].missing)
        self.assertFalse(report.results[0].satisfied)
        self.assertEqual(report.total_credits, 3)
        self.assertEqual(report.actions[0].office, "虛構系辦")
        self.assertEqual(report.actions[0].type, "substitution")

    def test_no_substitution_action_without_passed_replacement_or_missing_course(self):
        for records in ([], [rec("IM3303", grade=40)], [rec("IM3001"), rec("IM3303")]):
            with self.subTest(records=records):
                self.assertEqual(audit(self.stopped(), records).flags, [])
        self.assertEqual(audit(self.kb, [rec("IM3303")]).flags, [])

    def test_optional_course_not_required_does_not_create_substitution(self):
        kb = replace(self.stopped(), requirements=[
            Requirement("N", "n_of", "虛構選一門", self.source, courses=("IM3001", "IM3303"), n=1),
        ])
        self.assertEqual(audit(kb, [rec("IM3303")]).flags, [])

    def test_rule_loader_accepts_new_metadata_and_rejects_invalid_lists(self):
        raw = json.loads((Path(__file__).parent / "fixtures" / "curriculum_sample.json").read_text(encoding="utf-8"))
        raw["repeatable_courses"] = ["IM3303", "PE"]
        raw["courses"][0].update(discontinued=True, replacement_courses=["IM3303"])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rules.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            kb = knowledge_base.load(path)
            self.assertEqual(kb.repeatable_courses, ("IM3303", "PE"))
            self.assertEqual(kb.courses["IM1001"].replacement_courses, ("IM3303",))
            for invalid in ("IM3303", [""], ["IM3303", "IM3303"], [False]):
                with self.subTest(invalid=invalid):
                    raw["repeatable_courses"] = invalid
                    path.write_text(json.dumps(raw), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        knowledge_base.load(path)

    def test_rule_loader_rejects_invalid_substitution_metadata(self):
        original = json.loads((Path(__file__).parent / "fixtures" / "curriculum_sample.json").read_text(encoding="utf-8"))
        for fields in (dict(discontinued="false"),
                       dict(discontinued=True, replacement_courses="IM3303"),
                       dict(discontinued=True, replacement_courses=["NOPE"]),
                       dict(discontinued=True, replacement_courses=["IM1001"]),
                       dict(replacement_courses=["IM3303"]),
                       dict(discontinued=True, replacement_courses=["IM3303"], substitution_office="")):
            with self.subTest(fields=fields), tempfile.TemporaryDirectory() as folder:
                raw = json.loads(json.dumps(original))
                raw["courses"][0].update(fields)
                path = Path(folder) / "rules.json"
                path.write_text(json.dumps(raw), encoding="utf-8")
                with self.assertRaises(ValueError):
                    knowledge_base.load(path)


if __name__ == "__main__":
    unittest.main()
