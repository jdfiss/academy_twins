"""PDF 取文字與引文核對：用 repo 內虛構儀表板與公開規章 PDF，AI 回應全部替身，不連網。（A 組）"""
import copy
import csv
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from academic_twin import dashboard, knowledge_base, llm, pdf_text, rule_draft
from academic_twin.api import KB_PATH
from tests.test_dashboard import fake_response as dashboard_response
from tests.test_rule_draft import fake_response as rule_response

ROOT = Path(__file__).parent.parent
SAMPLES = ROOT / "data" / "samples"
SOURCES = ROOT / "data" / "sources"
D02 = (SAMPLES / "dashboard_demo_d02.pdf").read_bytes()
TABLE = (SOURCES / "im_114_table.pdf").read_bytes()
D02_PAGE2 = {"PE-2", "SL-1", "XFER-01"}  # 虛構 d02 第 2 頁的課號（依 demo_dashboard 的版面）


def answer(name: str) -> list[dict]:
    return list(csv.DictReader((SAMPLES / f"dashboard_demo_{name}_answer.csv").read_text(encoding="utf-8")
                               .splitlines()))


def row_text(r: dict) -> str:
    return " ".join(r[k] for k in ("term", "code", "name", "credits", "grade"))


def make_pdf(content: bytes, font: bytes = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
             compress: bool = True) -> bytes:
    """組一份最小的單頁 PDF（測試單位元組字型、未壓縮串流與純影像頁）。"""
    data = zlib.compress(content) if compress else content
    stream = (b"<< /Length %d" % len(data) + (b" /Filter /FlateDecode" if compress else b"") + b" >>\nstream\n"
              + data + b"\nendstream")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
            stream, font]
    out = b"%PDF-1.4\n"
    for i, obj in enumerate(objs, 1):
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    return out + b"trailer\n<< /Root 1 0 R /Size 6 >>\n%%EOF\n"


class ExtractTest(unittest.TestCase):
    def test_fictional_dashboards_extract_every_answer_row_on_its_page(self):
        for name, pages in (("d01", 3), ("d02", 2)):
            text = pdf_text.extract_pages((SAMPLES / f"dashboard_demo_{name}.pdf").read_bytes())
            with self.subTest(name=name):
                self.assertTrue(text.ok, text.reason)
                self.assertEqual(len(text.pages), pages)
                self.assertIn("畢業資格審查儀表板", text.pages[0])
                self.assertIn("學分區：應修 128 學分", text.pages[0])
                self.assertNotIn(pdf_text.REPLACEMENT, "".join(text.pages))
                for r in answer(name):
                    self.assertIn(pdf_text.locate(text, row_text(r), 1).status, (pdf_text.MATCH, pdf_text.OTHER_PAGE))
                    self.assertTrue(any(pdf_text.normalize(row_text(r)) in pdf_text.normalize(p) for p in text.pages))

    def test_lines_and_spaces_are_approximated(self):
        text = pdf_text.extract_pages(D02)
        self.assertIn("114-1 IM1001 計算機概論I 3 80", text.pages[0].splitlines())
        self.assertIn("114-1 XFER-01 轉學抵免：程式語", text.pages[1])
        self.assertEqual(pdf_text.locate(text, "114-1 XFER-01 轉學抵免：程式語言（他校） 3 80", 2).status,
                         pdf_text.MATCH)  # 文件用康熙部首「⾔」，NFKC 後與「言」相同

    def test_public_regulations_with_object_streams(self):
        table = pdf_text.extract_pages(TABLE)
        self.assertTrue(table.ok, table.reason)
        self.assertIn("必修科目計 100學分，最低畢業學分：128 學分。", table.pages[0])
        rules = pdf_text.extract_pages((SOURCES / "ncu_common_required_rules.pdf").read_bytes())
        self.assertTrue(rules.ok, rules.reason)
        self.assertIn("國立中央大學共同必修科目選修辦法", rules.pages[0])

    def test_simple_font_tj_and_uncompressed_stream(self):
        pdf = make_pdf(b"BT /F1 12 Tf 72 700 Td [(Credit) -400 (Rules)] TJ 0 -14 Td (Line\\0402) Tj ET",
                       compress=False)
        text = pdf_text.extract_pages(pdf)
        self.assertEqual(text.pages, ["Credit Rules\nLine 2"])

    def test_unverifiable_documents_report_reason(self):
        image_only = make_pdf(b"q 100 0 0 100 0 0 cm /Im1 Do Q")
        unknown_font = make_pdf(b"BT /F1 12 Tf 72 700 Td <0102030405> Tj ET",
                                font=b"<< /Type /Font /Subtype /Type0 /Encoding /Custom-H >>")
        cases = {"假 PDF": b"%PDF-1.7 fake", "非 PDF": b"not a pdf", "掃描影像": image_only,
                 "加密": D02.replace(b"/Root", b"/Encrypt 99 0 R /Root", 1), "不支援的字型": unknown_font}
        for label, data in cases.items():
            with self.subTest(label):
                text = pdf_text.extract_pages(data)
                self.assertFalse(text.ok)
                self.assertTrue(text.reason)
                self.assertEqual(pdf_text.locate(text, "任何原文", 1).status, pdf_text.UNVERIFIABLE)
        self.assertIn("加密", pdf_text.extract_pages(cases["加密"]).reason)
        self.assertIn("掃描影像", pdf_text.extract_pages(image_only).reason)


