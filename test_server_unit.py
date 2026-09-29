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

print("ALL PASS")
