"""虛構儀表板基準集量測：主模型、第二模型與交叉驗證的準確度。（A 組）

對 data/samples/bench/ 的每份虛構案例執行 cross_check.extract_pair N 次，與標準答案比對：
- 主模型與第二模型各自的逐欄正確率、漏列、多列、整份全對件數（比對方式同 dashboard.compare）；
- 交叉驗證：被標示的列數、實際錯誤列中被標示的比例（抓錯率）、兩模型一致列的錯誤率、
  需人工看的列占比，以及主模型漏列由第二模型補出的比例；
- 依情境標籤與開發／驗證分組。

輸出只有彙總數字（不含課程內容）：data/eval/benchmark_<時間>.json 與可貼進報告的 .md（data/eval/ 不進版控）。
案例是本專案自行產生的虛構版型，量測結果只能說明在虛構版面上的表現，不代表真實儀表板的準確率
（見 docs/TEAM_PLAN.md〈實證從第一階段開始〉）。兩模型一致不代表正確，所有列仍須承辦人確認。

    python -m academic_twin.benchmark --runs 3                 # 需要 GEMINI_API_KEY
    python -m academic_twin.benchmark --runs 1 --case bench_01 --case bench_12
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import cross_check, dashboard, llm

ROOT = Path(__file__).parent.parent
CASES = ROOT / "data" / "samples" / "bench"
OUT = ROOT / "data" / "eval"
EVAL_VERSION = "benchmark-v1"
DISCLAIMER = ("虛構案例：版面與課程由本專案自行產生（demo_dashboard --bench），只能說明 AI 在虛構版型上的表現，"
              "不代表真實儀表板的準確率或行政效益。兩模型一致不代表正確，所有列仍須承辦人確認。")
COMPARED = ["name", "credits", "grade", "category"]  # 同 dashboard.compare：對齊後再比的欄位


# ---------- 案例 ----------
def load_cases(folder: Path = CASES, only: list[str] | None = None) -> list[dict]:
    cases = []
    for meta_path in sorted(folder.glob("*.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if only and meta["case_id"] not in only:
            continue
        answer = folder / f"{meta['case_id']}_answer.csv"
        cases.append({"meta": meta, "pdf": folder / f"{meta['case_id']}.pdf",
                      "answer": list(csv.DictReader(answer.read_text(encoding="utf-8-sig").splitlines()))})
    if only and len(cases) != len(set(only)):
        found = {c["meta"]["case_id"] for c in cases}
        raise ValueError(f"找不到案例：{'、'.join(sorted(set(only) - found))}")
    return cases


# ---------- 單次執行的評分 ----------
def _key(r: dict) -> tuple[str, str]:
    return str(r.get("code", "")).strip(), str(r.get("term", "")).strip()


def row_errors(rows: list[dict], answer: list[dict]) -> list[bool]:
    """主模型每一列是否有錯：答案沒有的列、重複列、或任一比對欄位與答案不同。

    對齊方式與 dashboard.compare 相同（科目代碼, 學期），讓列層級的錯誤與欄位正確率一致。
    規則代碼不在答案 CSV 內，只透過對應的類別比對；A01、A02、B01、B02 之間讀錯不改變類別
    （由規則庫依課號判斷），不算錯誤列，但交叉驗證仍會標示，會拉低「被標示列中確實有錯的比例」。
    """
    truth = {_key(a): a for a in answer}
    seen, out = set(), []
    for r in rows:
        k = _key(r)
        a = truth.get(k)
        if a is None or k in seen:
            out.append(True)
        else:
            out.append(any(dashboard.normalize(f, r.get(f, "")) != dashboard.normalize(f, a.get(f, ""))
                           for f in COMPARED))
        seen.add(k)
    return out


def score_run(ext: dashboard.Extraction, other: dashboard.Extraction | str, answer: list[dict]) -> dict:
    """一次 extract_pair 的結果對標準答案評分；只回傳數字。"""
    cmp = cross_check.recheck(ext.rows, other, ext.model)
    result = {"primary_model": ext.model, "fallback": bool(ext.fallback_from),
              "primary": dashboard.compare(ext.rows, answer), "check_model": cmp.check_model,
              "check": dashboard.compare(other.rows, answer) if isinstance(other, dashboard.Extraction) else None,
              "cross_status": cmp.status, "cross": None}
    if cmp.status == "ok":
        errors = row_errors(ext.rows, answer)
        flagged = cmp.flagged_rows
        missing = {_key(a) for a in answer} - {_key(r) for r in ext.rows}
        supplied = {_key(r) for r in cmp.only_check}
        result["cross"] = {
            "rows": len(ext.rows), "flagged": len(flagged),
            "error_rows": sum(errors), "error_flagged": sum(e for i, e in enumerate(errors) if i in flagged),
            "agreed": len(ext.rows) - len(flagged),
            "agreed_errors": sum(e for i, e in enumerate(errors) if i not in flagged),
            "missing_rows": len(missing), "missing_caught": len(missing & supplied),
            "only_check": len(cmp.only_check)}
    return result


# ---------- 彙總 ----------
_MODEL_SUMS = ["answer_rows", "extracted_rows", "missing_rows", "extra_rows", "field_correct", "field_total"]
_CROSS_SUMS = ["rows", "flagged", "error_rows", "error_flagged", "agreed", "agreed_errors", "missing_rows",
               "missing_caught", "only_check"]


class Tally:
    """一組執行結果的加總（micro：先加總分子分母再算比例）。"""

    def __init__(self):
        self.cases: set[str] = set()
        self.runs = self.primary_failed = self.check_failed = 0
        self.cross_runs = Counter()  # ok / unavailable / same_model
        self.models = {"primary": Counter(), "check": Counter()}
        self.fully = {"primary": 0, "check": 0}
        self.ok_runs = {"primary": 0, "check": 0}
        self.sums = {"primary": Counter(), "check": Counter()}
        self.wrong = {"primary": Counter(), "check": Counter()}
        self.cross = Counter()
        self.unchecked_rows = 0  # 未交叉驗證的執行：每列都要逐列看

    def add(self, case_id: str, score: dict | None) -> None:
        self.cases.add(case_id)
        self.runs += 1
        if score is None:
            self.primary_failed += 1
            return
        for side in ("primary", "check"):
            s = score[side]
            if s is None:
                continue
            self.ok_runs[side] += 1
            self.fully[side] += bool(s["fully_correct"])
            self.sums[side].update({k: s[k] for k in _MODEL_SUMS})
            self.wrong[side].update(s["wrong_by_field"])
        self.models["primary"][score["primary_model"]] += 1
        if score["check"] is None:
            self.check_failed += 1
        else:
            self.models["check"][score["check_model"]] += 1
        self.cross_runs[score["cross_status"]] += 1
        if score["cross"]:
            self.cross.update(score["cross"])
        else:
            self.unchecked_rows += score["primary"]["extracted_rows"]

    def summary(self) -> dict:
        def model(side):
            s = self.sums[side]
            return {"runs": self.ok_runs[side], **{k: s[k] for k in _MODEL_SUMS},
                    "field_accuracy": _rate(s["field_correct"], s["field_total"]),
                    "fully_correct_docs": self.fully[side], "wrong_by_field": dict(self.wrong[side]),
                    "models": dict(self.models[side])}
        c = self.cross
        return {
            "cases": len(self.cases), "runs": self.runs, "primary_failed": self.primary_failed,
            "check_failed": self.check_failed, "primary": model("primary"), "check": model("check"),
            "cross_check": {
                "status_runs": dict(self.cross_runs), **{k: c[k] for k in _CROSS_SUMS},
                "catch_rate": _rate(c["error_flagged"], c["error_rows"]),
                "catch_rate_with_missing": _rate(c["error_flagged"] + c["missing_caught"],
                                                 c["error_rows"] + c["missing_rows"]),
                "agreed_error_rate": _rate(c["agreed_errors"], c["agreed"]),
                "review_share": _rate(c["flagged"], c["rows"]),
                "flag_precision": _rate(c["error_flagged"], c["flagged"]),
                "missing_caught_rate": _rate(c["missing_caught"], c["missing_rows"]),
                "unchecked_rows": self.unchecked_rows,
            },
        }


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def aggregate(results: list[tuple[dict, int, dict | None]]) -> dict:
    """results：（案例中繼資料, 第幾次執行, 評分或 None 表示主模型失敗）。"""
    groups: dict[str, Tally] = {}

    def tally(name):
        return groups.setdefault(name, Tally())

    for meta, run, score in results:
        names = ["all", f"split:{meta['split']}", f"run:{run}", f"case:{meta['case_id']}"]
        names += [f"tag:{t}" for t in meta["tags"]]
        for name in names:
            tally(name).add(meta["case_id"], score)
    out = {"overall": tally("all").summary(), "by_tag": {}, "by_split": {}, "by_run": {}, "by_case": {}}
    for name, t in sorted(groups.items()):
        kind, _, label = name.partition(":")
        if label:
            out[{"tag": "by_tag", "split": "by_split", "run": "by_run", "case": "by_case"}[kind]][label] = t.summary()
    return out


# ---------- 執行 ----------
def run_case(case: dict) -> dict | None:
    """執行一次 extract_pair 並評分；主模型失敗回傳 None（計入失敗次數，不中斷整批）。"""
    try:
        ext, other = cross_check.extract_pair(case["pdf"].read_bytes())
    except llm.LLMError as e:
        print(f"  {case['meta']['case_id']} 主模型失敗：{str(e)[:120]}", file=sys.stderr)
        return None
    return score_run(ext, other, case["answer"])


def run(cases: list[dict], runs: int, workers: int = 1) -> dict:
    started = datetime.now().isoformat(timespec="seconds")
    tasks = [(case, r) for r in range(1, runs + 1) for case in cases]

    def one(task):
        case, r = task
        score = run_case(case)
        acc = score["primary"]["field_accuracy"] if score else None
        print(f"  第 {r} 次 {case['meta']['case_id']}：主模型欄位正確率 {acc}", file=sys.stderr)
        return case["meta"], r, score

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(one, tasks))
    metas = [c["meta"] for c in cases]
    return {
        "eval_version": EVAL_VERSION, "disclaimer": DISCLAIMER,
        "case_set": sorted({m["case_set"] for m in metas}), "seeds": sorted({m["seed"] for m in metas}),
        "cases": len(cases), "runs_per_case": runs,
        "case_tags": dict(Counter(t for m in metas for t in m["tags"])),
        "case_splits": dict(Counter(m["split"] for m in metas)),
        "rows_per_case": {"min": min((m["rows"] for m in metas), default=0),
                          "max": max((m["rows"] for m in metas), default=0)},
        "prompt_version": dashboard.PROMPT_VERSION, "format": dashboard.FORMAT,
        "configured_models": {"primary": llm.model_name(), "fallbacks": llm.fallback_models(),
                              "check": cross_check.check_model()},
        "started_at": started, "finished_at": datetime.now().isoformat(timespec="seconds"),
        **aggregate(results),
    }


# ---------- 報告 ----------
def _pct(rate: float | None, num: int | None = None, den: int | None = None) -> str:
    if rate is None:
        return "—"
    text = f"{rate * 100:.2f}%" if 0 < rate < 0.01 else f"{rate * 100:.1f}%"  # 很小的比例不要顯示成 0.0%
    return f"{text}（{num}／{den}）" if num is not None else text


def _row_model(name: str, m: dict) -> str:
    return (f"| {name} | {m['runs']} | {_pct(m['field_accuracy'], m['field_correct'], m['field_total'])} | "
            f"{m['missing_rows']} | {m['extra_rows']} | {m['fully_correct_docs']}／{m['runs']} |")


def _row_group(label: str, s: dict) -> str:
    p, c = s["primary"], s["cross_check"]
    return (f"| {label} | {s['cases']} | {s['runs']} | {_pct(p['field_accuracy'])} | "
            f"{p['missing_rows']}／{p['extra_rows']} | {_pct(c['catch_rate'], c['error_flagged'], c['error_rows'])} | "
            f"{_pct(c['agreed_error_rate'], c['agreed_errors'], c['agreed'])} | {_pct(c['review_share'])} |")


def markdown(report: dict) -> str:
    from .demo_dashboard import TAGS

    o = report["overall"]
    cc = o["cross_check"]
    models = report["configured_models"]
    used = "、".join(f"{k} {v} 次" for k, v in o["primary"]["models"].items()) or "—"
    used_check = "、".join(f"{k} {v} 次" for k, v in o["check"]["models"].items()) or "—"
    lines = [
        f"### AI 解析儀表板基準量測（{report['started_at'][:10]}）",
        "",
        f"> {report['disclaimer']}",
        "",
        f"- 案例：{report['cases']} 份虛構案例（{'、'.join(report['case_set'])}，種子 "
        f"{'、'.join(map(str, report['seeds']))}；每份 {report['rows_per_case']['min']}～"
        f"{report['rows_per_case']['max']} 列），每份執行 {report['runs_per_case']} 次，共 {o['runs']} 次",
        f"- 模型：主模型設定 {models['primary']}（實際：{used}），第二模型 {models['check']}（實際：{used_check}）；"
        f"提示 {report['prompt_version']}、版型 {report['format']}",
        f"- 期間：{report['started_at']} 至 {report['finished_at']}；主模型失敗 {o['primary_failed']} 次、"
        f"第二模型失敗 {o['check_failed']} 次（失敗的執行不計入正確率，另列於此）",
        "",
        "| 模型 | 成功執行 | 欄位正確率 | 漏列 | 多列 | 整份全對 |",
        "|---|---|---|---|---|---|",
        _row_model("主模型", o["primary"]),
        _row_model("第二模型", o["check"]),
        "",
        "交叉驗證（只計兩個模型都成功且不同模型的執行；"
        f"未交叉驗證 {sum(v for k, v in cc['status_runs'].items() if k != 'ok')} 次，其 {cc['unchecked_rows']} 列全數需逐列核對）：",
        "",
        "| 指標 | 數值 |",
        "|---|---|",
        f"| 主模型擷取列數 | {cc['rows']} |",
        f"| 被標示（需優先核對）列數 | {cc['flagged']}（占 {_pct(cc['review_share'])}） |",
        f"| 實際錯誤列 | {cc['error_rows']} |",
        f"| 抓錯率：錯誤列中被標示的比例 | {_pct(cc['catch_rate'], cc['error_flagged'], cc['error_rows'])} |",
        f"| 兩模型一致列的錯誤率 | {_pct(cc['agreed_error_rate'], cc['agreed_errors'], cc['agreed'])} |",
        f"| 被標示列中確實有錯的比例 | {_pct(cc['flag_precision'], cc['error_flagged'], cc['flagged'])} |",
        f"| 主模型漏列由第二模型補出 | {_pct(cc['missing_caught_rate'], cc['missing_caught'], cc['missing_rows'])} |",
        f"| 抓錯率（含漏列） | {_pct(cc['catch_rate_with_missing'])} |",
        "",
        "依情境標籤（一份案例可有多個標籤；欄位正確率、漏列／多列為主模型）：",
        "",
        "| 情境 | 案例 | 執行 | 欄位正確率 | 漏列／多列 | 抓錯率 | 一致列錯誤率 | 需人工看占比 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lines += [_row_group(TAGS.get(tag, tag), s) for tag, s in report["by_tag"].items()]
    lines += ["", "| 分組 | 案例 | 執行 | 欄位正確率 | 漏列／多列 | 抓錯率 | 一致列錯誤率 | 需人工看占比 |",
              "|---|---|---|---|---|---|---|---|"]
    lines += [_row_group(f"{split}案例", s) for split, s in report["by_split"].items()]
    lines += [_row_group(f"第 {r} 次執行", s) for r, s in report["by_run"].items()]
    return "\n".join(lines) + "\n"


def write(report: dict, out: Path = OUT) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    stamp = report["started_at"].replace(":", "").replace("-", "")
    js, md = out / f"benchmark_{stamp}.json", out / f"benchmark_{stamp}.md"
    js.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md.write_text(markdown(report), encoding="utf-8")
    return js, md


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description="以虛構基準集量測 AI 解析儀表板與雙模型交叉驗證")
    parser.add_argument("--runs", type=int, default=3, help="每份案例執行次數")
    parser.add_argument("--case", action="append", help="只跑指定案例（可重複），例如 bench_01")
    parser.add_argument("--workers", type=int, default=1, help="同時處理的案例數（每份同時呼叫兩個模型）")
    parser.add_argument("--cases-dir", type=Path, default=CASES)
    parser.add_argument("--out", type=Path, default=OUT, help="輸出目錄（預設 data/eval/，不進版控）")
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs 至少為 1")
    try:
        llm.api_key()
    except llm.LLMError as e:
        sys.exit(f"無法量測：{e}")
    try:
        cases = load_cases(args.cases_dir, args.case)
    except ValueError as e:
        sys.exit(str(e))
    if not cases:
        sys.exit(f"{args.cases_dir} 沒有案例；先執行 python -m academic_twin.demo_dashboard --bench")
    print(f"{len(cases)} 份虛構案例 × {args.runs} 次；主模型 {llm.model_name()}、第二模型 {cross_check.check_model()}",
          file=sys.stderr)
    report = run(cases, args.runs, args.workers)
    js, md = write(report, args.out)
    print(markdown(report))
    print(f"已寫入 {js} 與 {md}", file=sys.stderr)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    main(sys.argv[1:])
