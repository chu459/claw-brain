"""
claw-brain Web 控制台守护进程
崩溃后自动重启，支持代码热重载。
用法：通过 启动控制台.bat 启动
"""
import subprocess
import time
import sys
import os
import threading
from datetime import datetime
from pathlib import Path

PROJECT_DIR = r"C:\Users\楚\WorkBuddy\2026-05-15-task-28"
PYTHON = r"C:\Users\楚\.workbuddy\binaries\python\versions\3.13.12\python.exe"
WEB_SCRIPT = os.path.join(PROJECT_DIR, "web_console.py")
LOG_DIR = os.path.join(PROJECT_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "watchdog.log")
RESTART_TRIGGER = os.path.join(LOG_DIR, "restart.trigger")  # 改完代码后写此文件触发重启

os.makedirs(LOG_DIR, exist_ok=True)
os.chdir(PROJECT_DIR)

# 全局：当前 web_console 进程引用（供 trigger 线程杀掉）
_current_proc = None
_trigger_event = threading.Event()  # 触发重启的事件


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def kill_port_users(port, name="服务"):
    """清理占用指定端口的进程（启动前必做，防止端口冲突）"""
    import socket
    # 先检查端口是否被占用
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        sock.close()
        # 端口有响应，说明有进程在监听 → 需要杀掉
    except (socket.timeout, ConnectionRefusedError, OSError):
        return  # 端口空闲，不需要处理

    # 用 PowerShell 找到占用端口的 PID（比 netstat 更可靠）
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-NetTCPConnection -LocalPort {port} -State Listen "
             f"-ErrorAction SilentlyContinue | "
             f"Select-Object -ExpandProperty OwningProcess -Unique"],
            capture_output=True, timeout=10
        )
        if result.stdout:
            for line in result.stdout.decode("utf-8", errors="replace").strip().splitlines():
                pid = line.strip()
                if pid.isdigit():
                    try:
                        subprocess.run(
                            ["taskkill", "/F", "/PID", pid],
                            capture_output=True, timeout=5
                        )
                        log(f"清理端口 {port} 上的旧进程 (PID={pid})")
                    except Exception as e:
                        log(f"[WARN] 清理 PID {pid} 失败: {e}")
        time.sleep(3)  # 等端口释放（Windows TIME_WAIT 需要时间）
    except Exception as e:
        log(f"[WARN] 端口清理异常: {e}")


def ensure_gateway(max_wait=15):
    """确保 OpenClaw 网关在运行，没运行则启动"""
    import socket
    port = 18789
    # 检查端口
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        sock.close()
        log("OpenClaw 网关已在运行")
        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        pass

    # 启动网关
    node = r"C:\Users\楚\.workbuddy\binaries\node\versions\22.16.0\node.exe"
    index_js = os.path.join(
        os.path.expanduser("~"),
        r".workbuddy\binaries\node\versions\22.16.0\node_modules\openclaw\dist\index.js"
    )
    if not os.path.isfile(index_js):
        log(f"[WARN] OpenClaw index.js 不存在: {index_js}，跳过网关启动")
        return False

    env = os.environ.copy()
    env["NODE_OPTIONS"] = ""
    env["HTTP_PROXY"] = "http://127.0.0.1:17890"
    env["HTTPS_PROXY"] = "http://127.0.0.1:17890"
    env["NO_PROXY"] = "localhost,127.0.0.1,::1"

    log("启动 OpenClaw 网关...")
    subprocess.Popen(
        [node, index_js, "gateway", "--port", str(port)],
        env=env,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    # 等待端口就绪
    for i in range(max_wait):
        time.sleep(1)
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=1)
            sock.close()
            log(f"OpenClaw 网关已就绪 (:{port})")
            return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            continue

    log(f"[WARN] OpenClaw 网关启动超时（{max_wait}秒）")
    return False


def trigger_watcher():
    """后台线程：每秒检查 trigger 文件，检测到就杀掉 web_console 触发重启"""
    global _current_proc
    while True:
        try:
            if os.path.exists(RESTART_TRIGGER):
                os.remove(RESTART_TRIGGER)
                log("检测到重启触发信号（代码变更），杀掉旧进程...")
                if _current_proc and _current_proc.poll() is None:
                    _current_proc.terminate()
                    try:
                        _current_proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        _current_proc.kill()
                        _current_proc.wait(timeout=5)
                _trigger_event.set()
        except Exception as e:
            log(f"[WARN] trigger 检查异常: {e}")
        time.sleep(2)  # 每 2 秒检查一次


MAX_RESTART = 50
restart_count = 0

log("=== claw-brain 守护进程启动 ===")

# 启动 trigger 监控线程（守护线程，随主进程退出）
_watcher = threading.Thread(target=trigger_watcher, daemon=True)
_watcher.start()

ensure_gateway()

while restart_count < MAX_RESTART:
    # 检查是否有重启信号
    if _trigger_event.is_set():
        _trigger_event.clear()
        restart_count = 0  # 外部触发的重启不计入熔断
        log("代码变更重启：立即重启...")
        time.sleep(1)
    else:
        # 每次启动前清理可能残留的旧进程（防止端口冲突）
        kill_port_users(7860, "Web 控制台")

    log(f"启动 Web 控制台 (第 {restart_count + 1} 次)...")
    try:
        _current_proc = subprocess.Popen(
            [PYTHON, WEB_SCRIPT],
            cwd=PROJECT_DIR,
            # 不用 PIPE：直接继承父进程的 stdout/stderr，避免输出缓冲阻塞
        )
        # 等待进程退出
        while _current_proc.poll() is None:
            time.sleep(1)
        exit_code = _current_proc.returncode
        log(f"Web 控制台退出 (code={exit_code})")

        # 如果是 trigger 触发的退出，立即重启
        if _trigger_event.is_set():
            _trigger_event.clear()
            restart_count = 0
            time.sleep(2)
            continue

        # 否则等 5 秒重启
        log(f"5秒后自动重启...")
        time.sleep(5)

    except Exception as e:
        log(f"启动异常: {e}，5秒后重启...")
        time.sleep(5)

    restart_count += 1

log(f"已达最大重启次数 ({MAX_RESTART})，停止守护。")
