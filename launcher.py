"""
claw-brain launcher - starts gateway + web console + opens browser
Runs silently (no console window) via pythonw.exe
"""
import subprocess
import sys
import time
import os
import webbrowser
import socket
import threading
import shutil
from pathlib import Path
from gateway_runtime import ensure_gateway as ensure_openclaw_gateway

USERPROFILE = os.environ.get("USERPROFILE", "")
PROJECT_DIR = str(Path(__file__).resolve().parent)
PYTHON = sys.executable
GW_SCRIPT = os.path.join(USERPROFILE, ".openclaw", "start-gateway.cmd")
WEB_SCRIPT = os.path.join(PROJECT_DIR, "web_console.py")
GW_PORT = 18789
WEB_PORT = 7860
BROWSER_OPENED = False


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.settimeout(0.5)
            s.connect(("127.0.0.1", port))
            return True
        except (ConnectionRefusedError, OSError):
            return False


def wait_port_free(port, timeout=10):
    """Wait until port is released (OS needs time after killing process)."""
    for _ in range(timeout):
        if not port_in_use(port):
            return True
        time.sleep(1)
    return False


def kill_port(port):
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                if parts:
                    pid = parts[-1]
                    subprocess.run(["taskkill", "/F", "/PID", pid],
                                   capture_output=True, timeout=10)
    except Exception:
        pass


def start_gateway():
    return ensure_openclaw_gateway(
        PROJECT_DIR,
        port=GW_PORT,
        max_wait=30,
        creationflags=0x08000000,  # CREATE_NO_WINDOW
    )


def start_web_console():
    """Watchdog: restart web console if it crashes."""
    while True:
        proc = subprocess.Popen(
            [PYTHON, WEB_SCRIPT],
            cwd=PROJECT_DIR,
            creationflags=0x08000000  # CREATE_NO_WINDOW
        )
        proc.wait()
        # Wait for port to be released before restarting
        wait_port_free(WEB_PORT, timeout=10)
        time.sleep(3)


def open_browser():
    global BROWSER_OPENED
    for _ in range(60):
        time.sleep(1)
        if port_in_use(WEB_PORT):
            time.sleep(1)
            webbrowser.open(f"http://127.0.0.1:{WEB_PORT}")
            BROWSER_OPENED = True
            return


if __name__ == "__main__":
    # Step 1: Kill old processes and wait for ports to release
    kill_port(GW_PORT)
    kill_port(WEB_PORT)
    wait_port_free(GW_PORT, timeout=10)
    wait_port_free(WEB_PORT, timeout=10)

    # Step 2: Start gateway
    start_gateway()

    # Step 3: Start web console in background thread (watchdog)
    web_thread = threading.Thread(target=start_web_console, daemon=True)
    web_thread.start()

    # Step 4: Wait for web console, then open browser
    open_browser()

    # Keep process alive (user closes via Task Manager or reboot)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
