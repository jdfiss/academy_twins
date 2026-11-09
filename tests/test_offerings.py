"""本學期開課清單（B 組）：解析學校課程查詢頁、只留學士班可選的課，且清單不得與規則庫衝突。"""
import json
import unittest
from pathlib import Path

from academic_twin.api import AcademicTwinService
from academic_twin.knowledge_base import load as load_kb
from academic_twin.offerings import (OUT_DIR, group_courses, parse_course_table, semester_to_term, simulate_term,
                                     undergraduate)

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "curriculum_im_114.json"

# 依課程查詢系統「依開課單位」頁面結構縮減的片段（課程與教師為虛構）
PAGE = """
<table><tbody>
<tr class="odd"><td>40001<br />IM2999-A</td>
<td>虛構程式設計
    <br /><span class="engclass">Fictional Programming</span>
    <span class="descript"><br />[預選]</span></td>
<td>王小明 <br/></td><td> 3 </td>
<td><span class="classtime">三789/I1-017 (管理二館-017 )<br/> </span></td>
<td> 選修 </td><td> 半 </td><td> 60 </td><td></td><td></td></tr>
<tr class="even"><td>40002<br />IM2999-B</td>
<td>虛構程式設計<br /></td><td>李小華 <br/></td><td> 3 </td>
<td><span class="classtime">五234/I1-405 (管理二館-405 )<br/> </span></td>
<td> 選修 </td><td> 半 </td><td> 60 </td><td></td><td></td></tr>
<tr class="odd"><td>40003<br />MT2998-*</td>
<td>虛構停開課
    <span class="notice">[已停開]</span><br /></td><td>陳老師</td><td> 2 </td><td></td>
<td> 選修 </td><td> 半 </td><td> 30 </td><td></td><td></td></tr>
<tr class="even"><td>40004<br />IM5999-*</td>
<td>虛構研究所課<br /></td><td>林老師</td><td> 3 </td><td></td>
<td> 選修 </td><td> 半 </td><td> 30 </td><td></td><td></td></tr>
</tbody></table>
"""


class ParseTest(unittest.TestCase):
    def test_parse_rows(self):
        rows = parse_course_table(PAGE)
        self.assertEqual([r["code"] for r in rows], ["IM2999", "IM2999", "MT2998", "IM5999"])
        first = rows[0]
        self.assertEqual((first["name"], first["credits"], first["teacher"], first["section"]),
                         ("虛構程式設計", 3, "王小明", "A"))
        self.assertEqual(first["time"], "三789/I1-017 (管理二館-017 )")
        self.assertEqual(rows[2]["name"], "虛構停開課")
        self.assertTrue(rows[2]["cancelled"])

    def test_only_open_undergraduate_courses_grouped_by_code(self):
        rows = [dict(r, dept="d") for r in parse_course_table(PAGE) if undergraduate(r)]
        courses = group_courses(rows, {"d": "資訊管理學系"})
        self.assertEqual([c["code"] for c in courses], ["IM2999"])
        self.assertEqual([s["teacher"] for s in courses[0]["sections"]], ["王小明", "李小華"])

    def test_page_without_table(self):
        self.assertEqual(parse_course_table("<html></html>"), [])

    def test_simulated_term_is_labelled(self):
        base = {"term": "115-1", "source": "課程查詢系統", "note": "", "courses": []}
        sim = simulate_term(base, "117-1")
        self.assertEqual((sim["term"], sim["based_on"], sim["simulated"]), ("117-1", "115-1", True))
        self.assertEqual(sim["source"], base["source"])  # 來源保留實際抓取的學期，畫面依 simulated 標示模擬
        with self.assertRaises(ValueError):
            simulate_term(base, "117-2")

    def test_semester_to_term(self):
        self.assertEqual(semester_to_term("1151"), "115-1")
        with self.assertRaises(ValueError):
            semester_to_term("115-1")


class OfferingFilesTest(unittest.TestCase):
    """已抓取的開課清單：檔名與學期一致，規則庫有的課學分一致（不一致時審查以規則庫為準，需人工確認）。"""

    def test_files_match_rule_library(self):
        kb = load_kb(KB_PATH)
        files = sorted(OUT_DIR.glob("*.json"))
        self.assertTrue(files)
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(path.stem, data["term"])
            codes = [c["code"] for c in data["courses"]]
            self.assertEqual(len(codes), len(set(codes)), path.name)
            for c in data["courses"]:
                self.assertTrue(c["sections"], c["code"])
                if c["code"] in kb.courses:
                    self.assertEqual(c["credits"], kb.courses[c["code"]].credits, c["code"])

    def test_offered_course_outside_rule_library_can_be_trialled(self):
        # S003 的本學期為 115-1；IM2038 本學期有開、但不在 114 規則庫，由審查引擎依規則判斷採計
        data = json.loads((OUT_DIR / "115-1.json").read_text(encoding="utf-8"))
        offered = {c["code"]: c for c in data["courses"]}
        self.assertIn("IM2038", offered)
        service = AcademicTwinService()
        self.assertNotIn("IM2038", service.ruleset("S003").kb.courses)
        c = offered["IM2038"]
        sim = service.simulate("S003", current=[{"code": c["code"], "name": c["name"], "credits": c["credits"],
                                                 "category": "", "counted": True}])
        self.assertEqual(sim["current"]["term"], "115-1")
        self.assertEqual(sim["current"]["courses"][0]["name"], c["name"])


if __name__ == "__main__":
    unittest.main()
