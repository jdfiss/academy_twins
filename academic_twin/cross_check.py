"""雙模型交叉驗證：同一份儀表板由兩個模型各自擷取，逐列比對，不一致處交給承辦人優先核對。（A 組）

兩個模型一致不代表正確（可能犯同樣的錯），所以只用來排出人工核對的重點，
所有列仍須承辦人確認（見 docs/TEAM_PLAN.md〈AI 輸入與啟用驗收〉）。
第二模型失敗或與主模型相同時，照常使用主模型結果，並標示「未交叉驗證」，不中斷流程。

第二模型由環境變數 GEMINI_CHECK_MODEL 指定，預設 DEFAULT_CHECK_MODEL。
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import dashboard, llm

DEFAULT_CHECK_MODEL = "gemini-2.5-pro"
COMPARED = ["rule_code", "name", "credits", "grade"]  # 對齊用的科目代碼、學期不再比
LABELS = {"rule_code": "規則代碼", "name": "科目名稱", "credits": "學分", "grade": "成績"}


def check_model() -> str:
    return os.environ.get("GEMINI_CHECK_MODEL", "").strip() or DEFAULT_CHECK_MODEL


@dataclass
class Comparison:
    """以主模型的列為準記錄差異；row 為主模型（或承辦人修正後）的列索引。"""
    primary_model: str
    check_model: str
    status: str  # ok：已交叉驗證；unavailable：第二模型失敗；same_model：兩者相同，無從比對
    note: str = ""
    differences: list[dict] = field(default_factory=list)  # {row, field, primary, check}
    only_primary: list[int] = field(default_factory=list)  # 第二模型沒有的列（可能多列）
    only_check: list[dict] = field(default_factory=list)  # 第二模型多出的列（可能漏列）

    @property
    def flagged_rows(self) -> set[int]:
        return {d["row"] for d in self.differences} | set(self.only_primary)

    def summary(self, total_rows: int) -> dict:
        return {"status": self.status, "primary_model": self.primary_model, "check_model": self.check_model,
                "note": self.note, "rows": total_rows, "flagged_rows": len(self.flagged_rows),
                "agreed_rows": total_rows - len(self.flagged_rows) if self.status == "ok" else 0,
                "missing_in_primary": len(self.only_check)}


def _key(r: dict) -> tuple[str, str]:
    return (str(dashboard.normalize("code", r.get("code", ""))).upper(),
            str(dashboard.normalize("term", r.get("term", ""))))


def compare(rows: list[dict], check_rows: list[dict], primary_model: str, check_model_name: str) -> Comparison:
    """以（科目代碼, 學期）對齊兩組列；同鍵多列時依出現順序配對。"""
    cmp = Comparison(primary_model, check_model_name, "ok")
    pool: dict[tuple, list[dict]] = {}
    for r in check_rows:
        pool.setdefault(_key(r), []).append(r)
    for i, r in enumerate(rows):
        match = pool.get(_key(r))
        if not match:
            cmp.only_primary.append(i)
            continue
        other = match.pop(0)
        for f in COMPARED:
            if dashboard.normalize(f, r.get(f, "")) != dashboard.normalize(f, other.get(f, "")):
                cmp.differences.append({"row": i, "field": f, "primary": str(r.get(f, "")),
                                        "check": str(other.get(f, ""))})
    cmp.only_check = [r for rest in pool.values() for r in rest]
    return cmp


def extract_pair(pdf: bytes, fmt: str = dashboard.FORMAT) -> tuple[dashboard.Extraction, dashboard.Extraction | str]:
    """兩個模型同時擷取，回傳主模型結果與第二模型結果（失敗時為原因）。

    主模型失敗時丟出錯誤（與單一模型相同）；第二模型失敗不中斷，只標示未交叉驗證。
    """
    with ThreadPoolExecutor(max_workers=2) as pool:
        primary = pool.submit(dashboard.extract, pdf, fmt)
        checker = pool.submit(dashboard.extract, pdf, fmt, check_model())
        ext = primary.result()
        try:
            return ext, checker.result()
        except llm.LLMError as e:
            return ext, f"第二模型擷取失敗：{e}"[:300]


def recheck(rows: list[dict], other: dashboard.Extraction | str, primary_model: str) -> Comparison:
    """主模型（或承辦人修正後）的列與第二模型比對；承辦人改成與第二模型一致的地方就不再提示。"""
    if isinstance(other, str):
        return Comparison(primary_model, check_model(), "unavailable", other)
    if other.model == primary_model:
        return Comparison(primary_model, other.model, "same_model",
                          "主模型改用備援後與第二模型相同，無法交叉驗證")
    return compare(rows, other.rows, primary_model, other.model)


def issues(cmp: Comparison) -> list[dashboard.Issue]:
    """不一致處列為警示：送出前須由承辦人對照原文並勾選已確認。"""
    if cmp.status != "ok":
        return [dashboard.Issue("warning", f"未交叉驗證：{cmp.note}；請逐列對照原文")]
    out = []
    for d in cmp.differences:
        out.append(dashboard.Issue("warning", f"兩個模型不一致：{LABELS[d['field']]}，主模型讀到「{d['primary']}」，"
                                              f"{cmp.check_model} 讀到「{d['check']}」，請對照原文", d["row"]))
    for i in cmp.only_primary:
        out.append(dashboard.Issue("warning", f"{cmp.check_model} 沒有擷取到這一列，可能是多列，請對照原文", i))
    if cmp.only_check:
        listed = "、".join(f"{r.get('code', '')}（{r.get('term', '')}）" for r in cmp.only_check[:5])
        more = f"等 {len(cmp.only_check)} 列" if len(cmp.only_check) > 5 else ""
        out.append(dashboard.Issue("warning", f"{cmp.check_model} 多擷取到 {listed}{more}，主模型可能漏列，請對照原文"))
    return out
