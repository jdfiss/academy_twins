"""應屆名單與修課紀錄比對：確保名單上每位學生都出現在審查結果或異常清單。"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RosterCheck:
    to_audit: list[str] = field(default_factory=list)       # 名單上有修課紀錄、可審查
    no_records: list[str] = field(default_factory=list)     # 名單上有、修課紀錄沒有
    not_on_roster: list[str] = field(default_factory=list)  # 修課紀錄有、名單沒有
    duplicates: list[str] = field(default_factory=list)     # 名單重複列出
    blank_rows: list[int] = field(default_factory=list)     # 名單中學號空白的列（檔案行號）
    other_cohort: dict[str, int] = field(default_factory=dict)  # 入學年度與規則庫不符，未審查

    @property
    def roster_size(self) -> int:
        """名單上不重複的學生數（學號空白的列除外）。"""
        return len(self.to_audit) + len(self.no_records) + len(self.other_cohort)

    @property
    def anomalies(self) -> int:
        return (len(self.no_records) + len(self.not_on_roster) + len(self.duplicates) + len(self.blank_rows)
                + len(self.other_cohort))


YES = {"y", "yes", "true", "1", "是", "通過"}
NO = {"n", "no", "false", "0", "否", "未通過"}


@dataclass
class Roster:
    ids: list[str]  # 名單學號（保留順序與重複）
    blank_rows: list[int]  # 學號空白的行號
    english_passed: dict[str, bool] = field(default_factory=dict)  # 選填欄位；空白的學生不列入
    cohorts: dict[str, int] = field(default_factory=dict)  # 選填欄位：入學學年度；空白視為規則庫年度


def parse_roster(text: str, source: str = "應屆名單") -> Roster:
    """CSV 須有 student_id 欄；選填 english_passed（Y／N，空白表示未知）、cohort（入學學年度，如 114）。"""
    roster = Roster([], [])
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if "student_id" not in (reader.fieldnames or []):
        raise ValueError(f"應屆名單缺少 student_id 欄：{source}")
    bad = []
    for row in reader:
        sid = (row["student_id"] or "").strip()
        if not sid:
            roster.blank_rows.append(reader.line_num)
            continue
        roster.ids.append(sid)
        flag = (row.get("english_passed") or "").strip()
        if flag.lower() in YES:
            roster.english_passed[sid] = True
        elif flag.lower() in NO:
            roster.english_passed[sid] = False
        elif flag:
            bad.append(f"第 {reader.line_num} 行 english_passed「{flag}」應為 Y、N 或空白")
        cohort = (row.get("cohort") or "").strip()
        if cohort.isdigit() and len(cohort) in (2, 3):
            roster.cohorts[sid] = int(cohort)
        elif cohort:
            bad.append(f"第 {reader.line_num} 行 cohort「{cohort}」應為入學學年度數字，例如 114")
    if bad:
        raise ValueError(f"應屆名單格式錯誤（{source}）：" + "；".join(bad))
    return roster


def load_roster(path: Path) -> Roster:
    return parse_roster(path.read_text(encoding="utf-8-sig"), str(path))


def check_roster(roster_ids: list[str], record_ids, blank_rows: list[int] | None = None,
                 cohorts: dict[str, int] | None = None, cohort: int | None = None) -> RosterCheck:
    """cohorts 為名單上的入學年度；與規則庫年度 cohort 不同的學生列為異常、不審查。"""
    records = set(record_ids)
    check = RosterCheck(blank_rows=list(blank_rows or []))
    cohorts = cohorts or {}
    seen: set[str] = set()
    for sid in roster_ids:
        if sid in seen:
            if sid not in check.duplicates:
                check.duplicates.append(sid)
            continue
        seen.add(sid)
        if cohort is not None and cohorts.get(sid, cohort) != cohort:
            check.other_cohort[sid] = cohorts[sid]
        else:
            (check.to_audit if sid in records else check.no_records).append(sid)
    check.not_on_roster = sorted(records - seen)
    return check
