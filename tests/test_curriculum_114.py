"""資管系 114 學年度實際規則庫的測試：條文對照應修科目表（data/sources/im_114_table.pdf）。"""
import unittest
from pathlib import Path

from academic_twin import knowledge_base
from academic_twin.graph import CourseGraph
from academic_twin.planner import Term, plan
from academic_twin.rule_engine import Record, Status, audit

ROOT = Path(__file__).parent.parent
KB = knowledge_base.load(ROOT / "data" / "curriculum_im_114.json")

COLLEGE_AND_DEPT = [c for c, course in KB.courses.items() if course.type in ("college", "required", "selective")]


def course(code, grade=80, term="115-1"):
    return Record(code, KB.courses[code].name, KB.courses[code].credits, grade, term)


def cat(code, credits, category, grade=80, term="115-1"):
    return Record(code, code, credits, grade, term, category)


def common_requirements():
    """國文 5、外文 6、通識 14（含 1 門核心）、體育 5 學期、服務學習 2 門。"""
    return [
        cat("CH-1", 3, "國文"), cat("CH-2", 2, "國文"),
        cat("EN-1", 3, "外文"), cat("EN-2", 3, "外文"),
        cat("GC-1", 3, "通識核心"), cat("GE-1", 3, "通識"), cat("GE-2", 3, "通識"),
        cat("GE-3", 3, "通識"), cat("GE-4", 2, "通識"),
        *[Record(f"PE-{i}", "大一體育", 0, 80, f"114-{i + 1}", "體育") for i in range(2)],
        *[cat(f"PE-{i}", 0, "體育") for i in range(2, 5)],
        cat("SL-1", 0, "服務學習"), cat("SL-2", 0, "服務學習"),
    ]


def result(report, rid):
    return next(r for r in report.results if r.requirement.id == rid)


