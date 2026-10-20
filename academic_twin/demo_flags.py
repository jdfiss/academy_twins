"""啟動 A 組的虛構 flags 示範，不修改正式規則或保存覆核資料。"""
from pathlib import Path

from .api import AcademicTwinService


def main() -> None:
    import web

    root = Path(__file__).resolve().parent.parent
    # 沿用既有網頁路由；還原按鈕也只載入本示範資料。
    web.STUDENTS_PATH = root / "data" / "students" / "demo_flags_records.csv"
    web.ROSTER_PATH = root / "data" / "students" / "demo_flags_roster.csv"
    web.service = AcademicTwinService(root / "data" / "curriculum_demo_flags.json",
                                     web.STUDENTS_PATH, web.ROSTER_PATH)
    web.main()


if __name__ == "__main__":
    main()
