#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
巢记 ChaoJi · 一键测试入口
================================================================================
依次运行：
  1) test_server_unit.py          —— server.py 纯函数单测（必须存在）
  2) test_server_integration.py   —— server.py 真实 HTTP 集成测试（存在才跑，否则 SKIP）
  3) test_e2e_ui.py               —— Playwright 真实浏览器 E2E（未装 playwright / 无 Chrome → SKIP）

用法：
  python run_all_tests.py                  # 跑全部
  python run_all_tests.py --only unit,e2e  # 只跑指定套件
  python run_all_tests.py -v               # 实时输出子进程日志
  python run_all_tests.py --list           # 列出套件
  python run_all_tests.py --e2e-filter md  # 透传给 E2E 的用例过滤

退出码：0 = 全部通过或仅 SKIP；1 = 存在失败；2 = 运行器自身错误。
说明：E2E 需要 playwright；本脚本会自动寻找带 playwright 的 Python
（优先级：--e2e-python > 环境变量 CHAOJI_E2E_PYTHON > D:\\dsh\\tmp\\chaoji-e2e\\venv
 > 仓库 .venv > 当前解释器），找不到时 E2E 明确标 SKIP 且不影响整体结果。
日志写入仓库外目录（默认 D:\\dsh\\tmp\\chaoji-e2e\\logs）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        enc = (getattr(_stream, "encoding", "") or "").lower()
        if enc not in ("utf-8", "utf8", "utf-8-sig"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
LOG_ROOT = Path(os.environ.get("CHAOJI_TEST_LOG_ROOT", r"D:\dsh\tmp\chaoji-e2e\logs"))
E2E_DEFAULT_PYTHONS = [
    Path(r"D:\dsh\tmp\chaoji-e2e\venv\Scripts\python.exe"),
    HERE / ".venv" / "Scripts" / "python.exe",
    HERE / "venv" / "Scripts" / "python.exe",
]


class Suite:
    def __init__(self, key, name, filename, *, required, timeout, e2e=False):
        self.key = key
        self.name = name
        self.path = HERE / filename
        self.required = required
        self.timeout = timeout
        self.e2e = e2e
        self.status = "PENDING"      # PASS / FAIL / SKIP / ERROR
        self.passed = None
        self.failed = None
        self.skipped = None
        self.duration = 0.0
        self.note = ""
        self.log_path: Path | None = None
        self.tail = ""

    def exists(self) -> bool:
        return self.path.is_file()


SUITES = [
    Suite("unit", "server.py 纯函数单测", "test_server_unit.py", required=True, timeout=180),
    Suite("integration", "server.py 集成测试", "test_server_integration.py", required=False, timeout=600),
    Suite("e2e", "真实浏览器 E2E", "test_e2e_ui.py", required=False, timeout=1800, e2e=True),
]


def _decode(data: bytes) -> str:
    if data is None:
        return ""
    return data.decode("utf-8", errors="replace")


def find_e2e_python(explicit: str | None, log) -> str | None:
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    env_py = os.environ.get("CHAOJI_E2E_PYTHON")
    if env_py:
        candidates.append(env_py)
    candidates += [str(p) for p in E2E_DEFAULT_PYTHONS]
    candidates.append(sys.executable)
    seen = set()
    for py in candidates:
        if not py or py in seen:
            continue
        seen.add(py)
        if not Path(py).is_file():
            continue
        try:
            r = subprocess.run([py, "-c", "import playwright, sys; sys.exit(0)"],
                               capture_output=True, timeout=60)
        except Exception:
            continue
        if r.returncode == 0:
            log(f"E2E Python: {py}（playwright 可用）")
            return py
    return None


def build_cmd(py: str, suite: Suite, e2e_filter: str) -> list[str]:
    if suite.e2e:
        cmd = [py, suite.path.name]
        if e2e_filter:
            cmd += ["--filter", e2e_filter]
        return cmd
    try:
        text = suite.path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        text = ""
    uses_unittest = re.search(r"^\s*(import unittest|from unittest)", text, re.M) is not None
    has_main = "__main__" in text or "unittest.main" in text
    if uses_unittest and not has_main:
        return [py, "-m", "unittest", suite.path.stem, "-v"]
    return [py, suite.path.name]


def count_pass_fail(text: str):
    passed = len(re.findall(r"^\s*PASS[:：]", text, re.M))
    failed = len(re.findall(r"^\s*FAIL[:：]", text, re.M))
    if passed or failed:
        return passed, failed
    # unittest 风格：Ran N tests ... OK / FAILED (failures=x, errors=y)
    m = re.search(r"^Ran\s+(\d+)\s+tests?\s+in\s", text, re.M)
    if m:
        total = int(m.group(1))
        fail_m = re.search(r"^FAILED\s*\(([^)]*)\)", text, re.M)
        if fail_m:
            counts = dict(re.findall(r"(failures|errors|skipped)=(\d+)", fail_m.group(1)))
            failed = sum(int(v) for k, v in counts.items() if k in ("failures", "errors"))
            skipped = int(counts.get("skipped", 0))
            return max(total - failed - skipped, 0), failed
        if re.search(r"^OK\b", text, re.M):
            skipped = 0
            skip_m = re.search(r"skipped=(\d+)", text)
            if skip_m:
                skipped = int(skip_m.group(1))
            return total - skipped, 0
        return total, 0
    return None, None


def parse_e2e_summary(text: str):
    marker = "__E2E_SUMMARY__ "
    idx = text.rfind(marker)
    if idx < 0:
        return None
    line = text[idx + len(marker):].splitlines()[0].strip()
    try:
        return json.loads(line)
    except Exception:
        return None


def run_suite(py: str, suite: Suite, log_dir: Path, *, verbose: bool, e2e_filter: str, log) -> None:
    if not suite.exists():
        suite.status = "SKIP"
        suite.note = "文件不存在，跳过"
        return
    suite.log_path = log_dir / f"{suite.key}.log"
    cmd = build_cmd(py, suite, e2e_filter)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    started = time.time()
    log(f"\n>>> [{suite.name}] {' '.join(cmd)}")
    captured: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, bufsize=0,
        )
        assert proc.stdout is not None
        deadline = started + suite.timeout
        timed_out = False
        for raw in iter(proc.stdout.readline, b""):
            line = _decode(raw)
            captured.append(line)
            if verbose:
                print("    " + line.rstrip("\n"), flush=True)
            if time.time() > deadline:
                timed_out = True
                proc.kill()
                break
        rc = proc.wait()
        if timed_out:
            captured.append(f"\n[超时] 套件超过 {suite.timeout}s 被终止\n")
            suite.status = "ERROR"
            suite.note = f"超时（>{suite.timeout}s）"
        else:
            text = "".join(captured)
            if suite.e2e:
                summary = parse_e2e_summary(text)
                if rc == 2:
                    suite.status = "SKIP"
                    suite.note = "环境缺失（playwright / Chrome 不可用），E2E 跳过"
                elif summary:
                    suite.passed = summary.get("passed")
                    suite.failed = summary.get("failed")
                    suite.skipped = summary.get("skipped")
                    suite.status = "PASS" if rc == 0 and not summary.get("failed") else "FAIL"
                elif rc == 0:
                    suite.status = "PASS"
                    suite.note = "未解析到汇总行，按退出码判定"
                else:
                    suite.status = "FAIL"
                    suite.note = f"退出码 {rc}"
            else:
                p, f = count_pass_fail(text)
                suite.passed, suite.failed = p, f
                suite.status = "PASS" if rc == 0 else ("FAIL" if rc == 1 else "ERROR")
                if rc not in (0, 1):
                    suite.note = f"退出码 {rc}"
    except Exception as e:  # noqa: BLE001
        suite.status = "ERROR"
        suite.note = f"运行器异常：{e}"
        captured.append(f"[运行器异常] {e}\n")
    finally:
        suite.duration = time.time() - started
        text = "".join(captured)
        suite.tail = "\n".join(text.splitlines()[-40:])
        if suite.log_path:
            try:
                suite.log_path.write_text(text, encoding="utf-8")
            except Exception:
                pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="巢记 ChaoJi 一键测试入口")
    parser.add_argument("--only", default="", help="只跑指定套件，逗号分隔：unit,integration,e2e")
    parser.add_argument("--list", action="store_true", help="列出套件")
    parser.add_argument("--verbose", "-v", action="store_true", help="实时输出子进程日志")
    parser.add_argument("--e2e-filter", default="", help="透传给 test_e2e_ui.py --filter")
    parser.add_argument("--e2e-python", default=None, help="指定带 playwright 的 Python 解释器")
    parser.add_argument("--keep-logs", action="store_true",
                        help="兼容参数（日志默认保留最近 8 次，便于失败回溯）")
    parser.add_argument("--python", default=sys.executable, help="unit/integration 使用的 Python（默认当前解释器）")
    args = parser.parse_args(argv)

    def log(msg: str):
        print(msg, flush=True)

    selected = SUITES
    if args.only:
        keys = {k.strip().lower() for k in args.only.split(",") if k.strip()}
        unknown = keys - {s.key for s in SUITES}
        if unknown:
            print(f"[参数错误] 未知套件：{sorted(unknown)}；可选 unit,integration,e2e")
            return 2
        selected = [s for s in SUITES if s.key in keys]

    if args.list:
        print("可用套件：")
        for s in SUITES:
            req = "必需" if s.required else "可选"
            print(f"  {s.key:<12} {s.name:<24} 文件={s.path.name:<28} {req}")
        return 0

    if not (HERE / "test_server_unit.py").is_file() and any(s.key == "unit" for s in selected):
        print(f"[环境错误] 缺少必需测试文件：{HERE / 'test_server_unit.py'}")
        return 2

    run_dir = LOG_ROOT / time.strftime("run-%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    log("=" * 84)
    log("巢记 ChaoJi · 一键测试")
    log("=" * 84)
    log(f"仓库: {HERE}")
    log(f"日志: {run_dir}")
    log(f"Python: {sys.version.split()[0]}（{args.python}）")

    unit_py = args.python
    e2e_py = find_e2e_python(args.e2e_python, log) if any(s.e2e for s in selected) else None
    if e2e_py is None and any(s.e2e for s in selected):
        log("E2E Python: 未找到已安装 playwright 的解释器（E2E 将 SKIP）")

    for suite in selected:
        if suite.e2e:
            if e2e_py is None:
                suite.status = "SKIP"
                suite.note = "未找到带 playwright 的 Python 解释器"
                continue
            py = e2e_py
        else:
            py = unit_py
            if not Path(py).is_file():
                suite.status = "SKIP"
                suite.note = f"Python 解释器不存在：{py}"
                continue
        run_suite(py, suite, run_dir, verbose=args.verbose,
                  e2e_filter=args.e2e_filter, log=log)
        status_line = f"<<< [{suite.name}] {suite.status}"
        if suite.passed is not None:
            status_line += f"（通过 {suite.passed}"
            if suite.failed is not None:
                status_line += f" / 失败 {suite.failed}"
            if suite.skipped:
                status_line += f" / 跳过 {suite.skipped}"
            status_line += "）"
        if suite.note:
            status_line += "：" + suite.note
        log(status_line + f"  用时 {suite.duration:.1f}s")

    # ---------- 汇总表 ----------
    log("\n" + "=" * 84)
    log("测试汇总")
    log("=" * 84)
    log(f"  {'套件':<24}{'状态':<8}{'通过':>6}{'失败':>6}{'跳过':>6}{'耗时':>9}  说明")
    log("  " + "-" * 80)
    n_pass = n_fail = n_skip = 0
    for s in selected:
        if s.status == "PASS":
            n_pass += 1
        elif s.status == "FAIL":
            n_fail += 1
        elif s.status == "SKIP":
            n_skip += 1
        else:
            n_fail += 1
        p = "-" if s.passed is None else str(s.passed)
        f = "-" if s.failed is None else str(s.failed)
        k = "-" if s.skipped is None else str(s.skipped)
        note = s.note
        if s.status == "FAIL" and s.tail:
            first_fail = next((ln.strip() for ln in s.tail.splitlines()
                               if "FAIL" in ln or "✗" in ln or "Error" in ln), "")
            if first_fail and not note:
                note = first_fail[:70]
        log(f"  {s.name:<24}{s.status:<8}{p:>6}{f:>6}{k:>6}{s.duration:>8.1f}s  {note}")
    log("  " + "-" * 80)
    log(f"套件 {len(selected)} 个：通过 {n_pass} / 失败 {n_fail} / 跳过 {n_skip}")
    log(f"日志目录：{run_dir}")

    failed_suites = [s for s in selected if s.status in ("FAIL", "ERROR")]
    if failed_suites:
        log("\n失败详情：")
        for s in failed_suites:
            log(f"\n----- [{s.name}] ({s.status}) -----")
            if s.log_path:
                log(f"完整日志：{s.log_path}")
            print(s.tail or "(无输出)", flush=True)
        log(f"\n结果：存在 {len(failed_suites)} 个失败套件")
        return 1

    log("\n结果：全部通过（SKIP 不计失败）")
    # 保留最近 8 次日志，清理更早的（日志很小，便于失败回溯）
    try:
        runs = sorted([p for p in LOG_ROOT.glob("run-*") if p.is_dir()], reverse=True)
        for old in runs[8:]:
            shutil.rmtree(old, ignore_errors=True)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