class Curriculum114Test(unittest.TestCase):
    def test_required_credits_match_table(self):
        # 備註二-1：必修科目計 100 學分 = 系訂 54 + 院訂 21 + 國文 5 + 外文 6 + 通識 14
        dept = sum(KB.courses[c].credits for c in KB.requirements[0].courses)
        college = sum(KB.courses[c].credits for c in KB.requirements[1].courses)
        common = sum(r.min_credits for r in KB.requirements if r.kind == "category_credits")
        self.assertEqual((dept, college, common), (54, 21, 25))
        self.assertEqual(dept + college + common, 100)

    def test_graph_is_acyclic_and_project_chain_holds(self):
        graph = CourseGraph(KB)
        self.assertIn("IM3028", graph.ancestors("IM4001"))

    def test_full_record_with_im_electives_passes(self):
        # 說明投影片：IM 課號選修即本系選修，不再標 Warning
        records = [course(c) for c in COLLEGE_AND_DEPT] + common_requirements()
        records += [Record(f"IM9{i:03}", "本系選修", 3, 85, "117-1") for i in range(7)]  # 21 學分
        report = audit(KB, records, english_passed=True)
        self.assertEqual(report.total_credits, 84 + 25 + 21)
        self.assertEqual(report.credit_breakdown["本系選修"], 21 + 6)  # 含 IM1013、IM3029
        self.assertEqual(report.credit_breakdown["外系認列"], 3)  # MA1006
        self.assertEqual(report.status, Status.PASS)

    def test_selective_course_only_needs_to_be_taken(self):
        # 說明投影片：必選課「一定要修過，但不通過沒關係」；不及格沒有學分
        passed = audit(KB, [course("IM1013"), course("IM3029"), course("MA1006")])
        failed = audit(KB, [course("IM1013", grade=40), course("IM3029"), course("MA1006")])
        self.assertTrue(result(failed, "R3").satisfied)
        self.assertEqual(result(passed, "R11").have - result(failed, "R11").have, 3)
        self.assertFalse(result(audit(KB, [course("IM1013"), course("IM3029")]), "R3").satisfied)

    def full_record(self):
        records = [course(c) for c in COLLEGE_AND_DEPT] + common_requirements()
        return records + [Record(f"IM9{i:03}", "本系選修", 3, 85, "117-1") for i in range(7)]

    def test_english_threshold_from_roster_flag(self):
        self.assertEqual(audit(KB, self.full_record(), english_passed=True).status, Status.PASS)
        failed = audit(KB, self.full_record(), english_passed=False)
        self.assertEqual(failed.status, Status.FAIL)
        self.assertFalse(result(failed, "R12").pending)

    def test_english_threshold_unknown_needs_manual_review(self):
        report = audit(KB, self.full_record())
        self.assertTrue(result(report, "R12").pending)
        self.assertEqual(report.status, Status.MANUAL_REVIEW)

    def test_remedial_english_4_credits_passes_threshold(self):
        # 語言中心註(2)：英檢未達標者修進修英文，成績及格亦可畢業；進修英文不計入畢業學分
        records = self.full_record() + [cat("RE-1", 2, "進修英文"), cat("RE-2", 2, "進修英文")]
        report = audit(KB, records, english_passed=False)
        self.assertTrue(result(report, "R12").satisfied)
        self.assertEqual(report.status, Status.PASS)
        self.assertEqual(report.total_credits, audit(KB, self.full_record()).total_credits)
        short = audit(KB, self.full_record() + [cat("RE-1", 2, "進修英文")])
        self.assertTrue(result(short, "R12").pending)

    def test_pending_english_does_not_hide_failures(self):
        self.assertEqual(audit(KB, [course("IM1001")]).status, Status.FAIL)

    def test_pe_needs_two_first_year_semesters(self):
        # 111 畢審說明第 14 點：體育須含大一體育上下兩學期；成績單課名會寫「大一體育」
        records = [r for r in self.full_record() if r.category != "體育"]
        five_general = records + [cat(f"PE-{i}", 0, "體育") for i in range(5)]
        report = audit(KB, five_general, english_passed=True)
        self.assertFalse(result(report, "R8").satisfied)
        self.assertEqual(result(report, "R8").detail, "大一體育 0／2")
        self.assertEqual(report.status, Status.FAIL)
        self.assertEqual(audit(KB, self.full_record(), english_passed=True).status, Status.PASS)

    def test_dept_elective_needs_16_credits(self):
        base = [course(c) for c in COLLEGE_AND_DEPT] + common_requirements()
        short = audit(KB, base + [Record(f"IM9{i:03}", "本系選修", 3, 85, "117-1") for i in range(3)])
        self.assertEqual((result(short, "R11").have, result(short, "R11").satisfied), (6 + 9, False))
        self.assertEqual(short.status, Status.FAIL)

    def test_ma1006_shares_outside_cap(self):
        records = [course("MA1006")] + [Record(f"EC{i:04}", "外系", 3, 85, "117-1") for i in range(4)]
        self.assertEqual(audit(KB, records).credit_breakdown["外系認列"], 12)

    def test_failed_selective_does_not_block_planning(self):
        report = audit(KB, [course("MA1005", term="114-1"), course("IM1013", grade=40, term="114-1")])
        p = plan(KB, CourseGraph(KB), report, Term(114, 2))
        self.assertEqual(p.unplaceable, [])
        self.assertNotIn("IM1013", p.targets)
        self.assertEqual(p.term_of("IM1023"), Term(114, 2))

    def test_outside_credits_capped_at_12(self):
        records = [course(c) for c in COLLEGE_AND_DEPT] + common_requirements()
        records += [Record(f"EC{i:04}", "外系", 3, 85, "117-1") for i in range(7)]  # 21 學分外系
        report = audit(KB, records)
        self.assertEqual(report.credit_breakdown["外系認列"], 12)
        self.assertFalse(result(report, "R10").satisfied)
        self.assertEqual(report.status, Status.FAIL)

    def test_general_education_excess_capped_at_4(self):
        records = common_requirements() + [cat(f"GX-{i}", 3, "通識") for i in range(3)]  # 通識超修 9
        report = audit(KB, records)
        self.assertEqual(report.credit_breakdown["通識"], 14)
        self.assertEqual(report.credit_breakdown["外系認列"], 4)

    def test_internship_capped_at_3(self):
        report = audit(KB, [cat("MT-INT1", 3, "企業實習"), cat("MT-INT2", 3, "企業實習")])
        self.assertEqual(report.credit_breakdown["外系認列"], 3)

    def test_remedial_english_not_counted(self):
        report = audit(KB, [cat("ENR-1", 2, "進修英文")])
        self.assertEqual(report.total_credits, 0)

    def test_second_language_does_not_satisfy_english(self):
        report = audit(KB, [cat("JP-1", 3, "第二外語"), cat("JP-2", 3, "第二外語")])
        self.assertFalse(result(report, "R5").satisfied)

    def test_core_gened_counts_toward_gened_total(self):
        report = audit(KB, [cat("GC-1", 3, "通識核心")])
        self.assertTrue(result(report, "R7").satisfied)
        self.assertEqual(result(report, "R6").have, 3)

    def test_pe_counted_by_semesters(self):
        report = audit(KB, [cat(f"PE-{i}", 0, "體育") for i in range(4)])
        r8 = result(report, "R8")
        self.assertEqual((r8.have, r8.need, r8.satisfied), (4, 5, False))

    def test_sample_students_statuses(self):
        import app
        from academic_twin.roster import load_roster
        students = app.load_students(app.STUDENTS_PATH)
        english = load_roster(app.ROSTER_PATH).english_passed
        statuses = {sid: audit(KB, recs, english_passed=english.get(sid)).status for sid, recs in students.items()}
        self.assertEqual(statuses, {
            "S001": Status.PASS,
            "S002": Status.FAIL,
            "S003": Status.MANUAL_REVIEW,
            "S004": Status.FAIL,
        })


if __name__ == "__main__":
    unittest.main()
