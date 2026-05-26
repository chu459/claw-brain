"""
Claw-brain Web Console - Backend (v3 Frontend-Separated)
=========================================================
FastAPI backend serving static files + JSON API.
Frontend: Vanilla JS in static/ directory.

Start: python server.py
Access: http://127.0.0.1:7860
"""

import json
import threading
import time
import os
import sys
import queue
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# Auto-load .env
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

# Import core modules
sys.path.insert(0, str(Path(__file__).parent))
from autonomous_system import OpenClawClient, Brain, Memory

# ===================== Config =====================
BRAIN_API_KEY = os.environ.get("BRAIN_API_KEY", "")
BRAIN_BASE_URL = os.environ.get("BRAIN_BASE_URL", "https://api.deepseek.com/v1")
BRAIN_MODEL = os.environ.get("BRAIN_MODEL", "deepseek-chat")
OPENCLAW_GATEWAY_URL = os.environ.get("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
SESSION_KEY = "autonomous-money-maker"
MEMORY_FILE = str(Path(__file__).parent / "system_memory.json")

AGENTS = ["main", "brain", "content-agent", "research-agent", "dev-agent", "bd-agent"]

STATIC_DIR = Path(__file__).parent / "static"

# ===================== Global State =====================
state_lock = threading.Lock()
system_running = False
loop_count = 0
event_queue: queue.Queue = queue.Queue()
brain_log: list = []
claw_log: list = []
chat_history: list = []
pending_question: str = ""
answer_event = threading.Event()
user_answer: str = ""


# ===================== FastAPI App =====================

app = FastAPI(title="Claw-brain Web Console")

# Serve static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# ===================== API Endpoints =====================

@app.post("/api/start")
async def api_start(req: Request):
    global system_running, loop_count, brain_log, claw_log, chat_history

    with state_lock:
        if system_running:
            return JSONResponse({"error": "系统已在运行中"})

    try:
        body = await req.json()
    except Exception as e:
        return JSONResponse({"error": f"请求格式错误: {e}"})

    goal = body.get("goal", "").strip()
    agent = body.get("agent", "main")
    max_loops = int(body.get("max_loops", 10))
    interval = int(body.get("loop_interval", 15))

    if not goal:
        return JSONResponse({"error": "请输入目标"})

    if not BRAIN_API_KEY:
        return JSONResponse({"error": "未设置 BRAIN_API_KEY 环境变量。请在 .env 文件或系统环境中配置。"})

    # Health check
    try:
        import urllib.request
        urllib.request.urlopen(f"{OPENCLAW_GATEWAY_URL}/health", timeout=5)
    except Exception as e:
        return JSONResponse({"error": f"OpenClaw Gateway 离线: {e}。请先运行 openclaw gateway run --force"})

    with state_lock:
        system_running = True
        loop_count = 0
        brain_log = []
        claw_log = []
        chat_history = []
    while not event_queue.empty():
        event_queue.get_nowait()

    t = threading.Thread(
        target=run_loop,
        args=(goal, agent, max_loops, interval),
        daemon=True
    )
    t.start()
    return JSONResponse({"ok": True})


@app.post("/api/stop")
async def api_stop():
    global system_running, pending_question
    with state_lock:
        system_running = False
        pending_question = ""
    return JSONResponse({"ok": True})


@app.post("/api/answer")
async def api_answer(req: Request):
    global user_answer, pending_question
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": "请求格式错误"})

    answer = body.get("answer", "").strip()
    if not answer:
        return JSONResponse({"error": "回复不能为空"})

    with state_lock:
        if not pending_question:
            return JSONResponse({"error": "当前没有需要回答的问题"})
        chat_history.append({"role": "usr", "text": answer})
        user_answer = answer
        pending_question = ""

    answer_event.set()
    return JSONResponse({"ok": True, "messages": chat_history[-20:]})


@app.get("/api/state")
async def api_state():
    global loop_count, system_running

    # Drain event queue
    while not event_queue.empty():
        try:
            event_queue.get_nowait()
        except Exception:
            break

    with state_lock:
        lc = loop_count
        sr = system_running
        pq = pending_question
        ch = list(chat_history[-20:])
        bl = list(brain_log)
        cl = list(claw_log)

    status = f"运行中 - Round {lc}" if sr else "已停止"

    return JSONResponse({
        "status": status,
        "round": lc,
        "running": sr,
        "claw_count": len(cl),
        "has_question": bool(pq),
        "chat_messages": ch,
        "brain_log": bl,
        "claw_log": cl,
    })


# ===================== Background Loop =====================

def run_loop(goal: str, agent: str, max_loops: int, interval: int):
    global system_running, loop_count, pending_question, user_answer
    import traceback

    mem = Memory(MEMORY_FILE)
    brain = Brain(BRAIN_API_KEY, BRAIN_BASE_URL, BRAIN_MODEL)

    try:
        claw = OpenClawClient(agent, SESSION_KEY, OPENCLAW_GATEWAY_URL)
    except Exception as e:
        print(f"[LOOP] OpenClaw 初始化失败: {e}")
        traceback.print_exc()
        _append_brain_log({
            "round": 0, "thought": f"OpenClaw 初始化失败: {e}",
            "observation": "system_error", "action": "",
            "update_memory": "", "status": "blocked",
        })
        with state_lock:
            system_running = False
        return

    last_fb = "系统刚刚启动，请开始第一步行动。"

    while True:
        with state_lock:
            if not system_running:
                break

        loop_count += 1
        if 0 < max_loops < loop_count:
            break

        event_queue.put(("status", f"Round {loop_count} - Brain 思考中..."))

        try:
            ctx = {
                "goal": goal,
                "memory_summary": mem.get_summary(),
                "last_feedback": last_fb,
                "history_summary": mem.get_summary(3),
                "loop_count": loop_count,
            }
            dec = brain.think(ctx)
        except Exception as e:
            print(f"[LOOP] Brain 错误: {e}")
            traceback.print_exc()
            _append_brain_log({
                "round": loop_count, "thought": f"Brain 调用失败: {e}",
                "observation": "api_error", "action": "",
                "update_memory": "", "status": "blocked",
            })
            break

        thought = dec.get("thought", "")
        observation = dec.get("observation", "")
        action = dec.get("action_to_openclaw", "").strip()
        upd = dec.get("update_memory", "")
        st = dec.get("status", "continue")

        _append_brain_log({
            "round": loop_count, "thought": thought,
            "observation": observation, "action": action,
            "update_memory": upd, "status": st,
        })

        if st == "need_input":
            question = dec.get("question_for_user", thought) or "系统需要你的输入"
            with state_lock:
                pending_question = question
            _append_chat({"role": "sys", "text": question})
            event_queue.put(("status", f"Round {loop_count} - 等待用户输入..."))

            answer_event.clear()
            answer_event.wait(timeout=300)

            with state_lock:
                pending_question = ""
            if not user_answer:
                last_fb = "用户超时未回复"
            else:
                last_fb = f"用户回复: {user_answer}"
                user_answer = ""
            _wait(interval)
            continue

        if st in ("blocked", "pause"):
            break

        if upd:
            mem.update_strategy(upd)
        if st == "milestone" and upd:
            mem.add_milestone(upd)

        if not action:
            last_fb = "大脑未给出指令"
            _wait(interval)
            continue

        event_queue.put(("status", f"Round {loop_count} - 小龙虾执行中..."))

        try:
            result = claw.execute(action)
        except Exception as e:
            traceback.print_exc()
            result = {"success": False, "content": f"执行异常: {e}"}

        _append_claw_log({
            "round": loop_count, "instruction": action,
            "result": result["content"], "success": result["success"],
        })

        mem.add_action(action, result["content"], result["success"])
        last_fb = result["content"] if result["success"] else f"失败: {result['content']}"

        _wait(interval)

    with state_lock:
        system_running = False
    event_queue.put(("status", "已停止"))


def _append_brain_log(entry):
    with state_lock:
        brain_log.append(entry)
        if len(brain_log) > 100:
            brain_log[:] = brain_log[-100:]


def _append_claw_log(entry):
    with state_lock:
        claw_log.append(entry)
        if len(claw_log) > 100:
            claw_log[:] = claw_log[-100:]


def _append_chat(msg):
    with state_lock:
        chat_history.append(msg)
        if len(chat_history) > 50:
            chat_history[:] = chat_history[-50:]


def _wait(seconds):
    for _ in range(seconds):
        with state_lock:
            if not system_running:
                return
        time.sleep(1)


# ===================== Entry =====================

if __name__ == "__main__":
    print()
    print("  Claw-brain Web Console")
    print("  http://127.0.0.1:7860")
    print()
    uvicorn.run(app, host="127.0.0.1", port=7860, log_level="warning")
