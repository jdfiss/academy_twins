"""本學期開課清單：從中央大學課程查詢系統（公開頁面）抓取，供學生端「本學期修課」挑課。

只抓公開的開課資料（課號、班別、課名、學分、教師、時間、必選修），不含任何學生資料。
輸出放在 web/assets/offerings/<學期>.json（如 115-1.json），學生頁依學生的本學期讀取；
沒有對應學期的檔案時，學生頁退回原本的「本系課程＋手動輸入」。

開課清單只是挑課的方便：學分、類別與判定仍以規則庫與審查引擎為準（規則庫有的課以規則庫為準）。

用法：py -m academic_twin.offerings 1151
      py -m academic_twin.offerings 1151 --simulate 117-1   # 示範用：依 115-1 實際開課模擬 117-1
"""
from __future__ import annotations

import argparse
import html
import http.cookiejar
import json
import re
import ssl
import sys
import urllib.request
from datetime import date
from pathlib import Path

BASE = "https://cis.ncu.edu.tw/Course/main"
# 課程查詢系統「依開課單位」的代碼
DEPTS = {
    "deptI1I4003I0": "資訊管理學系",
    "deptI1I4000I0": "管理學院",
}
OUT_DIR = Path(__file__).resolve().parent.parent / "web" / "assets" / "offerings"
USER_AGENT = "academic-twin-course-fetch/1.0"


def semester_to_term(semester: str) -> str:
    """課程系統的學期代碼轉成本專案的學期字串："1151" → "115-1"。"""
    if not re.fullmatch(r"\d{3}[12]", semester):
        raise ValueError(f"學期代碼應為 4 碼，如 1151：{semester}")
    return f"{semester[:3]}-{semester[3]}"


def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def parse_course_table(page: str) -> list[dict]:
    """解析「依開課單位」查詢結果頁的課程表格，每個班別一筆。"""
    if "<tbody>" not in page:
        return []
    body = page.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
        tds = re.findall(r"<td>(.*?)</td>", tr, re.S)
        if len(tds) < 6:
            continue
        m = re.match(r"\s*(\d+)\s*<br\s*/?>\s*([A-Z0-9]+)-(\S+)", tds[0])
        if not m:
            continue
        name_part = re.split(r"<br", tds[1], maxsplit=1)[0]
        credits = re.sub(r"\D", "", _text(tds[3]))
        rows.append({
            "serial": m.group(1),
            "code": m.group(2),
            "section": m.group(3),
            "name": _text(re.sub(r'<span class="notice">.*?</span>', "", name_part, flags=re.S)),
            "credits": int(credits) if credits else 0,
            "teacher": _text(tds[2]),
            "time": _text(tds[4]),
            "required": _text(tds[5]) == "必修",
            "cancelled": "已停開" in tds[1],
        })
    return rows


def undergraduate(row: dict) -> bool:
    """學士班學生可選的課：課號數字 1–4 字頭，排除已停開與碩士班專屬課程。

    5 字頭以上為研究所課程，是否開放大學部、能否採計需另外確認，不列入挑課清單。
    """
    m = re.match(r"[A-Z]+(\d)", row["code"])
    return bool(m) and m.group(1) in "1234" and not row["cancelled"] and "碩士" not in row["name"]


def group_courses(rows: list[dict], dept_names: dict[str, str]) -> list[dict]:
    """同一課號的多個班別合併成一門課，班別資訊保留給學生挑選參考。"""
    courses: dict[str, dict] = {}
    for row in rows:
        c = courses.setdefault(row["code"], {
            "code": row["code"], "name": row["name"], "credits": row["credits"],
            "dept": dept_names[row["dept"]], "sections": [],
        })
        c["sections"].append({k: row[k] for k in ("section", "serial", "teacher", "time")})
    return sorted(courses.values(), key=lambda c: c["code"])


def fetch(semester: str) -> dict:
    """抓取指定學期的開課清單（課程系統只提供當前學期，學期不符時拒絕）。"""
    jar = http.cookiejar.CookieJar()
    # 仍驗證憑證；只關閉 Python 3.13 起預設的 X.509 嚴格檢查（學校部分主機的憑證缺 Subject Key Identifier）
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar),
                                         urllib.request.HTTPSHandler(context=ctx))
    opener.addheaders = [("User-Agent", USER_AGENT)]

    def get(url: str) -> str:
        with opener.open(url, timeout=30) as resp:
            return resp.read().decode("utf-8")

    if "課程名稱" not in get(f"{BASE}/query/byUnion?dept={next(iter(DEPTS))}"):
        get(f"{BASE}/lang")  # 預設可能是英文介面，切換成中文
    rows = []
    for dept in DEPTS:
        page = get(f"{BASE}/query/byUnion?dept={dept}")
        if f"semester={semester}" not in page:
            raise RuntimeError(f"課程系統目前不是 {semester} 學期，無法抓取")
        rows += [{**r, "dept": dept} for r in parse_course_table(page)]
    if not rows:
        raise RuntimeError("課程系統沒有回傳任何課程，頁面格式可能已改變")
    courses = group_courses([r for r in rows if undergraduate(r)], DEPTS)
    return {
        "term": semester_to_term(semester),
        "source": "國立中央大學課程查詢系統（依開課單位）",
        "source_url": f"{BASE}/query/byUnion",
        "fetched": date.today().isoformat(),
        "departments": list(DEPTS.values()),
        "note": "只列學士班可選、未停開的課；通識、體育與外系課不在清單內，請手動輸入。",
        "courses": courses,
    }


def simulate_term(data: dict, term: str) -> dict:
    """以實際抓到的學期為底，模擬另一個同為上／下學期的開課清單（示範學生的本學期在未來學期時使用）。

    課程照抄實際開課；教師、時間只是沿用，畫面須標示為模擬。
    """
    if term.split("-")[1] != data["term"].split("-")[1]:
        raise ValueError(f"只能用同為上／下學期的實際開課模擬：{data['term']} → {term}")
    return {**data, "term": term, "simulated": True, "based_on": data["term"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取本學期開課清單給學生端使用")
    parser.add_argument("semester", help="課程系統學期代碼，如 1151")
    parser.add_argument("--simulate", metavar="TERM",
                        help="不抓取，改用已抓到的 semester 清單模擬此學期（如 117-1）")
    args = parser.parse_args(argv)
    if args.simulate:
        base = json.loads((OUT_DIR / f"{semester_to_term(args.semester)}.json").read_text(encoding="utf-8"))
        data = simulate_term(base, args.simulate)
    else:
        data = fetch(args.semester)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{data['term']}.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"已寫入 {out}：{len(data['courses'])} 門課")
    return 0


if __name__ == "__main__":
    sys.exit(main())