class LocateTest(unittest.TestCase):
    TEXT = pdf_text.PdfText(["第一頁 會計學 MT1031\n國文", "5\n第二頁 最低畢業學分：128 學分", ""])

    def test_three_outcomes_and_loose_match(self):
        locate = pdf_text.locate
        self.assertEqual(locate(self.TEXT, "最低畢業學分 128 學分", 2).status, pdf_text.MATCH)  # 忽略標點
        wrong = locate(self.TEXT, "最低畢業學分：128 學分", 1)
        self.assertEqual((wrong.status, wrong.pages), (pdf_text.OTHER_PAGE, [2]))
        fake = locate(self.TEXT, "最低畢業學分 120 學分", 2)
        self.assertEqual((fake.status, fake.missing), (pdf_text.NOT_FOUND, ["最低畢業學分 120 學分"]))
        loose = locate(self.TEXT, "會計學 國文", 1)
        self.assertEqual((loose.status, loose.loose), (pdf_text.MATCH, True))
        spread = locate(self.TEXT, "會計學 MT1031\n最低畢業學分", 1)  # 多段分在兩頁
        self.assertEqual((spread.status, spread.pages), (pdf_text.MATCH, [1, 2]))
        self.assertEqual(locate(self.TEXT, "完全不存在", 3).status, pdf_text.UNVERIFIABLE)  # 該頁沒有文字
        self.assertEqual(locate(self.TEXT, "會計學 MT1031", 9).status, pdf_text.OTHER_PAGE)  # 頁碼超出範圍

    def test_descriptions(self):
        describe, locate = pdf_text.describe, pdf_text.locate
        self.assertEqual(describe(locate(self.TEXT, "會計學 MT1031", 1), 1), "")
        self.assertIn("頁碼可能錯", describe(locate(self.TEXT, "會計學 MT1031", 2), 2))
        self.assertIn("可能是 AI 編造", describe(locate(self.TEXT, "虛構規章", 1), 1))
        self.assertIn("無法自動核對", describe(locate(pdf_text.PdfText([], "加密"), "x", 1), 1))


