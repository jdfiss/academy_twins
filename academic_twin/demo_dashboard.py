"""產生虛構的畢審儀表板 PDF，供 AI 解析開發與量測使用。（A 組）

版面依 2026-10-14 承辦人訪談的描述模擬：上方學分區列出應修與實得學分，
下方依規則代碼分段列出課程。真實儀表板格式待取得去識別化範例後調整。
所有學生代號、課程與成績都沿用虛構示例資料；同時輸出標準答案 CSV。

    python -m academic_twin.demo_dashboard          # 產生 data/samples/ 下的 PDF 與答案
    python -m academic_twin.demo_dashboard --bench  # 產生 data/samples/bench/ 的虛構基準集（固定種子）

基準集（--bench）在同一個虛構版型上變化出 24 份案例，涵蓋容易讓 AI 出錯的情境
（長紀錄跨頁、重修、不及格、成績 59／60 邊界、少見學分、長課名與全形括號、同段落多列、
抵免段落、非 114 入學年度），每份附標準答案 CSV 與情境標籤 JSON，
供 academic_twin/benchmark.py 量測。仍是本專案自行產生的版面，量測結果不代表真實儀表板。
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import random
import re
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


def render_html(demo_id: str, rows: list[dict], cohort: int | None = None) -> str:
    passed = [r for r in rows if float(r["grade"]) >= KB.passing_grade]
    earned = sum(int(r["credits"]) for r in passed)
    e = html.escape
    parts = [f"""<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>畢業審查儀表板（虛構）</title>
