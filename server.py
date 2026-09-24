#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
巢记 ChaoJi 本地服务器模式（纯标准库，无任何依赖，无 root）
用法：
    python3 server.py              # 服务目录 = 脚本所在目录
    python3 server.py --port 8765  # 自定义端口（默认 8765）
    python3 server.py --dir ~/notes
然后浏览器打开  http://127.0.0.1:8765/
"""
import argparse
import json
import os
import re
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
APP_FILE = "chaoji.html"          # 优先找带版本号的最新文件
VERSION_RE = re.compile(r"^chaoji-v(\d+)\.(\d+)\.html$")

VALID_NAME = re.compile(r"^[\w\u4e00-\u9fff.-]+\.nbk$")   # 文件名白名单


def find_frontend():
    """优先 chaoji-vX.Y.html 中版本号最大的，否则 chaoji.html；找不到返回 None"""
    best, best_key = None, (-1, -1)
    for fn in os.listdir(HERE):
        m = VERSION_RE.match(fn)
        if m:
            key = (int(m.group(1)), int(m.group(2)))
            if key > best_key:
                best, best_key = fn, key
    if best:
        return best
    return APP_FILE if os.path.exists(os.path.join(HERE, APP_FILE)) else None


DATA_DIR = HERE  # 默认与脚本同目录；--dir 可改


class Handler(BaseHTTPRequestHandler):
    server_version = "ChaoJi/0.3"

    # ---------- 基础 ----------
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 防缓存：保存接口靠它保证每次都是最新
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    # ---------- 路由 ----------
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self.serve_frontend()
        if path == "/api/ping":
            return self._json({"ok": True, "mode": "server", "dataDir": DATA_DIR})
        if path == "/api/list":
            return self.api_list()
        if path.startswith("/api/load/"):
            return self.api_load(path[len("/api/load/"):])
        if path == "/favicon.ico":
            return self._send(404, b"", "text/plain")
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/save":
            return self.api_save()
        return self._json({"error": "not found"}, 404)

    # ---------- 前端文件 ----------
    def serve_frontend(self):
        fn = find_frontend()
        if not fn:
            return self._send(500, "chaoji.html not found next to server.py", "text/plain; charset=utf-8")
        full = os.path.join(HERE, fn)
        with open(full, "rb") as f:
            body = f.read()
        self._send(200, body, "text/html; charset=utf-8")

    # ---------- API ----------
    def api_list(self):
        items = []
        for fn in sorted(os.listdir(DATA_DIR)):
            if not fn.endswith(".nbk"):
                continue
            full = os.path.join(DATA_DIR, fn)
            try:
                st = os.stat(full)
                items.append({"file": fn, "size": st.st_size,
                              "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")})
            except OSError:
                pass
        self._json({"ok": True, "items": items})

    def _safe_path(self, name):
        """只允许同目录下合法 .nbk 文件名，杜绝路径穿越"""
        if not name or not VALID_NAME.match(name):
            return None
        return os.path.join(DATA_DIR, name)

    def api_load(self, name):
        # URL 段可能带 URL 编码
        from urllib.parse import unquote
        name = unquote(name)
        full = self._safe_path(name)
        if not full or not os.path.exists(full):
            return self._json({"error": "file not found: %s" % name}, 404)
        with open(full, "r", encoding="utf-8") as f:
            text = f.read()
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            return self._json({"error": "bad notebook json: %s" % e}, 500)
        self._json({"ok": True, "file": name, "state": obj})

    def api_save(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
            name = payload.get("file", "")
            state = payload.get("state")
        except Exception as e:
            return self._json({"error": "bad request: %s" % e}, 400)
        full = self._safe_path(name)
        if not full:
            return self._json({"error": "invalid file name (只允许字母数字下划线中文点-，且必须 .nbk 结尾)"}, 400)
        if not isinstance(state, dict):
            return self._json({"error": "state must be an object"}, 400)
        # 原子写：先写临时文件再 rename，防写一半断电损坏
        tmp = full + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(tmp, full)
        except OSError as e:
            if os.path.exists(tmp):
                try: os.remove(tmp)
                except OSError: pass
            return self._json({"error": "save failed: %s" % e}, 500)
        self._json({"ok": True, "file": name, "savedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})


def main():
    global DATA_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--dir", default=None, help="数据目录（默认脚本所在目录）")
    args = ap.parse_args()
    if args.dir:
        DATA_DIR = os.path.expanduser(args.dir)
    if not os.path.isdir(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)

    # 前端文件探测（提前给出友好错误）
    if not find_frontend():
        print("[错误] 未在 %s 找到 chaoji.html（或 chaoji-v*.html），请把前端文件和 server.py 放同一目录" % HERE)
        sys.exit(1)

    # 只绑 127.0.0.1，不暴露局域网
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("巢记服务器模式已启动")
    print("  地址     : http://127.0.0.1:%d/" % args.port)
    print("  数据目录 : %s" % DATA_DIR)
    print("  Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
