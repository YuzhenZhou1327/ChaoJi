#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""server.py 纯函数单测（无框架，python3 test_server_unit.py）"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import valid_nbk_name, percent_decode, json_escape

def expect(cond, msg):
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("PASS:", msg)

# valid_nbk_name
expect(valid_nbk_name("我的笔记本.nbk"), "中文文件名合法")
expect(valid_nbk_name("note-1_v2.nbk"), "ASCII 合法")
expect(valid_nbk_name("工作笔记 (2026) [副本].nbk"), "含空格与括号的文件名合法")
expect(not valid_nbk_name(".nbk"), "拒绝空主文件名 .nbk")
expect(not valid_nbk_name(" leading.nbk"), "拒绝空格开头")
expect(not valid_nbk_name("../etc.nbk"), "拒绝 ..")
expect(not valid_nbk_name("a/b.nbk"), "拒绝路径分隔 /")
expect(not valid_nbk_name("a\\b.nbk"), "拒绝路径分隔 \\")
expect(not valid_nbk_name(".hidden.nbk"), "拒绝点开头")
expect(not valid_nbk_name("x.txt"), "拒绝非 .nbk")
expect(not valid_nbk_name(""), "拒绝空名")
expect(not valid_nbk_name('q"uote.nbk'), "拒绝引号")
expect(not valid_nbk_name("a" * 201 + ".nbk"), "拒绝超长")

# percent_decode
expect(percent_decode("%E4%B8%AD%E6%96%87.nbk") == "中文.nbk", "解码 UTF-8 中文")
expect(percent_decode("abc.nbk") == "abc.nbk", "无转义原样")
expect(percent_decode("a%20b.nbk") == "a b.nbk", "解码空格")
expect(percent_decode("%ZZ") == "%ZZ", "非法转义保留")

# json_escape
expect(json_escape('a"b') == 'a\\"b', "转义双引号")
expect(json_escape("a\\b") == "a\\\\b", "转义反斜杠")
expect(json_escape("a\nb") == "a\\u000ab", "转义控制字符")
expect(json_escape("中文") == "中文", "中文原样")

expect(not valid_nbk_name("a .nbk"), "拒绝主文件名以空格结尾")
expect(not valid_nbk_name("a..nbk"), "拒绝主文件名以点结尾")
expect(valid_nbk_name("a" * 196 + ".nbk"), "长度边界内合法")
expect(not valid_nbk_name("a" * 201 + ".nbk"), "拒绝超长")

# Windows 保留设备名（即使带扩展名也无法创建）
import server as _server
for _reserved in ("CON.nbk", "con.nbk", "NUL.nbk", "AUX.nbk", "PRN.nbk", "COM1.nbk", "COM9.nbk", "LPT1.nbk", "LPT9.nbk"):
    expect(not valid_nbk_name(_reserved), "拒绝 Windows 保留名 %s" % _reserved)
expect(valid_nbk_name("my.CON.nbk"), "允许保留名出现在中间 my.CON.nbk")

# safe_origin：同源校验（防 CSRF / DNS rebinding）
_server._BOUND_PORT = 8765
expect(_server.safe_origin({"host": "127.0.0.1:8765"}), "Host 127.0.0.1 放行")
expect(_server.safe_origin({"host": "localhost:8765"}), "Host localhost 放行")
expect(_server.safe_origin({"host": "127.0.0.1:8765", "origin": "http://127.0.0.1:8765"}), "同源 Origin 放行")
expect(_server.safe_origin({"host": "127.0.0.1:8765", "origin": "HTTP://LOCALHOST:8765"}), "Origin 大小写不敏感")
expect(not _server.safe_origin({"host": "127.0.0.1:8765", "origin": "http://evil.example"}), "跨源 Origin 拒绝")
expect(not _server.safe_origin({"host": "127.0.0.1:8765", "origin": "http://127.0.0.1:9999"}), "不同端口 Origin 拒绝")
expect(not _server.safe_origin({"host": "127.0.0.1:8765", "origin": "null"}), "Origin: null 拒绝")
expect(not _server.safe_origin({"host": "evil.example:8765"}), "伪造 Host 拒绝")
expect(_server.safe_origin({}), "无 Host/Origin 的非浏览器客户端放行")
_server._BOUND_PORT = 0

# atomic_write：并发安全（唯一临时文件，写后无残留）
import tempfile, threading as _threading
_tmpdir = tempfile.mkdtemp(prefix="chaoji_unit_")
_target = os.path.join(_tmpdir, "x.nbk")
_server.atomic_write(_target, b"one")
expect(open(_target, "rb").read() == b"one", "atomic_write 写入内容")
def _race(i):
    _server.atomic_write(_target, ('{"i":%d}' % i).encode())
_ts = [_threading.Thread(target=_race, args=(i,)) for i in range(8)]
[t.start() for t in _ts]
[t.join() for t in _ts]
_final = open(_target, "rb").read()
expect(_final.startswith(b'{"i":') and _final.endswith(b"}"), "并发 atomic_write 后文件完整")
expect(not [f for f in os.listdir(_tmpdir) if ".tmp." in f], "atomic_write 无临时文件残留")
for _f in os.listdir(_tmpdir):
    os.remove(os.path.join(_tmpdir, _f))
os.rmdir(_tmpdir)

print("ALL PASS")
