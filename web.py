"""NCU Academic Twin 網頁介面（只用標準函式庫）。

    python web.py            # 開 http://localhost:8000
    python web.py 8080       # 換 port
"""
from __future__ import annotations

import base64
import binascii
import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from academic_twin.api import REVIEWS_PATH, ROSTER_PATH, STUDENTS_PATH, AcademicTwinService
from academic_twin.llm import LLMError
from academic_twin.records import RecordsError

STATIC = (Path(__file__).parent / "web").resolve()
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8"}
MAX_UPLOAD = 20 * 1024 * 1024
service = AcademicTwinService(reviews_path=REVIEWS_PATH)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
        query = parse_qs(url.query)
        try:
            if not parts or parts[0] != "api":
                return self._static(parts or ["index.html"])
            route = parts[1:]
            # ---------- 共用 ----------
            if route == ["meta"]:
                return self._json(service.meta())
            # ---------- 系辦端（A 組） ----------
            if route == ["students"]:
                return self._json(service.batch())
            if route == ["data-source"]:
                return self._json(service.data_source())
            if route == ["roster-anomalies"]:
                return self._json(service.roster_anomalies())
            if route == ["export.csv"]:
                return self._csv(service.export_csv(), f"audit_{service.rule_version}.csv")
            if len(route) == 2 and route[0] == "students":
                return self._json(service.student(route[1]))
            if len(route) == 3 and route[0] == "students" and route[2] == "report":
                body = service.report_html(route[1]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            # ---------- 學生端（B 組） ----------
            if route == ["twin"]:
                return self._json(service.twin_ids())
            if len(route) == 2 and route[0] == "twin":
                return self._json(service.twin(route[1]))
            if len(route) == 3 and route[0] == "students" and route[2] == "simulate":
                return self._json(service.simulate(
                    route[1],
                    skip=query.get("skip", []),
                    away=query.get("away", []),
                    away_credits=int(query.get("away_credits", ["15"])[0]),
                ))
            if len(route) == 2 and route[0] == "courses":
                return self._json(service.course_impact(route[1]))
            return self._json({"error": "not found"}, 404)
        except KeyError as e:
            return self._json({"error": str(e.args[0])}, 404)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)

    def do_POST(self):
        """上傳資料只放在記憶體，重開伺服器就回到示例資料；覆核紀錄存檔。

        POST /api/upload  {"records": {"name", "text"}?, "roster": {"name", "text"}?}
        POST /api/reset   回到示例資料
        POST /api/students/<id>/reviews  {"code", "term", "action", "category"?, "reviewer", "note"?}
        POST /api/dashboard/extract  {"student_id", "data"（PDF 的 base64）}：AI 解析為待確認草稿
        POST /api/dashboard/confirm  {"draft_id", "rows", "reviewer", "cohort"?, "acknowledge_warnings"?}
        POST /api/rule-drafts/extract  {"data"（公開 PDF 的 base64）, "year"}
        POST /api/rule-drafts/check 或 confirm  {"draft_id", "requirements", "reviewer"?, "acknowledge"?}
        """
        route = urlparse(self.path).path.strip("/")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            return self._json({"error": "檔案太大（上限 20 MB）"}, 413)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise TypeError
        except (ValueError, TypeError):
            return self._json({"error": "請求內容不是有效的 JSON 物件"}, 400)
        try:
            if route == "api/upload":
                files = {}
                for k in ("records", "roster"):
                    f = body.get(k)
                    if f:
                        if not (isinstance(f, dict) and isinstance(f.get("name"), str) and isinstance(f.get("text"), str)):
                            return self._json({"error": f"{k} 需要 name 與 text 欄位"}, 400)
                        files[k] = (f["name"], f["text"])
                if not files:
                    return self._json({"error": "沒有選擇檔案"}, 400)
                service.load_data(**files)
            elif route.startswith("api/students/") and route.endswith("/reviews"):
                missing = [k for k in ("code", "term", "action") if not isinstance(body.get(k), str)]
                if missing:
                    return self._json({"error": f"缺少欄位：{'、'.join(missing)}"}, 400)
                return self._json(service.add_review(
                    unquote(route.split("/")[2]), body["code"], body["term"], body["action"],
                    str(body.get("reviewer") or ""), str(body.get("category") or ""), str(body.get("note") or ""),
                ))
            elif route == "api/dashboard/extract":
                try:
                    pdf = base64.b64decode(str(body.get("data") or ""), validate=True)
                except (binascii.Error, ValueError):
                    return self._json({"error": "PDF 內容無法讀取"}, 400)
                return self._json(service.dashboard_extract(pdf, str(body.get("student_id") or "")))
            elif route == "api/dashboard/confirm":
                if not isinstance(body.get("rows"), list) or not all(isinstance(r, dict) for r in body["rows"]):
                    return self._json({"error": "rows 需為紀錄陣列"}, 400)
                cohort = body.get("cohort")
                if cohort not in (None, "") and not str(cohort).isdigit():
                    return self._json({"error": "入學學年度須為數字"}, 400)
                return self._json(service.dashboard_confirm(
                    str(body.get("draft_id") or ""), body["rows"], str(body.get("reviewer") or ""),
                    int(cohort) if cohort not in (None, "") else None, bool(body.get("acknowledge_warnings")),
                ))
            elif route == "api/rule-drafts/extract":
                try:
                    pdf = base64.b64decode(str(body.get("data") or ""), validate=True)
                except (binascii.Error, ValueError):
                    return self._json({"error": "PDF 內容無法讀取"}, 400)
                return self._json(service.rule_draft_extract(pdf, body.get("year")))
            elif route in ("api/rule-drafts/check", "api/rule-drafts/confirm"):
                rows = body.get("requirements")
                if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                    return self._json({"error": "requirements 需為要求陣列"}, 400)
                draft_id = str(body.get("draft_id") or "")
                if route.endswith("/check"):
                    return self._json(service.rule_draft_check(draft_id, rows))
                return self._json(service.rule_draft_confirm(
                    draft_id, rows, str(body.get("reviewer") or ""), body.get("acknowledge") is True))
            elif route == "api/reset":
                service.load_data(
                    (STUDENTS_PATH.name, STUDENTS_PATH.read_text(encoding="utf-8-sig")),
                    (ROSTER_PATH.name, ROSTER_PATH.read_text(encoding="utf-8-sig")),
                )
            else:
                return self._json({"error": "not found"}, 404)
            return self._json(service.data_source())
        except RecordsError as e:
            return self._json({"error": "資料格式錯誤，未套用", "errors": e.errors}, 400)
        except LLMError as e:
            if route.startswith("api/rule-drafts/"):
                return self._json({"error": f"AI 草擬暫時無法使用，草稿未確認、已啟用規則不變：{e}"}, 502)
            return self._json({"error": f"AI 解析暫時無法使用，可改用上傳修課紀錄 CSV：{e}"}, 502)
        except KeyError as e:
            return self._json({"error": str(e.args[0])}, 404)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _csv(self, text: str, filename: str):
        body = text.encode("utf-8-sig")  # 帶 BOM，Excel 才不會亂碼
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, parts: list[str]):
        """web/ 底下的檔案；不允許跳出 web/。"""
        path = STATIC.joinpath(*parts).resolve()
        if not path.is_relative_to(STATIC) or not path.is_file() or path.suffix not in CONTENT_TYPES:
            return self._json({"error": "not found"}, 404)
        return self._file(path, CONTENT_TYPES[path.suffix])

    def _file(self, path: Path, content_type: str):
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def main():
    port = next((int(a) for a in sys.argv[1:] if a.isdigit()), 8000)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    print(f"NCU Academic Twin 介面：{url}（Ctrl+C 結束）")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main()
