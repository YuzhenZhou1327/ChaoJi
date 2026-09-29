#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
巢记 环境自检脚本（内网/无 root/精简 Python 通用）
用法：python3 check.py

对齐 server.py 极简依赖版 v2：
  必需：Python>=3.6、socket、可写数据目录、可绑本地端口
  可选：time（时间戳）/ threading（并发）/ webbrowser（自动开浏览器）
  不再检查 http.server / json / argparse / re —— v2 手写 HTTP、不解析 JSON
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

def mark_opt(name, fn):
    """可选项：失败只警告，不算 FAIL"""
    try:
        r = fn()
        print(f"[PASS] {name}" + (f" — {r}" if r else ""))
    except Exception as e:
        print(f"[WARN] {name} — {e}（可选，缺失时功能降级）")

print("=" * 46)
print("巢记运行环境自检（server v2）")
print("=" * 46)

mark("Python 版本 >= 3.6",
     lambda: str(sys.version.split()[0]) if sys.version_info >= (3, 6)
     else (_ for _ in ()).throw(Exception("低于 3.6")))

mark("socket 可用（服务器模式必需）", lambda: __import__("socket") and None)

def port_test():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return f"可绑定 127.0.0.1 高端口(测试用 {p})，无需 root"
mark("无需 root 绑定本地回环端口", port_test)

def fs_test():
    import tempfile, os
    d = tempfile.mkdtemp(prefix="chaoji_")
    f = os.path.join(d, "t.nbk")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("{}")
    os.replace(f, f + ".bak")  # 原子写路径同款 API
    os.remove(f + ".bak")
    os.rmdir(d)
    return "临时目录读写/原子改名正常"
mark("数据目录可写（含原子 rename）", fs_test)

mark_opt("time 可用（时间戳打印）", lambda: __import__("time") and None)
mark_opt("threading 可用（并发处理）", lambda: __import__("threading") and None)
mark_opt("webbrowser 可用（自动开浏览器）", lambda: __import__("webbrowser") and None)

print("=" * 46)
print("总结：" + (
    "全部必需项通过，可以运行  python3 server.py"
    if ok else "存在 FAIL 项；若仅缺可选模块，服务器仍可 --no-browser 运行；"
               "完全无法起服务时，双击 chaoji-*.html 用浏览器文件模式"))
print("=" * 46)
sys.exit(0 if ok else 1)
