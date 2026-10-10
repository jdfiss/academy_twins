"""NCU Academic Twin 網頁介面（只用標準函式庫）。

    python web.py            # 開 http://localhost:8000
    python web.py 8080       # 換 port
"""
from __future__ import annotations

import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from academic_twin.api import REVIEWS_PATH, ROSTER_PATH, STUDENTS_PATH, AcademicTwinService
from academic_twin.records import RecordsError

STATIC = Path(__file__).parent / "web"
MAX_UPLOAD = 20 * 1024 * 1024
service = AcademicTwinService(reviews_path=REVIEWS_PATH)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
        query = parse_qs(url.query)
        try:
            if not parts or parts == ["index.html"]:
                return self._file(STATIC / "index.html", "text/html; charset=utf-8")
            if parts[0] != "api":
                return self._json({"error": "not found"}, 404)
            route = parts[1:]
            if route == ["meta"]:
                return self._json(service.meta())
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
