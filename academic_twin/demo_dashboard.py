"""產生虛構的畢審儀表板 PDF，供 AI 解析開發與量測使用。（A 組）

版面依 2026-10-14 承辦人訪談的描述模擬：上方學分區列出應修與實得學分，
下方依規則代碼分段列出課程。真實儀表板格式待取得去識別化範例後調整。
所有學生代號、課程與成績都沿用虛構示例資料；同時輸出標準答案 CSV。

    python -m academic_twin.demo_dashboard          # 產生 data/samples/ 下的 PDF 與答案
"""
from __future__ import annotations

import csv
import html
import io
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import knowledge_base
from .records import REQUIRED

ROOT = Path(__file__).parent.parent
OUT = ROOT / "data" / "samples"
KB = knowledge_base.load(ROOT / "data" / "curriculum_im_114.json")
SOURCE = ROOT / "data" / "students" / "sample_students.csv"
BROWSERS = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe", "msedge", "google-chrome", "chromium"]

# 虛構的校務系統規則代碼；正式代碼待承辦人提供
SECTIONS = [("A01", "院系必修"), ("A02", "必選"), ("B01", "系選修"), ("B02", "外系選修"),
            ("C01", "國文"), ("C02", "外文"), ("C03", "通識"), ("C04", "通識核心"), ("C05", "體育"),
            ("C06", "服務學習"), ("Z99", "待認定（抵免）")]
CATEGORY_SECTION = {"國文": "C01", "外文": "C02", "通識": "C03", "通識核心": "C04", "體育": "C05",
                    "服務學習": "C06", "抵免": "Z99"}

# 虛構情境：每份文件由哪位示例學生的紀錄產生，以及額外加入的紀錄（測試不及格列）
DEMOS = {
    "DEMO-D01": ("S001", [{"code": "IM3999", "name": "虛構專題研討", "credits": "2", "grade": "52",
                           "term": "114-1", "category": ""}]),
    "DEMO-D02": ("S003", []),
}


def section_of(r: dict) -> str:
    if r["category"]:
        return CATEGORY_SECTION.get(r["category"], "Z99")
    course = KB.courses.get(r["code"])
    if course:
        return "A02" if course.type == "selective" else "A01"
    return "B01" if r["code"].startswith(KB.dept_prefixes) else "B02"


def demo_records(demo_id: str) -> list[dict]:
    source_id, extra = DEMOS[demo_id]
    rows = [r for r in csv.DictReader(SOURCE.read_text(encoding="utf-8-sig").splitlines()) if r["student_id"] == source_id]
    rows += extra
    return sorted(({**r, "student_id": demo_id} for r in rows), key=lambda r: (r["term"], r["code"]))


def render_html(demo_id: str, rows: list[dict]) -> str:
    passed = [r for r in rows if float(r["grade"]) >= KB.passing_grade]
    earned = sum(int(r["credits"]) for r in passed)
    e = html.escape
    parts = [f"""<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>畢業審查儀表板（虛構）</title>
<style>body{{font-family:"Microsoft JhengHei",sans-serif;font-size:11pt;margin:24px}}
h1{{font-size:16pt}}table{{border-collapse:collapse;width:100%;margin:6px 0 14px}}
td,th{{border:1px solid #999;padding:3px 6px}}th{{background:#eee}}.num{{text-align:right}}
.box{{border:2px solid #333;padding:8px;margin-bottom:12px}}.note{{color:#a00}}</style>
<h1>畢業資格審查儀表板</h1><p class="note">※ 虛構示範文件，非真實學生資料</p>
<div class="box">學號：{e(demo_id)}　系所：資訊管理學系　入學學年度：{KB.cohort}<br>
學分區：應修 128 學分　實得 {earned} 學分</div>"""]
    for code, title in SECTIONS:
        items = [r for r in rows if section_of(r) == code]
        if not items:
            continue
        got = sum(int(r["credits"]) for r in items if float(r["grade"]) >= KB.passing_grade)
        parts.append(f"<h3>規則代碼 {code}　{e(title)}　實得 {got} 學分</h3><table>"
                     "<tr><th>學期</th><th>科目代碼</th><th>科目名稱</th><th>學分</th><th>成績</th></tr>")
        parts += [f"<tr><td>{e(r['term'])}</td><td>{e(r['code'])}</td><td>{e(r['name'])}</td>"
                  f"<td class='num'>{e(r['credits'])}</td><td class='num'>{e(r['grade'])}</td></tr>" for r in items]
        parts.append("</table>")
    return "\n".join(parts) + "</html>"


def to_pdf(html_text: str, pdf: Path) -> None:
    browser = next((b for b in BROWSERS if Path(b).is_file() or shutil.which(b)), None)
    if not browser:
        sys.exit("找不到 Edge 或 Chrome，無法輸出 PDF")
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "dashboard.html"
        page.write_text(html_text, encoding="utf-8")
        subprocess.run([browser, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                        f"--print-to-pdf={pdf}", page.as_uri()], check=True, capture_output=True, timeout=120)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for demo_id in DEMOS:
        rows = demo_records(demo_id)
        stem = demo_id.lower().replace("-", "_")
        to_pdf(render_html(demo_id, rows), OUT / f"dashboard_{stem}.pdf")
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=REQUIRED + ["category"], lineterminator="\n")
        writer.writeheader()
        writer.writerows({k: r[k] for k in REQUIRED + ["category"]} for r in rows)
        (OUT / f"dashboard_{stem}_answer.csv").write_text(buf.getvalue(), encoding="utf-8")
        print(f"{demo_id}：{len(rows)} 列 → data/samples/dashboard_{stem}.pdf")


if __name__ == "__main__":
    main()
