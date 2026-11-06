#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""server.py HTTP 集成测试（无第三方依赖，python3 test_server_integration.py）

每个测试用独立临时数据目录，通过真实 HTTP 请求验证 server.py 的行为。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")


def _start_server(data_dir):
    proc = subprocess.Popen(
        [sys.executable, "-u", SERVER, "--port", "0", "--dir", data_dir, "--no-browser"],
        cwd=HERE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )
    port = None
    deadline = time.time() + 20
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        m = re.search(r"127\.0\.0\.1:(\d+)", line)
        if m:
            port = int(m.group(1))
            break
    # 继续消费输出，避免管道写满阻塞服务端
    threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()
    if port is None:
        proc.kill()
        raise RuntimeError("server 启动失败")
    return proc, port


class ServerIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = tempfile.mkdtemp(prefix="chaoji_it_")
        cls.proc, cls.port = _start_server(cls.data_dir)
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except Exception:
            cls.proc.kill()
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    # ---------- helpers ----------
    def req(self, path, data=None, headers=None, method=None):
        h = dict(headers or {})
        r = urllib.request.Request(self.base + path, data=data, headers=h,
                                   method=method or ("POST" if data is not None else "GET"))
        try:
            with urllib.request.urlopen(r, timeout=15) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def save(self, name, state, headers=None):
        return self.req("/api/save/" + urllib.parse.quote(name), data=json.dumps(state, ensure_ascii=False).encode("utf-8"), headers=headers)

    def load_json(self, name):
        st, body, _ = self.req("/api/load/" + urllib.parse.quote(name))
        return st, json.loads(body.decode("utf-8"))

    def state(self, title="测试页", html="<p>你好</p>", nb_name="笔记本"):
        return {"app": "chaoji", "ver": 1, "notebooks": [{
            "id": "nb1", "name": nb_name,
            "sections": [{"id": "sec1", "name": "分区", "pages": [
                {"id": "pg1", "title": title, "html": html, "created": 1, "updated": 2, "pinned": False}
            ]}]
        }]}

    def list_files(self):
        st, body, _ = self.req("/api/list")
        self.assertEqual(st, 200)
        j = json.loads(body.decode("utf-8"))
        self.assertTrue(j["ok"])
        return [it["file"] for it in j["items"]]

    # ---------- tests ----------
    def test_01_index_and_ping(self):
        st, body, hdrs = self.req("/")
        self.assertEqual(st, 200)
        self.assertIn(b"<!DOCTYPE html>", body)
        self.assertIn("巢记".encode("utf-8"), body)
        st, body, _ = self.req("/api/ping")
        j = json.loads(body.decode("utf-8"))
        self.assertEqual(st, 200)
        self.assertTrue(j["ok"])
        self.assertEqual(j["mode"], "server")
        self.assertTrue(j["dataDir"])

    def test_02_save_load_roundtrip_chinese(self):
        state = self.state(title="中文标题", html="<p>正文内容 <b>加粗</b></p>", nb_name="工作笔记")
        st, body, _ = self.save("我的笔记.nbk", state)
        self.assertEqual(st, 200, body)
        self.assertTrue(json.loads(body.decode("utf-8"))["ok"])
        st, j = self.load_json("我的笔记.nbk")
        self.assertEqual(st, 200)
        self.assertTrue(j["ok"])
        self.assertEqual(j["state"]["notebooks"][0]["name"], "工作笔记")
        self.assertEqual(j["state"]["notebooks"][0]["sections"][0]["pages"][0]["title"], "中文标题")

    def test_03_list_after_save(self):
        self.save("列表测试.nbk", self.state())
        files = self.list_files()
        self.assertIn("列表测试.nbk", files)
        st, body, _ = self.req("/api/list")
        item = [it for it in json.loads(body.decode("utf-8"))["items"] if it["file"] == "列表测试.nbk"][0]
        self.assertGreater(item["size"], 0)
        self.assertTrue(item["mtime"])

    def test_04_load_missing_returns_404(self):
        st, body, _ = self.req("/api/load/" + urllib.parse.quote("不存在.nbk"))
        self.assertEqual(st, 404)
        self.assertFalse(json.loads(body.decode("utf-8"))["ok"])

    def test_05_invalid_names_rejected(self):
        for bad in ("a/b.nbk", "../x.nbk", "a\\b.nbk", "abc.txt", "CON.nbk", "a:b.nbk", ".hidden.nbk"):
            st, body, _ = self.save(bad, self.state())
            self.assertEqual(st, 400, "%r 应被拒绝，实际 %d" % (bad, st))
        # 路径穿越的 URL 编码形式（用 %5C 避免 urllib 把 ..%2F 归一化成 /api/server.py）
        st, body, _ = self.req("/api/load/%2e%2e%5cserver.nbk")
        self.assertEqual(st, 400)

    def test_06_invalid_body_rejected(self):
        for payload in (b"hello", b"", b"[1,2,3]", b'"str"'):
            st, body, _ = self.req("/api/save/bad.nbk", data=payload)
            self.assertEqual(st, 400, "payload=%r 应返回 400" % payload)

    def test_07_legacy_save_api(self):
        body = json.dumps({"file": "legacy.nbk", "state": self.state(title="旧接口")}, ensure_ascii=False).encode("utf-8")
        st, resp, _ = self.req("/api/save", data=body)
        self.assertEqual(st, 200, resp)
        st, j = self.load_json("legacy.nbk")
        self.assertEqual(j["state"]["notebooks"][0]["sections"][0]["pages"][0]["title"], "旧接口")

    def test_08_rename_and_delete(self):
        self.save("改名前.nbk", self.state())
        st, body, _ = self.req("/api/rename/" + urllib.parse.quote("改名前.nbk") + "/" + urllib.parse.quote("改名后.nbk"), data=b"")
        self.assertEqual(st, 200, body)
        self.assertIn("改名后.nbk", self.list_files())
        self.assertNotIn("改名前.nbk", self.list_files())
        # 重命名到已存在文件应被拒绝
        self.save("占用名.nbk", self.state())
        st, body, _ = self.req("/api/rename/" + urllib.parse.quote("改名后.nbk") + "/" + urllib.parse.quote("占用名.nbk"), data=b"")
        self.assertEqual(st, 400)
        # 删除
        st, body, _ = self.req("/api/delete/" + urllib.parse.quote("占用名.nbk"), data=b"")
        self.assertEqual(st, 200, body)
        self.assertNotIn("占用名.nbk", self.list_files())
        st, body, _ = self.req("/api/delete/" + urllib.parse.quote("占用名.nbk"), data=b"")
        self.assertEqual(st, 404)

    def test_09_bom_file_loads(self):
        with open(os.path.join(self.data_dir, "bom.nbk"), "wb") as f:
            f.write(b"\xef\xbb\xbf" + json.dumps(self.state(title="BOM")).encode("utf-8"))
        st, j = self.load_json("bom.nbk")
        self.assertEqual(st, 200)
        self.assertTrue(j["ok"])
        self.assertEqual(j["state"]["notebooks"][0]["sections"][0]["pages"][0]["title"], "BOM")

    def test_10_invalid_json_file_returns_400(self):
        with open(os.path.join(self.data_dir, "broken.nbk"), "wb") as f:
            f.write(b"not a json")
        st, body, _ = self.req("/api/load/broken.nbk")
        self.assertEqual(st, 400)
        self.assertFalse(json.loads(body.decode("utf-8"))["ok"])
        # 空文件返回空笔记本（不是错误）
        open(os.path.join(self.data_dir, "empty.nbk"), "wb").close()
        st, j = self.load_json("empty.nbk")
        self.assertEqual(st, 200)
        self.assertEqual(j["state"]["notebooks"], [])

    def test_11_csrf_origin_host_checks(self):
        state = self.state()
        # 跨源写请求拒绝
        st, _, _ = self.save("csrf.nbk", state, headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        # 同源写请求放行
        st, _, _ = self.save("csrf.nbk", state, headers={"Origin": self.base})
        self.assertEqual(st, 200)
        # Origin: null 拒绝
        st, _, _ = self.save("csrf2.nbk", state, headers={"Origin": "null"})
        self.assertEqual(st, 403)
        # 伪造 Host 拒绝（DNS rebinding）
        st, _, _ = self.req("/api/list", headers={"Host": "evil.example:%d" % self.port})
        self.assertEqual(st, 403)
        # 无 Origin（脚本/curl）放行
        st, _, _ = self.save("curl.nbk", state)
        self.assertEqual(st, 200)

    def test_12_concurrent_saves_not_corrupted(self):
        payloads = [json.dumps({"app": "chaoji", "ver": 1, "pad": "x" * (1000 + i * 700), "notebooks": []}).encode("utf-8") for i in range(12)]
        errors = []

        def worker(i):
            st, body, _ = self.req("/api/save/race.nbk", data=payloads[i])
            if st != 200:
                errors.append((i, st, body[:120]))

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        with open(os.path.join(self.data_dir, "race.nbk"), "rb") as f:
            raw = f.read()
        parsed = json.loads(raw.decode("utf-8"))
        self.assertIn("pad", parsed)
        self.assertTrue(parsed["pad"].startswith("x"))
        # 无临时文件残留
        leftovers = [f for f in os.listdir(self.data_dir) if ".tmp." in f]
        self.assertEqual(leftovers, [])

    def test_13_unknown_route_404(self):
        st, _, _ = self.req("/api/nope")
        self.assertEqual(st, 404)
        st, _, _ = self.req("/no-such-file.nbk")
        self.assertEqual(st, 404)

    def test_14_method_not_allowed(self):
        st, _, _ = self.req("/api/list", data=b"{}", method="PUT")
        self.assertEqual(st, 405)

    def test_15_content_type_and_body(self):
        # 无 Content-Length 的 POST 视为空 body
        st, body, _ = self.req("/api/save/nolen.nbk", data=b"")
        self.assertEqual(st, 400)


if __name__ == "__main__":
    import urllib.parse  # noqa: F401  (供 helper 使用)
    unittest.main(verbosity=2)
