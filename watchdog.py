"""
claw-brain web console watchdog.

Starts the OpenClaw gateway when possible, runs web_console.py, and restarts it
after crashes or when logs/restart.trigger is created.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable
WEB_SCRIPT = PROJECT_DIR / "web_console.py"
LOG_DIR = PROJECT_DIR / "logs"
LOG_FILE = LOG_DIR / "watchdog.log"
RESTART_TRIGGER = LOG_DIR / "restart.trigger"

WEB_PORT = 7860
GATEWAY_PORT = 18789
MAX_RESTART = 50

LOG_DIR.mkdir(exist_ok=True)
os.chdir(PROJECT_DIR)

_current_proc: subprocess.Popen | None = None
_trigger_event = threading.Event()


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def kill_port_users(port: int) -> None:
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Get-NetTCPConnection -LocalPort {port} -State Listen "
                "-ErrorAction SilentlyContinue | "
                "Select-Object -ExpandProperty OwningProcess -Unique",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.splitlines():
            pid = line.strip()
            if pid.isdigit():
                subprocess.run(
                    ["taskkill", "/F", "/PID", pid],
                    capture_output=True,
                    timeout=5,
                )
                log(f"已清理端口 {port} 的旧进程 PID={pid}")
        time.sleep(2)
    except Exception as exc:
        log(f"[WARN] 端口清理失败: {exc}")


def gateway_command() -> list[str] | None:
    openclaw = shutil.which("openclaw")
    if openclaw:
        return [openclaw, "gateway", "run", "--force"]
    npx = shutil.which("npx")
    if npx:
        return [npx, "openclaw", "gateway", "run", "--force"]
    return None


def ensure_gateway(max_wait: int = 20) -> bool:
    if port_open(GATEWAY_PORT):
        log("OpenClaw gateway already running")
        return True

    cmd = gateway_command()
    if not cmd:
        log("[WARN] openclaw/npx not found, skip gateway start")
        return False

    env = os.environ.copy()
    env["NODE_OPTIONS"] = ""
    env.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")

    log("Starting OpenClaw gateway...")
    subprocess.Popen(
        cmd,
        cwd=PROJECT_DIR,
        env=env,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    for _ in range(max_wait):
        time.sleep(1)
        if port_open(GATEWAY_PORT):
            log(f"OpenClaw gateway ready on {GATEWAY_PORT}")
            return True

    log(f"[WARN] OpenClaw gateway start timeout after {max_wait}s")
    return False


def trigger_watcher() -> None:
    global _current_proc
    while True:
        try:
            if RESTART_TRIGGER.exists():
                RESTART_TRIGGER.unlink(missing_ok=True)
                log("Restart trigger detected, stopping web console...")
                if _current_proc and _current_proc.poll() is None:
                    _current_proc.terminate()
                    try:
                        _current_proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        _current_proc.kill()
                        _current_proc.wait(timeout=5)
                _trigger_event.set()
        except Exception as exc:
            log(f"[WARN] trigger watcher error: {exc}")
        time.sleep(2)


def run_web_console() -> int:
    global _current_proc
    _current_proc = subprocess.Popen(
        [PYTHON, str(WEB_SCRIPT)],
        cwd=PROJECT_DIR,
    )
    while _current_proc.poll() is None:
        time.sleep(1)
    return int(_current_proc.returncode or 0)


def main() -> None:
    log("=== claw-brain watchdog started ===")
    threading.Thread(target=trigger_watcher, daemon=True).start()
    ensure_gateway()

    restart_count = 0
    while restart_count < MAX_RESTART:
        if _trigger_event.is_set():
            _trigger_event.clear()
            restart_count = 0
            log("Restarting after code change...")
        else:
            kill_port_users(WEB_PORT)

        log(f"Starting web console, attempt {restart_count + 1}...")
        try:
            exit_code = run_web_console()
            log(f"Web console exited with code {exit_code}")
        except Exception as exc:
            log(f"Web console start failed: {exc}")

        time.sleep(5)
        restart_count += 1

    log(f"Reached max restart count {MAX_RESTART}, watchdog stopped.")


if __name__ == "__main__":
    main()
