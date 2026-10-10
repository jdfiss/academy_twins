"""NCU Academic Twin — 規則庫 + 畢業資格檢查 + 先修圖 + 路徑模擬。

用法：
    python app.py                              # 系辦批次畢審（依應屆名單）
    python app.py --roster 名單.csv            # 指定應屆名單
    python app.py --records 修課紀錄.csv      # 指定修課紀錄（會先做格式檢查）
    python app.py --export 結果.csv            # 批次畢審結果匯出 CSV（附規則版本與時間）
    python app.py S002                         # 單一學生的畢業進度
    python app.py S002 --plan                  # 排出到畢業的修課計畫
    python app.py S002 --skip IM2001@114-1     # What-if：114-1 不修資料結構
    python app.py S002 --away 115-1            # What-if：115-1 出國交換
    python app.py --course IM2001              # 這門不修會卡到哪些課
    python app.py --bottlenecks                # 瓶頸課排行
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from academic_twin import knowledge_base
from academic_twin.api import AcademicTwinService
from academic_twin.graph import CourseGraph
from academic_twin.planner import Plan, Scenario, Term, explain_delay, plan
from academic_twin.records import RecordsError, load_students
from academic_twin.roster import RosterCheck, check_roster, load_roster
from academic_twin.rule_engine import AuditReport, Status, audit

ROOT = Path(__file__).parent
KB_PATH = ROOT / "data" / "curriculum_im_114.json"
STUDENTS_PATH = ROOT / "data" / "students" / "sample_students.csv"
ROSTER_PATH = ROOT / "data" / "students" / "sample_roster.csv"

STATUS_LABEL = {Status.PASS: "✅ Pass", Status.WARNING: "⚠️ Warning", Status.FAIL: "❌ Fail",
                Status.MANUAL_REVIEW: "🔍 Manual Review"}


def course_label(kb, code: str) -> str:
    return f"{code} {kb.courses[code].name}"


def print_student(kb, sid: str, report: AuditReport) -> None:
    print(f"\n學生 {sid}　{kb.program} {kb.cohort} 學年度入學　總學分 {report.total_credits}")
    print(f"判定：{STATUS_LABEL[report.status]}")
    print("學分採計：" + "、".join(f"{k} {v}" for k, v in report.credit_breakdown.items()) + "\n")
    for r in report.results:
        mark = "✔" if r.satisfied else "✘"
        print(f"  {mark} [{r.requirement.id}] {r.requirement.title}　({r.have}/{r.need})")
        if not r.satisfied:
            options = '、'.join(course_label(kb, c) for c in r.missing)
            if r.requirement.kind == "n_of":
                print(f"      尚需 {r.shortfall} 門，可從中選：{options}")
            elif r.missing:
                print(f"      缺：{options}")
            elif r.requirement.kind == "category_count":
                print(f"      尚缺 {r.shortfall} 門")
            else:
                print(f"      尚缺 {r.shortfall} 學分")
            print(f"      依據：{r.citation}")
    t = report.transcript
    for rec in t.review:
        print(f"  ? {rec.code} {rec.name}（{rec.term}，類別「{rec.category}」）需人工判斷如何採計")
    for rec in t.dept_unverified:
        print(f"  ⚠ {rec.code} {rec.name}（{rec.term}）不在規則庫，暫以本系選修 {rec.credits} 學分計，請系辦確認")
    for rec in t.failed:
        print(f"  ! {rec.code} {rec.name}（{rec.term}）不及格，尚未重修通過")


def print_batch(kb, reports: dict[str, AuditReport], check: RosterCheck) -> None:
    roster_size = len(check.to_audit) + len(check.no_records)
    print(f"系辦批次畢審：{kb.program} {kb.cohort} 學年度，應屆名單 {roster_size} 位，審查 {len(reports)} 位\n")
    for sid, report in reports.items():
        reasons = [f"{r.requirement.id} {r.requirement.title}" for r in report.results if not r.satisfied]
        t = report.transcript
        if t.review:
            reasons.insert(0, f"{len(t.review)} 筆紀錄需人工判斷")
        if t.dept_unverified and not reasons:
            reasons.append(f"{len(t.dept_unverified)} 門本系選修未在規則庫核對")
        print(f"  {sid}  {STATUS_LABEL[report.status]:<18} {'；'.join(reasons) or '全部符合'}")
    print_roster_anomalies(check)


def print_roster_anomalies(check: RosterCheck) -> None:
    if not check.anomalies:
        print("\n名單比對：無異常")
        return
    print(f"\n名單異常（{check.anomalies} 筆）")
    for sid in check.no_records:
        print(f"  ! {sid}  在應屆名單上，但沒有任何修課紀錄，未審查")
    for sid in check.not_on_roster:
        print(f"  ? {sid}  有修課紀錄，但不在應屆名單，未審查")
    for sid in check.duplicates:
        print(f"  ? {sid}  應屆名單重複列出")
    for line in check.blank_rows:
        print(f"  ? 名單第 {line} 行學號空白")


def print_plan(kb, title: str, p: Plan) -> None:
    print(f"\n{title}")
    for term, codes in p.terms.items():
        if not codes and not p.free.get(term):
            continue
        courses = "、".join(course_label(kb, c) for c in codes) or "—"
        credits = sum(kb.courses[c].credits for c in codes) + p.free.get(term, 0)
        extra = f"＋其他學分 {p.free[term]}" if p.free.get(term) else ""
        print(f"  {term}  ({credits:>2} 學分)  {courses} {extra}")
    if p.unplaceable:
        print(f"  ⚠ 排不進去：{'、'.join(course_label(kb, c) for c in p.unplaceable)}")
    else:
        print(f"  → 最早畢業學期：{p.graduation or '已符合'}")
    print("  （修課順序依建議先修排定，非正式擋修；「其他學分」指國文、外文、通識、本系選修等）")


def print_course_impact(kb, graph: CourseGraph, code: str) -> None:
    if code not in kb.courses:
        sys.exit(f"規則庫沒有 {code}")
    before = graph.ancestors(code)
    after = graph.descendants(code)
    print(f"{course_label(kb, code)}（開課：{'/'.join(kb.courses[code].terms)}）")
    print(f"  需要先修：{'、'.join(course_label(kb, c) for c in before) or '無'}")
    if any(graph.is_soft(p, code) for p in graph.parents(code)):
        print("  （含建議先修：依建議修課學期推論，非正式擋修）")
    print(f"  沒修會卡到 {len(after)} 門：{'、'.join(course_label(kb, c) for c in after) or '無'}")


def print_bottlenecks(kb, graph: CourseGraph) -> None:
    print("瓶頸課（先修鏈越長、卡住越多課越前面）")
    for b in graph.bottlenecks(top=8):
        print(f"  {course_label(kb, b.code):<16} 往下鏈長 {b.depth}　卡住 {b.blocked} 門")


def parse_skip(value: str) -> tuple[str, Term]:
    code, _, term = value.partition("@")
    if not term:
        raise argparse.ArgumentTypeError("格式為 課號@學期，例如 IM2001@114-1")
    return code, Term.parse(term)


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description="NCU Academic Twin")
    parser.add_argument("student", nargs="?")
    parser.add_argument("--plan", action="store_true", help="排出到畢業的修課計畫")
    parser.add_argument("--skip", type=parse_skip, action="append", default=[], help="課號@學期：該學期不修")
    parser.add_argument("--away", type=Term.parse, action="append", default=[], help="學期：交換／休學")
    parser.add_argument("--away-credits", type=int, default=15, help="交換學期可抵的自由學分")
    parser.add_argument("--course", help="查這門課的先修與後續影響")
    parser.add_argument("--bottlenecks", action="store_true")
    parser.add_argument("--assumptions", action="store_true", help="列出規則庫中尚待確認的假設")
    parser.add_argument("--roster", type=Path, default=ROSTER_PATH, help="應屆名單 CSV（需有 student_id 欄）")
    parser.add_argument("--records", type=Path, default=STUDENTS_PATH, help="修課紀錄 CSV")
    parser.add_argument("--export", type=Path, help="批次畢審結果匯出成 CSV")
    args = parser.parse_args(argv)

    kb = knowledge_base.load(KB_PATH)
    graph = CourseGraph(kb)

    if args.course:
        return print_course_impact(kb, graph, args.course)
    if args.bottlenecks:
        return print_bottlenecks(kb, graph)
    if args.assumptions:
        print("規則庫假設（待系辦確認）：")
        for i, a in enumerate(kb.assumptions, 1):
            print(f"  {i}. {a}")
        return

    try:
        students = load_students(args.records)
    except FileNotFoundError:
        sys.exit(f"找不到修課紀錄：{args.records}")
    except RecordsError as e:
        sys.exit(str(e))
    if not args.student:
        try:
            roster_ids, blank_rows = load_roster(args.roster)
        except FileNotFoundError:
            sys.exit(f"找不到應屆名單：{args.roster}")
        except ValueError as e:
            sys.exit(str(e))
        check = check_roster(roster_ids, students, blank_rows)
        print_batch(kb, {sid: audit(kb, students[sid]) for sid in check.to_audit}, check)
        if args.export:
            service = AcademicTwinService(KB_PATH, args.records, args.roster)
            args.export.write_text(service.export_csv(), encoding="utf-8-sig")
            print(f"\n已匯出 {args.export}（規則版本 {service.rule_version}）")
        return

    sid = args.student
    if sid not in students:
        sys.exit(f"找不到學生 {sid}")
    report = audit(kb, students[sid])
    print_student(kb, sid, report)
    if not (args.plan or args.skip or args.away):
        return

    start = max(Term.parse(r.term) for r in students[sid]).next()
    base = plan(kb, graph, report, start)
    print_plan(kb, f"修課計畫（從 {start} 開始，每學期上限 {kb.max_credits_per_term} 學分）", base)

    if args.skip or args.away:
        scenario = Scenario(skip=dict(args.skip), away=set(args.away), away_credits=args.away_credits)
        alt = plan(kb, graph, report, start, scenario)
        conditions = [f"{t} 不修 {c}" for c, t in args.skip] + [f"{t} 交換" for t in args.away]
        print_plan(kb, f"What-if：{'、'.join(conditions)}", alt)
        print("\n影響：")
        for line in explain_delay(kb, base, alt):
            print(f"  {line}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    main(sys.argv[1:])
