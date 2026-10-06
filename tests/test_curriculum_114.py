"""資管系 114 學年度實際規則庫的測試：條文對照應修科目表（data/sources/im_114_table.pdf）。"""
import unittest
from pathlib import Path

from academic_twin import knowledge_base
from academic_twin.graph import CourseGraph
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
        *[cat(f"PE-{i}", 0, "體育") for i in range(5)],
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

    def test_full_record_with_unverified_electives_is_warning(self):
        records = [course(c) for c in COLLEGE_AND_DEPT] + common_requirements()
        records += [Record(f"IM9{i:03}", "本系選修", 3, 85, "117-1") for i in range(7)]  # 21 學分
        report = audit(KB, records)
        self.assertEqual(report.total_credits, 84 + 25 + 21)
        self.assertEqual(report.status, Status.WARNING)

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
        students = app.load_students(app.STUDENTS_PATH)
        statuses = {sid: audit(KB, recs).status for sid, recs in students.items()}
        self.assertEqual(statuses, {
            "S001": Status.WARNING,
            "S002": Status.FAIL,
            "S003": Status.MANUAL_REVIEW,
            "S004": Status.FAIL,
        })


if __name__ == "__main__":
    unittest.main()
