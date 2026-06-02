"""
自主赚钱系统 - 带看门狗的启动器
=================================
监控后端进程健康，卡死时自动重启。

用法: python run_with_watchdog.py
"""

import subprocess
import sys
import time
import urllib.request
import os
import signal

PORT = 7860
MAX_CONSECUTIVE_FAILS = 3  # 连续失败次数阈值
CHECK_INTERVAL = 15  # 检查间隔（秒）
GRACE_PERIOD = 30  # 启动后宽限期（秒）


def is_backend_healthy():
    """检查后端是否正常响应"""
    try:
        req = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/state", timeout=5)
        return req.getcode() == 200
    except Exception:
        return False


def kill_port_processes():
    """杀掉占用端口的进程"""
    try:
        import subprocess
        result = subprocess.run(
            ["powershell", "-Command",
             f"Get-NetTCPConnection -LocalPort {PORT} -ErrorAction SilentlyContinue | "
             "Select-Object -ExpandProperty OwningProcess -Unique | "
             "ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }"],
            capture_output=True, timeout=10
        )
    except Exception:
        pass
    time.sleep(2)


def main():
    print("=" * 50)
    print("  自主赚钱系统 - 看门狗模式")
    print(f"  后端: http://127.0.0.1:{PORT}")
    print(f"  健康检查: 每 {CHECK_INTERVAL}s，连续 {MAX_CONSECUTIVE_FAILS} 次失败重启")
    print("=" * 50)

    python_exe = sys.executable
    script = os.path.join(os.path.dirname(__file__), "web_console.py")
    restart_count = 0

    while True:
        print(f"\n[WATCHDOG] 启动后端 (第 {restart_count + 1} 次)...")

        # 先清理残留进程
        kill_port_processes()

        # 启动后端子进程
        proc = subprocess.Popen(
            [python_exe, script],
            stdout=sys.stdout,
            stderr=sys.stderr,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )

        # 等待启动
        print(f"[WATCHDOG] 后端进程 PID={proc.pid}，等待启动...")
        time.sleep(GRACE_PERIOD)

        # 监控循环
        consecutive_fails = 0
        while True:
            # 检查子进程是否还在
            retcode = proc.poll()
            if retcode is not None:
                print(f"[WATCHDOG] 后端进程已退出 (code={retcode})，3秒后重启...")
                time.sleep(3)
                break

            # 健康检查
            if is_backend_healthy():
                if consecutive_fails > 0:
                    print(f"[WATCHDOG] 后端恢复正常")
                consecutive_fails = 0
            else:
                consecutive_fails += 1
                print(f"[WATCHDOG] 后端无响应 (失败 {consecutive_fails}/{MAX_CONSECUTIVE_FAILS})")

                if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                    print(f"[WATCHDOG] 连续 {consecutive_fails} 次失败，强制杀掉后端进程...")
                    try:
                        if sys.platform == "win32":
                            proc.send_signal(signal.CTRL_BREAK_EVENT)
                        else:
                            proc.terminate()
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                    print("[WATCHDOG] 进程已终止，3秒后重启...")
                    time.sleep(3)
                    break

            time.sleep(CHECK_INTERVAL)

        restart_count += 1
        # 最多重启 10 次后放弃
        if restart_count >= 10:
            print("[WATCHDOG] 已重启 10 次，停止。请手动检查问题。")
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[WATCHDOG] 用户中断退出")
