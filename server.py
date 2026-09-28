"""IPI Guard Chat - ローカル専用サーバー（Python標準ライブラリのみ）

起動:  python server.py        （または uv run python server.py）
画面:  http://127.0.0.1:8765
ファイルは自分のPCの中だけで処理され、外部には送信されません。
"""
import json
import pathlib
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from detector import scan_bytes

HOST, PORT = "127.0.0.1", 8765          # localhost 以外からは接続させない
MAX_BYTES = 20 * 1024 * 1024            # 1ファイル20MBまで
STATIC = pathlib.Path(__file__).parent / "static"
FILES = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
         "/style.css": ("style.css", "text/css")}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
                               "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json")

    def do_GET(self):
        if self.path in FILES:
            name, ctype = FILES[self.path]
            self._send(200, (STATIC / name).read_bytes(), ctype)
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/scan":
            return self._json(404, {"message": "not found"})
        # 他サイトからの送信を拒否（ブラウザ経由のなりすまし対策）
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"):
            return self._json(403, {"message": "許可されていない送信元です"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return self._json(400, {"message": "ファイルが空です"})
        if length > MAX_BYTES:
            return self._json(413, {"message": "20MBを超えるファイルは検査できません"})
        name = urllib.parse.unquote(self.headers.get("X-Filename") or "upload")
        name = pathlib.PurePath(name).name[:200]
        data = self.rfile.read(length)
        try:
            self._json(200, scan_bytes(name, data))
        except Exception as e:  # 想定外の形式でもサーバーを落とさない
            self._json(500, {"message": f"検査中にエラーが発生しました：{type(e).__name__}"})

    def log_message(self, fmt, *args):  # ファイル名などを端末に残さない
        pass


if __name__ == "__main__":
    print(f"IPI Guard Chat: http://{HOST}:{PORT}  （終了は Ctrl+C）")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
