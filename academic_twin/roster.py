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

    @property
    def anomalies(self) -> int:
        return len(self.no_records) + len(self.not_on_roster) + len(self.duplicates) + len(self.blank_rows)


def parse_roster(text: str, source: str = "應屆名單") -> tuple[list[str], list[int]]:
    """回傳名單學號（保留順序與重複）及學號空白的行號。CSV 須有 student_id 欄。"""
    ids, blank = [], []
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if "student_id" not in (reader.fieldnames or []):
        raise ValueError(f"應屆名單缺少 student_id 欄：{source}")
    for row in reader:
        sid = (row["student_id"] or "").strip()
        if sid:
            ids.append(sid)
        else:
            blank.append(reader.line_num)
    return ids, blank


def load_roster(path: Path) -> tuple[list[str], list[int]]:
    return parse_roster(path.read_text(encoding="utf-8-sig"), str(path))


def check_roster(roster_ids: list[str], record_ids, blank_rows: list[int] | None = None) -> RosterCheck:
    records = set(record_ids)
    check = RosterCheck(blank_rows=list(blank_rows or []))
    seen: set[str] = set()
    for sid in roster_ids:
        if sid in seen:
            if sid not in check.duplicates:
                check.duplicates.append(sid)
            continue
        seen.add(sid)
        (check.to_audit if sid in records else check.no_records).append(sid)
    check.not_on_roster = sorted(records - seen)
    return check
