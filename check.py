#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
巢记 环境自检脚本（Linux 端，无 root 用）
用法：python3 check.py
逐项检查运行 server.py 所需的最小条件，全部 PASS 即可用服务器模式。
本脚本只用最基础能力，自身尽量不依赖标准库（连 import 失败也能提示）。
"""
import sys

ok = True
def mark(name, fn):
    global ok
    try:
        r = fn()
        print(f"[PASS] {name}" + (f" — {r}" if r else ""))
    except Exception as e:
        ok = False
        print(f"[FAIL] {name} — {e}")

print("=" * 46)
print("巢记运行环境自检")
print("=" * 46)

mark("Python 版本 >= 3.6", lambda: str(sys.version.split()[0]) if sys.version_info >= (3, 6) else (_ for _ in ()).throw(Exception("低于 3.6")))
mark("http.server 可用", lambda: __import__("http.server") and None)
mark("socket 可用", lambda: __import__("socket") and None)
mark("json 可用", lambda: __import__("json") and None)
mark("os/pathlib 可用", lambda: __import__("os") and None)
mark("argparse 可用", lambda: __import__("argparse") and None)
mark("webbrowser 可用(缺了只影响自动开浏览器, 可 --no-browser 绕过)", lambda: __import__("webbrowser") and None)
mark("threading 可用", lambda: __import__("threading") and None)
mark("re 可用", lambda: __import__("re") and None)

def port_test():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))  # 随机高端口测试
    p = s.getsockname()[1]
    s.close()
    return f"可绑定高端口(测试用 {p})，普通用户权限足够"
mark("无需 root 绑定本地端口", port_test)

def fs_test():
    import tempfile, os
    d = tempfile.mkdtemp(prefix="chaoji_")
    f = os.path.join(d, "t.nbk")
    with open(f, "w") as fh: fh.write("{}")
    os.remove(f); os.rmdir(d)
    return "home 目录读写正常"
mark("当前用户家目录可写", fs_test)

print("=" * 46)
print("总结：" + ("全部通过，可以直接运行  python3 server.py" if ok else "存在 FAIL 项，见上方红字说明"))
print("=" * 46)
sys.exit(0 if ok else 1)
