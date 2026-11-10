"""AI 解析畢審儀表板：PDF → 待確認紀錄 → 檢查 → 承辦人確認後才成為審查輸入。（A 組）

AI 只抽出文件上的原始欄位，並逐列附來源頁碼與原文；規則代碼對應到類別用固定對照表，
不讓 AI 推測。所有結果都標示待確認，檢查出的錯誤未修正前不得送入審查
（見 docs/TEAM_PLAN.md〈AI 輸入與啟用驗收〉）。

支援虛構示範版型（demo-v1，academic_twin/demo_dashboard.py 產生）與本校校務系統「畢業審核紀錄表」
（ncu-v1，2026-11-10 以一份真實儀表板確認），依規則代碼樣式自動判斷。校務系統的規則代碼對照依段落標題
推定，正式意義待承辦人確認。不支援掃描影像，無法辨識時須顯示失敗，不能當成沒有修課紀錄。

    python -m academic_twin.dashboard data/samples/dashboard_demo_d01.pdf \\
        --answer data/samples/dashboard_demo_d01_answer.csv     # 解析並與標準答案比對
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import threading
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import llm, pdf_text, replay
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
    # 本校校務系統「畢業審核紀錄表」：2026-11-10 以一份組員本人同意提供的真實儀表板確認版面。
    # 代碼依段落標題對應到規則庫類別，正式意義待承辦人確認；沒列出的代碼（例如 18301 軍訓、
    # 19xxx 特殊檢核）若出現修課列仍報錯，不猜類別。
    "ncu-v1": {"11201": "外文", "11202": "外文", "11321": "人文與思想", "11322": "自然科學",
               "11323": "應用科學", "11324": "社會思潮與現象", "11325": "通識", "11401": "國文",
               "12101": "", "13101": "", "13102": "", "13103": "", "14101": "",
               "18101": "", "18201": "體育", "18202": "體育", "18401": "服務學習"},
}
# 規則庫沒有對應要求、不列入審查的段落（確認時略過，並以整份警示告知承辦人）
NOT_REVIEWED = {"ncu-v1": {"18101": "操行"}}
# 非數字成績（只在支援的版型出現）：通過制課程以及格計入；尚無成績的修課中課程不列入這次審查
PASS, PENDING = "通過", "未到"
TEXT_GRADES = {"ncu-v1": {PASS, PENDING}}
NCU_TERM = re.compile(r"^(\d{2,3})([12])$")  # 1131 → 113-1

PROMPT = """你是資料擷取工具。附件是一份大學「畢業資格審查儀表板」PDF。
請逐列抽出文件上「每一筆修課紀錄」，不要推論、不要補寫、不要合併，只抄錄文件上實際出現的內容：
- rule_code：該紀錄所在段落標題的規則代碼（例如 A01）
- term、code、name、credits、grade：照文件原樣抄錄
- source_page：該列所在頁碼（從 1 開始）
- source_text：該列在文件上的原文（整列文字）
小計、標題、學分區等不是修課紀錄的列不要輸出。跨頁的表格要完整列出每一頁的列。
另外抄錄文件上宣告的數字：入學學年度、學分區的應修與實得學分，以及每個規則代碼段落標示的實得學分。
文件看不清楚或無法辨識時，在 unreadable 說明原因，不要猜。"""

# 有界自我修正：只回饋檢查理由（不加入學號或其他新資料），重新擷取 1 次；之後走同一道檢查。
RETRY_VERSION = "dashboard-retry-v1"
RETRY = """

