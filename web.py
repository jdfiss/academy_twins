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

from academic_twin.api import AcademicTwinService

STATIC = Path(__file__).parent / "web"
service = AcademicTwinService()


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
            if route == ["roster-anomalies"]:
                return self._json(service.roster_anomalies())
            if len(route) == 2 and route[0] == "students":
                return self._json(service.student(route[1]))
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

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
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
