#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claw-brain 一键启动器
====================
双击 → 自动开机 → 等启动 → 开隧道 → 启动系统 → 打开浏览器
按任意键 → 关系统 → 关隧道 → 关机（停止计费）
"""

import subprocess
import time
import sys
import os
import json
import urllib.request
import webbrowser
import socket
import threading

# ========== 配置 ==========
PROJECT_DIR = r"C:\Users\楚\WorkBuddy\2026-05-15-task-28"
PYTHON = r"C:\Users\楚\.workbuddy\binaries\python\versions\3.13.12\python.exe"
NODE = r"C:\Users\楚\.workbuddy\binaries\node\versions\22.16.0\node.exe"
OPENCLAW_JS = r"C:\Users\楚\.workbuddy\binaries\node\versions\22.16.0\node_modules\openclaw\dist\index.js"

from pathlib import Path as _Path
import shutil as _shutil
from gateway_runtime import gateway_command as _gateway_command
from gateway_runtime import gateway_env as _gateway_env

# 覆盖旧项目路径，确保从当前 claw-brain-latest 启动。
PROJECT_DIR = str(_Path(__file__).resolve().parent)
PYTHON = sys.executable
NODE = _shutil.which("node") or NODE
OPENCLAW_JS = os.environ.get("OPENCLAW_JS", OPENCLAW_JS if os.path.isfile(OPENCLAW_JS) else "")

# AutoDL
INSTANCE_UUID = "pro-77977e96aa06"
API_BASE = "https://api.autodl.com"
API_TOKEN = os.environ.get("AUTODL_API_TOKEN", "")

# 端口
TUNNEL_PORT = 8001
GW_PORT = 18789
WEB_PORT = 7860

# SSH（检测实例是否就绪）
SSH_HOST = "connect.bjb1.seetacloud.com"
SSH_PORT = 48216

# 子进程追踪
_procs = []

# 闲置自动关机（可被 watchdog 触发）
_shutdown_flag = threading.Event()
IDLE_TIMEOUT = 1800  # 默认 30 分钟


def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def api_post(path, data):
    if not API_TOKEN:
        raise RuntimeError("AUTODL_API_TOKEN 未设置，无法调用 AutoDL API")
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{API_BASE}{path}", data=body,
        headers={"Authorization": API_TOKEN, "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def power_on():
    try:
        r = api_post("/api/v1/dev/instance/pro/power_on", {
            "instance_uuid": INSTANCE_UUID, "payload": "gpu",
        })
        ok = r.get("code") == "Success"
        if ok:
            log("开机指令已发送")
        else:
            log(f"开机返回: {r.get('msg', '未知')}")
        return ok
    except Exception as e:
        log(f"开机请求失败: {e}")
        return False


def power_off():
    try:
        r = api_post("/api/v1/dev/instance/pro/power_off", {
            "instance_uuid": INSTANCE_UUID,
        })
        ok = r.get("code") == "Success"
        if ok:
            log("关机指令已发送")
        else:
            log(f"关机返回: {r.get('msg', '未知')}")
        return ok
    except Exception as e:
        log(f"关机请求失败: {e}")
        return False


def wait_ssh_ready(timeout=300):
    """等待 SSH 端口可连接（实例真正就绪）"""
    log("等待实例就绪，测试 SSH 端口...")
    for i in range(timeout):
        if i > 0 and i % 10 == 0:
            log(f"  已等待 {i} 秒，继续...")
        try:
            sock = socket.create_connection((SSH_HOST, SSH_PORT), timeout=2)
            sock.close()
            log("实例已就绪！")
            return True
        except Exception:
            time.sleep(1)
    log("SSH 连接超时，实例可能未启动")
    return False


def port_ok(port):
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=1)
        sock.close()
        return True
    except Exception:
        return False


def kill_port(port, name=""):
    """清理占用指定端口的进程"""
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, encoding="gbk", errors="replace", timeout=10
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                if parts:
                    pid = parts[-1]
                    subprocess.run(["taskkill", "/F", "/PID", pid],
                                   capture_output=True, timeout=5)
                    log(f"  清理端口 {port} 的旧进程 (PID={pid})")
                    time.sleep(1)
    except Exception:
        pass


def kill_by_cmdline(pattern):
    """通过命令行匹配杀掉进程（清理可能的残留）"""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{pattern}*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"],
            capture_output=True, timeout=15, encoding="gbk", errors="replace"
        )
    except Exception:
        pass


def start_tunnel():
    log("启动 SSH 隧道...")
    tunnel_py = os.path.join(PROJECT_DIR, "tunnel.py")
    proc = subprocess.Popen([PYTHON, tunnel_py], cwd=PROJECT_DIR)
    _procs.append(("tunnel", proc))
    for i in range(15):
        time.sleep(1)
        if port_ok(TUNNEL_PORT):
            log("SSH 隧道已就绪")
            return True
    log("[WARN] 隧道端口未就绪，但继续...")
    return False


def start_gateway():
    log("启动 OpenClaw 网关...")
    if not os.path.isfile(OPENCLAW_JS):
        log(f"[ERROR] OpenClaw 未找到: {OPENCLAW_JS}")
        return False

    env = os.environ.copy()
    env["HTTP_PROXY"] = "http://127.0.0.1:17890"
    env["HTTPS_PROXY"] = "http://127.0.0.1:17890"
    env["NO_PROXY"] = "localhost,127.0.0.1,::1"
    env["NODE_OPTIONS"] = ""

    proc = subprocess.Popen(
        [NODE, OPENCLAW_JS, "gateway", "--port", str(GW_PORT)],
        env=env
    )
    _procs.append(("gateway", proc))

    for i in range(20):
        time.sleep(1)
        if port_ok(GW_PORT):
            log("OpenClaw 网关已就绪")
            return True
    log("[WARN] 网关启动超时")
    return False


def _openclaw_gateway_command():
    cmd = _gateway_command()
    if cmd:
        return cmd
    if OPENCLAW_JS and os.path.isfile(OPENCLAW_JS) and NODE:
        return [NODE, OPENCLAW_JS, "gateway", "--port", str(GW_PORT)]
    return None


def start_gateway():
    log("启动 OpenClaw 网关...")
    cmd = _openclaw_gateway_command()
    if not cmd:
        log("[ERROR] 未找到 openclaw/npx，也没有可用 OPENCLAW_JS")
        return False

    env = _gateway_env()
    env["HTTP_PROXY"] = "http://127.0.0.1:17890"
    env["HTTPS_PROXY"] = "http://127.0.0.1:17890"
    env["NO_PROXY"] = "localhost,127.0.0.1,::1"
    env["NODE_OPTIONS"] = ""

    proc = subprocess.Popen(cmd, cwd=PROJECT_DIR, env=env)
    _procs.append(("gateway", proc))

    for _ in range(20):
        time.sleep(1)
        if port_ok(GW_PORT):
            log("OpenClaw 网关已就绪")
            return True
    log("[WARN] 网关启动超时")
    return False


def start_web():
    log("启动 Web 控制台...")
    web_py = os.path.join(PROJECT_DIR, "start_web.py")
    proc = subprocess.Popen([PYTHON, web_py], cwd=PROJECT_DIR,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _procs.append(("web", proc))

    for i in range(30):
        time.sleep(1)
        if port_ok(WEB_PORT):
            log("Web 控制台已就绪")
            return True
    log("[WARN] Web 启动超时")
    return False


def open_browser():
    url = f"http://127.0.0.1:{WEB_PORT}"
    log(f"打开浏览器: {url}")
    webbrowser.open(url)


def wait_key():
    log("\n" + "=" * 50)
    log("Claw-brain 已启动！浏览器应该已经打开。")
    log("用完后请在此窗口按任意键，自动关闭系统并关机。")
    log("闲置 30 分钟无操作将自动关机。")
    log("=" * 50 + "\n")

    if os.name == "nt":
        try:
            import msvcrt
            # 每 1 秒检查一次键盘输入 + 关机信号
            while not _shutdown_flag.is_set():
                if msvcrt.kbhit():
                    msvcrt.getch()
                    return
                time.sleep(1)
        except Exception:
            os.system("pause >nul")
    else:
        input("按 Enter 关闭...")


def start_idle_watchdog():
    """启动闲置检测线程：每隔 30 秒检查一次活动时间，超时则自动关机"""
    def _watch():
        check_interval = 30  # 每 30 秒检查一次
        state_url = f"http://127.0.0.1:{WEB_PORT}/api/state"
        while not _shutdown_flag.is_set():
            time.sleep(check_interval)
            try:
                req = urllib.request.Request(state_url)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                last_ts = data.get("last_activity_time", 0)
                timeout = data.get("idle_timeout", IDLE_TIMEOUT)
                if last_ts <= 0:
                    continue
                idle_sec = time.time() - last_ts
                if idle_sec > timeout:
                    log(f"\n[闲置关机] 已闲置 {int(idle_sec)} 秒（超时 {timeout} 秒），自动关机...")
                    break
            except Exception:
                pass  # web 可能仍在启动中，忽略
        # 触发关机
        _shutdown_flag.set()

    t = threading.Thread(target=_watch, daemon=True)
    t.start()
    return t


def cleanup():
    log("\n正在关闭系统...")

    # 1. 按反向顺序杀掉已知子进程
    for name, proc in reversed(_procs):
        try:
            if proc.poll() is None:
                log(f"  停止 {name} (PID={proc.pid})...")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                    try:
                        proc.wait(timeout=3)
                    except Exception:
                        pass
        except Exception as e:
            log(f"  停止 {name} 出错: {e}")

    # 2. 清理可能残留的进程（worker 等孙子进程）
    log("  清理残留进程...")
    for pattern in ["web_console.py", "worker.py", "tunnel.py", "openclaw"]:
        kill_by_cmdline(pattern)
    time.sleep(1)

    # 3. 清理端口
    for port in [WEB_PORT, GW_PORT, TUNNEL_PORT]:
        kill_port(port)

    # 4. 关机
    log("正在关机（停止计费）...")
    power_off()

    log("已关闭。下次见！")


def main():
    try:
        # 0. 清理残留
        log("清理残留进程...")
        for port in [TUNNEL_PORT, GW_PORT, WEB_PORT]:
            kill_port(port)
        for pattern in ["web_console.py", "worker.py", "tunnel.py", "openclaw", "claw-brain-temp"]:
            kill_by_cmdline(pattern)
        time.sleep(2)

        # 1. 开机（如果已经在运行，SSH 测试会通过）
        power_on()

        # 2. 等实例就绪
        if not wait_ssh_ready(timeout=300):
            log("实例未就绪，退出。请检查 AutoDL 控制台。")
            return

        # 3. 启动隧道
        start_tunnel()

        # 4. 启动网关
        start_gateway()

        # 5. 启动 Web
        start_web()

        # 6. 打开浏览器
        time.sleep(2)
        open_browser()

        # 7. 启动闲置自动关机 watchdog（30 分钟无操作自动关机）
        start_idle_watchdog()
        log(f"闲置监测已启动，{IDLE_TIMEOUT // 60} 分钟无操作将自动关机")

        # 8. 等待用户关闭（按任意键或 watchdog 触发）
        wait_key()

    except KeyboardInterrupt:
        log("收到中断信号...")
    finally:
        _shutdown_flag.set()
        cleanup()


if __name__ == "__main__":
    main()