上一次擷取沒有通過程式檢查，問題如下（第幾列指上一次輸出的順序）：
{errors}
請重新對照文件，從頭完整輸出一次。只修正抄錯、漏抄或格式問題；
如果文件上確實就是這樣寫，照原樣抄錄，不要為了通過檢查而改寫或補值。"""

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
    # 從 PDF 取出的逐頁文字，供 check 核對 AI 宣稱的原文與頁碼；None 表示未要求核對（舊行為）
    pages: pdf_text.PdfText | None = field(default=None, repr=False)
    replay: dict | None = None  # 評審模式重播時為 {model, generated_at, reason}；即時呼叫為 None


PAGE = re.compile(rb"/Type\s*/Page(?![A-Za-z])")
DEFAULT_MAX_PAGES = 10  # 虛構儀表板 2–4 頁；真實版型確認後再調整


def page_count(pdf: bytes) -> int:
    """以頁面物件計數（不需完整解析 PDF）；用於費用上限，不用於判斷內容。"""
    return len(PAGE.findall(pdf))


def max_pages() -> int:
    raw = os.environ.get("ACADEMIC_TWIN_MAX_PDF_PAGES", "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_MAX_PAGES
    except ValueError:
        return DEFAULT_MAX_PAGES


def extract(pdf: bytes, fmt: str = FORMAT, model: str | None = None, read_text: bool = False,
            feedback: list[str] | None = None) -> Extraction:
    """送 Gemini 擷取；失敗時丟 llm.LLMError，呼叫端應保留手動上傳 CSV 的流程。

    指定 model 時只用該模型、不改用備援（交叉驗證的第二模型用）。
    read_text=True 時另從 PDF 取出逐頁文字（不送給 AI），check 會據以核對每列原文與頁碼。
    feedback 為上一次的檢查理由（自我修正用），附在提示後面請模型重新擷取。
    """
    if not pdf.startswith(b"%PDF"):
        raise ValueError("不是 PDF 檔案")
    count, limit = page_count(pdf), max_pages()
    if count > limit:  # 費用控制：PDF 整份送模型，頁數越多 token 越多；異常大的檔案不送
        raise ValueError(f"PDF 有 {count} 頁，超過 AI 解析上限 {limit} 頁；請確認是否為畢審儀表板，或改由人工輸入")
    pages = pdf_text.extract_pages(pdf) if read_text else None
    prompt = PROMPT + RETRY.format(errors="\n".join(f"- {m}" for m in feedback)) if feedback else PROMPT
    result = replay.generate_json(prompt, SCHEMA, [llm.Attachment("application/pdf", pdf)], model=model,
                                  purpose="dashboard")
    data = result.data if isinstance(result.data, dict) else {}
    rows = []
    for r in data.get("rows", []):
        row = {k: str(r.get(k) or "").strip() for k in ("rule_code", "term", "code", "name", "credits", "grade",
                                                        "source_text")}
        row["source_page"] = r.get("source_page")
        rows.append(row)
    if fmt == FORMAT:
        fmt = detect_format(rows)
    mapping = FORMATS[fmt]
    for row in rows:
        if fmt == "ncu-v1":
            _normalize_ncu(row)
        row["category"] = mapping.get(row["rule_code"], "")
    return Extraction(rows, data.get("sections", []), data.get("declared_required"), data.get("declared_earned"),
                      str(data.get("unreadable") or ""), result.model, result.generated_at, PROMPT_VERSION, fmt,
                      data.get("cohort"), result.fallback_from, pages, getattr(result, "replay", None))


def detect_format(rows: list[dict]) -> str:
    """依 AI 抄回的規則代碼判斷版型：全部是 5 位數字時為校務系統版型，否則沿用虛構示範版型。
    判斷只看代碼樣式，不讓 AI 決定；兩種都不符時照示範版型檢查，錯誤會照常列出。"""
    codes = [r.get("rule_code", "") for r in rows]
    return "ncu-v1" if codes and all(re.fullmatch(r"\d{5}", c) for c in codes) else FORMAT


def _normalize_ncu(row: dict) -> None:
    """校務系統版型的寫法換成審查用格式，原文仍保留在 source_text：學期 1131 → 113-1；
    「勞動服務通過(Pass)」→ 通過；「未到(Not Entered)」→ 未到。其他寫法不動，交給 check 報錯。"""
    m = NCU_TERM.match(row["term"])
    if m:
        row["term"] = f"{int(m[1])}-{m[2]}"
    grade = re.sub(r"\s+", "", unicodedata.normalize("NFKC", row["grade"]))
    if "未到" in grade or "NotEntered" in grade:
        row["grade"] = PENDING
    elif "未通過" not in grade and ("通過" in grade or "Pass" in grade):
        row["grade"] = PASS


def review_rows(fmt: str, rows: list[dict]) -> list[dict]:
    """確認後要送入審查的列：略過尚無成績的修課中課程與規則庫沒有對應要求的段落（如操行）。"""
    skip = NOT_REVIEWED.get(fmt, {})
    return [r for r in rows if r.get("grade") != PENDING and r.get("rule_code") not in skip]


def check(ext: Extraction, kb: KnowledgeBase, pages: pdf_text.PdfText | None = None) -> list[Issue]:
    """格式、重複、來源與文件宣告數字的檢查。通過檢查不代表擷取正確，仍須人工對照原文。

    有 PDF 逐頁文字時（pages 參數，未給則用 ext.pages）另核對原文與頁碼，結果為警示，見 check_sources。
    """
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
            if r.get("grade") not in TEXT_GRADES.get(ext.format, ()) and not 0 <= float(r.get("grade", "")) <= 100:
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
                   and (r.get("grade") == PASS or (_grade(r) is not None and _grade(r) >= kb.passing_grade)))

    def in_section(r, code):
        # 校務系統版型的代碼有層級（11000 ⊃ 11200 ⊃ 11201），段落實得含所有下層代碼的列
        if ext.format == "ncu-v1":
            return str(r.get("rule_code", "")).startswith(code.rstrip("0"))
        return r.get("rule_code") == code

    for s in ext.sections:
        declared, code = s.get("declared_earned"), str(s.get("rule_code") or "")
        if ext.format == "ncu-v1" and (code.startswith("19") or code == "10000"):
            continue  # 19xxx 特殊檢核是推算的條件、10000 是全系總計（另與學分區比對），都不是修課列的合計
        got = earned([r for r in ext.rows if code and in_section(r, code)])
        if declared is not None and declared != got:
            issues.append(Issue("warning", f"規則代碼 {s.get('rule_code')} 文件標示實得 {declared} 學分，"
                                           f"擷取的及格紀錄合計 {got} 學分，可能有漏列或多列"))
    if ext.declared_earned is not None and ext.declared_earned != earned(ext.rows):
        issues.append(Issue("warning", f"學分區實得 {ext.declared_earned} 學分，擷取的及格紀錄合計 "
                                       f"{earned(ext.rows)} 學分，可能有漏列或多列"))
    pending = [r for r in ext.rows if r.get("grade") == PENDING]
    if pending:
        issues.append(Issue("warning", f"{len(pending)} 列尚無成績（修課中），不列入這次審查："
                                       + "、".join(r.get("code", "") for r in pending)))
    for code, label in NOT_REVIEWED.get(ext.format, {}).items():
        n = sum(r.get("rule_code") == code for r in ext.rows)
        if n:
            issues.append(Issue("warning", f"{n} 列為{label}（規則代碼 {code}），規則庫沒有這項要求，不列入審查"))
    text = pages if pages is not None else ext.pages
    if text is not None:
        issues += check_sources(ext.rows, text)
    return issues


def check_sources(rows: list[dict], text: pdf_text.PdfText) -> list[Issue]:
    """到 PDF 文字裡核對 AI 宣稱的原文與頁碼，並檢查課號、學分、成績是否出現在該列原文。

    全部是警示（承辦人須對照文件確認），不是錯誤：取出的文字是近似，找不到不等於一定錯。
    PDF 取不出文字時只列一則整份警示，不當成原文不符。
    """
    issues: list[Issue] = []
    if not text.ok:
        issues.append(Issue("warning", f"無法自動核對原文：{text.reason}；請逐列對照文件"))
    for i, r in enumerate(rows):
        source, page = r.get("source_text"), r.get("source_page")
        if not isinstance(source, str) or not source.strip() or type(page) is not int or page < 1:
            continue  # 缺來源已是錯誤
        if text.ok:
            message = pdf_text.describe(pdf_text.locate(text, source, page), page)
            if message:
                issues.append(Issue("warning", message, i))
        missing = _fields_not_in_source(r, source)
        if missing:
            issues.append(Issue("warning", f"此列原文中找不到{'、'.join(missing)}，擷取值可能與原文不符", i))
    return issues


def _fields_not_in_source(r: dict, source: str) -> list[str]:
    """課號以正規化後的子字串比對；學分、成績比對原文中出現的數字（數值相同即可，如 80 與 80.0）。"""
    out = []
    code = pdf_text.normalize(r.get("code", ""))
    if code and code not in pdf_text.normalize(source):
        out.append(f"科目代碼「{r.get('code')}」")
    numbers = {float(n) for n in re.findall(r"\d+(?:\.\d+)?", unicodedata.normalize("NFKC", source))}
    glued = _glued_match(r, source)
    for k, label in (("credits", "學分"), ("grade", "成績")):
        value = str(r.get(k, "")).strip()
        try:
            if float(value) not in numbers and not glued:
                out.append(f"{label}「{value}」")
        except ValueError:
            continue  # 非數字已是錯誤
    return out


def _glued_match(r: dict, source: str) -> bool:
    """有些模型抄原文時把欄位黏在一起（如「計算機概論I389」，2026-11-06 實測備援模型），學分與成績就不是
    獨立的數字。此時改看課名之後的文字：去掉空白後必須恰好是「學分」接「成績」，前後只能有非數字，
    學期與課號裡的數字不算數。讀錯的值（例如把 89 讀成 8 或 38）不會對上。找不到課名時不採用。"""
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", source))
    name = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(r.get("name", ""))))
    at = text.rfind(name) if name else -1
    if at < 0:
        return False
    tail = text[at + len(name):]
    credits, grade = str(r.get("credits", "")).strip(), str(r.get("grade", "")).strip()
    try:
        forms = {(c, g) for c in {credits, f"{float(credits):g}"} for g in {grade, f"{float(grade):g}"}}
    except ValueError:
        return False
    return any(re.fullmatch(rf"\D*{re.escape(c)}\D*{re.escape(g)}\D*", tail) for c, g in forms)


def _grade(r: dict) -> float | None:
    try:
        return float(r.get("grade", ""))
    except ValueError:
        return None


def to_records_csv(student_id: str, rows: list[dict], passing_grade: float = 60) -> str:
    """確認後的紀錄轉成修課紀錄 CSV，交給 records.parse_records 再檢查一次。
    通過制成績（通過）以及格分數寫入；尚無成績等不審查的列由呼叫端先以 review_rows 略過。"""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=REQUIRED + ["category"], lineterminator="\n")
    writer.writeheader()
    for r in rows:
        row = {k: str(r.get(k, "")).strip() for k in FIELDS}
        if row["grade"] == PASS:
            row["grade"] = f"{passing_grade:g}"
        writer.writerow({"student_id": student_id, **row})
    return out.getvalue()


# ---------- 量測（見 docs/TEAM_PLAN.md〈實證從第一階段開始〉） ----------
def compare(rows: list[dict], answer: list[dict]) -> dict:
    """以（科目代碼, 學期）對齊，逐欄比對；缺漏列的欄位全部算錯。

    文字欄位先做 NFKC 正規化，全形與半形標點（如「（」與「(」）視為相同；其餘差異都算錯。
    """
    def key(r):
        return r["code"].strip(), r["term"].strip()

    norm = normalize
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


def normalize(k: str, v) -> object:
    """比對用：NFKC 正規化（全形與半形標點視為相同）；學分與成績比數值。"""
    v = unicodedata.normalize("NFKC", str(v)).strip()
    if k in ("credits", "grade"):
        try:
            return float(v)
        except ValueError:
            return v
    return v


_LOG_LOCK = threading.Lock()


def log_run(entry: dict, path: Path | None = None) -> None:
    """追加一行執行紀錄；批次解析與收件匣在多個執行緒同時寫，需加鎖，否則可能少一行。"""
    path = path or EVAL_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with _LOG_LOCK, path.open("a", encoding="utf-8") as f:
        f.write(line)


def main(argv: list[str]) -> None:
    from .api import KB_PATH
    from . import knowledge_base

    parser = argparse.ArgumentParser(description="AI 解析畢審儀表板（僅限去識別化或虛構文件）")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--answer", type=Path, help="標準答案 CSV（修課紀錄格式），用於量測")
    parser.add_argument("--case-set", default="demo-v1", help="案例集合版本，寫入量測紀錄")
    args = parser.parse_args(argv)

    replay.disable()  # 量測一律即時呼叫，不用錄製結果
    kb = knowledge_base.load(KB_PATH)
    try:
        ext = extract(args.pdf.read_bytes(), read_text=True)
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
             "fallback_from": ext.fallback_from[:200], "replay": bool(ext.replay), "errors": sum(i.level == "error" for i in issues), "warnings": sum(i.level == "warning" for i in issues)}
    if args.answer:
        answer = list(csv.DictReader(args.answer.read_text(encoding="utf-8-sig").splitlines()))
        result = compare(ext.rows, answer)
        entry["before_correction"] = result
        print("與標準答案比對（人工修正前）：" + json.dumps(result, ensure_ascii=False))
    log_run(entry)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main(sys.argv[1:])
