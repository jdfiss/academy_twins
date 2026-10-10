"""修課紀錄 CSV 讀取與格式檢查：有任何一列格式錯就整份不收，並列出每個錯誤的行號。"""
from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from pathlib import Path

from .rule_engine import Record

REQUIRED = ["student_id", "code", "name", "credits", "grade", "term"]
TERM_RE = re.compile(r"^\d{2,3}-[12]$")  # 例如 114-1；1 = 上學期、2 = 下學期


class RecordsError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("修課紀錄格式錯誤：\n" + "\n".join(errors))
        self.errors = errors


def parse_records(text: str) -> dict[str, list[Record]]:
    """解析修課紀錄 CSV 文字；格式有錯時丟 RecordsError，附全部錯誤。"""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
    if missing:
        raise RecordsError([f"缺少欄位：{'、'.join(missing)}（需要 {','.join(REQUIRED)}，category 可省略）"])

    students: dict[str, list[Record]] = defaultdict(list)
    errors: list[str] = []
    for row in reader:
        line = reader.line_num
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue  # 整列空白直接略過
        get = lambda k: (row.get(k) or "").strip()
        problems = [f"{k} 空白" for k in ("student_id", "code") if not get(k)]
        try:
            credits = int(get("credits"))
            if credits < 0:
                raise ValueError
        except ValueError:
            problems.append(f"學分「{get('credits')}」不是非負整數")
        try:
            grade = float(get("grade"))
            if not 0 <= grade <= 100:
                raise ValueError
        except ValueError:
            problems.append(f"成績「{get('grade')}」不是 0–100 的數字")
        if not TERM_RE.match(get("term")):
            problems.append(f"學期「{get('term')}」格式應為 學年-學期，例如 114-1")
        if problems:
            errors.append(f"第 {line} 行：" + "；".join(problems))
            continue
        students[get("student_id")].append(
            Record(get("code"), get("name"), credits, grade, get("term"), get("category"))
        )
    if errors:
        raise RecordsError(errors)
    if not students:
        raise RecordsError(["檔案沒有任何修課紀錄"])
    return dict(students)


def load_students(path: Path) -> dict[str, list[Record]]:
    return parse_records(path.read_text(encoding="utf-8-sig"))