class DashboardCitationTest(unittest.TestCase):
    def setUp(self):
        self.kb = knowledge_base.load(KB_PATH)

    def extract(self, response, pdf=D02):
        with mock.patch.object(llm, "generate_json", return_value=response):
            return dashboard.extract(pdf, read_text=True)

    def faithful(self):
        response = dashboard_response()
        for r in response.data["rows"]:
            r["source_page"] = 2 if r["code"] in D02_PAGE2 else 1
        return response

    def warnings(self, ext, **kw):
        return [(i.row, i.message) for i in dashboard.check(ext, self.kb, **kw) if i.level == "warning"]

    def test_faithful_rows_have_no_warnings(self):
        self.assertEqual(dashboard.check(self.extract(self.faithful()), self.kb), [])

    def test_wrong_page_fabricated_text_and_field_mismatch_warn(self):
        response = self.faithful()
        rows = response.data["rows"]
        page2 = next(i for i, r in enumerate(rows) if r["code"] == "XFER-01")
        rows[page2]["source_page"] = 1
        rows[0]["source_text"] = "114-1 IM9999 虛構不存在課程 3 99"
        rows[0]["code"] = "IM9999"
        rows[1]["grade"] = "95"  # 擷取值與原文（80）不同
        warnings = self.warnings(self.extract(response))
        self.assertTrue(any(r == page2 and "頁碼可能錯" in m and "第 2 頁" in m for r, m in warnings))
        self.assertTrue(any(r == 0 and "可能是 AI 編造" in m for r, m in warnings))
        self.assertTrue(any(r == 1 and "成績「95」" in m for r, m in warnings))
        self.assertFalse(any(r == 0 and "科目代碼" in m for r, m in warnings))  # 課號與原文一致
        self.assertEqual({r for r, _ in warnings}, {0, 1, page2})

    def test_unextractable_pdf_gives_one_document_warning(self):
        ext = self.extract(self.faithful(), pdf=b"%PDF-1.7 fake")
        warnings = self.warnings(ext)
        self.assertEqual(len(warnings), 1)
        self.assertIsNone(warnings[0][0])
        self.assertIn("無法自動核對原文", warnings[0][1])
        self.assertFalse([i for i in dashboard.check(ext, self.kb) if i.level == "error"])

    def test_without_page_text_check_is_unchanged(self):
        response = self.faithful()
        response.data["rows"][0]["source_text"] = "完全捏造的原文"
        with mock.patch.object(llm, "generate_json", return_value=response):
            ext = dashboard.extract(D02)
        self.assertIsNone(ext.pages)
        self.assertEqual(dashboard.check(ext, self.kb), [])  # 舊呼叫方式：不核對
        text = pdf_text.extract_pages(D02)
        self.assertTrue(any("可能是 AI 編造" in m for _, m in self.warnings(ext, pages=text)))  # 由參數傳入

    def test_page_text_survives_row_edits(self):
        from dataclasses import replace
        ext = self.extract(self.faithful())
        rows = [dict(r) for r in ext.rows]
        rows[0]["credits"] = "4"
        edited = replace(ext, rows=rows)  # office_api 確認時的作法
        self.assertTrue(any(r == 0 and "學分「4」" in m for r, m in self.warnings(edited)))


class RuleDraftCitationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log = Path(tmp.name) / "runs.jsonl"
        for name, value in (("DRAFT_DIR", Path(tmp.name) / "drafts"), ("EVAL_LOG", self.log)):
            patcher = mock.patch.object(rule_draft, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def draft(self, response, pdf=TABLE):
        with mock.patch.object(llm, "generate_json", return_value=response):
            return rule_draft.run_draft(pdf, 114)

    def test_quotes_checked_against_public_table(self):
        d = self.draft(rule_response())
        self.assertEqual(d["issues"], [])
        self.assertNotIn("pdf_text", d)
        by_id = {w["id"]: w["message"] for w in d["warnings"]}
        self.assertEqual(set(by_id), {"R4"})  # R2 清單、R10 備註都逐字在第 1 頁
        self.assertIn("不相連", by_id["R4"])  # 「國文 5」在表格中分屬兩格

    def test_fabricated_and_wrong_page_quotes_warn_without_blocking(self):
        response = rule_response()
        rows = {r["id"]: r for r in response.data["requirements"]}
        rows["R10"]["source_quote"] = "最低畢業學分 120 學分"
        rows["R2"]["source_page"] = 2
        d = self.draft(response)
        self.assertEqual(d["issues"], [])
        by_id = {w["id"]: w["message"] for w in d["warnings"]}
        self.assertIn("可能是 AI 編造", by_id["R10"])
        self.assertIn("頁碼可能錯", by_id["R2"])
        # 重新檢查時沿用保存的文字；承辦人改正引文後警示消失
        fixed = copy.deepcopy(d["requirements"])
        for r in fixed:
            if r["id"] == "R10":
                r["source_quote"] = "必修科目計 100學分，最低畢業學分：128 學分。"
            if r["id"] == "R2":
                r["source_page"] = 1
        checked = rule_draft.review_draft(d["draft_id"], fixed)
        self.assertEqual({w["id"] for w in checked["warnings"]}, {"R4"})
        self.assertNotIn("最低畢業學分", self.log.read_text(encoding="utf-8"))  # 量測紀錄只有計數

    def test_unextractable_pdf_one_warning_and_confirm_still_possible(self):
        d = self.draft(rule_response(), pdf=b"%PDF-1.7 fake public regulation")
        self.assertEqual(d["issues"], [])
        self.assertEqual(len(d["warnings"]), 1)
        self.assertIn("無法自動核對引文", d["warnings"][0]["message"])
        confirmed = rule_draft.confirm(d["draft_id"], d["requirements"], "虛構確認人", True)
        self.assertEqual(confirmed["state"], "confirmed")


if __name__ == "__main__":
    unittest.main()
