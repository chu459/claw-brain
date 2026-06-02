"""
Claw-brain Worker Process
=========================
独立进程运行 run_loop，与 Web Server 通过文件系统通信。

通信协议：
  - command.json: Web Server → Worker（启动/停止/注入消息/回答）
  - snapshot.json: Worker → Web Server（每轮结束写入当前状态快照）

设计原则：
  - Worker 可以卡死/崩溃，不影响 Web Server 响应
  - 快照文件是原子的（write + rename），不会读到半写状态
  - Worker 内部不做任何 HTTP 服务，纯计算 + 文件 IO
"""

import json
import os
import sys
import time
import signal
import threading
from pathlib import Path
from datetime import datetime

# 确保能导入核心模块
sys.path.insert(0, str(Path(__file__).parent))

# 自动加载 .env
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = value

# === 通信文件路径 ===
PIPE_DIR = Path(__file__).parent / "pipe"
PIPE_DIR.mkdir(exist_ok=True)
COMMAND_FILE = PIPE_DIR / "command.json"
SNAPSHOT_FILE = PIPE_DIR / "snapshot.json"
SNAPSHOT_TMP = PIPE_DIR / "snapshot.tmp"  # 原子写入临时文件

# === Worker 状态 ===
_worker_state = {
    "task_id": "",
    "session_id": "",
    "goal": "",
    "agent": "",
    "running": False,
    "loop_count": 0,
    "brain_log": [],
    "claw_log": [],
    "chat_history": [],
    "status_text": "待命",
    "has_question": False,
    "pending_question": "",
    "error": "",
    "started_at": "",
    "last_update": 0,
}
_worker_lock = threading.Lock()
_stop_event = threading.Event()


def _atomic_write_json(path: Path, data: dict):
    """原子写入 JSON 文件：先写临时文件，再 rename"""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _read_command() -> dict:
    """读取命令文件，读取后删除"""
    if not COMMAND_FILE.exists():
        return {}
    try:
        data = json.loads(COMMAND_FILE.read_text(encoding="utf-8"))
        # 读取后删除命令文件，避免重复消费
        COMMAND_FILE.unlink(missing_ok=True)
        return data
    except Exception:
        return {}


def _write_snapshot():
    """写入当前状态快照"""
    with _worker_lock:
        snapshot = {
            "task_id": _worker_state["task_id"],
            "session_id": _worker_state["session_id"],
            "goal": _worker_state["goal"],
            "agent": _worker_state["agent"],
            "running": _worker_state["running"],
            "loop_count": _worker_state["loop_count"],
            "brain_log": _worker_state["brain_log"][-50:],  # 只保留最近50条
            "claw_log": _worker_state["claw_log"][-30:],
            "chat_history": _worker_state["chat_history"][-30:],
            "status_text": _worker_state["status_text"],
            "has_question": _worker_state["has_question"],
            "pending_question": _worker_state["pending_question"],
            "error": _worker_state["error"],
            "started_at": _worker_state["started_at"],
            "last_update": time.time(),
            "pid": os.getpid(),
        }
    _atomic_write_json(SNAPSHOT_FILE, snapshot)


def _command_poller():
    """后台线程：每 0.5 秒检查一次命令文件"""
    while not _stop_event.is_set():
        cmd = _read_command()
        if cmd:
            _handle_command(cmd)
        _stop_event.wait(0.5)


def _handle_command(cmd: dict):
    """处理来自 Web Server 的命令"""
    action = cmd.get("action", "")

    if action == "stop":
        print(f"[WORKER] 收到停止命令")
        with _worker_lock:
            _worker_state["running"] = False
        _stop_event.set()

    elif action == "inject_feedback":
        # 用户中途发消息——注入到 run_loop 的 state
        text = cmd.get("text", "")
        task_id = cmd.get("task_id", "")
        if text and task_id == _worker_state["task_id"]:
            # 通过修改 state_logs 文件来传递消息
            # Worker 的 run_loop 会读取 injected_feedback
            inject_file = PIPE_DIR / "inject.json"
            inject_file.write_text(json.dumps({
                "type": "feedback",
                "text": text,
                "time": time.strftime("%H:%M:%S"),
            }), encoding="utf-8")
            print(f"[WORKER] 注入用户反馈: {text[:60]}")
            # 同时更新 chat_history
            with _worker_lock:
                _worker_state["chat_history"].append({"role": "usr", "text": text})

    elif action == "answer":
        # 回答 need_input 问题
        answer = cmd.get("answer", "")
        if answer:
            answer_file = PIPE_DIR / "answer.json"
            answer_file.write_text(json.dumps({
                "answer": answer,
                "time": time.strftime("%H:%M:%S"),
            }), encoding="utf-8")
            print(f"[WORKER] 收到回答: {answer[:60]}")
            with _worker_lock:
                _worker_state["chat_history"].append({"role": "usr", "text": answer})
                _worker_state["has_question"] = False
                _worker_state["pending_question"] = ""


