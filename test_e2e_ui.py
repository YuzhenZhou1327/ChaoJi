#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
巢记 ChaoJi v2.0 · 真实浏览器 E2E 测试（Playwright + Python，零外部测试框架）
================================================================================
特点
  - 直接 `python test_e2e_ui.py` 运行，无需 pytest；
  - 使用系统已安装 Chrome：chromium.launch(channel="chrome")，**不会** 触发
    `playwright install` 下载浏览器（只有系统 Chrome 不可用时才把 channel 换成
    executable_path，仍指向本机 Chrome）；
  - 每个用例独立 BrowserContext（独立 localStorage / IndexedDB / Cookie），
    服务器模式用例独立临时数据目录 + `--port 0` 随机端口，结束后一定清理子进程；
  - 监听 pageerror 与 console error，任何未预期的 JS 异常 / 控制台错误都判失败，
    仅白名单两类“设计内探测噪声”（见 EXPECTED_CONSOLE_RULES，报告里会注明）；
  - 失败时自动保存截图 + 页面 DOM + 错误清单到临时目录（不写入仓库）。

用法
  python test_e2e_ui.py                 # 跑全部用例
  python test_e2e_ui.py --list          # 列出用例
  python test_e2e_ui.py --filter md,search
  python test_e2e_ui.py --headed        # 有头模式（观察浏览器操作）
  python test_e2e_ui.py --slow 250      # 每步放慢 250ms
  python test_e2e_ui.py --keep          # 全部通过也保留截图/临时目录

退出码
  0 = 全部通过（允许 SKIP）；1 = 有失败；2 = 环境缺失（未装 playwright / 找不到 Chrome），
  供 run_all_tests.py 判为 SKIP。

环境变量
  CHAOJI_CHROME      指定 Chrome/Edge 可执行文件路径
  CHAOJI_E2E_ARTIFACTS  截图与失败现场根目录（默认 D:\\dsh\\tmp\\chaoji-e2e\\artifacts）
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

