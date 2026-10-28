"""實證比對：審查結果對承辦人答案、原流程與輔助流程計時。（A 組）

輸出只有彙總數字，不含學號、課程或答案內容，承辦人可在本機執行後只回傳結果
（見 docs/TEAM_PLAN.md〈實證從第一階段開始〉）。--detail 會列出不一致案例的學號，只供本機核對，不可外傳。

    python -m academic_twin.evaluate audit --records 修課紀錄.csv --roster 名單.csv --answers 答案.csv
    python -m academic_twin.evaluate timing 計時.csv

答案 CSV（每位學生一列）：
    student_id  學號（去識別化代號）
    status      承辦人判定：通過／未通過（或 PASS／FAIL）
    unmet       未達成的要求編號，以分號分隔，全部達成時留空（如 R1;R5）
    flags       應提示的情況，以分號分隔，沒有時留空（overflow、repeatable、substitution_pending）
    split       開發（可用來調整規則與提示）／驗證（保留到最後，不先看）
    source      虛構／實務
    confirmed   Y／N：人工答案是否已由承辦人確認
    note        選填，不讀取

計時 CSV（每個案例、每種流程一列）：
    case_id     案例代號（只用來配對兩種流程，不輸出）
    method      原流程／輔助流程
    minutes     完成時間（分鐘，含等待 AI、核對與修正）
    order       執行順序（全體案例共用的流水號，用來報告先後順序）
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

from .api import KB_PATH, CoreService
from .rule_engine import OVERFLOW, REPEATABLE, SUBSTITUTION_PENDING

EVAL_VERSION = "evaluate-v1"
FLAG_TYPES = (OVERFLOW, REPEATABLE, SUBSTITUTION_PENDING)
ANSWER_COLUMNS = ["student_id", "status", "unmet", "flags", "split", "source", "confirmed"]
STATUS = {"通過": "PASS", "pass": "PASS", "未通過": "FAIL", "不通過": "FAIL", "fail": "FAIL"}
SPLIT = {"開發": "dev", "dev": "dev", "驗證": "holdout", "holdout": "holdout"}
SOURCE = {"虛構": "fictional", "fictional": "fictional", "實務": "real", "real": "real"}
CONFIRMED = {"y": True, "yes": True, "是": True, "n": False, "no": False, "否": False}
DECIDED = {"PASS", "FAIL"}  # 引擎的 WARNING、MANUAL_REVIEW 需人工判斷，另計為「未定」
TIMING_COLUMNS = ["case_id", "method", "minutes", "order"]
METHOD = {"原流程": "original", "original": "original", "輔助流程": "assisted", "assisted": "assisted"}


class AnswerError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


def _split(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(";") if v.strip()]


def _rows(text: str, columns: list[str], label: str) -> list[tuple[int, dict]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    missing = [c for c in columns if c not in (reader.fieldnames or [])]
    if missing:
        raise AnswerError([f"{label}缺少欄位：{'、'.join(missing)}"])
    return [(reader.line_num, {k: (v or "").strip() for k, v in row.items() if k}) for row in reader]


def parse_answers(text: str) -> dict[str, dict]:
    """檢查答案格式；有錯就整份不用並列出行號，不靜默略過。"""
    answers: dict[str, dict] = {}
    errors: list[str] = []
    for line, row in _rows(text, ANSWER_COLUMNS, "答案 "):
        def err(msg: str) -> None:
            errors.append(f"第 {line} 行：{msg}")
        sid = row["student_id"]
        if not sid:
            err("學號空白")
            continue
        if sid in answers:
            err(f"學號 {sid} 重複")
            continue
        status = STATUS.get(row["status"].lower())
        split = SPLIT.get(row["split"].lower())
        source = SOURCE.get(row["source"].lower())
        confirmed = CONFIRMED.get(row["confirmed"].lower())
        flags = _split(row["flags"])
        if status is None:
            err(f"status「{row['status']}」應為 通過 或 未通過")
        if split is None:
            err(f"split「{row['split']}」應為 開發 或 驗證")
        if source is None:
            err(f"source「{row['source']}」應為 虛構 或 實務")
        if confirmed is None:
            err(f"confirmed「{row['confirmed']}」應為 Y 或 N")
        unknown = [f for f in flags if f not in FLAG_TYPES]
        if unknown:
            err(f"不認得的提示類型：{'、'.join(unknown)}（可用 {'、'.join(FLAG_TYPES)}）")
        unmet = _split(row["unmet"])
        if status == "PASS" and unmet:
            err("判定為通過，但列了未達成的要求")
        if status == "FAIL" and not unmet:
            err("判定為未通過，但沒有列出未達成的要求")
        answers[sid] = {"status": status, "unmet": set(unmet), "flags": set(flags), "split": split,
                        "source": source, "confirmed": confirmed, "line": line}
    if errors:
        raise AnswerError(errors)
    return answers


def _counter() -> dict:
    return {"agree": 0, "disagree": 0, "undecided": 0}


def _group_key(a: dict) -> str:
    return f"{a['source']}/{a['split']}/{'confirmed' if a['confirmed'] else 'unconfirmed'}"


def evaluate_audit(service: CoreService, answers: dict[str, dict]) -> tuple[dict, list[str]]:
    """回傳（彙總結果, 本機用的不一致明細）。分組：案例來源／開發或驗證／答案是否已確認，各自計算。"""
    roster = service.roster
    audited = set(roster.to_audit)
    groups: dict[str, dict] = {}
    details: list[str] = []
    errors: list[str] = []
    rule_versions: set[str] = set()
    for sid, a in answers.items():
        g = groups.setdefault(_group_key(a), {
            "cases": 0, "not_audited": {"no_records": 0, "other_cohort": 0, "not_on_roster": 0},
            "status": {**_counter(), "matrix": {}}, "requirements": {**_counter(), "by_requirement": {}},
            "flags": {t: {"tp": 0, "fp": 0, "fn": 0, "tn": 0} for t in FLAG_TYPES}, "fully_agree": 0,
        })
        g["cases"] += 1
        if sid not in audited:
            reason = ("no_records" if sid in roster.no_records else
                      "other_cohort" if sid in roster.other_cohort else "not_on_roster")
            g["not_audited"][reason] += 1
            details.append(f"{sid}：未審查（{reason}）")
            continue
        result = service.student(sid)
        rule_versions.add(result["rule_version"])
        req_ids = {r["id"] for r in result["requirements"]}
        unknown = a["unmet"] - req_ids
        if unknown:
            errors.append(f"答案第 {a['line']} 行：要求編號 {'、'.join(sorted(unknown))} 不在 {sid} 適用的規則庫")
            continue
        diffs: list[str] = []

        engine = result["status"]
        cell = f"{engine}->{a['status']}"
        g["status"]["matrix"][cell] = g["status"]["matrix"].get(cell, 0) + 1
        if engine not in DECIDED:
            g["status"]["undecided"] += 1
            diffs.append(f"引擎 {engine}，承辦人 {a['status']}")
        elif engine == a["status"]:
            g["status"]["agree"] += 1
        else:
            g["status"]["disagree"] += 1
            diffs.append(f"引擎 {engine}，承辦人 {a['status']}")

        for r in result["requirements"]:
            per = g["requirements"]["by_requirement"].setdefault(r["id"], _counter())
            if r["pending"]:
                outcome = "undecided"
            elif r["satisfied"] == (r["id"] not in a["unmet"]):
                outcome = "agree"
            else:
                outcome = "disagree"
            per[outcome] += 1
            g["requirements"][outcome] += 1
            if outcome != "agree":
                diffs.append(f"{r['id']} 引擎{'待確認' if r['pending'] else '達成' if r['satisfied'] else '未達成'}，"
                             f"承辦人{'未達成' if r['id'] in a['unmet'] else '達成'}")

        got = {f["type"] for f in result["flags"]}
        for t in FLAG_TYPES:
            key = ("tp" if t in a["flags"] else "fp") if t in got else ("fn" if t in a["flags"] else "tn")
            g["flags"][t][key] += 1
            if key in ("fp", "fn"):
                diffs.append(f"{t} {'誤報' if key == 'fp' else '漏報'}")

        if diffs:
            details.append(f"{sid}：" + "；".join(diffs))
        else:
            g["fully_agree"] += 1
    if errors:
        raise AnswerError(errors)
    summary = {
        "audited_without_answer": len(audited - answers.keys()),
        "rule_versions": sorted(rule_versions),
        "groups": dict(sorted(groups.items())),
    }
    return summary, details


def evaluate_timing(text: str) -> dict:
    """依流程彙整中位時間；兩種流程都有的案例另算逐案差距與先後順序。"""
    times: dict[tuple[str, str], tuple[float, int]] = {}
    errors: list[str] = []
    for line, row in _rows(text, TIMING_COLUMNS, "計時 "):
        method = METHOD.get(row["method"].lower())
        try:
            minutes = float(row["minutes"])
            if not minutes > 0:
                raise ValueError
        except ValueError:
            minutes = None
        row_errors = [msg for bad, msg in (
            (not row["case_id"], "案例代號空白"),
            (method is None, f"method「{row['method']}」應為 原流程 或 輔助流程"),
            (minutes is None, f"minutes「{row['minutes']}」應為正數"),
            (not row["order"].isdigit(), f"order「{row['order']}」應為正整數"),
            ((row["case_id"], method) in times, f"同一案例的{row['method']}重複"),
        ) if bad]
        errors += [f"第 {line} 行：{msg}" for msg in row_errors]
        if not row_errors:
            times[(row["case_id"], method)] = (minutes, int(row["order"]))
    if errors:
        raise AnswerError(errors)

    def stats(values: list[float]) -> dict:
        if not values:
            return {"n": 0}
        return {"n": len(values), "median": statistics.median(values), "min": min(values), "max": max(values)}

    by_method = {m: [v for (_, mm), (v, _) in times.items() if mm == m] for m in ("original", "assisted")}
    paired = sorted(c for c, m in times if m == "original" and (c, "assisted") in times)
    saved = [times[(c, "original")][0] - times[(c, "assisted")][0] for c in paired]
    return {
        **{m: stats(v) for m, v in by_method.items()},
        "paired": {**stats(saved), "original_first": sum(times[(c, "original")][1] < times[(c, "assisted")][1]
                                                         for c in paired)},
        "note": "paired 為逐案（原流程－輔助流程）分鐘數；正值表示輔助流程較快",
    }


def _sha256(*texts: str) -> str:
    h = hashlib.sha256()
    for t in texts:
        h.update(hashlib.sha256(t.encode("utf-8")).digest())
    return h.hexdigest()[:12]


def run_audit(rules: Path, records: Path, roster: Path, answers: Path) -> tuple[dict, list[str]]:
    texts = [p.read_text(encoding="utf-8-sig") for p in (rules, records, roster, answers)]
    service = CoreService(rules, records, roster)  # 不讀覆核紀錄：量測的是引擎本身的判定
    summary, details = evaluate_audit(service, parse_answers(texts[3]))
    return {"type": "audit", "eval_version": EVAL_VERSION, "evaluated_at": datetime.now().isoformat(timespec="seconds"),
            "case_set": _sha256(*texts[1:]), **summary}, details


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="實證比對（只輸出彙總數字）")
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("audit", help="審查結果對承辦人答案")
    a.add_argument("--records", type=Path, required=True)
    a.add_argument("--roster", type=Path, required=True)
    a.add_argument("--answers", type=Path, required=True)
    a.add_argument("--rules", type=Path, default=KB_PATH)
    a.add_argument("--detail", action="store_true", help="另列不一致案例的學號（只供本機核對，不可外傳）")
    t = sub.add_parser("timing", help="原流程與輔助流程計時")
    t.add_argument("timing", type=Path)
    for p in (a, t):
        p.add_argument("--out", type=Path, help="彙總結果另存 JSON")
    args = parser.parse_args(argv)
    try:
        if args.command == "audit":
            result, details = run_audit(args.rules, args.records, args.roster, args.answers)
        else:
            text = args.timing.read_text(encoding="utf-8-sig")
            result = {"type": "timing", "eval_version": EVAL_VERSION,
                      "evaluated_at": datetime.now().isoformat(timespec="seconds"),
                      "case_set": _sha256(text), **evaluate_timing(text)}
            details = []
    except AnswerError as e:
        print("資料有誤，未計算：\n" + "\n".join(e.errors), file=sys.stderr)
        return 1
    output = json.dumps(result, ensure_ascii=False, indent=2)
    print(output)
    if args.out:
        args.out.write_text(output + "\n", encoding="utf-8")
    if args.command == "audit" and args.detail:
        print("\n---- 不一致案例（含學號，只供本機核對，不可外傳） ----")
        print("\n".join(details) or "（無）")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv[1:]))
