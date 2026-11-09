"""監控台指標：由執行軌跡算出 AI 的表現與人工介入情形。（A 組）

對應 docs/TEAM_PLAN.md〈AI 可驗證做法〉的指標頁：完成率、人工介入率、自我修正成功率、模型失敗轉人工、
平均等待；另加抽查結果、交叉驗證命中率、學生問題收件匣的核准情形，以及最新一次虛構基準集量測。

- 每項指標都以 {"k": 分子, "n": 分母} 回傳，畫面一律顯示樣本數；n 太小時只顯示 k／n，不顯示百分比。
- 軌跡只有計數與代號（不含學號、成績、原文），本檔也只輸出彙總數字。
- 示範與測試都是虛構資料：指標只說明系統在虛構案例上的行為，不代表實際行政成效。

    python -m academic_twin.metrics            # 印出累計指標
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

from . import dashboard

ROOT = Path(__file__).parent.parent
EVAL_DIR = ROOT / "data" / "eval"
MIN_N = 5  # 分母小於此值時畫面不顯示百分比


def read_log(path: Path) -> list[dict]:
    """讀 jsonl；損毀的行略過（執行中當機可能留下半行）。"""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def ratio(k: int, n: int) -> dict:
    return {"k": k, "n": n}


def dashboard_metrics(entries: list[dict]) -> dict:
    traces = [e for e in entries if e.get("type") == "extract_trace"]
    confirms = [e for e in entries if e.get("type") == "confirm"]
    samples = [e for e in entries if e.get("type") == "sample"]
    retried = [t for t in traces if any(s.get("step") == "retry" and s.get("ok") for s in t.get("steps", []))]
    waits = [t["wait_seconds"] for t in traces if isinstance(t.get("wait_seconds"), (int, float))
             and t.get("status") == "draft" and not t.get("replay")]
    flagged = sum(c.get("cross_check", {}).get("edited_rows_flagged", 0) for c in confirms)
    unflagged = sum(c.get("cross_check", {}).get("edited_rows_unflagged", 0) for c in confirms)
    edited_docs = sum(1 for c in confirms if c.get("fields_changed") or c.get("rows_added") or c.get("rows_removed"))
    modes = {m: sum(c.get("mode", "single") == m for c in confirms) for m in ("single", "sampled", "batch")}
    return {
        "runs": len(traces),
        "completed": ratio(sum(t.get("status") == "draft" for t in traces), len(traces)),
        "model_failed": ratio(sum(t.get("status") == "failed" for t in traces), len(traces)),
        "to_manual": ratio(sum(bool(t.get("to_manual")) for t in traces), len(traces)),
        "self_corrected": ratio(sum(bool(t.get("corrected")) for t in retried), len(retried)),
        "replayed": sum(bool(t.get("replay")) for t in traces),
        "wait_median": round(statistics.median(waits), 1) if waits else None,
        "wait_mean": round(statistics.fmean(waits), 1) if waits else None,
        "wait_n": len(waits),
        "confirms": len(confirms),
        "confirm_modes": modes,
        "docs_edited": ratio(edited_docs, len(confirms)),
        # 人工修改的列有多少落在交叉驗證標示的列：衡量標示有沒有抓到真正要改的地方
        "edits_in_flagged_rows": ratio(flagged, flagged + unflagged),
        "samples": len(samples),
        "sampled_docs_edited": ratio(sum(s.get("sampled_edited", 0) for s in samples),
                                     sum(s.get("sampled_checked", 0) for s in samples)),
        "released_by_batch": sum(s.get("released", 0) for s in samples),
        "batches_cancelled_by_sample": sum(s.get("outcome") == "cancelled_by_sample" for s in samples),
    }


def inbox_metrics(entries: list[dict]) -> dict:
    traces = [e for e in entries if e.get("type") == "inbox_trace"]
    decisions = [e for e in entries if e.get("type") == "inbox_decision"]
    replies = [d for d in decisions if d.get("action") in ("approve", "reject")]
    return {
        "runs": len(traces),
        "drafted": ratio(sum(not t.get("to_manual") for t in traces), len(traces)),
        "to_manual": ratio(sum(bool(t.get("to_manual")) for t in traces), len(traces)),
        "grounding_failed": ratio(sum(t.get("grounding") == "failed" for t in traces), len(traces)),
        "reflected": ratio(sum(bool(t.get("reflected")) for t in traces), len(traces)),
        "decisions": len(replies),
        # 監控者「沒改就核准」的比例：太高可能代表只是蓋章，搭配抽查錯誤率一起看
        "approved_unedited": ratio(sum(d["action"] == "approve" and not d.get("edited_chars") for d in replies),
                                   len(replies)),
        "approved_edited": ratio(sum(d["action"] == "approve" and bool(d.get("edited_chars")) for d in replies),
                                 len(replies)),
        "rejected": ratio(sum(d["action"] == "reject" for d in replies), len(replies)),
        "proposals_applied": sum(d.get("action") == "apply_proposal" for d in decisions),
    }


def latest_benchmark(folder: Path = EVAL_DIR) -> dict | None:
    """最新一次虛構基準集量測的重點數字；沒有量測時為 None。"""
    files = sorted(folder.glob("benchmark_*.json"))
    for path in reversed(files):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            o = report["overall"]
            cc = o["cross_check"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        return {
            "file": path.name, "date": report.get("started_at", "")[:10], "cases": report.get("cases"),
            "runs": o.get("runs"), "disclaimer": report.get("disclaimer", ""),
            "primary_models": o["primary"].get("models", {}), "check_models": o["check"].get("models", {}),
            "field_accuracy": ratio(o["primary"].get("field_correct", 0), o["primary"].get("field_total", 0)),
            "fully_correct_docs": ratio(o["primary"].get("fully_correct_docs", 0), o["primary"].get("runs", 0)),
            "catch_rate": ratio(cc.get("error_flagged", 0), cc.get("error_rows", 0)),
            "agreed_error_rate": ratio(cc.get("agreed_errors", 0), cc.get("agreed", 0)),
            "review_share": ratio(cc.get("flagged", 0), cc.get("rows", 0)),
            "missing_caught": ratio(cc.get("missing_caught", 0), cc.get("missing_rows", 0)),
        }
    return None


def compute(batch_id: str | None = None, dashboard_log: Path | None = None, inbox_log: Path | None = None,
            eval_dir: Path | None = None) -> dict:
    """累計與本批次（batch_id）指標。"""
    from . import inbox as inbox_module
    dash = read_log(dashboard_log or dashboard.EVAL_LOG)
    inbox = read_log(inbox_log or inbox_module.EVAL_LOG)

    def scoped(entries):
        return [e for e in entries if e.get("batch_id") == batch_id] if batch_id else []
    return {
        "min_n": MIN_N,
        "note": "虛構資料上的系統行為，不代表實際行政成效",
        "batch": {"dashboard": dashboard_metrics(scoped(dash)), "inbox": inbox_metrics(scoped(inbox))},
        "total": {"dashboard": dashboard_metrics(dash), "inbox": inbox_metrics(inbox)},
        "benchmark": latest_benchmark(eval_dir or EVAL_DIR),
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(compute()["total"], ensure_ascii=False, indent=2))