# Windows 控制台默认 GBK：先确保输出 UTF-8，避免中文输出抛 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        enc = (getattr(_stream, "encoding", "") or "").lower()
        if enc not in ("utf-8", "utf8", "utf-8-sig"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
FRONTEND = HERE / "chaoji-v2.0.html"
SERVER_PY = HERE / "server.py"
SUMMAR_MARKER = "__E2E_SUMMARY__ "
DEFAULT_ARTIFACT_ROOT = Path(os.environ.get("CHAOJI_E2E_ARTIFACTS", r"D:\dsh\tmp\chaoji-e2e\artifacts"))

# 1x1 透明 PNG，用于“插入图片”用例（走应用内 compressImage → data:image 内联）
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


# ============================================================================
# 用例注册
# ============================================================================
TESTS: list[tuple[str, str, object]] = []


def e2e(name: str, mode: str = "server"):
    """注册一个 E2E 用例；mode: 'server'（真实 server.py）或 'file'（file:// 文件模式）。"""
    assert mode in ("server", "file"), mode

    def deco(fn):
        TESTS.append((name, mode, fn))
        return fn

    return deco


class SkipTest(Exception):
    """用例内部主动跳过（环境能力缺失，例如某浏览器不支持 File System API）。"""


# ============================================================================
# 基础设施：Chrome 定位 / 浏览器启动 / TestCase
# ============================================================================
def find_chrome() -> str | None:
    env = (os.environ.get("CHAOJI_CHROME") or "").strip()
    candidates = [env] if env else []
    candidates += [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


def launch_browser(pw, *, headed: bool, slow: int, prefer: str, log):
    """优先 channel='chrome'（系统 Chrome），失败再回退 executable_path。绝不下载浏览器。"""
    kwargs = dict(
        headless=not headed,
        slow_mo=slow or 0,
        args=[
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-features=Translate,MediaRouter",
            "--disable-popup-blocking",
        ],
    )
    errors = []
    if prefer in ("chrome", "auto"):
        try:
            b = pw.chromium.launch(channel="chrome", **kwargs)
            log(f"浏览器: 系统 Chrome（channel='chrome'） v{b.version}")
            return b
        except Exception as e:  # noqa: BLE001
            errors.append(f"channel=chrome 启动失败: {e}")
    if prefer in ("edge", "auto"):
        try:
            b = pw.chromium.launch(channel="msedge", **kwargs)
            log(f"浏览器: 系统 Edge（channel='msedge'） v{b.version}")
            return b
        except Exception as e:  # noqa: BLE001
            errors.append(f"channel=msedge 启动失败: {e}")
    exe = find_chrome()
    if exe:
        try:
            b = pw.chromium.launch(executable_path=exe, **kwargs)
            log(f"浏览器: {exe}（executable_path） v{b.version}")
            return b
        except Exception as e:  # noqa: BLE001
            errors.append(f"executable_path={exe} 启动失败: {e}")
    raise RuntimeError("无法启动本机 Chromium 内核浏览器：\n  " + "\n  ".join(errors))


# 白名单说明（写进失败报告，便于 review）：
#   1) 文件模式：应用启动时会 fetch('/api/ping') 探测服务器模式；file:// 下该请求
#      必然被 CORS 拒绝（应用 catch 后走文件模式），Chrome 会打印 1~2 条控制台错误。
#      这是设计内行为，不是 bug；其余 file:// 模式下的 console error 一律判失败。
#   2) 服务器模式：新建 .nbk 时 serverLoadFile() 先 GET /api/load/<新文件名>，
#      服务器返回 404 后前端走“不存在则创建”分支。Chrome 会打印 1 条
#      "Failed to load resource: ... 404"，属设计内探测；其余 404/错误一律判失败。
def _expected_console(reason_mode: str, text: str, loc_url: str) -> str | None:
    if reason_mode == "file":
        if "/api/ping" in text and "CORS policy" in text:
            return "file:// 下 /api/ping 探测被 CORS 拒绝（应用已 catch，设计内）"
        if "Failed to load resource" in text and "net::ERR_FAILED" in text:
            return "file:// 下 /api/ping 探测失败伴随的资源错误（设计内）"
    else:
        if "Failed to load resource" in text and "404" in text and "/api/load/" in loc_url:
            return "新建 .nbk 前 GET /api/load 探测 404（serverLoadFile 的“不存在则创建”分支，设计内）"
    return None


class Case:
    """一个独立用例：独立 context + 独立临时目录 + 独立服务器子进程。"""

    ACTION_TIMEOUT = 15000

    def __init__(self, runtime, name: str, mode: str, index: int):
        self.rt = runtime
        self.name = name
        self.mode = mode
        self.index = index
        self.tmpdir = Path(tempfile.mkdtemp(prefix="chaoji-e2e-"))
        self.data_dir = self.tmpdir / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir = runtime.artifacts_dir
        self.server_proc: subprocess.Popen | None = None
        self.server_url: str | None = None
        self.server_stdout: list[str] = []
        self._drain_thread: threading.Thread | None = None
        self.console_msgs: list[dict] = []
        self.page_errors: list[str] = []
        self.dialogs: list[str] = []

        self.context = runtime.browser.new_context(
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        self.context.set_default_timeout(self.ACTION_TIMEOUT)
        self.page = self.context.new_page()
        self.page.set_default_navigation_timeout(20000)
        self.page.on("console", self._on_console)
        self.page.on("pageerror", self._on_pageerror)
        self.page.on("dialog", self._on_dialog)

    # ---------- 事件收集 ----------
    def _on_console(self, msg):
        if msg.type != "error":
            return
        loc = msg.location or {}
        text = msg.text or ""
        url = loc.get("url") or ""
        reason = _expected_console(self.mode, text, url)
        self.console_msgs.append({"text": text, "url": url, "expected_reason": reason})
        if reason:
            self.rt.log_verbose(f"    [白名单 console] {text}  ← {reason}")

    def _on_pageerror(self, exc):
        self.page_errors.append(str(exc))

    def _on_dialog(self, dialog):
        self.dialogs.append(f"{dialog.type}: {dialog.message}")
        try:
            dialog.dismiss()
        except Exception:
            pass

    # ---------- 服务器子进程 ----------
    def start_server(self):
        assert SERVER_PY.is_file(), f"找不到 server.py: {SERVER_PY}"
        cmd = [sys.executable, "-u", str(SERVER_PY), "--port", "0",
               "--dir", str(self.data_dir), "--no-browser"]
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        self.server_proc = subprocess.Popen(
            cmd, cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, bufsize=0,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            line = self.server_proc.stdout.readline()
            if not line:
                if self.server_proc.poll() is not None:
                    break
                continue
            text = line.decode("utf-8", "replace")
            self.server_stdout.append(text)
            m = re.search(r"http://127\.0\.0\.1:\d+/", text)
            if m:
                self.server_url = m.group(0)
                break
        self._drain_thread = threading.Thread(target=self._drain_server_stdout, daemon=True)
        self._drain_thread.start()
        if not self.server_url:
            out = "".join(self.server_stdout)[-1500:]
            raise AssertionError(f"server.py 未能在 30s 内启动。输出：\n{out}")
        self.rt.log_verbose(f"    服务器: pid={self.server_proc.pid} url={self.server_url} dir={self.data_dir}")

    def _drain_server_stdout(self):
        try:
            for raw in iter(self.server_proc.stdout.readline, b""):
                self.server_stdout.append(raw.decode("utf-8", "replace"))
        except Exception:
            pass

    def stop_server(self):
        proc = self.server_proc
        if not proc:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        except Exception:
            pass
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass
        self.server_proc = None

    # ---------- 导航与等待 ----------
    @property
    def url(self) -> str:
        return self.server_url or FRONTEND.as_uri()

    def goto(self, wait_until: str = "load"):
        self.page.goto(self.url, wait_until=wait_until)
        self.page.wait_for_function(
            "() => document.readyState === 'complete'", timeout=self.ACTION_TIMEOUT
        )
        self.page.wait_for_timeout(250)

    def wait_fn(self, expression: str, arg=None, timeout: int | None = None):
        return self.page.wait_for_function(
            expression, arg=arg, timeout=timeout or self.ACTION_TIMEOUT
        )

    def wait_visible(self, selector: str, timeout: int | None = None):
        self.page.wait_for_selector(selector, state="visible", timeout=timeout or self.ACTION_TIMEOUT)

    def wait_modal(self):
        self.wait_visible(".modal-mask")

    def wait_modal_gone(self):
        self.page.wait_for_selector(".modal-mask", state="detached")

    def wait_toast(self, substring: str, timeout: int = 10000):
        self.wait_fn(
            "(s) => { const t = document.getElementById('toast'); "
            "return t && t.textContent.includes(s); }",
            arg=substring, timeout=timeout,
        )

    def wait_file_state(self, substring: str, timeout: int = 15000):
        self.wait_fn(
            "(s) => { const el = document.getElementById('fileState'); "
            "return el && el.textContent.includes(s); }",
            arg=substring, timeout=timeout,
        )

    def wait_layout_ready(self):
        """等待异步启动逻辑结束：出现导航项 / 空状态 / 恢复横幅 之一。"""
        self.wait_fn(
            "() => { const vis = el => el && getComputedStyle(el).display !== 'none';"
            "return !!document.querySelector('#nbList .nav-item') || vis(document.getElementById('emptyState'))"
            " || vis(document.getElementById('handleBanner')) || vis(document.getElementById('draftBanner')); }"
        )
        self.page.wait_for_timeout(200)

    # ---------- 弹窗 / 下载 / 文件选择 ----------
    def modal_fill(self, value: str):
        self.page.fill(".modal-mask input", value)

    def modal_ok(self):
        self.page.click(".modal-mask .m-ok")

    def modal_click_button(self, label: str):
        self.page.locator(".modal-mask").get_by_role("button", name=label).first.click()

    def download(self, trigger, save_name: str | None = None) -> Path:
        with self.page.expect_download(timeout=self.ACTION_TIMEOUT) as info:
            trigger()
        dl = info.value
        fn = save_name or dl.suggested_filename or "download.bin"
        dest = self.tmpdir / "downloads" / fn
        dest.parent.mkdir(parents=True, exist_ok=True)
        dl.save_as(str(dest))
        return dest

    def choose_file(self, trigger, files):
        flist = files if isinstance(files, (list, tuple)) else [files]
        with self.page.expect_file_chooser(timeout=self.ACTION_TIMEOUT) as info:
            trigger()
        chooser = info.value
        chooser.set_files([str(f) for f in flist])

    # ---------- 业务快捷操作（服务器模式） ----------
    def create_nbk(self, stem: str) -> Path:
        """服务器模式：点「选择笔记本」→ 输入文件名 → 新建并打开，断言自动落盘。"""
        self.wait_visible("#handleRestore")
        self.page.click("#handleRestore")
        self.wait_modal()
        self.modal_fill(stem + ".nbk")
        self.page.get_by_role("button", name="新建并打开").click()
        self.wait_file_state(stem, timeout=15000)
        self.wait_visible("#editor")
        self.page.wait_for_timeout(150)
        return self.wait_file(stem + ".nbk")

    def wait_file(self, name: str, needles=(), timeout: float = 15.0) -> Path:
        """轮询等待数据目录中出现指定文件（可要求内容包含若干字符串），返回路径。"""
        if isinstance(needles, str):
            needles = [needles]
        needles = list(needles)
        path = self.data_dir / name
        deadline = time.time() + timeout
        last_text = ""
        while time.time() < deadline:
            if path.is_file():
                try:
                    last_text = path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    last_text = ""
                if all(n in last_text for n in needles):
                    return path
            self.page.wait_for_timeout(100)
        raise AssertionError(
            f"等待磁盘文件 {name} 超时（{timeout}s）；needles={needles!r}；"
            f"文件存在={path.is_file()}；当前内容片段={last_text[:300]!r}"
        )

    def read_nbk(self, name: str) -> dict:
        path = self.data_dir / name
        assert path.is_file(), f"找不到 .nbk 文件：{path}"
        raw = path.read_text(encoding="utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise AssertionError(f".nbk 文件不是合法 JSON（{e}）；前 300 字符：{raw[:300]!r}") from e

    # ---------- 断言 / 状态 ----------
    def js_state(self):
        return self.page.evaluate("() => state ? JSON.parse(JSON.stringify(state)) : null")

    def editor_text(self) -> str:
        return self.page.inner_text("#editor")

    def count(self, selector: str) -> int:
        return self.page.locator(selector).count()

    def add_page(self, title: str):
        """点 ＋ 新建页面并填标题。"""
        self.page.click("#addPg")
        self.page.wait_for_timeout(120)
        self.page.fill("#pgTitle", title)
        self.page.wait_for_timeout(80)

    def finish(self):
        """收尾：给异步 console 一点时间，然后检查未预期 JS 错误 / 原生对话框。"""
        self.page.wait_for_timeout(250)
        problems = []
        unexpected = [m for m in self.console_msgs if not m["expected_reason"]]
        if self.page_errors:
            problems.append("未捕获 JS 异常（pageerror）:\n    " + "\n    ".join(self.page_errors))
        if unexpected:
            problems.append(
                "未预期的 console error:\n    "
                + "\n    ".join(f"{m['text']}  @ {m['url']}" for m in unexpected)
            )
        if self.dialogs:
            problems.append("出现了未预期的原生对话框: " + "; ".join(self.dialogs))
        if problems:
            raise AssertionError("运行时错误检查失败：\n  " + "\n  ".join(problems))

    def capture_failure(self, exc: Exception):
        """保存截图 / DOM / 错误现场到 artifacts 目录（仓库外）。"""
        safe = f"{self.index:02d}-{self.name}"
        paths = []
        try:
            shot = self.artifacts_dir / f"{safe}.png"
            self.page.screenshot(path=str(shot), full_page=True)
            paths.append(str(shot))
        except Exception:
            pass
        try:
            dom = self.artifacts_dir / f"{safe}.html"
            dom.write_text(self.page.content(), encoding="utf-8")
            paths.append(str(dom))
        except Exception:
            pass
        try:
            info = self.artifacts_dir / f"{safe}.txt"
            lines = [f"用例: {self.name}", f"模式: {self.mode}", f"URL: {self.url}",
                     "", "== 异常 ==", traceback.format_exc(), "",
                     f"== pageerrors ({len(self.page_errors)}) =="]
            lines += self.page_errors or ["(无)"]
            lines += ["", f"== console errors ({len(self.console_msgs)}) =="]
            for m in self.console_msgs:
                tag = " [白名单: " + m["expected_reason"] + "]" if m["expected_reason"] else ""
                lines.append(f"- {m['text']}  @ {m['url']}{tag}")
            lines += ["", "== 服务器输出 ==", "".join(self.server_stdout)[-3000:]]
            try:
                lines += ["", "== 页面可见文本（截断） ==", self.page.inner_text("body")[:3000]]
            except Exception:
                pass
            info.write_text("\n".join(lines), encoding="utf-8")
            paths.append(str(info))
        except Exception:
            pass
        return paths

    def close(self):
        self.stop_server()
        try:
            self.context.close()
        except Exception:
            pass
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass


# ============================================================================
# 文件模式用例（file://）
# ============================================================================
@e2e("file_boot_and_three_columns", mode="file")
def test_file_boot(c: Case):
    c.goto()
    c.wait_layout_ready()

    title = c.page.title()
    assert "巢记" in title, f"页面标题未包含“巢记”：{title!r}"

    # 顶栏关键按钮就绪
    for sel in ("#btnOpen", "#btnNew", "#btnSave", "#btnImportMd", "#btnExportMenu",
                "#btnTheme", "#btnToc", "#btnWide", "#btnToggleNav", "#btnHelp"):
        assert c.page.locator(sel).is_visible(), f"顶栏按钮不可见：{sel}"

    # 三栏骨架：左笔记本 / 左分区 / 中页面列表 / 右编辑区容器
    for sel in ("#main", "#nbList", "#secList", "#pgList", "#emptyState", "#col3Body", "#editor"):
        assert c.count(sel) == 1, f"缺少关键 DOM：{sel}"
    nb_box = c.page.locator("#nbList").bounding_box()
    pg_box = c.page.locator("#pgList").bounding_box()
    assert nb_box and pg_box, "左栏/中栏不可见，无法计算布局"
    assert nb_box["x"] < pg_box["x"], f"三栏布局顺序异常：nbList.x={nb_box['x']} pgList.x={pg_box['x']}"
    assert c.page.locator("#emptyState").evaluate("el => getComputedStyle(el).display") != "none", "空状态未显示"
    assert c.page.locator("#col3Body").evaluate("el => getComputedStyle(el).display") == "none", "无内容时右栏应隐藏"

    # 工具栏按钮齐全
    toolbar_buttons = c.page.locator("#toolbar button").count()
    assert toolbar_buttons >= 12, f"工具栏按钮过少：{toolbar_buttons}"
    assert c.page.locator("#editor").get_attribute("contenteditable") == "true", "编辑区不是 contenteditable"

    # 专注模式：Ctrl+\ 折叠侧边栏（合成 keydown 事件，避免键盘布局差异）
    c.page.evaluate(
        r"""() => document.dispatchEvent(new KeyboardEvent('keydown',
              {key: '\\', ctrlKey: true, bubbles: true, cancelable: true}))"""
    )
    c.page.wait_for_timeout(150)
    assert "sidebar-collapsed" in (c.page.locator("body").get_attribute("class") or ""), "Ctrl+\\ 未折叠侧边栏"
    c.page.click("#btnToggleNav")
    c.page.wait_for_timeout(150)
    assert "sidebar-collapsed" not in (c.page.locator("body").get_attribute("class") or ""), "再次切换未展开侧边栏"


@e2e("file_theme_wide_toc_prefs", mode="file")
def test_file_theme_prefs(c: Case):
    c.goto()
    c.wait_layout_ready()

    def theme():
        return c.page.locator("html").get_attribute("data-theme")

    assert theme() == "light", f"初始主题应为 light，实际 {theme()!r}"
    c.page.click("#btnTheme")
    c.page.wait_for_timeout(150)
    assert theme() == "sepia", f"第一次切换主题应为 sepia，实际 {theme()!r}"
    c.page.click("#btnTheme")
    c.page.wait_for_timeout(150)
    assert theme() == "dark", f"第二次切换主题应为 dark，实际 {theme()!r}"
    c.page.click("#btnTheme")
    c.page.wait_for_timeout(150)
    assert theme() == "light", f"第三次切换主题应回到 light，实际 {theme()!r}"
    c.wait_toast("已切换主题")

    # 宽屏模式 + TOC 抽屉
    c.page.click("#btnWide")
    c.page.wait_for_timeout(120)
    assert "wide-editor" in (c.page.locator("body").get_attribute("class") or ""), "宽屏模式未生效"
    c.page.click("#btnToc")
    c.page.wait_for_timeout(120)
    assert "open" in (c.page.locator("#tocPanel").get_attribute("class") or ""), "TOC 抽屉未打开"

    # 设为暗夜主题后刷新，偏好应持久化（localStorage）
    c.page.click("#btnTheme")  # light -> sepia
    c.page.click("#btnTheme")  # sepia -> dark
    c.page.wait_for_timeout(150)
    prefs = c.page.evaluate("() => JSON.parse(localStorage.getItem('chaoji_ui_prefs_v2') || '{}')")
    assert prefs.get("theme") == "dark", f"localStorage 未记住主题：{prefs!r}"

    c.page.reload(wait_until="load")
    c.wait_layout_ready()
    assert theme() == "dark", f"刷新后主题未恢复：{theme()!r}"
    assert "wide-editor" in (c.page.locator("body").get_attribute("class") or ""), "刷新后宽屏模式未恢复"
    assert "open" in (c.page.locator("#tocPanel").get_attribute("class") or ""), "刷新后 TOC 状态未恢复"


@e2e("file_draft_banner_restore", mode="file")
def test_file_draft_restore(c: Case):
    c.goto()
    c.wait_layout_ready()

    # 预写 IndexedDB 草稿（等价于“上次未保存的会话草稿”），刷新后应出现恢复横幅
    seeded = c.page.evaluate(
        """async () => {
            const st = {app:'chaoji', ver:1, notebooks:[{
                id:'nb-draft', name:'草稿笔记本',
                sections:[{id:'sec-draft', name:'草稿分区', pages:[{
                    id:'pg-draft', title:'草稿页标题',
                    html:'<p>DRAFT-CONTENT-草稿正文</p>',
                    created:1, updated:2, pinned:false
                }]}]
            }]};
            await idbSet('draft', {state: st, savedAt: Date.now(), dirty: true});
            const got = await idbGet('draft');
            return !!(got && got.state && got.state.notebooks && got.state.notebooks.length);
        }"""
    )
    assert seeded, "IndexedDB 草稿写入失败（file:// 下 IDB 不可用？）"

    c.page.reload(wait_until="load")
    c.wait_fn(
        "() => { const el = document.getElementById('draftBanner');"
        "return el && getComputedStyle(el).display !== 'none'; }",
        timeout=15000,
    )
    assert c.page.locator("#draftBanner").inner_text().strip() != "", "草稿恢复横幅文案为空"
    assert c.page.locator("#handleBanner").evaluate("el => getComputedStyle(el).display") == "none", \
        "仅草稿场景不应显示文件句柄横幅"

    c.page.click("#draftRestore")
    c.wait_fn("() => document.querySelectorAll('#nbList .nav-item').length === 1")
    assert "草稿笔记本" in c.page.inner_text("#nbList"), "恢复后左栏缺少草稿笔记本"
    c.wait_visible("#editor")
    assert "DRAFT-CONTENT-草稿正文" in c.editor_text(), f"恢复后编辑器内容丢失：{c.editor_text()!r}"
    assert "草稿页标题" in (c.page.input_value("#pgTitle") or ""), "恢复后页面标题丢失"
    assert "草稿" in c.page.inner_text("#fileState"), "恢复后文件状态栏未标记草稿来源"
    assert c.page.locator("#draftBanner").evaluate("el => getComputedStyle(el).display") == "none", \
        "恢复后草稿横幅应隐藏"


@e2e("file_no_state_guards", mode="file")
def test_file_no_state_guards(c: Case):
    c.goto()
    c.wait_layout_ready()

    # 没有打开任何笔记本时，点保存/导出应给出提示而不是抛异常。
    # 注：无 page 时右栏工具栏整体隐藏，#btnMdMode 不可见；用户实际也点不到，
    # 这里用 JS 触发同一 handler，验证“无页面”守卫本身不抛异常。
    c.page.click("#btnSave")
    c.wait_toast("当前还没有打开或创建笔记本")

    c.page.evaluate("() => document.getElementById('btnMdMode').click()")
    c.wait_toast("请先选中或新建一个页面")

    c.page.click("#btnExportMenu")
    c.page.get_by_text("导出整本备份", exact=False).first.click()
    c.wait_toast("还没有内容")

    c.page.keyboard.press("Control+s")
    c.page.wait_for_timeout(200)
    assert c.js_state() is None, "无状态时不应创建 state"


@e2e("file_create_type_export_backup", mode="file")
def test_file_create_export(c: Case):
    c.goto()
    c.wait_layout_ready()

    # 取消一次新建（验证 uiPrompt 可取消）
    c.page.click("#btnNewBlank")
    c.wait_modal()
    c.page.click(".modal-mask .m-cancel")
    c.wait_modal_gone()
    assert c.js_state() is None, "取消新建后不应创建 state"

    # 正式新建：输入名称 → 确定
    c.page.click("#btnNewBlank")
    c.wait_modal()
    c.modal_fill("文件模式笔记")
    c.modal_ok()
    c.wait_visible("#editor")
    c.wait_fn("() => document.querySelectorAll('#nbList .nav-item').length === 1")
    assert "文件模式笔记" in c.page.inner_text("#nbList"), "新建笔记本未出现在左栏"
    assert c.count("#pgList .pg-card") == 1, "新建笔记本后应有一篇欢迎页面"

    # 输入正文（文件模式下不绑定句柄，走 Ctrl+Shift+S 导出备份）
    c.page.click("#editor")
    c.page.keyboard.type("文件模式输入内容 E2E-FILE-BACKUP")
    c.page.wait_for_timeout(300)

    backup = c.download(lambda: c.page.keyboard.press("Control+Shift+S"))
    assert backup.suffix.lower() == ".nbk", f"备份文件名后缀异常：{backup.name}"
    data = json.loads(backup.read_text(encoding="utf-8"))
    assert data.get("app") == "chaoji", "备份 .nbk 的 app 字段错误"
    blob = json.dumps(data, ensure_ascii=False)
    assert "文件模式笔记" in blob, "备份 .nbk 未包含笔记本名"
    assert "E2E-FILE-BACKUP" in blob, "备份 .nbk 未包含刚输入的正文"


# ============================================================================
# 服务器模式用例（真实 server.py）
# ============================================================================
@e2e("server_boot_ready", mode="server")
def test_server_boot(c: Case):
    assert c.server_url and c.server_url.startswith("http://127.0.0.1:"), f"服务器地址异常：{c.server_url}"
    assert any("地址" in line for line in c.server_stdout), "server.py 启动输出缺少“地址”行"
    c.goto()
    c.wait_file_state("服务器模式", timeout=15000)
    c.wait_layout_ready()

    fs = c.page.inner_text("#fileState")
    assert "服务器模式" in fs, f"文件状态栏未标记服务器模式：{fs!r}"
    # 启动探测应成功：/api/ping 返回 ok + mode=server
    ping = c.page.evaluate("async () => { const r = await fetch('/api/ping'); return await r.json(); }")
    assert ping.get("ok") is True and ping.get("mode") == "server", f"/api/ping 返回异常：{ping!r}"
    # 全新数据目录：应显示选择/新建笔记本的横幅 + 空状态
    assert c.page.locator("#handleBanner").evaluate("el => getComputedStyle(el).display") != "none", \
        "服务器模式首次进入应显示“选择笔记本”横幅"
    assert c.page.locator("#emptyState").evaluate("el => getComputedStyle(el).display") != "none", \
        "服务器模式首次进入应显示空状态"
    listing = c.page.evaluate("async () => { const r = await fetch('/api/list'); return await r.json(); }")
    assert listing.get("ok") is True and listing.get("items") == [], f"/api/list 初始应为空：{listing!r}"


@e2e("server_create_nbk_autosave", mode="server")
def test_server_create(c: Case):
    path = c.create_nbk("E2E创建测试")
    assert path.is_file(), f"新建笔记本未自动落盘：{path}"
    data = c.read_nbk("E2E创建测试.nbk")
    assert data.get("app") == "chaoji" and isinstance(data.get("notebooks"), list), ".nbk JSON 结构非法"
    assert len(data["notebooks"]) == 1 and data["notebooks"][0]["name"] == "E2E创建测试", \
        f"落盘内容与新建笔记本不一致：{data['notebooks'][0].get('name')!r}"

    assert "E2E创建测试.nbk" in c.page.inner_text("#fileState"), "文件状态栏未显示当前文件名"
    assert c.count("#nbList .nav-item") == 1, "左栏应有 1 个笔记本"
    assert c.count("#secList .nav-item") == 1, "左栏应有 1 个分区"
    assert c.count("#pgList .pg-card") == 1, "中栏应有 1 篇页面"
    c.wait_visible("#editor")

    # 三栏渲染：左 < 中 < 右
    nb_box = c.page.locator("#nbList").bounding_box()
    pg_box = c.page.locator("#pgList").bounding_box()
    ed_box = c.page.locator("#editor").bounding_box()
    assert nb_box and pg_box and ed_box, "三栏中有不可见元素"
    assert nb_box["x"] < pg_box["x"] < ed_box["x"], \
        f"三栏横向顺序异常：nb={nb_box['x']}, pg={pg_box['x']}, editor={ed_box['x']}"

    # 自动保存：改标题 + 输入正文，不按 Ctrl+S，1.5s 内应自动写盘
    c.page.fill("#pgTitle", "自动保存标题")
    c.page.click("#editor")
    c.page.keyboard.type("自动保存正文 AUTOSAVE-2024")
    c.page.wait_for_timeout(300)
    c.wait_file("E2E创建测试.nbk", needles=["自动保存标题", "AUTOSAVE-2024"], timeout=15)

    # 刷新后内容仍在（会话 + 服务器文件双重恢复）
    c.page.reload(wait_until="load")
    c.wait_file_state("E2E创建测试.nbk", timeout=20000)
    c.wait_visible("#editor")
    c.wait_fn("() => document.getElementById('editor').innerText.includes('AUTOSAVE-2024')")
    assert (c.page.input_value("#pgTitle") or "") == "自动保存标题", "刷新后页面标题丢失"
    assert "AUTOSAVE-2024" in c.editor_text(), "刷新后正文丢失"
    assert c.count("#pgList .pg-card") == 1, "刷新后页面列表异常"


@e2e("server_ctrl_s_save", mode="server")
def test_server_ctrl_s(c: Case):
    c.create_nbk("E2E手动保存")
    c.page.click("#editor")
    c.page.keyboard.type("MANUAL-SAVE-9x7")
    c.page.wait_for_timeout(200)
    c.page.keyboard.press("Control+s")
    c.wait_toast("已保存", timeout=10000)
    c.wait_file("E2E手动保存.nbk", needles=["MANUAL-SAVE-9x7"], timeout=10)

    # 状态栏应有保存时间/已保存标记
    c.page.wait_for_timeout(300)
    st = c.page.inner_text("#statusbar") + c.page.inner_text("#saveChip")
    assert ("已保存" in st) or ("保存" in st), f"状态栏未显示保存状态：{st!r}"


@e2e("server_markdown_three_modes", mode="server")
def test_server_md_modes(c: Case):
    c.create_nbk("E2E-MD模式")

    c.page.click("#btnMdMode")  # wysiwyg -> split
    c.wait_fn("() => document.getElementById('editorBodySplit').className === 'mode-split'")
    assert c.page.locator("#mdSourcePane").is_visible(), "分屏模式源码面板不可见"
    assert "分屏" in c.page.inner_text("#btnMdMode"), "按钮文案未更新为分屏"

    c.page.click("#btnMdMode")  # split -> source
    c.wait_fn("() => document.getElementById('editorBodySplit').className === 'mode-source'")
    assert c.page.locator("#mdSourceEditor").is_visible(), "源码模式 textarea 不可见"
    assert not c.page.locator("#editor").is_visible(), "源码模式富文本区应隐藏"

    md = ("# 一级标题甲\n\n"
          "标题下的导语段落。\n\n"
          "## 二级标题乙\n\n"
          "这是 **粗体文本** 与 *斜体文本*。\n\n"
          "- 列表项一\n"
          "- 列表项二\n\n"
          "| 列A | 列B |\n| --- | --- |\n| 1 | 2 |\n")
    c.page.fill("#mdSourceEditor", md)
    c.page.wait_for_timeout(400)

    # 纯源码模式下富文本格式按钮被禁用（应用设计：避免写入隐藏编辑器）；
    # 模式切换按钮 #btnMdMode 保持可用（用户可随时退出源码模式），也能用源码面板的“✓ 完成并返回富文本”。
    assert c.page.locator("#toolbar button[data-cmd]").first.is_disabled(), "纯源码模式下富文本工具栏应禁用"
    assert not c.page.locator("#btnMdMode").is_disabled(), "源码模式下应保留模式切换按钮可用"
    c.page.get_by_text("✓ 完成并返回富文本").click()
    c.wait_fn("() => document.getElementById('editorBodySplit').className === 'mode-wysiwyg'")
    assert c.page.locator("#editor").is_visible(), "切回富文本后编辑区不可见"

    assert c.page.locator("#editor h1").count() == 1, "Markdown 源码的 H1 未渲染"
    assert "一级标题甲" in c.page.inner_text("#editor h1"), "H1 文本错误"
    assert c.page.locator("#editor strong").count() >= 1, "粗体未渲染"
    assert c.page.locator("#editor li").count() == 2, "无序列表渲染数量错误"
    assert c.page.locator("#editor table").count() == 1, "Markdown 表格未渲染"

    # 保存并刷新，渲染结果应持久化
    c.page.keyboard.press("Control+s")
    c.wait_toast("已保存")
    c.wait_file("E2E-MD模式.nbk", needles=["一级标题甲"], timeout=10)
    c.page.reload(wait_until="load")
    c.wait_file_state("E2E-MD模式.nbk", timeout=20000)
    c.wait_visible("#editor")
    c.wait_fn("() => document.querySelectorAll('#editor h1').length >= 1")
    assert c.count("#editor table") >= 1, "刷新后表格丢失"


@e2e("server_export_md_and_html", mode="server")
def test_server_export(c: Case):
    c.create_nbk("E2E导出")
    c.page.fill("#pgTitle", "导出页标题")
    c.page.click("#editor")
    c.page.keyboard.type("导出正文 EXPORT-BODY 内容")
    c.page.wait_for_timeout(200)
    c.page.keyboard.press("Control+s")
    c.wait_toast("已保存")

    # 导出本页 Markdown：菜单 → 弹窗 → 下载
    c.page.click("#btnExportMenu")
    c.page.get_by_text("导出本页为 Markdown", exact=False).first.click()
    c.wait_visible("#mdeDownload")
    md_path = c.download(lambda: c.page.click("#mdeDownload"), save_name="page-export.md")
    md = md_path.read_text(encoding="utf-8", errors="replace")
    assert "# 导出页标题" in md, f"导出的 Markdown 缺少标题行：{md[:200]!r}"
    assert "EXPORT-BODY" in md, f"导出的 Markdown 缺少正文：{md[:300]!r}"

    # 导出整本独立网页（菜单项点击即触发下载，直接包在 expect_download 里）
    def export_book_html():
        c.page.click("#btnExportMenu")
        c.page.get_by_text("导出整本为独立网页", exact=False).first.click()

    html_path = c.download(export_book_html, save_name="book-export.html")
    html = html_path.read_text(encoding="utf-8", errors="replace")
    assert html.lstrip().lower().startswith("<!doctype html"), "整本导出的 HTML 缺少 DOCTYPE"
    assert "导出页标题" in html and "EXPORT-BODY" in html, "整本 HTML 未包含页面内容"


@e2e("server_open_second_nbk_via_list", mode="server")
def test_server_open_second(c: Case):
    c.create_nbk("E2E文件A")
    c.page.fill("#pgTitle", "A页标题")
    c.page.click("#editor")
    c.page.keyboard.type("AAA-CONTENT-甲")
    c.page.wait_for_timeout(200)
    c.page.keyboard.press("Control+s")
    c.wait_toast("已保存")
    c.wait_file("E2E文件A.nbk", needles=["AAA-CONTENT-甲"], timeout=10)

    # 新建第二个独立 .nbk：顶栏“新建文件”→ 选择“另建全新的 .nbk 独立文件” → 输入名称
    c.page.click("#btnNew")
    c.wait_modal()
    c.wait_visible("#cnNewFile")
    c.page.click("#cnNewFile")
    # 选择“另建独立文件”后立即弹出文件名输入框（两个弹窗衔接，不能等 .modal-mask 消失）
    c.wait_fn("() => { const m = document.querySelector('.modal-mask');"
              "return !!(m && m.textContent.includes('新建笔记本文件')); }")
    c.modal_fill("E2E文件B")
    c.modal_ok()
    c.wait_file_state("E2E文件B.nbk", timeout=15000)
    c.wait_visible("#editor")
    c.wait_file("E2E文件B.nbk", timeout=10)
    assert "E2E文件A.nbk" not in c.page.inner_text("#fileState"), "新建第二个文件后仍指向旧文件"

    # 打开旧文件：顶栏“打开”→ /api/list 弹窗 → 点 E2E文件A.nbk
    c.page.click("#btnOpen")
    c.wait_modal()
    assert "E2E文件A.nbk" in c.page.inner_text(".modal-mask"), "服务器文件列表缺少 E2E文件A.nbk"
    assert "E2E文件B.nbk" in c.page.inner_text(".modal-mask"), "服务器文件列表缺少 E2E文件B.nbk"
    c.page.locator(".modal-mask").get_by_text("📕 E2E文件A.nbk").first.click()
    c.wait_file_state("E2E文件A.nbk", timeout=20000)
    c.wait_visible("#editor")
    c.wait_fn("() => document.getElementById('editor').innerText.includes('AAA-CONTENT-甲')")
    assert "AAA-CONTENT-甲" in c.editor_text(), "打开第二个笔记本后内容未恢复"


@e2e("server_structure_ops_and_persist", mode="server")
def test_server_structure(c: Case):
    c.create_nbk("E2E结构")
    original = c.read_nbk("E2E结构.nbk")

    # 新增第二个笔记本（左栏 ＋）
    c.page.click("#addNb")
    c.wait_modal()
    c.modal_fill("第二笔记本")
    c.modal_ok()
    c.wait_fn("() => document.querySelectorAll('#nbList .nav-item').length === 2")

    # 选中第一个笔记本后新增分区
    c.page.locator("#nbList .nav-item").first.click()
    c.page.wait_for_timeout(150)
    c.page.click("#addSec")
    c.wait_modal()
    c.modal_fill("第二分区")
    c.modal_ok()
    c.wait_fn("() => document.querySelectorAll('#secList .nav-item').length === 2")

    # 双击左栏笔记本名重命名
    c.page.locator("#nbList .nav-item").first.dblclick()
    c.wait_modal()
    c.modal_fill("重命名笔记本")
    c.modal_ok()
    c.wait_fn("() => document.getElementById('nbList').innerText.includes('重命名笔记本')")

    c.page.keyboard.press("Control+s")
    c.wait_toast("已保存")
    c.wait_file("E2E结构.nbk", needles=["第二笔记本", "第二分区", "重命名笔记本"], timeout=12)
    data = c.read_nbk("E2E结构.nbk")
    assert len(data["notebooks"]) == 2, f"落盘笔记本数量错误：{len(data['notebooks'])}"
    names = [nb["name"] for nb in data["notebooks"]]
    assert "重命名笔记本" in names and "第二笔记本" in names, f"落盘笔记本名错误：{names!r}"
    sec_names = [s["name"] for nb in data["notebooks"] for s in nb["sections"]]
    assert "第二分区" in sec_names, f"落盘分区名错误：{sec_names!r}"
    assert original.get("notebooks"), "基线文件异常"


@e2e("server_search_pin_sort", mode="server")
def test_server_search_pin(c: Case):
    c.create_nbk("E2E搜索")
    c.wait_visible("#editor")

    c.page.fill("#pgTitle", "苹果笔记")
    c.add_page("香蕉笔记")
    c.add_page("樱桃笔记")
    c.wait_fn("() => document.querySelectorAll('#pgList .pg-card').length === 3")

    # 搜索过滤 + 关键词高亮
    c.page.fill("#pgSearch", "香蕉")
    c.wait_fn("() => document.querySelectorAll('#pgList .pg-card').length === 1")
    assert "香蕉笔记" in c.page.inner_text("#pgList")
    assert c.count("#pgList .pg-card mark.search-hit") >= 1, "搜索结果未高亮关键词"
    c.page.click("#pgSearchClear")
    c.wait_fn("() => document.querySelectorAll('#pgList .pg-card').length === 3")

    # 置顶：卡片出现 📌 且排到首位
    apple = c.page.locator("#pgList .pg-card", has_text="苹果笔记").first
    apple.hover()
    apple.locator(".act-icon.pin").click()
    c.page.wait_for_timeout(250)
    first_title = c.page.locator("#pgList .pg-card .pg-title").first.inner_text()
    assert "📌" in first_title and "苹果笔记" in first_title, f"置顶后首张卡片异常：{first_title!r}"
    # 取消置顶（卡片操作图标悬停才显示）
    first_card = c.page.locator("#pgList .pg-card").first
    first_card.hover()
    first_card.locator(".act-icon.pin").click()
    c.page.wait_for_timeout(250)
    assert "📌" not in c.page.locator("#pgList .pg-card .pg-title").first.inner_text(), "取消置顶失败"

    # 排序切换
    c.page.select_option("#pgSort", "title")
    c.page.wait_for_timeout(200)
    assert c.count("#pgList .pg-card") == 3, "按标题排序后页面数量异常"
    c.page.select_option("#pgSort", "created")
    c.page.wait_for_timeout(200)
    assert c.count("#pgList .pg-card") == 3, "按创建时间排序后页面数量异常"

    # 搜索范围切换：全域 / 本分区
    c.page.click('#searchScopeToggle span[data-scope="sec"]')
    c.page.wait_for_timeout(150)
    assert "active" in (c.page.locator('#searchScopeToggle span[data-scope="sec"]').get_attribute("class") or ""), \
        "本分区搜索范围未激活"
    c.page.fill("#pgSearch", "樱桃")
    c.wait_fn("() => document.querySelectorAll('#pgList .pg-card').length === 1")
    c.page.click('#searchScopeToggle span[data-scope="all"]')
    c.page.wait_for_timeout(150)


@e2e("server_move_page_between_sections", mode="server")
def test_server_move_page(c: Case):
    c.create_nbk("E2E移动")
    c.wait_visible("#editor")

    # 新建第二个分区（应用会同时创建并选中一篇新页面）
    c.page.click("#addSec")
    c.wait_modal()
    c.modal_fill("移动目标分区")
    c.modal_ok()
    c.wait_fn("() => document.querySelectorAll('#secList .nav-item').length === 2")

    # 回到第一个分区，给页面改标题
    c.page.locator("#secList .nav-item").first.click()
    c.page.wait_for_timeout(150)
    c.page.fill("#pgTitle", "待移动页面")
    c.wait_fn("() => document.querySelectorAll('#pgList .pg-card').length >= 1")

    # 右键卡片 → “移动到其他分区…” → 选择目标分区
    card = c.page.locator("#pgList .pg-card", has_text="待移动页面").first
    card.click(button="right")
    c.wait_fn("() => document.body.innerText.includes('移动到其他分区')")
    c.page.get_by_text("移动到其他分区", exact=False).first.click()
    c.wait_modal()
    assert "移动目标分区" in c.page.inner_text(".modal-mask"), "移动弹窗未列出目标分区"
    c.page.locator(".modal-mask .modal-body > div", has_text="移动目标分区").first.click()
    c.wait_modal_gone()
    c.wait_toast("已移动到")
    assert "移动目标分区" in c.page.inner_text("#secList .nav-item.sel"), "移动后未切换到目标分区"
    assert "待移动页面" in c.page.inner_text("#pgList"), "移动后目标分区看不到页面"

    # 落盘校验：页面确实改挂在目标分区下
    c.page.keyboard.press("Control+s")
    c.wait_toast("已保存")
    c.wait_file("E2E移动.nbk", needles=["移动目标分区", "待移动页面"], timeout=12)
    data = c.read_nbk("E2E移动.nbk")
    secs = {s["name"]: [p["title"] for p in s["pages"]]
            for nb in data["notebooks"] for s in nb["sections"]}
    assert "移动目标分区" in secs, f"落盘缺少目标分区：{list(secs)!r}"
    assert "待移动页面" in secs["移动目标分区"], f"移动后页面不在目标分区下：{secs!r}"


@e2e("server_delete_page_undo", mode="server")
def test_server_delete_undo(c: Case):
    c.create_nbk("E2E删除撤销")
    c.page.fill("#pgTitle", "待删除页面")
    c.page.click("#editor")
    c.page.keyboard.type("DEL-UNDO-CONTENT")
    c.page.wait_for_timeout(200)
    c.page.keyboard.press("Control+s")
    c.wait_toast("已保存")

    card = c.page.locator("#pgList .pg-card").first
    card.hover()
    card.locator(".act-icon.del").click()
    c.wait_modal()
    c.modal_click_button("删除")
    c.wait_fn("() => document.querySelectorAll('#pgList .pg-card').length === 0")
    assert "已删除页面" in c.page.inner_text("#toast"), "删除后未出现撤销提示"

    # 点 toast 里的“撤销”恢复页面
    undo = c.page.locator("#toast button", has_text="撤销").first
    assert undo.is_visible(), "撤销按钮不可见"
    undo.click()
    c.wait_fn("() => document.querySelectorAll('#pgList .pg-card').length === 1")
    assert "待删除页面" in c.page.inner_text("#pgList"), "撤销后页面标题丢失"

    c.page.keyboard.press("Control+s")
    c.wait_toast("已保存")
    c.wait_file("E2E删除撤销.nbk", needles=["待删除页面"], timeout=12)
    data = c.read_nbk("E2E删除撤销.nbk")
    titles = [p["title"] for nb in data["notebooks"] for s in nb["sections"] for p in s["pages"]]
    assert "待删除页面" in titles, f"撤销后保存的 JSON 中页面丢失：{titles!r}"


@e2e("server_rich_blocks_and_image", mode="server")
def test_server_rich_blocks(c: Case):
    c.create_nbk("E2E富文本")
    c.wait_visible("#editor")

    # 待办清单
    c.page.click("#editor")
    c.page.click("#btnTodo")
    c.wait_fn("() => document.querySelectorAll('#editor .todo-item').length >= 1")
    assert c.count("#editor input.todo-cb, #editor input[type=checkbox]") >= 1, "待办清单缺少复选框"

    # 表格（2 列 3 行：首行表头 + 2 行数据）
    c.page.click("#btnTable")
    c.wait_modal()
    c.page.fill("#tblCols", "2")
    c.page.fill("#tblRows", "3")
    c.page.click("#tblOk")
    c.wait_modal_gone()
    c.wait_fn("() => document.querySelectorAll('#editor table').length === 1")
    assert c.count("#editor table th") == 2, "表格表头列数错误"
    assert c.count("#editor table tbody tr") == 2, "表格数据行数错误"

    # 代码块 + 语言标识
    c.page.click("#editor")
    c.page.keyboard.press("Control+End")
    c.page.click("#btnCodeBlock")
    c.wait_fn("() => document.querySelectorAll('#editor pre').length === 1")
    c.page.click("#editor pre code")
    c.wait_visible("#codeLangSelect")
    c.page.select_option("#codeLangSelect", "python")
    c.page.wait_for_timeout(200)
    assert (c.page.locator("#editor pre").first.get_attribute("data-lang") or "") == "python", \
        "代码块语言标识未写入 data-lang"

    # 分割线
    c.page.click("#editor")
    c.page.keyboard.press("Control+End")
    c.page.click("#btnHr")
    c.wait_fn("() => document.querySelectorAll('#editor hr').length >= 1")

    # 引用块
    c.page.click("#editor")
    c.page.keyboard.press("Control+End")
    c.page.keyboard.press("Enter")
    c.page.keyboard.type("引用内容 QUOTE-TEXT")
    c.page.click("#btnQuote")
    c.wait_fn("() => document.querySelectorAll('#editor blockquote').length >= 1")
    assert "QUOTE-TEXT" in c.page.inner_text("#editor blockquote").replace("\n", " "), "引用块缺少文本"

    # Callout 提示块
    c.page.click("#btnCallout")
    c.wait_modal()
    c.page.fill("#coTitle", "提示标题")
    c.page.fill("#coBody", "提示正文 CALLOUT-BODY")
    c.page.click("#coOk")
    c.wait_modal_gone()
    c.wait_fn("() => document.querySelectorAll('#editor .callout').length >= 1")
    assert "CALLOUT-BODY" in c.page.inner_text("#editor .callout"), "Callout 内容丢失"

    # 插入图片（工具栏 → 文件选择器 → compressImage → data:image 内联）
    png = c.tmpdir / "test-image.png"
    png.write_bytes(PNG_1PX)
    c.choose_file(lambda: c.page.click("#btnImg"), png)
    c.wait_fn("() => document.querySelectorAll('#editor img').length >= 1", timeout=15000)
    img_src = c.page.locator("#editor img").first.get_attribute("src") or ""
    assert img_src.startswith("data:image/"), f"图片未内联为 data:image：{img_src[:40]!r}"

    # 保存 → 刷新，富文本结构应持久化
    c.page.keyboard.press("Control+s")
    c.wait_toast("已保存")
    c.wait_file("E2E富文本.nbk", needles=["data:image/", "todo-item", "blockquote"], timeout=15)
    c.page.reload(wait_until="load")
    c.wait_file_state("E2E富文本.nbk", timeout=20000)
    c.wait_visible("#editor")
    c.wait_fn("() => document.querySelectorAll('#editor table').length >= 1")
    for sel, label in (("#editor .todo-item", "待办"), ("#editor pre", "代码块"),
                       ("#editor blockquote", "引用块"), ("#editor hr", "分割线"),
                       ("#editor img", "图片")):
        assert c.count(sel) >= 1, f"刷新后{label}丢失（{sel}）"
    assert c.count("#editor table") >= 1, "刷新后表格丢失"


@e2e("server_markdown_import", mode="server")
def test_server_md_import(c: Case):
    c.create_nbk("E2E导入")

    md_file = c.tmpdir / "导入样例.md"
    md_file.write_text(
        "# 导入标题\n\n"
        "导入标题下的导语。\n\n"
        "## 第一节\n\n"
        "这是 **导入粗体** 内容。\n\n"
        "- 导入列表一\n"
        "- 导入列表二\n\n"
        "| 列甲 | 列乙 |\n| --- | --- |\n| 1 | 2 |\n",
        encoding="utf-8",
    )

    # 第一次导入：作为新页面
    c.choose_file(lambda: c.page.click("#btnImportMd"), md_file)
    c.wait_modal()
    c.wait_visible("#impAsNew")
    c.page.click("#impAsNew")
    c.wait_modal_gone()
    c.wait_toast("已导入为新页面", timeout=10000)
    c.wait_fn("() => document.querySelectorAll('#pgList .pg-card').length === 2")
    c.wait_visible("#editor")
    assert c.count("#editor strong") >= 1, "导入的 Markdown 粗体未渲染"
    assert c.count("#editor table") >= 1, "导入的 Markdown 表格未渲染"

    # 第二次导入：按章节标题拆分为多篇页面
    c.choose_file(lambda: c.page.click("#btnImportMd"), md_file)
    c.wait_modal()
    c.wait_visible("#impSplit")
    c.page.click("#impSplit")
    c.wait_modal_gone()
    c.wait_toast("拆分", timeout=10000)
    c.wait_fn("() => document.querySelectorAll('#pgList .pg-card').length >= 4")
    assert c.count("#pgList .pg-card") >= 4, "按章节拆分后页面数量未增加"

    # 保存 → 刷新，导入内容仍在
    c.page.keyboard.press("Control+s")
    c.wait_toast("已保存")
    c.wait_file("E2E导入.nbk", needles=["导入标题", "第一节"], timeout=12)
    c.page.reload(wait_until="load")
    c.wait_file_state("E2E导入.nbk", timeout=20000)
    c.page.wait_for_timeout(300)
    assert c.count("#pgList .pg-card") >= 4, "刷新后导入页面数量异常"


# ============================================================================
# 运行器
# ============================================================================
class Runtime:
    def __init__(self, *, headed: bool, slow: int, verbose: bool, keep: bool, prefer: str):
        self.headed = headed
        self.slow = slow
        self.verbose = verbose
        self.keep = keep
        self.prefer = prefer
        self.browser = None
        self.artifacts_dir = DEFAULT_ARTIFACT_ROOT / time.strftime("run-%Y%m%d-%H%M%S")
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.failure_count = 0

    def log(self, msg: str):
        print(msg, flush=True)

    def log_verbose(self, msg: str):
        if self.verbose:
            print(msg, flush=True)


def run_tests(runtime: Runtime, tests) -> int:
    results = []
    total = len(tests)
    started_all = time.time()
    for idx, (name, mode, fn) in enumerate(tests, 1):
        runtime.log(f"\n[{idx:02d}/{total:02d}] {name}  ({'服务器模式' if mode == 'server' else '文件模式'})")
        case = Case(runtime, name, mode, idx)
        started = time.time()
        status, error = "PASS", None
        artifacts = []
        try:
            if mode == "server":
                case.start_server()
            case.goto()
            fn(case)
            case.finish()
        except SkipTest as e:
            status, error = "SKIP", str(e)
        except Exception as e:  # noqa: BLE001
            status = "FAIL"
            runtime.failure_count += 1
            error = f"{type(e).__name__}: {e}"
            artifacts = case.capture_failure(e)
            runtime.log("     ✗ FAIL: " + error.replace("\n", "\n       "))
            if artifacts:
                runtime.log("     现场文件:")
                for p in artifacts:
                    runtime.log("       - " + p)
        finally:
            duration = time.time() - started
            case.close()
        if status == "PASS":
            runtime.log(f"     ✓ PASS  ({duration:.2f}s)")
        elif status == "SKIP":
            runtime.log(f"     ○ SKIP  ({duration:.2f}s)：{error}")
        results.append({"name": name, "mode": mode, "status": status,
                        "duration": round(duration, 2), "error": error, "artifacts": artifacts})

    elapsed = time.time() - started_all
    passed = sum(1 for r in results if r["status"] == "PASS")
    skipped = sum(1 for r in results if r["status"] == "SKIP")
    failed = sum(1 for r in results if r["status"] == "FAIL")

    runtime.log("\n" + "=" * 78)
    runtime.log("E2E 汇总")
    runtime.log("=" * 78)
    for r in results:
        tag = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP"}[r["status"]]
        line = f"  {tag}  {r['name']:<38} {r['mode']:<6} {r['duration']:>7.2f}s"
        if r["error"]:
            line += "  " + r["error"].splitlines()[0][:90]
        runtime.log(line)
    runtime.log("-" * 78)
    runtime.log(f"用例 {total} 个：通过 {passed} / 失败 {failed} / 跳过 {skipped}；总耗时 {elapsed:.1f}s")
    runtime.log(f"截图与失败现场目录：{runtime.artifacts_dir}")

    print(SUMMAR_MARKER + json.dumps({
        "total": total, "passed": passed, "failed": failed, "skipped": skipped,
        "duration": round(elapsed, 1), "results": results,
    }, ensure_ascii=False), flush=True)

    if not runtime.keep and failed == 0:
        try:
            shutil.rmtree(runtime.artifacts_dir, ignore_errors=True)
        except Exception:
            pass
    return 0 if failed == 0 else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="巢记 ChaoJi E2E 测试（Playwright + 系统 Chrome）")
    parser.add_argument("--list", action="store_true", help="只列出用例名")
    parser.add_argument("--filter", default="", help="只跑名称包含这些子串的用例（逗号分隔）")
    parser.add_argument("--headed", action="store_true", help="有头模式")
    parser.add_argument("--slow", type=int, default=0, help="每步放慢的毫秒数")
    parser.add_argument("--verbose", "-v", action="store_true", help="打印细节（含白名单 console）")
    parser.add_argument("--keep", action="store_true", help="全部通过也保留截图/临时目录")
    parser.add_argument("--browser", choices=["chrome", "edge", "auto"], default="chrome",
                        help="使用哪个系统浏览器（默认 chrome）")
    parser.add_argument("--max-fail", type=int, default=0, help="失败数达到该值后仍继续跑完（默认全跑）")
    args = parser.parse_args(argv)

    if not FRONTEND.is_file():
        print(f"[环境错误] 找不到前端文件：{FRONTEND}")
        return 2
    if not SERVER_PY.is_file():
        print(f"[环境错误] 找不到服务端文件：{SERVER_PY}")
        return 2

    selected = [(n, m, f) for (n, m, f) in TESTS
                if not args.filter or any(k.strip().lower() in n.lower() for k in args.filter.split(",") if k.strip())]
    if not selected:
        print(f"[环境错误] --filter={args.filter!r} 没有匹配到任何用例")
        return 2
    if args.list:
        print(f"共 {len(selected)} 个用例：")
        for i, (n, m, _) in enumerate(selected, 1):
            print(f"  {i:02d}. [{m:<6}] {n}")
        return 0

    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[SKIP] 未安装 playwright，无法运行 E2E。")
        print("       安装：python -m pip install -r requirements-test.txt")
        return 2

    try:
        from importlib.metadata import version as _pkg_version
        pw_version = _pkg_version("playwright")
    except Exception:
        pw_version = "?"
    chrome_exe = find_chrome()
    if args.browser != "edge" and not chrome_exe:
        print("[SKIP] 未找到系统 Chrome/Edge 可执行文件，无法运行 E2E（本测试不下载浏览器）。")
        return 2

    print("=" * 78)
    print("巢记 ChaoJi E2E · Playwright 真实浏览器测试")
    print("=" * 78)
    print(f"仓库: {HERE}")
    print(f"前端: {FRONTEND.name}；服务端: {SERVER_PY.name}")
    print(f"Python: {sys.version.split()[0]}；playwright: {pw_version}；浏览器: {args.browser}"
          f"（headless={not args.headed}）")
    print(f"用例数: {len(selected)}；单步超时: {Case.ACTION_TIMEOUT}ms")
    if args.verbose:
        print(f"Chrome 路径: {chrome_exe}")

    runtime = Runtime(headed=args.headed, slow=args.slow, verbose=args.verbose,
                      keep=args.keep, prefer=args.browser)
    try:
        with sync_playwright() as pw:
            try:
                runtime.browser = launch_browser(pw, headed=args.headed, slow=args.slow,
                                                 prefer=args.browser, log=runtime.log)
            except Exception as e:  # noqa: BLE001
                print(f"[SKIP] 无法启动本机浏览器，E2E 跳过：{e}")
                return 2
            try:
                return run_tests(runtime, selected)
            finally:
                try:
                    runtime.browser.close()
                except Exception:
                    pass
    except Exception as e:  # noqa: BLE001
        print(f"[错误] E2E 运行器异常：{e}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
