#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
巢记 ChaoJi 本地服务器模式 · 极简依赖版 v2
==========================================
设计目标：公司内网"有 python3 但可能缺标准库、无 sudo"的机器也能跑。

依赖策略（从最不可能缺失到可能缺失，全部有兜底）：
  os / sys   —— Python 内核，任何环境都有
  socket     —— 内置 C 模块，极可靠；仍包 try 给出中文指引
  time       —— 仅用于打印时间戳，缺失不影响功能
  threading  —— 缺失退回单线程串行（单用户笔记足够）
  webbrowser —— 仅影响自动开浏览器，缺失打印手动地址
  不使用：json / http.server / argparse / urllib / re / datetime

  JSON 解析职责移到前端（浏览器本来就有 JSON.parse）；服务器只做文件读写与原样转发。

用法：
    python3 server.py              # 数据目录 = 脚本所在目录，端口 8765
    python3 server.py --port 8899 --dir ~/notes
    python3 server.py --no-browser
"""
import os
import sys

# ---------- 可选基础模块（全部有兜底） ----------
try:
    import time
    def now_str():
        return time.strftime("%Y-%m-%d %H:%M:%S")
except ImportError:
    time = None
    def now_str():
        return ""

try:
    import socket
except ImportError:
    print("[错误] socket 模块不可用——这个 Python 精简到无法运行服务器模式")
    print("       请改用浏览器文件模式：直接双击 chaoji-*.html 即可使用全部功能")
    sys.exit(1)

try:
    import threading
except ImportError:
    threading = None

try:
    import webbrowser
except ImportError:
    webbrowser = None

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = HERE


def find_frontend():
    """优先 chaoji-vX.Y.html 中版本号最大的，否则 chaoji.html（不用 re）"""
    best, best_key = None, (-1, -1)
    try:
        names = os.listdir(HERE)
    except OSError:
        names = []
    for fn in names:
        if not (fn.startswith("chaoji-v") and fn.endswith(".html")):
            continue
        mid = fn[len("chaoji-v"):-len(".html")]
        parts = mid.split(".")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            key = (int(parts[0]), int(parts[1]))
            if key > best_key:
                best, best_key = fn, key
    if best:
        return best
    return "chaoji.html" if os.path.exists(os.path.join(HERE, "chaoji.html")) else None


def valid_nbk_name(name):
    """不用 re 的文件名白名单：字母/数字/下划线/中文/点/连字符，.nbk 结尾，禁路径分隔"""
    if not name or not name.endswith(".nbk") or len(name) > 200:
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    if name.startswith("."):
        return False
    for ch in name:
        o = ord(ch)
        if not (ch in "-._" or 0x30 <= o <= 0x39 or 0x41 <= o <= 0x5A
                or 0x61 <= o <= 0x7A or o >= 0x80):
            return False
    return True


def percent_decode(s):
    """不用 urllib 的最小 URL 解码（中文文件名场景）"""
    out = []
    i = 0
    HEX = "0123456789abcdefABCDEF"
    while i < len(s):
        c = s[i]
        if c == "%" and i + 2 < len(s) and s[i + 1] in HEX and s[i + 2] in HEX:
            # 按 UTF-8 字节解码（收集连续的 %XX 再统一 decode）
            bts = []
            while i + 2 < len(s) + 1 and i < len(s) and s[i] == "%" \
                    and i + 2 < len(s) and s[i + 1] in HEX and s[i + 2] in HEX:
                bts.append(int(s[i + 1:i + 3], 16))
                i += 3
            try:
                out.append(bytes(bts).decode("utf-8"))
            except UnicodeDecodeError:
                out.append("".join(chr(b) for b in bts))
        else:
            out.append(c)
            i += 1
    return "".join(out)


# ---------- 极简 HTTP 层（只依赖 socket） ----------

def parse_http_request(conn):
    """读一个 HTTP 请求，返回 (method, path, headers, body) 或 None"""
    conn.settimeout(10)
    data = b""
    while b"\r\n\r\n" not in data and len(data) < 65536:
        c = conn.recv(4096)
        if not c:
            break
        data += c
    if b"\r\n\r\n" not in data:
        return None
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("utf-8", "replace").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 2:
        return None
    method, target = parts[0].upper(), parts[1]
    headers = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    try:
        cl = int(headers.get("content-length", "0") or 0)
    except ValueError:
        cl = 0
    while len(body) < cl and cl < 220 * 1024 * 1024:
        c = conn.recv(65536)
        if not c:
            break
        body += c
    body = body[:cl] if cl else body
    if "?" in target:
        target = target.split("?", 1)[0]
    return method, target, headers, body


def http_response(conn, code, body, ctype="application/json; charset=utf-8"):
    reason = {200: "OK", 400: "Bad Request", 404: "Not Found",
              405: "Method Not Allowed", 500: "Server Error"}.get(code, "OK")
    head = ("HTTP/1.1 %d %s\r\n" % (code, reason)) + \
        ("Content-Type: %s\r\n" % ctype) + \
        ("Content-Length: %d\r\n" % len(body)) + \
        "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
    conn.sendall(head.encode("utf-8") + body)


class MiniHTTPServer:
    """只依赖 socket 的极简 HTTP 服务器，只绑本机回环"""

    def __init__(self, host, port, handler):
        self.handler = handler
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]

    def serve_forever(self):
        while True:
            conn, _addr = self.sock.accept()
            if threading is not None:
                t = threading.Thread(target=self._safe, args=(conn,))
                t.daemon = True
                t.start()
            else:
                self._safe(conn)

    def _safe(self, conn):
        try:
            req = parse_http_request(conn)
            if req:
                self.handler(req[0], req[1], req[2], req[3], conn)
            else:
                http_response(conn, 400, b'{"error":"bad request"}')
        except Exception as e:
            try:
                msg = ('{"error":"%s"}' % str(e).replace('"', "'")).encode("utf-8", "replace")
                http_response(conn, 500, msg)
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


# ---------- 路由 ----------

def route(method, path, headers, body, conn):
    if method not in ("GET", "POST"):
        return http_response(conn, 405, b'{"error":"method not allowed"}')

    if path in ("/", "/index.html"):
        fn = find_frontend()
        if not fn:
            return http_response(conn, 500,
                "未找到巢记前端 HTML，请与 server.py 放同一目录".encode("utf-8"),
                "text/plain; charset=utf-8")
        with open(os.path.join(HERE, fn), "rb") as f:
            return http_response(conn, 200, f.read(), "text/html; charset=utf-8")

    if path == "/favicon.ico":
        return http_response(conn, 404, b"", "text/plain")

    if path == "/api/ping" and method == "GET":
        resp = '{"ok":true,"mode":"server","dataDir":"%s"}' % DATA_DIR.replace("\\", "/")
        return http_response(conn, 200, resp.encode("utf-8"))

    if path == "/api/list" and method == "GET":
        return api_list(conn)

    if path.startswith("/api/load/") and method == "GET":
        return api_load(percent_decode(path[len("/api/load/"):]), conn)

    if path.startswith("/api/save/") and method == "POST":
        return api_save(percent_decode(path[len("/api/save/"):]), body, conn)

    # 兼容旧版前端 POST /api/save {file, state} —— 需要 json；若 json 缺失则要求新路径
    if path == "/api/save" and method == "POST":
        return api_save_jsonbody(body, conn)

    return http_response(conn, 404, b'{"error":"not found"}')


def api_list(conn):
    items = []
    try:
        names = sorted(os.listdir(DATA_DIR))
    except OSError:
        names = []
    for fn in names:
        if not fn.endswith(".nbk"):
            continue
        full = os.path.join(DATA_DIR, fn)
        try:
            mt = os.path.getmtime(full)
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mt)) if time else ""
        except OSError:
            ts = ""
        try:
            size = os.path.getsize(full)
        except OSError:
            size = 0
        items.append('{"file":"%s","size":%d,"mtime":"%s"}' % (fn, size, ts))
    return http_response(conn, 200, ('{"ok":true,"items":[%s]}' % ",".join(items)).encode())


def safe_path(name):
    if not valid_nbk_name(name):
        return None
    return os.path.join(DATA_DIR, name)


def atomic_write(full, data):
    tmp = full + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, full)


def api_load(name, conn):
    full = safe_path(name)
    if not full or not os.path.exists(full):
        return http_response(conn, 404, b'{"error":"file not found"}')
    with open(full, "rb") as f:
        return http_response(conn, 200,
            ('{"ok":true,"file":"%s","state":' % name).encode() + f.read() + b"}")
    # 注：state 直接内嵌文件字节（文件本身是合法 JSON object），省掉服务端 JSON 解析


def api_save(name, body, conn):
    full = safe_path(name)
    if not full:
        return http_response(conn, 400,
            "文件名不合法或未以 .nbk 结尾（禁止路径分隔与特殊字符）".encode("utf-8"))
    if len(body) > 200 * 1024 * 1024:
        return http_response(conn, 400, b'{"error":"body too large"}')
    # body 即 state 的 JSON 字节（由前端 JSON.stringify 产生），服务端不解析直接原子写
    tmp = full + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, full)
    except OSError as e:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return http_response(conn, 500,
            ('{"error":"save failed: %s"}' % str(e).replace('"', "'")).encode("utf-8", "replace"))
    return http_response(conn, 200,
        ('{"ok":true,"file":"%s","savedAt":"%s"}' % (name, now_str())).encode())


def api_save_jsonbody(body, conn):
    """旧版协议兼容：POST /api/save 携带 {"file":..., "state":...} —— 需要 json 模块"""
    try:
        import json
    except ImportError:
        return http_response(conn, 500,
            b'{"error":"\u6b64\u73af\u5883\u7f3a json \u6a21\u5757\uff0c\u8bf7\u5347\u7ea7\u524d\u7aef\u6216\u6362\u5b8c\u6574 Python"}')
    try:
        payload = json.loads(body.decode("utf-8"))
        name = payload.get("file", "")
        state = payload.get("state")
    except Exception:
        return http_response(conn, 400, b'{"error":"bad json body"}')
    if not isinstance(state, dict):
        return http_response(conn, 400, b'{"error":"state must be object"}')
    data = json.dumps(state, ensure_ascii=False).encode("utf-8")
    return api_save(name, data, conn)


# ---------- 启动 ----------

def main():
    global DATA_DIR
    # 手工解析参数（不依赖 argparse）
    port, dir_arg, no_browser = 8765, None, False
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--port" and i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                pass
            i += 2
        elif a == "--dir" and i + 1 < len(argv):
            dir_arg = argv[i + 1]
            i += 2
        elif a == "--no-browser":
            no_browser = True
            i += 1
        elif a in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        else:
            i += 1

    if dir_arg:
        DATA_DIR = os.path.expanduser(dir_arg)
    if not os.path.isdir(DATA_DIR):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
        except OSError as e:
            print("[错误] 数据目录不可创建：%s（%s）" % (DATA_DIR, e))
            print("       可用 --dir 指定一个可写目录，如 --dir /tmp/chaoji")
            sys.exit(1)

    if not find_frontend():
        print("[错误] 未在 %s 找到 chaoji.html（或 chaoji-v*.html）" % HERE)
        print("       请把前端 HTML 和 server.py 放同一目录")
        sys.exit(1)

    try:
        srv = MiniHTTPServer("127.0.0.1", port, route)
    except OSError as e:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
        probe.close()
        print("[提示] 端口 %d 被占用(%s)，自动改用 %d" % (port, e, free))
        srv = MiniHTTPServer("127.0.0.1", free, route)

    url = "http://127.0.0.1:%d/" % srv.port
    print("巢记服务器模式已启动（极简依赖版 v2）")
    print("  地址      : " + url)
    print("  数据目录  : %s" % DATA_DIR)
    if threading is None:
        print("  [提示] threading 缺失，已退回单线程模式（单用户无影响）")
    print("  Ctrl+C 停止")

    if not no_browser:
        def open_browser():
            if time:
                time.sleep(0.6)
            if webbrowser is None:
                print("  [提示] webbrowser 缺失，请手动访问 " + url)
                return
            try:
                if not webbrowser.open(url, new=2):
                    print("  [提示] 请手动访问 " + url)
            except Exception as e:
                print("  [提示] 自动打开浏览器失败(%s)，请访问 %s" % (e, url))
        if threading is not None:
            t = threading.Thread(target=_open_browser, args=(url,))
            t.daemon = True
            t.start()
        else:
            _open_browser(url)

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        try:
            srv.sock.close()
        except Exception:
            pass


def _open_browser(url):
    if time:
        time.sleep(0.6)
    if webbrowser is None:
        print("  [提示] webbrowser 缺失，请手动访问 " + url)
        return
    try:
        if not webbrowser.open(url, new=2):
            print("  [提示] 请手动访问 " + url)
    except Exception as e:
        print("  [提示] 自动打开浏览器失败(%s)，请访问 %s" % (e, url))


if __name__ == "__main__":
    main()