def _on_input_needed_web(question: str) -> str:
    """Web 模式下的输入回调——通过文件等待用户回答"""
    with _worker_lock:
        _worker_state["has_question"] = True
        _worker_state["pending_question"] = question
        _worker_state["chat_history"].append({"role": "sys", "text": question})
    _write_snapshot()

    # 等待用户回答（通过 answer.json）
    answer_file = PIPE_DIR / "answer.json"
    timeout = 300  # 5分钟
    start = time.time()
    while time.time() - start < timeout:
        if _stop_event.is_set():
            return ""
        if not _worker_state.get("running", False):
            return ""
        if answer_file.exists():
            try:
                data = json.loads(answer_file.read_text(encoding="utf-8"))
                answer_file.unlink(missing_ok=True)
                return data.get("answer", "")
            except Exception:
                pass
        time.sleep(0.5)

    return ""  # 超时


def _on_event(event_type: str, msg: str):
    """事件回调——更新状态快照"""
    with _worker_lock:
        _worker_state["status_text"] = msg
    _write_snapshot()


def _check_injected_feedback() -> str:
    """检查是否有用户注入的反馈消息"""
    inject_file = PIPE_DIR / "inject.json"
    if not inject_file.exists():
        return ""
    try:
        data = json.loads(inject_file.read_text(encoding="utf-8"))
        inject_file.unlink(missing_ok=True)
        return data.get("text", "")
    except Exception:
        return ""


def run_worker(goal: str, agent: str, max_loops: int, interval: int,
               task_id: str, session_id: str, session_key: str,
               memory_file: str, continue_from: str = "",
               prev_loop_count: int = 0, prev_brain_log: list = None,
               prev_claw_log: list = None):
    """Worker 主函数——在独立进程中运行 run_loop"""

    from core import SystemState, RunLoopConfig, run_loop, SessionManager
    from autonomous_system import OutputManager, OUTPUT_DIR

    # 配置环境变量
    brain_api_key = os.environ.get("BRAIN_API_KEY", "")
    brain_base_url = os.environ.get("BRAIN_BASE_URL", "https://api.deepseek.com/v1")
    brain_model = os.environ.get("BRAIN_MODEL", "deepseek-chat")
    gateway_url = os.environ.get("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")

    # 也从凭据库尝试读取
    if not brain_api_key:
        try:
            from credential_store import get_credential_value
            brain_api_key = get_credential_value("DeepSeek", "api_key") or ""
            if not brain_base_url or brain_base_url == "https://api.deepseek.com/v1":
                brain_base_url = get_credential_value("DeepSeek", "base_url") or brain_base_url
            if not brain_model or brain_model == "deepseek-chat":
                brain_model = get_credential_value("DeepSeek", "model") or brain_model
        except Exception:
            pass

    # 初始化 Worker 状态
    with _worker_lock:
        _worker_state["task_id"] = task_id
        _worker_state["session_id"] = session_id
        _worker_state["goal"] = goal
        _worker_state["agent"] = agent
        _worker_state["running"] = True
        _worker_state["started_at"] = datetime.now().strftime("%H:%M:%S")

    print(f"[WORKER] 启动: task_id={task_id}, goal={goal[:40]}...")
    _write_snapshot()

    # 创建 SystemState
    state = SystemState(task_id=task_id)
    if prev_brain_log:
        with state.lock:
            state.brain_log = list(prev_brain_log)
            state.claw_log = list(prev_claw_log or [])

    with state.lock:
        state.running = True

    # 启动命令轮询线程
    cmd_thread = threading.Thread(target=_command_poller, daemon=True, name="cmd-poller")
    cmd_thread.start()

    # Monkey-patch: 让 run_loop 能检查注入的反馈
    _original_wait = None

    # 创建自定义 on_input_needed 回调
    def on_input_needed(question: str) -> str:
        return _on_input_needed_web(question)

    # 初始化 SessionManager 用于增量保存
    worker_session_mgr = SessionManager()

    # 上次保存时的日志长度（用于增量判断）
    _last_saved_counts = {"brain": 0, "claw": 0}

    # 启动状态更新线程：每 3 秒写一次快照 + 增量保存 Session 日志
    def snapshot_updater():
        while not _stop_event.is_set() and _worker_state.get("running"):
            # 检查注入的反馈
            feedback = _check_injected_feedback()
            if feedback:
                with state.lock:
                    state.injected_feedback = feedback
                    state.feedback_event.set()

            # 同步 state 到 worker_state
            with state.lock:
                _worker_state["loop_count"] = state.loop_count
                _worker_state["brain_log"] = list(state.brain_log)
                _worker_state["claw_log"] = list(state.claw_log)
                _worker_state["chat_history"] = list(state.chat_history)
                _worker_state["running"] = state.running

            _write_snapshot()

            # 增量保存 Session 日志（日志有变化时才写入）
            cur_brain = len(_worker_state["brain_log"])
            cur_claw = len(_worker_state["claw_log"])
            if cur_brain > _last_saved_counts["brain"] or cur_claw > _last_saved_counts["claw"]:
                try:
                    worker_session_mgr.save_session_logs(
                        session_id,
                        list(_worker_state["brain_log"]),
                        list(_worker_state["claw_log"]),
                    )
                    _last_saved_counts["brain"] = cur_brain
                    _last_saved_counts["claw"] = cur_claw
                except Exception as e:
                    print(f"[WORKER] 增量保存 Session 日志失败: {e}")

            _stop_event.wait(3)

    snap_thread = threading.Thread(target=snapshot_updater, daemon=True, name="snapshot")
    snap_thread.start()

    # 运行 run_loop
    try:
        config = RunLoopConfig(
            goal=goal, agent=agent, max_loops=max_loops, interval=interval,
            brain_api_key=brain_api_key, brain_base_url=brain_base_url,
            brain_model=brain_model, gateway_url=gateway_url,
            session_key=session_key, memory_file=memory_file,
            output_manager=OutputManager(OUTPUT_DIR),
        )
        run_loop(state, config, on_input_needed=on_input_needed, on_event=_on_event)
    except Exception as e:
        import traceback
        print(f"[WORKER] run_loop 异常: {e}")
        traceback.print_exc()
        with _worker_lock:
            _worker_state["error"] = str(e)
    finally:
        with _worker_lock:
            _worker_state["running"] = False
            _worker_state["status_text"] = "已停止"
            _worker_state["loop_count"] = state.loop_count
            _worker_state["brain_log"] = list(state.brain_log)
            _worker_state["claw_log"] = list(state.claw_log)
            _worker_state["chat_history"] = list(state.chat_history)

        # 保存 session 日志
        try:
            session_mgr = SessionManager()
            if session_id:
                session_mgr.save_session_logs(session_id, list(state.brain_log), list(state.claw_log))
                session_mgr.archive_session(session_id, "stopped")
        except Exception as e:
            print(f"[WORKER] session 保存失败: {e}")

        # 清理 OpenClaw 残留进程
        try:
            _cleanup_openclaw(session_key)
        except Exception:
            pass

        _stop_event.set()
        _write_snapshot()
        print(f"[WORKER] 退出: task_id={task_id}")


