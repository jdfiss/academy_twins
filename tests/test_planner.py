import unittest
from pathlib import Path

from academic_twin import knowledge_base
from academic_twin.graph import CourseGraph
from academic_twin.planner import Scenario, Term, explain_delay, plan
from academic_twin.rule_engine import Record, audit

KB = knowledge_base.load(Path(__file__).parent / "fixtures" / "curriculum_sample.json")
GRAPH = CourseGraph(KB)

# 大一上學期修完，從 111-2 開始排
FRESHMAN = [Record(c, c, KB.courses[c].credits, 80, "111-1") for c in ["IM1001", "IM1002", "IM1004", "GE0001"]]
START = Term(111, 2)


class GraphTest(unittest.TestCase):
    def test_descendants_follow_prereq_chain(self):
        blocked = GRAPH.descendants("IM2001")
        self.assertIn("IM2003", blocked)
        self.assertIn("IM3002", blocked)  # 間接：IM2001 → IM2003 → IM3002
        self.assertLess(blocked.index("IM2003"), blocked.index("IM3002"))

    def test_ancestors(self):
        self.assertEqual(GRAPH.ancestors("IM2001"), ["IM1002", "IM1003"])

    def test_top_bottleneck_is_programming(self):
        self.assertEqual(GRAPH.bottlenecks(1)[0].code, "IM1002")

    def test_cycle_rejected(self):
        import json, tempfile
        raw = json.loads((Path(__file__).parent / "fixtures" / "curriculum_sample.json").read_text(encoding="utf-8"))
        raw["courses"][1]["prereqs"] = ["IM1003"]  # IM1002 ↔ IM1003 互為先修
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(raw, f)
        with self.assertRaises(ValueError):
            CourseGraph(knowledge_base.load(f.name))
        Path(f.name).unlink()


class PlannerTest(unittest.TestCase):
    def setUp(self):
        self.report = audit(KB, FRESHMAN)
        self.base = plan(KB, GRAPH, self.report, START)

    def test_plan_respects_prereqs_and_offerings(self):
        for term, codes in self.base.terms.items():
            for c in codes:
                self.assertIn(term.season, KB.courses[c].terms, f"{c} 在 {term} 沒開")
                for p in KB.courses[c].prereqs:
                    pt = self.base.term_of(p)
                    self.assertTrue(pt is None or pt < term, f"{c} 排在先修 {p} 之前")

    def test_plan_respects_credit_cap(self):
        for term, codes in self.base.terms.items():
            load = sum(KB.courses[c].credits for c in codes) + self.base.free.get(term, 0)
            self.assertLessEqual(load, KB.max_credits_per_term)

    def test_completed_plan_passes_audit(self):
        records = list(FRESHMAN)
        for term, codes in self.base.terms.items():
            records += [Record(c, c, KB.courses[c].credits, 80, str(term)) for c in codes]
            if self.base.free.get(term):
                records.append(Record(f"FREE{term}", "自由學分", self.base.free[term], 80, str(term)))
        self.assertTrue(all(r.satisfied for r in audit(KB, records).results))

    def test_earliest_graduation_follows_critical_path(self):
        # 程設一 111-1 → 程設二 111-2 → 資結 112-1 → 資料庫 112-2 → 專題（只開下學期）113-2
        self.assertTrue(self.base.feasible)
        self.assertEqual(self.base.graduation, Term(113, 2))

    def test_skipping_fall_only_bottleneck_delays_a_year(self):
        alt = plan(KB, GRAPH, self.report, START, Scenario(skip={"IM2001": Term(112, 1)}))
        self.assertEqual(alt.graduation, Term(114, 2))
        self.assertIn("延後 2 學期", explain_delay(KB, self.base, alt)[-1])

    def test_skipping_non_critical_course_no_delay(self):
        alt = plan(KB, GRAPH, self.report, START, Scenario(skip={"GE0003": Term(111, 2)}))
        self.assertEqual(alt.graduation, self.base.graduation)

    def test_term_arithmetic(self):
        self.assertEqual(Term(113, 1).next(), Term(113, 2))
        self.assertEqual(Term(113, 2).next(), Term(114, 1))
        self.assertEqual(str(Term.parse("114-2")), "114-2")


if __name__ == "__main__":
    unittest.main()
