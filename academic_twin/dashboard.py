"""AI 解析畢審儀表板：PDF → 待確認紀錄 → 檢查 → 承辦人確認後才成為審查輸入。（A 組）

AI 只抽出文件上的原始欄位，並逐列附來源頁碼與原文；規則代碼對應到類別用固定對照表，
不讓 AI 推測。所有結果都標示待確認，檢查出的錯誤未修正前不得送入審查
（見 docs/TEAM_PLAN.md〈AI 輸入與啟用驗收〉）。

目前只支援虛構示範版型（academic_twin/demo_dashboard.py 產生）；真實儀表板格式待取得
去識別化範例後新增版型。不支援掃描影像，無法辨識時須顯示失敗，不能當成沒有修課紀錄。

    python -m academic_twin.dashboard data/samples/dashboard_demo_d01.pdf \\
        --answer data/samples/dashboard_demo_d01_answer.csv     # 解析並與標準答案比對
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import llm
from .knowledge_base import KnowledgeBase
from .records import REQUIRED, TERM_RE

ROOT = Path(__file__).parent.parent
EVAL_LOG = ROOT / "data" / "eval" / "dashboard_runs.jsonl"  # 只記彙總數字，不記課程內容
PROMPT_VERSION = "dashboard-v1"
FORMAT = "demo-v1"
FIELDS = ["term", "code", "name", "credits", "grade", "category"]

# 支援版型的規則代碼 → 本專案類別（空字串表示依課號由規則庫判斷）。正式代碼待承辦人提供。
FORMATS = {
    "demo-v1": {"A01": "", "A02": "", "B01": "", "B02": "", "C01": "國文", "C02": "外文", "C03": "通識",
                "C04": "通識核心", "C05": "體育", "C06": "服務學習", "Z99": "抵免"},
}

PROMPT = """你是資料擷取工具。附件是一份大學「畢業資格審查儀表板」PDF。
請逐列抽出文件上「每一筆修課紀錄」，不要推論、不要補寫、不要合併，只抄錄文件上實際出現的內容：
- rule_code：該紀錄所在段落標題的規則代碼（例如 A01）
- term、code、name、credits、grade：照文件原樣抄錄
- source_page：該列所在頁碼（從 1 開始）
- source_text：該列在文件上的原文（整列文字）
小計、標題、學分區等不是修課紀錄的列不要輸出。跨頁的表格要完整列出每一頁的列。
另外抄錄文件上宣告的數字：入學學年度、學分區的應修與實得學分，以及每個規則代碼段落標示的實得學分。
文件看不清楚或無法辨識時，在 unreadable 說明原因，不要猜。"""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "cohort": {"type": "INTEGER", "nullable": True},
        "declared_required": {"type": "INTEGER", "nullable": True},
        "declared_earned": {"type": "INTEGER", "nullable": True},
        "sections": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "rule_code": {"type": "STRING"}, "title": {"type": "STRING"},
            "declared_earned": {"type": "INTEGER", "nullable": True}}, "required": ["rule_code"]}},
        "rows": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "rule_code": {"type": "STRING"}, "term": {"type": "STRING"}, "code": {"type": "STRING"},
            "name": {"type": "STRING"}, "credits": {"type": "STRING"}, "grade": {"type": "STRING"},
            "source_page": {"type": "INTEGER", "nullable": True}, "source_text": {"type": "STRING"}},
            "required": ["rule_code", "term", "code", "name", "credits", "grade", "source_page", "source_text"]}},
        "unreadable": {"type": "STRING"},
    },
    "required": ["rows", "sections"],
}


@dataclass
class Issue:
    level: str  # error：修正前不得確認；warning：須由承辦人確認後才能送出
    message: str
    row: int | None = None  # 第幾列（從 0 開始）；None 表示整份文件


@dataclass
class Extraction:
    rows: list[dict]  # 每列含 FIELDS 與 rule_code、source_page、source_text
    sections: list[dict] = field(default_factory=list)
    declared_required: int | None = None
    declared_earned: int | None = None
    unreadable: str = ""
    model: str = ""
    generated_at: str = ""
    prompt_version: str = PROMPT_VERSION
    format: str = FORMAT
    cohort: int | None = None  # 文件上的入學學年度，承辦人確認後寫入名單
    fallback_from: str = ""  # 改用備援模型時，前面模型失敗的原因


def extract(pdf: bytes, fmt: str = FORMAT) -> Extraction:
    """送 Gemini 擷取；失敗時丟 llm.LLMError，呼叫端應保留手動上傳 CSV 的流程。"""
    if not pdf.startswith(b"%PDF"):
        raise ValueError("不是 PDF 檔案")
    result = llm.generate_json(PROMPT, SCHEMA, [llm.Attachment("application/pdf", pdf)])
    data = result.data if isinstance(result.data, dict) else {}
    mapping = FORMATS[fmt]
    rows = []
    for r in data.get("rows", []):
        row = {k: str(r.get(k) or "").strip() for k in ("rule_code", "term", "code", "name", "credits", "grade",
                                                        "source_text")}
        row["source_page"] = r.get("source_page")
        row["category"] = mapping.get(row["rule_code"], "")
        rows.append(row)
    return Extraction(rows, data.get("sections", []), data.get("declared_required"), data.get("declared_earned"),
                      str(data.get("unreadable") or ""), result.model, result.generated_at, PROMPT_VERSION, fmt,
                      data.get("cohort"), result.fallback_from)


def check(ext: Extraction, kb: KnowledgeBase) -> list[Issue]:
    """格式、重複、來源與文件宣告數字的檢查。通過檢查不代表擷取正確，仍須人工對照原文。"""
    issues: list[Issue] = []
    mapping = FORMATS[ext.format]
    if ext.unreadable:
        issues.append(Issue("error", f"AI 回報文件無法辨識：{ext.unreadable}"))
    if not ext.rows:
        issues.append(Issue("error", "沒有擷取到任何修課紀錄；請確認文件版型，不能當成沒有修課紀錄"))
    seen: dict[tuple[str, str], int] = {}
    for i, r in enumerate(ext.rows):
        def err(msg, level="error"):
            issues.append(Issue(level, msg, i))
        for k, label in (("code", "科目代碼"), ("name", "科目名稱"), ("term", "學期")):
            if not r.get(k):
                err(f"{label}空白")
        if r.get("term") and not TERM_RE.match(r["term"]):
            err(f"學期「{r['term']}」格式應為 學年-學期，例如 114-1")
        if not str(r.get("credits", "")).isdigit():
            err(f"學分「{r.get('credits')}」不是非負整數")
        try:
            if not 0 <= float(r.get("grade", "")) <= 100:
                raise ValueError
        except ValueError:
            err(f"成績「{r.get('grade')}」不是 0–100 的數字")
        if r.get("rule_code") not in mapping:
            err(f"規則代碼「{r.get('rule_code')}」不在支援版型 {ext.format} 的對照表，無法決定類別")
        if (type(r.get("source_page")) is not int or r["source_page"] < 1
                or not isinstance(r.get("source_text"), str) or not r["source_text"].strip()):
            err("缺少來源頁碼或原文，無法對照")
        key = (r.get("code", ""), r.get("term", ""))
        if all(key) and key in seen:
            err(f"與第 {seen[key] + 1} 列的科目代碼與學期重複")
        seen.setdefault(key, i)
        course = kb.courses.get(r.get("code", ""))
        if course and str(r.get("credits")).isdigit() and int(r["credits"]) != course.credits:
            err(f"學分 {r['credits']} 與規則庫 {course.code} 的 {course.credits} 學分不同", "warning")

    def earned(rows):
        return sum(int(r["credits"]) for r in rows if str(r.get("credits")).isdigit()
                   and _grade(r) is not None and _grade(r) >= kb.passing_grade)

    for s in ext.sections:
        declared = s.get("declared_earned")
        got = earned([r for r in ext.rows if r.get("rule_code") == s.get("rule_code")])
        if declared is not None and declared != got:
            issues.append(Issue("warning", f"規則代碼 {s.get('rule_code')} 文件標示實得 {declared} 學分，"
                                           f"擷取的及格紀錄合計 {got} 學分，可能有漏列或多列"))
    if ext.declared_earned is not None and ext.declared_earned != earned(ext.rows):
        issues.append(Issue("warning", f"學分區實得 {ext.declared_earned} 學分，擷取的及格紀錄合計 "
                                       f"{earned(ext.rows)} 學分，可能有漏列或多列"))
    return issues


def _grade(r: dict) -> float | None:
    try:
        return float(r.get("grade", ""))
    except ValueError:
        return None


def to_records_csv(student_id: str, rows: list[dict]) -> str:
    """確認後的紀錄轉成修課紀錄 CSV，交給 records.parse_records 再檢查一次。"""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=REQUIRED + ["category"], lineterminator="\n")
    writer.writeheader()
    for r in rows:
        writer.writerow({"student_id": student_id, **{k: str(r.get(k, "")).strip() for k in FIELDS}})
    return out.getvalue()


# ---------- 量測（見 docs/TEAM_PLAN.md〈實證從第一階段開始〉） ----------
def compare(rows: list[dict], answer: list[dict]) -> dict:
    """以（科目代碼, 學期）對齊，逐欄比對；缺漏列的欄位全部算錯。

    文字欄位先做 NFKC 正規化，全形與半形標點（如「（」與「(」）視為相同；其餘差異都算錯。
    """
    def key(r):
        return r["code"].strip(), r["term"].strip()

    def norm(k, v):
        v = unicodedata.normalize("NFKC", str(v)).strip()
        if k in ("credits", "grade"):
            try:
                return float(v)
            except ValueError:
                return v
        return v

    got = {}
    for r in rows:
        got.setdefault(key(r), r)
    fields = [k for k in FIELDS if k not in ("term", "code")]
    correct = total = 0
    missing = extra = 0
    wrong: Counter[str] = Counter()
    for a in answer:
        total += len(FIELDS)
        r = got.get(key(a))
        if r is None:
            missing += 1
            continue
        correct += 2  # 對齊即代表科目代碼與學期正確
        for k in fields:
            if norm(k, r.get(k, "")) == norm(k, a.get(k, "")):
                correct += 1
            else:
                wrong[k] += 1
    answer_keys = {key(a) for a in answer}
    extra = sum(1 for k in got if k not in answer_keys) + (len(rows) - len(got))
    return {"answer_rows": len(answer), "extracted_rows": len(rows), "missing_rows": missing, "extra_rows": extra,
            "field_correct": correct, "field_total": total,
            "field_accuracy": round(correct / total, 4) if total else None,
            "wrong_by_field": dict(wrong), "fully_correct": missing == 0 and extra == 0 and correct == total}


def log_run(entry: dict, path: Path | None = None) -> None:
    path = path or EVAL_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main(argv: list[str]) -> None:
    from .api import KB_PATH
    from . import knowledge_base

    parser = argparse.ArgumentParser(description="AI 解析畢審儀表板（僅限去識別化或虛構文件）")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--answer", type=Path, help="標準答案 CSV（修課紀錄格式），用於量測")
    parser.add_argument("--case-set", default="demo-v1", help="案例集合版本，寫入量測紀錄")
    args = parser.parse_args(argv)

    kb = knowledge_base.load(KB_PATH)
    try:
        ext = extract(args.pdf.read_bytes())
    except llm.LLMError as e:
        log_run({"type": "extract_failed", "file": args.pdf.name, "case_set": args.case_set,
                 "model": llm.model_name(), "prompt_version": PROMPT_VERSION,
                 "at": datetime.now().isoformat(timespec="seconds"), "error": str(e)[:200]})
        sys.exit(f"AI 解析失敗（已記錄）：{e}")
    issues = check(ext, kb)
    print(f"模型 {ext.model}（{ext.generated_at}，提示 {ext.prompt_version}，版型 {ext.format}）")
    if ext.fallback_from:
        print(f"已改用備援模型：{ext.fallback_from}")
    print(f"擷取 {len(ext.rows)} 列；錯誤 {sum(i.level == 'error' for i in issues)}、"
          f"警示 {sum(i.level == 'warning' for i in issues)}")
    for i in issues:
        where = f"第 {i.row + 1} 列" if i.row is not None else "整份"
        print(f"  [{i.level}] {where}：{i.message}")
    entry = {"type": "extract", "file": args.pdf.name, "case_set": args.case_set, "model": ext.model,
             "prompt_version": ext.prompt_version, "format": ext.format, "generated_at": ext.generated_at,
             "fallback_from": ext.fallback_from[:200], "errors": sum(i.level == "error" for i in issues), "warnings": sum(i.level == "warning" for i in issues)}
    if args.answer:
        answer = list(csv.DictReader(args.answer.read_text(encoding="utf-8-sig").splitlines()))
        result = compare(ext.rows, answer)
        entry["before_correction"] = result
        print("與標準答案比對（人工修正前）：" + json.dumps(result, ensure_ascii=False))
    log_run(entry)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main(sys.argv[1:])