def _cleanup_openclaw(session_key: str):
    """清理 OpenClaw 残留进程"""
    import subprocess
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"WINDOWTITLE eq {session_key}"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if "node" in line.lower():
                parts = line.split()
                if parts:
                    try:
                        subprocess.run(["taskkill", "/PID", parts[1], "/F"],
                                       capture_output=True, timeout=5)
                    except Exception:
                        pass
    except Exception:
        pass


# === 入口：通过命令行参数或 pipe/startup.json 启动 ===
if __name__ == "__main__":
    # 方式1：命令行参数
    if len(sys.argv) > 1:
        startup_file = sys.argv[1]
        try:
            startup = json.loads(Path(startup_file).read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[WORKER] 无法读取启动参数: {e}")
            sys.exit(1)
    else:
        # 方式2：读取 pipe/startup.json
        startup_file = PIPE_DIR / "startup.json"
        if not startup_file.exists():
            print("[WORKER] 没有启动参数，退出")
            sys.exit(1)
        try:
            startup = json.loads(startup_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[WORKER] 无法读取启动参数: {e}")
            sys.exit(1)

    print(f"[WORKER] PID={os.getpid()}, 启动参数加载完成")

    # 处理信号
    def _sig_handler(signum, frame):
        print(f"[WORKER] 收到信号 {signum}，正在退出...")
        with _worker_lock:
            _worker_state["running"] = False
        _stop_event.set()

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    try:
        run_worker(
            goal=startup.get("goal", ""),
            agent=startup.get("agent", "main"),
            max_loops=startup.get("max_loops", 10),
            interval=startup.get("interval", 15),
            task_id=startup.get("task_id", ""),
            session_id=startup.get("session_id", ""),
            session_key=startup.get("session_key", ""),
            memory_file=startup.get("memory_file", ""),
            continue_from=startup.get("continue_from", ""),
            prev_loop_count=startup.get("prev_loop_count", 0),
            prev_brain_log=startup.get("prev_brain_log"),
            prev_claw_log=startup.get("prev_claw_log"),
        )
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        print(f"[WORKER] Fatal error in run_worker: {e}")
        traceback.print_exc()
        sys.exit(1)