<style>body{{font-family:"Microsoft JhengHei",sans-serif;font-size:11pt;margin:24px}}
h1{{font-size:16pt}}table{{border-collapse:collapse;width:100%;margin:6px 0 14px}}
td,th{{border:1px solid #999;padding:3px 6px}}th{{background:#eee}}.num{{text-align:right}}
.box{{border:2px solid #333;padding:8px;margin-bottom:12px}}.note{{color:#a00}}</style>
<h1>畢業資格審查儀表板</h1><p class="note">※ 虛構示範文件，非真實學生資料</p>
<div class="box">學號：{e(demo_id)}　系所：資訊管理學系　入學學年度：{cohort or KB.cohort}<br>
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


def answer_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=REQUIRED + ["category"], lineterminator="\n")
    writer.writeheader()
    writer.writerows({k: r[k] for k in REQUIRED + ["category"]} for r in rows)
    return buf.getvalue()


# ---------- 虛構基準集（--bench） ----------
BENCH_OUT = OUT / "bench"
BENCH_VERSION = "bench-v1"  # 產生規則改變時要改版本，量測紀錄據此區分案例集合
BENCH_SEED = 20261027
BENCH_COUNT = 24
TAGS = {
    "long_multipage": "長紀錄跨頁（70 列以上）",
    "retake": "重修（同課號不同學期）",
    "failed": "不及格",
    "grade_boundary": "成績邊界（59／60）",
    "rare_credits": "少見學分數（0、1、4、6、9）",
    "long_name": "長課名或全形括號",
    "dense_section": "同一段落很多列",
    "substitution": "抵免段落",
    "other_cohort": "非 114 入學年度",
    "baseline": "一般（無特殊情境，對照用）",
}
SCENARIOS = [t for t in TAGS if t != "baseline"]
# 虛構課名素材；課號一律是規則庫沒有的虛構代碼（IM39xx、IM49xx、外系四碼），不對應真實課程
TOPICS = ["資料探勘", "雲端運算", "資訊安全", "區塊鏈應用", "數位行銷", "供應鏈管理", "人機互動", "商業分析",
          "專案管理", "物聯網應用", "社群媒體分析", "自然語言處理", "金融科技", "知識管理", "軟體工程",
          "企業資源規劃", "深度學習實務", "資料視覺化", "服務科學", "創新創業", "決策支援系統", "資訊倫理"]
OTHER_DEPTS = [("FN", "財務"), ("EC", "經濟"), ("EE", "電機"), ("CS", "資工"), ("LW", "法律"), ("MG", "企管"),
               ("PH", "物理"), ("HK", "客家"), ("ST", "統計")]
LONG_NAMES = ["跨域專題：資料治理、隱私保護與校務研究方法之實務應用（含期末實作報告）",
              "企業數位轉型個案研討（二）：製造業、零售業與公部門之比較分析",
              "人工智慧倫理與法規（英語授課）（全英語）",
              "資訊系統稽核與內部控制：理論、標準（ISO／IEC 27001）與實務",
              "產業實習（暑期）─ 校外實習機構媒合與成果發表"]
RARE_CREDITS = [("虛構新生講座", 0), ("虛構專業倫理講座", 1), ("虛構程式設計工作坊", 1),
                ("虛構全學期校外實習", 9), ("虛構海外研修", 6), ("虛構整合實作（四學分）", 4)]
TRANSFERS = ["程式語言（他校）", "計算機概論（他校）", "英文寫作（他校）", "統計學（他校）", "通識：哲學概論（他校）"]


class _Case:
    """產生一份案例時的暫存：同一案例內課號不與其他課程重複（重修除外），（課號, 學期）不重複，
    讓答案與擷取結果可以一對一對齊。"""

    def __init__(self, rng: random.Random, cohort: int):
        self.rng, self.cohort, self.rows = rng, cohort, []

    def codes(self) -> set[str]:
        return {r["code"] for r in self.rows}

    def add(self, code: str, name: str, credits: int, grade: int, year: int, sem: int, category: str = "") -> dict:
        term = f"{year}-{sem}"
        assert not any(r["code"] == code and r["term"] == term for r in self.rows), (code, term)
        row = {"code": code, "name": name, "credits": str(credits), "grade": str(grade), "term": term,
               "category": category}
        self.rows.append(row)
        return row

    def grade(self) -> int:
        return self.rng.randint(61, 98)

    def new_code(self, make) -> str:
        """產生規則庫與本案例都沒有的虛構課號。"""
        while True:
            code = make()
            if code not in KB.courses and code not in self.codes():
                return code


def _base_pool() -> list[dict]:
    """示例學生 S001–S004 的課程（去重、不含抵免），保留相對入學的學年差與學期。"""
    pool, seen = [], set()
    for r in csv.DictReader(SOURCE.read_text(encoding="utf-8-sig").splitlines()):
        if r["code"] in seen or r["category"] == "抵免":
            continue
        seen.add(r["code"])
        year, sem = r["term"].split("-")
        pool.append({**r, "offset": int(year) - 114, "sem": int(sem)})
    return pool


def bench_case(index: int, seed: int = BENCH_SEED) -> dict:
    """產生第 index 份基準案例（從 1 開始）。同種子、同編號一定得到相同結果。"""
    rng = random.Random(f"{BENCH_VERSION}-{seed}-{index}")
    if index % 12 == 0:  # 每 12 份放一份無特殊情境的對照案例
        tags = ["baseline"]
    else:  # 主、副情境依編號輪流排開，讓每種情境的案例數接近；約四成再隨機疊加第三種（不含長紀錄）
        n = len(SCENARIOS)
        tags = [SCENARIOS[(index - 1) % n], SCENARIOS[(index + (index - 1) // n) % n]]
        if rng.random() < 0.4:
            tags.append(rng.choice([t for t in SCENARIOS if t not in tags and t != "long_multipage"]))
    cohort = rng.choice([111, 112, 113]) if "other_cohort" in tags else 114
    years = 4 if {"long_multipage", "dense_section", "retake"} & set(tags) else rng.randint(2, 4)
    last = cohort + years - 1
    case = _Case(rng, cohort)
    for c in _base_pool():
        if c["offset"] < years:
            case.add(c["code"], c["name"], int(c["credits"]), case.grade(), cohort + c["offset"], c["sem"],
                     c["category"])
    base = list(case.rows)  # 不及格、邊界、重修只改原本就有的課程

    def any_term(start: int = cohort) -> tuple[int, int]:
        return rng.randint(start, last), rng.randint(1, 2)

    if "dense_section" in tags:  # 系選修段落（B01）塞很多列
        for topic in rng.sample(TOPICS, rng.randint(16, 20)):
            case.add(case.new_code(lambda: f"IM39{rng.randint(10, 99)}"), topic, 3, case.grade(),
                     *any_term(cohort + 1))
    if "long_multipage" in tags:  # 外系選修補到 70 列以上，跨多頁
        target = rng.randint(72, 85)
        while len(case.rows) < target:
            dept, label = rng.choice(OTHER_DEPTS)
            case.add(case.new_code(lambda: f"{dept}{rng.randint(1000, 4999)}"),
                     f"{rng.choice(TOPICS)}（{label}系開）", rng.choice([2, 3]), case.grade(), *any_term())
    if "long_name" in tags:
        for name in rng.sample(LONG_NAMES, rng.randint(2, 3)):
            case.add(case.new_code(lambda: f"IM49{rng.randint(10, 99)}"), name, 3, case.grade(), last,
                     rng.randint(1, 2))
        for r in case.rows:  # 部分原有課名改用全形括號
            if "(" in r["name"] and rng.random() < 0.6:
                r["name"] = r["name"].replace("(", "（").replace(")", "）")
    if "rare_credits" in tags:
        for name, credits in rng.sample(RARE_CREDITS, rng.randint(2, 4)):
            case.add(case.new_code(lambda: f"IM49{rng.randint(10, 99)}"), name, credits, case.grade(), *any_term())
    if "substitution" in tags:
        for n, name in enumerate(rng.sample(TRANSFERS, rng.randint(1, 3)), 1):
            case.add(f"XFER-{n:02d}", f"轉學抵免：{name}", rng.choice([2, 3]), rng.randint(70, 90), cohort, 1,
                     "抵免")
    graded = [r for r in base if r["credits"] != "0"]
    touched: set[int] = set()
    if "retake" in tags:  # 先不及格，隔年同學期重修通過；約三成兩次不及格
        early = [r for r in graded if int(r["term"][:3]) < last]
        for r in rng.sample(early, min(len(early), rng.randint(1, 3))):
            touched.add(id(r))
            r["grade"] = str(rng.randint(30, 58))
            year, sem = int(r["term"][:3]), int(r["term"][-1])
            if rng.random() < 0.3 and year + 2 <= last:
                year += 1
                case.add(r["code"], r["name"], int(r["credits"]), rng.randint(30, 58), year, sem)
            case.add(r["code"], r["name"], int(r["credits"]), rng.randint(60, 85), year + 1, sem)
    free = [r for r in graded if id(r) not in touched]
    if "failed" in tags:
        for r in rng.sample(free, rng.randint(1, 3)):
            touched.add(id(r))
            r["grade"] = str(rng.randint(0, 55))
    if "grade_boundary" in tags:
        picks = rng.sample([r for r in free if id(r) not in touched], rng.randint(3, 5))
        for r, g in zip(picks, [59, 60] + [rng.choice([59, 60]) for _ in picks]):
            r["grade"] = str(g)
    student_id = f"DEMO-B{index:02d}"
    rows = sorted(({**r, "student_id": student_id} for r in case.rows), key=lambda r: (r["term"], r["code"]))
    return {"case_id": f"bench_{index:02d}", "student_id": student_id, "tags": tags, "cohort": cohort,
            "split": "驗證" if index % 3 == 0 else "開發", "rows": rows,
            "sections": sorted({section_of(r) for r in rows}), "seed": seed, "case_set": BENCH_VERSION}


def bench_cases(count: int = BENCH_COUNT, seed: int = BENCH_SEED) -> list[dict]:
    return [bench_case(i, seed) for i in range(1, count + 1)]


def case_meta(case: dict, pages: int | None = None) -> dict:
    """情境標籤中繼資料（不含課程內容，課程在答案 CSV）。"""
    return {"case_id": case["case_id"], "student_id": case["student_id"], "case_set": case["case_set"],
            "seed": case["seed"], "split": case["split"], "tags": case["tags"],
            "tag_labels": [TAGS[t] for t in case["tags"]], "cohort": case["cohort"], "rows": len(case["rows"]),
            "sections": case["sections"], "pages": pages, "fictional": True,
            "note": "虛構案例，版面由本專案自行產生，不代表真實儀表板"}


def pdf_pages(pdf: Path) -> int | None:
    count = len(re.findall(rb"/Type\s*/Page(?![a-zA-Z])", pdf.read_bytes()))
    return count or None


def write_bench(out: Path = BENCH_OUT, count: int = BENCH_COUNT, seed: int = BENCH_SEED) -> list[dict]:
    out.mkdir(parents=True, exist_ok=True)
    metas = []
    for case in bench_cases(count, seed):
        pdf = out / f"{case['case_id']}.pdf"
        to_pdf(render_html(case["student_id"], case["rows"], case["cohort"]), pdf)
        (out / f"{case['case_id']}_answer.csv").write_text(answer_csv(case["rows"]), encoding="utf-8")
        meta = case_meta(case, pdf_pages(pdf))
        (out / f"{case['case_id']}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                                                     encoding="utf-8")
        metas.append(meta)
        print(f"{case['case_id']}：{meta['rows']} 列、{meta['pages']} 頁、{'、'.join(case['tags'])}")
    return metas


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="產生虛構畢審儀表板 PDF 與標準答案")
    parser.add_argument("--bench", action="store_true", help="改為產生 data/samples/bench/ 的虛構基準集")
    parser.add_argument("--count", type=int, default=BENCH_COUNT, help="基準案例數（--bench）")
    parser.add_argument("--seed", type=int, default=BENCH_SEED, help="亂數種子，同種子輸出相同（--bench）")
    args = parser.parse_args(argv)
    if args.bench:
        write_bench(BENCH_OUT, args.count, args.seed)
        return
    OUT.mkdir(parents=True, exist_ok=True)
    for demo_id in DEMOS:
        rows = demo_records(demo_id)
        stem = demo_id.lower().replace("-", "_")
        to_pdf(render_html(demo_id, rows), OUT / f"dashboard_{stem}.pdf")
        (OUT / f"dashboard_{stem}_answer.csv").write_text(answer_csv(rows), encoding="utf-8")
        print(f"{demo_id}：{len(rows)} 列 → data/samples/dashboard_{stem}.pdf")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
