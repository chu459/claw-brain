"""
自主赚钱系统 - Web 控制台
===========================
FastAPI 后端 + 自定义 HTML 前端
参考 OpenClaw Control UI 深色主题风格

启动: python web_console.py
访问: http://127.0.0.1:7860
"""

import json
import asyncio
import threading
import time
import queue
import os
import sys
import socket
import shutil
import subprocess
import uuid
import urllib.request
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# 自动加载 .env 文件
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

# 确保能导入核心模块
sys.path.insert(0, str(Path(__file__).parent))
from codex_adapter import codex_available
from gateway_runtime import ensure_gateway as ensure_openclaw_gateway
from autonomous_system import OpenClawClient, Brain, Memory, GOAL_TEMPLATES, OUTPUT_DIR, OutputManager
from credential_store import (
    list_accounts, get_account, add_account, update_account,
    delete_account, get_credential_value, ACCOUNT_TEMPLATES, PRESET_FIELDS,
)
from core import SystemState, RunLoopConfig, run_loop as _core_run_loop, SessionManager
from checkpoint_supervisor import review_checkpoints
from message_center import latest_message_payload, open_latest_message_center
from task_contract import read_task_contract
from task_manager import get_task_manager, TaskManager

# ===================== 配置 =====================
BRAIN_API_KEY = os.environ.get("BRAIN_API_KEY", "") or (get_credential_value("DeepSeek", "api_key") or "")
BRAIN_BASE_URL = os.environ.get("BRAIN_BASE_URL", "https://api.deepseek.com/v1") or (get_credential_value("DeepSeek", "base_url") or "https://api.deepseek.com/v1")
BRAIN_MODEL = os.environ.get("BRAIN_MODEL", "deepseek-chat") or (get_credential_value("DeepSeek", "model") or "deepseek-chat")
OPENCLAW_GATEWAY_URL = os.environ.get("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
SESSION_KEY = "autonomous-money-maker"
MEMORY_FILE = str(Path(__file__).parent / "system_memory.json")

# 产物管理器
output_manager = OutputManager(OUTPUT_DIR)

AGENTS = ["main", "brain", "content-agent", "research-agent", "dev-agent", "bd-agent"]

# ===================== 闲置检测 =====================
LAST_ACTIVITY_TIME = time.time()  # 每次 API 调用自动更新
IDLE_TIMEOUT_SECONDS = int(os.environ.get("CLAWBRAIN_IDLE_TIMEOUT", "1800"))  # 默认 30 分钟

# ===================== 进程隔离架构 =====================
# Web Server 和 Worker 完全分离：
#   - Web Server：纯 async FastAPI，只读快照文件，不做任何重计算
#   - Worker：独立 Python 进程，运行 run_loop，每 3 秒写入快照
#   - 通信：pipe/snapshot.json（Worker→Server）+ pipe/command.json（Server→Worker）

PIPE_DIR = Path(__file__).parent / "pipe"
PIPE_DIR.mkdir(exist_ok=True)
SESSIONS_DIR = Path(__file__).parent / "sessions"
SNAPSHOT_FILE = PIPE_DIR / "snapshot.json"
COMMAND_FILE = PIPE_DIR / "command.json"

# Worker 子进程引用（这是唯一保留的全局状态——进程本身）
_worker_process: subprocess.Popen | None = None
_worker_log_file = None  # Worker stdout 日志文件句柄，必须保持引用防止 GC 关闭 fd

# 快照缓存（减少文件读取次数）
_snapshot_cache = {"data": None, "ts": 0}
_SNAPSHOT_CACHE_TTL = 0.8  # 0.8秒缓存

# 没有运行任务时暂存的用户消息
pending_user_feedbacks: list[dict] = []

# 会话管理器
session_mgr = SessionManager()


def _read_snapshot() -> dict:
    """读取 Worker 快照（带缓存）"""
    now = time.time()
    if now - _snapshot_cache["ts"] < _SNAPSHOT_CACHE_TTL and _snapshot_cache["data"]:
        return _snapshot_cache["data"]
    if not SNAPSHOT_FILE.exists():
        return {}
    try:
        data = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
        _snapshot_cache["data"] = data
        _snapshot_cache["ts"] = now
        return data
    except Exception:
        return _snapshot_cache["data"] or {}


def _send_command(cmd: dict):
    """发送命令给 Worker（原子写入）"""
    tmp = COMMAND_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cmd, ensure_ascii=False), encoding="utf-8")
    tmp.replace(COMMAND_FILE)


def _latest_checkpoint_payload(limit: int = 5) -> dict:
    data_dir = Path(__file__).parent / "data" / "checkpoints"
    if not data_dir.exists():
        rows = []
        session = ""
    else:
        files = sorted(data_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        path = files[0] if files else None
        session = path.stem if path else ""
        rows = []
        if path and path.exists():
            try:
                for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
                    if line.strip():
                        rows.append(json.loads(line))
            except Exception:
                rows = []
    return {
        "session": session,
        "items": rows,
        "review": review_checkpoints(rows).to_dict(),
    }


def _latest_task_contract_payload(session: str = "") -> dict:
    data_dir = Path(__file__).parent / "data" / "task_contracts"
    data = read_task_contract(data_dir, session_id=session)
    return data or {}


def _pid_exists(pid: int) -> bool:
    """跨平台检测 PID 是否存在（OS 级别）"""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        # Windows: 用 OpenProcess 检测
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if handle:
                # 检查进程是否还在跑（不是已退出但句柄未释放）
                exit_code = ctypes.c_ulong()
                ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                ctypes.windll.kernel32.CloseHandle(handle)
                # 259 = STILL_ACTIVE
                return exit_code.value == 259
            return False
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _is_worker_alive() -> bool:
    """检查 Worker 是否真的在工作。
    
    判断标准（多重）：
    1. 优先：内存中的 _worker_process 存活
    2. 兜底：快照里的 PID 在 OS 中存活，且 last_update 在 30 秒内
    
    后者解决：Web Server 重启后 _worker_process=None，但旧 Worker 进程还在的情况
    """
    global _worker_process
    # 内存变量在 → 这个进程是我们启动的，最可信
    if _worker_process is not None:
        return _worker_process.poll() is None

    # 内存变量没有 → 检查快照里有没有遗留 Worker
    snap = _read_snapshot()
    if not snap:
        return False
    pid = snap.get("pid", 0)
    last_update = snap.get("last_update", 0)
    # PID 在 OS 中存活 且 心跳在 30 秒内 = 真在跑
    if _pid_exists(pid) and (time.time() - last_update) < 30:
        return True
    return False


def _kill_orphan_worker() -> bool:
    """杀掉遗留的 Worker 进程（Web Server 重启后可能存在）。返回是否真的杀了"""
    snap = _read_snapshot()
    if not snap:
        return False
    pid = snap.get("pid", 0)
    if pid and _pid_exists(pid):
        try:
            if os.name == "nt":
                import ctypes
                PROCESS_TERMINATE = 0x0001
                handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
                if handle:
                    ctypes.windll.kernel32.TerminateProcess(handle, 1)
                    ctypes.windll.kernel32.CloseHandle(handle)
                    print(f"[CLEANUP] 杀掉遗留 Worker PID={pid}")
                    return True
            else:
                os.kill(pid, 9)
                print(f"[CLEANUP] 杀掉遗留 Worker PID={pid}")
                return True
        except Exception as e:
            print(f"[CLEANUP] 杀掉遗留 Worker 失败: {e}")
    return False


def _atomic_write_snapshot(data: dict):
    """原子写入快照文件"""
    tmp = SNAPSHOT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SNAPSHOT_FILE)


def _read_last_session_from_disk() -> dict | None:
    """从 Sessions 目录读取最近一个有日志的 Session"""
    sessions_dir = Path(__file__).parent / "sessions"
    if not sessions_dir.exists():
        return None
    # 读 index 获取 session 顺序
    index_file = sessions_dir / "index.json"
    if not index_file.exists():
        return None
    try:
        idx = json.loads(index_file.read_text(encoding="utf-8"))
        if not idx:
            return None
        # 倒序找最近一个有实际日志的
        for entry in reversed(idx):
            sess_file = sessions_dir / f"{entry['id']}.json"
            if sess_file.exists():
                try:
                    d = json.loads(sess_file.read_text(encoding="utf-8"))
                    if d.get("brain_log") or d.get("claw_log"):
                        d["session_id"] = entry["id"]
                        d["loop_count"] = entry.get("loop_count", 0)
                        return d
                except Exception:
                    pass
    except Exception:
        pass
    return None


def _atomic_write_snapshot(data: dict):
    """原子写入快照文件"""
    tmp = SNAPSHOT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SNAPSHOT_FILE)


def _cleanup_dead_worker():
    """清理已退出的 Worker 进程"""
    global _worker_process
    if _worker_process and _worker_process.poll() is not None:
        # Worker 已退出
        exit_code = _worker_process.returncode
        stdout = ""
        try:
            stdout = _worker_process.stdout.read()[-2000:] if _worker_process.stdout else ""
        except Exception:
            pass
        if exit_code != 0:
            print(f"[WORKER] Worker 进程退出 code={exit_code}")
            if stdout:
                print(f"[WORKER] stdout: {stdout[:500]}")
        _worker_process = None


# ===================== HTML =====================

CUSTOM_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0e1015;--bg-accent:#13151b;--bg-elevated:#191c24;--bg-hover:#1f2330;--card:#161920;--text:#d4d4d8;--text-strong:#f4f4f5;--muted:#838387;--border:#1e2028;--border-strong:#2e3040;--accent:#ff5c5c;--accent-hover:#ff7070;--accent-subtle:#ff5c5c1a;--ok:#22c55e;--danger:#ef4444;--warn:#f59e0b;--info:#3b82f6;--purple:#534AB7;--purple-light:#7F77DD;--teal:#0F6E56;--teal-light:#1D9E75;--radius-sm:6px;--radius-md:10px;--radius-lg:14px;--mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Consolas,monospace;--font:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
body{background:var(--bg);color:var(--text);font-family:var(--font);font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased;overflow:hidden}
#app{display:grid;grid-template-columns:340px 1fr;height:100vh;overflow:hidden}

/* === 左侧面板 === */
#left{background:var(--bg);border-right:1px solid var(--border);padding:24px 20px;display:flex;flex-direction:column;gap:18px;overflow-y:auto;position:relative}
.logo-row{display:flex;align-items:center;gap:12px;padding-bottom:16px;border-bottom:1px solid var(--border)}
.logo{width:38px;height:38px;background:linear-gradient(135deg,#ff5c5c,#ff8c5c);border-radius:10px;display:flex;align-items:center;justify-content:center;color:#fff;font-size:20px;font-weight:800;flex-shrink:0;font-family:var(--font)}
.logo-text h1{color:var(--text-strong);font-size:17px;font-weight:700;line-height:1.2}
.logo-text p{color:var(--accent);font-size:13px;margin-top:3px;font-weight:600;letter-spacing:.5px}
.dev-tag{color:var(--muted);font-size:10px;margin-top:2px;display:block}

/* === 卡片 === */
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-lg);padding:16px}
.card-label{color:var(--muted);font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;margin-bottom:12px}

/* === 状态行 === */
.srow{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.srow:last-child{margin-bottom:0}
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.dot-g{background:var(--ok);box-shadow:0 0 8px rgba(34,197,94,.4);animation:pulse 2s ease-in-out infinite}
.dot-r{background:var(--danger);box-shadow:0 0 8px rgba(239,68,68,.4)}
.dot-y{background:var(--warn);box-shadow:0 0 8px rgba(245,158,11,.4);animation:pulse 2s ease-in-out infinite}
.dot-x{background:#5F5E5A}
.srow .label{color:var(--text);font-size:13px;font-weight:500}
.srow .val{color:var(--muted);font-size:12px;margin-left:auto;font-family:var(--mono)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}

/* === 输入 === */
textarea.inp,select.inp,input[type=number].inp{width:100%;background:var(--bg);border:1px solid var(--border-strong);border-radius:var(--radius-sm);padding:10px 12px;color:var(--text);font-size:13px;outline:none;transition:border-color .2s;font-family:var(--font)}
textarea.inp{resize:vertical;min-height:80px;line-height:1.6}
select.inp{appearance:none;cursor:pointer;background-image:url("data:image/svg+xml,%3Csvg width='12' height='8' viewBox='0 0 12 8' fill='none' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M1 1.5L6 6.5L11 1.5' stroke='%23838387' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 12px center;padding-right:32px}
textarea.inp:focus,select.inp:focus,input.inp:focus{border-color:var(--accent)}
.row{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.row:last-child{margin-bottom:0}
.row .lbl{color:var(--muted);font-size:12px;white-space:nowrap;min-width:50px}
.num{width:80px}

/* === 按钮 === */
.btn{width:100%;padding:12px;border:none;border-radius:var(--radius-md);font-size:14px;font-weight:700;cursor:pointer;transition:all .2s;letter-spacing:.02em;font-family:var(--font)}
.btn-go{background:linear-gradient(135deg,#ff5c5c,#ff4040);color:#fff;box-shadow:0 4px 16px rgba(255,92,92,.3)}
.btn-go:hover{box-shadow:0 6px 24px rgba(255,92,92,.4);transform:translateY(-1px)}
.btn-stop{background:var(--bg-hover);color:var(--accent);border:1px solid var(--accent-subtle)}
.btn-stop:hover{background:var(--accent-subtle)}
.mode-btn{background:var(--bg-elevated);border:1px solid var(--border);color:var(--text)!important;padding:10px 12px;font-size:13px;font-weight:600;width:auto}
.mode-btn:hover{border-color:var(--accent)!important;color:var(--accent)!important}
.mode-active{border-color:var(--accent)!important;color:var(--accent)!important;background:rgba(255,92,92,0.1)!important}

/* === 快速目标 === */
.qbtn{background:var(--bg-hover);border:1px solid var(--border-strong);border-radius:8px;padding:8px 12px;color:var(--text);font-size:12px;cursor:pointer;text-align:left;transition:all .2s;font-family:var(--font);width:100%}
.qbtn:hover{border-color:rgba(255,92,92,.3);background:var(--bg-elevated)}

/* === 账号管理 === */
.cred-list{display:flex;flex-direction:column;gap:6px;max-height:200px;overflow-y:auto}
.cred-item{display:flex;align-items:center;justify-content:space-between;padding:8px 10px;background:var(--bg-hover);border-radius:8px;cursor:pointer;transition:all .15s;border:1px solid transparent}
.cred-item:hover{border-color:var(--border-strong);background:var(--bg-elevated)}
.cred-info{display:flex;align-items:center;gap:8px;min-width:0}
.cred-icon{font-size:16px;flex-shrink:0}
.cred-name{font-size:12px;color:var(--text-strong);font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cred-cat{font-size:10px;color:var(--muted)}
.cred-actions{display:flex;gap:4px;flex-shrink:0}
.cred-btn{background:0 0;border:1px solid var(--border);border-radius:6px;color:var(--muted);font-size:11px;cursor:pointer;padding:2px 8px;transition:all .15s;font-family:var(--font)}
.cred-btn:hover{color:var(--text);border-color:var(--border-strong)}
.cred-btn.del:hover{color:var(--danger);border-color:var(--danger)}
.cred-empty{color:var(--muted);font-size:11px;text-align:center;padding:12px 0}
.cred-add-btn{width:100%;padding:8px;border:1px dashed var(--border-strong);border-radius:8px;background:0 0;color:var(--muted);font-size:12px;cursor:pointer;transition:all .15s;font-family:var(--font);margin-top:6px}
.cred-add-btn:hover{border-color:var(--accent);color:var(--accent)}

/* === 凭据弹窗 === */
.cred-modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:1000;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)}
.cred-modal{background:var(--bg-accent);border:1px solid var(--border-strong);border-radius:var(--radius-lg);padding:24px;width:min(460px,90vw);max-height:80vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,.4)}
.cred-modal h3{color:var(--text-strong);font-size:16px;margin:0 0 16px;font-weight:700}
.cred-field{margin-bottom:12px}
.cred-field label{display:block;color:var(--muted);font-size:11px;font-weight:600;margin-bottom:4px;letter-spacing:.05em}
.cred-field input,.cred-field select{width:100%;padding:8px 12px;background:var(--bg-hover);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px;font-family:var(--font);box-sizing:border-box;outline:0;transition:border-color .15s}
.cred-field input:focus,.cred-field select:focus{border-color:var(--accent)}
.cred-field select{cursor:pointer;appearance:auto}
.cred-modal-btns{display:flex;gap:8px;justify-content:flex-end;margin-top:20px}
.cred-modal-btns button{padding:8px 20px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;transition:all .15s;font-family:var(--font);border:none}
.cred-btn-cancel{background:var(--bg-hover);color:var(--text)}
.cred-btn-cancel:hover{background:var(--bg-elevated)}
.cred-btn-save{background:var(--accent);color:#fff}
.cred-btn-save:hover{background:var(--accent-hover)}
.cred-dynamic-fields{display:flex;flex-direction:column;gap:8px}
.cred-field-row{display:flex;gap:8px;align-items:end}
.cred-field-row input{flex:1}
.cred-field-row button{background:var(--bg-hover);border:1px solid var(--border);border-radius:8px;color:var(--danger);font-size:11px;cursor:pointer;padding:8px 10px;flex-shrink:0}
.cred-field-row button:hover{background:var(--accent-subtle)}

/* === 右侧面板 === */
#right{background:var(--bg);padding:24px;display:flex;flex-direction:column;gap:18px;overflow:hidden}
.tabs{display:flex;gap:2px;background:var(--bg);padding:4px;border-radius:var(--radius-md);border:1px solid var(--border)}
.tab{flex:1;padding:8px 16px;border:none;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;color:var(--muted);background:0 0;transition:all .2s;font-family:var(--font)}
.tab.on{background:var(--bg-hover);color:var(--text-strong)}
.tab:hover:not(.on){color:var(--text)}

/* === 看板 === */
.board{flex:1;background:var(--card);border:1px solid var(--border);border-radius:var(--radius-lg);overflow:hidden;display:flex;flex-direction:column;min-height:0}
.board.hide{display:none}
.bhead{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--border);background:var(--bg-accent);flex-shrink:0}
.bhead-l{display:flex;align-items:center;gap:8px}
.bicon{width:24px;height:24px;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:13px}
.bicon-brain{background:rgba(83,74,183,.12);color:var(--purple-light)}
.bicon-claw{background:rgba(15,110,86,.12);color:var(--teal-light)}
.bicon-mem{background:rgba(245,158,11,.12);color:var(--warn)}
.bname{color:var(--text-strong);font-size:14px;font-weight:600}
.badge{background:var(--accent-subtle);color:var(--accent);font-size:10px;font-weight:700;padding:3px 8px;border-radius:20px;letter-spacing:.05em}
.badge-ok{background:rgba(15,110,86,.12);color:var(--teal-light)}
.bbody{flex:1;overflow-y:auto;padding:16px 18px;min-height:0}
.bfoot{display:flex;gap:16px;padding:10px 14px;background:var(--bg-accent);border-top:1px solid var(--border);flex-shrink:0}
.bf-item{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:11px}
.bf-item strong{color:var(--text);font-weight:600}

/* === 条目 === */
.entry{margin-bottom:16px;padding-left:14px;border-left:2px solid rgba(83,74,183,.25);animation:fadeIn .4s ease-out}
.entry.claw{border-left-color:rgba(15,110,86,.25)}
@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.entry-round{color:var(--muted);font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px;font-family:var(--mono)}
.entry-label{font-size:11px;font-weight:600;margin-bottom:4px}
.entry-label.brain{color:var(--purple-light)}
.entry-label.claw{color:var(--teal-light)}
.entry-text{color:var(--text);font-size:13px;line-height:1.6;margin-bottom:4px}
.action-box{background:var(--bg);border:1px solid var(--border-strong);border-radius:8px;padding:10px 12px;margin-top:8px}
.action-label{color:var(--accent);font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px}
.action-text{color:var(--text-strong);font-size:13px;font-family:var(--mono);line-height:1.5}
.result-box{margin-top:8px;padding:8px 10px;border-radius:6px;font-size:12px;line-height:1.5}
.result-ok{background:rgba(34,197,94,.06);color:var(--ok);border-left:2px solid rgba(34,197,94,.25)}
.result-fail{background:rgba(239,68,68,.06);color:var(--danger);border-left:2px solid rgba(239,68,68,.25)}

/* === 质量评审 === */
.quality-review{border-left:3px solid var(--warn)!important;padding-left:12px;margin-left:4px}
.quality-score{background:rgba(234,179,8,.08);border:1px solid rgba(234,179,8,.2);border-radius:8px;padding:8px 12px;margin-top:8px;font-size:12px;line-height:1.6;color:var(--warn)}

/* === 记忆卡片 === */
.mem-card{background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-md);padding:12px 14px;margin-bottom:10px}
.mem-title{color:var(--text-strong);font-size:12px;font-weight:600;margin-bottom:6px;display:flex;align-items:center;gap:6px}
.mem-body{color:var(--muted);font-size:12px;line-height:1.5}

/* === 空状态 === */
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:60px 20px;color:#5F5E5A;text-align:center}
.empty-icon{font-size:48px;margin-bottom:16px;opacity:.3}
.empty-text{font-size:14px;margin-bottom:6px}
.empty-hint{font-size:12px;color:#3e4050}

/* === 滚动条 === */
::-webkit-scrollbar{width:6px}
::-webkit-scrollbar-track{background:0 0}
::-webkit-scrollbar-thumb{background:var(--border-strong);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:#3e4050}

/* === 加载动画 === */
.spinner{display:inline-block;width:14px;height:14px;border:2px solid var(--border-strong);border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite;vertical-align:middle;margin-right:6px}
@keyframes spin{to{transform:rotate(360deg)}}

/* === 浮动对话框 === */
#chat-fab{position:fixed;bottom:28px;right:28px;z-index:1000;cursor:pointer}
#chat-fab-btn{width:56px;height:56px;border-radius:50%;border:none;background:linear-gradient(135deg,#ff5c5c,#ff4040);color:#fff;font-size:22px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .25s;box-shadow:0 4px 20px rgba(255,92,92,.35)}
#chat-fab-btn:hover{transform:scale(1.08);box-shadow:0 6px 28px rgba(255,92,92,.45)}
#chat-fab-badge{position:absolute;top:-2px;right:-2px;min-width:20px;height:20px;border-radius:10px;background:var(--warn);color:#000;font-size:11px;font-weight:700;display:none;align-items:center;justify-content:center;padding:0 6px;animation:fabPop .3s ease-out}
#chat-fab-badge.show{display:flex}
@keyframes fabPop{from{transform:scale(0)}to{transform:scale(1)}}

#chat-panel{position:fixed;bottom:96px;right:28px;width:380px;max-height:520px;border-radius:var(--radius-lg);background:var(--card);border:1px solid var(--border-strong);z-index:1000;display:none;flex-direction:column;overflow:hidden;animation:chatSlide .25s ease-out}
#chat-panel.open{display:flex}
@keyframes chatSlide{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}

.chat-head{display:flex;align-items:center;gap:10px;padding:14px 16px;border-bottom:1px solid var(--border);background:var(--bg-accent);flex-shrink:0}
.chat-head-icon{width:32px;height:32px;border-radius:8px;background:rgba(83,74,183,.12);display:flex;align-items:center;justify-content:center;font-size:16px;color:var(--purple-light)}
.chat-head-title{flex:1;color:var(--text-strong);font-size:13px;font-weight:600}
.chat-head-sub{color:var(--muted);font-size:11px}
.chat-close{width:28px;height:28px;border-radius:6px;border:none;background:0 0;color:var(--muted);font-size:16px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .2s}
.chat-close:hover{background:var(--bg-hover);color:var(--text)}

.chat-body{flex:1;overflow-y:auto;padding:14px 16px;min-height:200px;max-height:340px;display:flex;flex-direction:column;gap:10px}
.chat-msg{max-width:85%;padding:10px 14px;border-radius:12px;font-size:13px;line-height:1.6;animation:msgIn .3s ease-out}
@keyframes msgIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
.chat-msg.sys{align-self:flex-start;background:var(--bg-elevated);border:1px solid var(--border);color:var(--text);border-bottom-left-radius:4px}
.chat-msg.usr{align-self:flex-end;background:rgba(83,74,183,.15);border:1px solid rgba(83,74,183,.2);color:var(--text-strong);border-bottom-right-radius:4px}
.chat-msg-label{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px;color:var(--muted)}
.chat-msg.sys .chat-msg-label{color:var(--purple-light)}
.chat-msg.usr .chat-msg-label{color:var(--teal-light)}
.chat-empty{text-align:center;color:#3e4050;font-size:13px;padding:40px 20px}

.chat-foot{padding:12px 14px;border-top:1px solid var(--border);display:flex;gap:8px;flex-shrink:0;background:var(--bg-accent)}
.chat-input{flex:1;background:var(--bg);border:1px solid var(--border-strong);border-radius:8px;padding:10px 12px;color:var(--text);font-size:13px;outline:none;transition:border-color .2s;font-family:var(--font)}
.chat-input:focus{border-color:var(--purple-light)}
.chat-input::placeholder{color:#3e4050}
.chat-send{width:38px;height:38px;border-radius:8px;border:none;background:linear-gradient(135deg,var(--purple-light),var(--purple));color:#fff;font-size:16px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .2s;flex-shrink:0}
.chat-send:hover{transform:scale(1.05);box-shadow:0 2px 12px rgba(83,74,183,.4)}
.chat-send:disabled{opacity:.4;cursor:not-allowed;transform:none}

/* === 目标标签 === */
.goal-tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.goal-tag{display:inline-flex;align-items:center;gap:4px;padding:5px 10px;background:var(--bg-hover);border:1px solid var(--border);border-radius:6px;font-size:11px;color:var(--text);cursor:pointer;transition:all .15s;max-width:100%}
.goal-tag:hover{border-color:var(--accent);color:var(--accent)}
.goal-tag .x{font-size:13px;color:var(--muted);cursor:pointer;margin-left:2px;opacity:0;transition:opacity .15s}
.goal-tag:hover .x{opacity:1}
.goal-tag .x:hover{color:var(--danger)}
.goal-save{background:0 0;border:1px dashed var(--border-strong);border-radius:6px;color:var(--muted);font-size:11px;cursor:pointer;padding:5px 10px;transition:all .15s;font-family:var(--font);margin-top:10px}
.goal-save:hover{border-color:var(--accent);color:var(--accent)}

/* === 任务历史栏 === */
.sess-bar{display:flex;align-items:center;gap:6px;padding:6px 10px;background:var(--bg-accent);border:1px solid var(--border);border-radius:var(--radius-md);flex-shrink:0;min-height:40px}
.sess-bar::-webkit-scrollbar{height:4px}
.sess-bar::-webkit-scrollbar-thumb{background:var(--border-strong);border-radius:2px}
.sess-bar .sess-new-btn{order:-1;flex-shrink:0}
.sess-sessions{display:flex;align-items:center;gap:6px;overflow-x:auto;flex:1;min-width:0}
.sess-sessions::-webkit-scrollbar{height:4px}
.sess-sessions::-webkit-scrollbar-thumb{background:var(--border-strong);border-radius:2px}
.sess-chip{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;background:var(--bg-hover);border:1px solid var(--border);border-radius:20px;font-size:11px;color:var(--text);cursor:pointer;white-space:nowrap;transition:all .15s;flex-shrink:0}
.sess-chip:hover{border-color:var(--border-strong);background:var(--bg-elevated)}
.sess-chip.active{border-color:var(--accent);color:var(--accent);background:rgba(255,92,92,.08)}
.sess-chip .sess-time{color:var(--muted);font-size:10px}
.sess-chip .sess-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.sess-chip .sess-dot.stopped{background:var(--muted)}
.sess-chip .sess-dot.running{background:var(--ok);box-shadow:0 0 6px rgba(34,197,94,.4);animation:pulse 2s ease-in-out infinite}
.sess-chip .sess-dot.error{background:var(--danger)}
.sess-chip .sess-loops{color:var(--accent);font-size:9px;font-weight:600;background:rgba(255,92,92,.1);padding:1px 5px;border-radius:8px}
.sess-chip .sess-continue{display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border-radius:50%;background:var(--accent);color:#fff;font-size:9px;cursor:pointer;flex-shrink:0;margin-left:2px;transition:transform .15s}
.sess-chip .sess-continue:hover{transform:scale(1.2)}
.sess-chip .sess-del{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:50%;color:var(--muted);font-size:10px;cursor:pointer;flex-shrink:0;margin-left:1px;transition:all .15s;opacity:0}
.sess-chip:hover .sess-del{opacity:1}
.sess-chip .sess-del:hover{color:#fff;background:var(--danger)}
.task-chip{display:flex;align-items:center;gap:5px;padding:4px 10px;border-radius:var(--radius-sm);background:var(--bg-hover);cursor:pointer;font-size:11px;transition:all .15s;border:1px solid transparent;max-width:200px}
.task-chip:hover{background:var(--bg-elevated);border-color:var(--border-strong)}
.task-chip-active{border-color:var(--accent);background:var(--accent-subtle)}
.task-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0;background:var(--muted)}
.task-dot-run{background:var(--ok);box-shadow:0 0 6px rgba(34,197,94,.4);animation:pulse 2s ease-in-out infinite}
.task-chip-text{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text)}
.task-chip-round{font-size:10px;color:var(--muted);flex-shrink:0}
.sess-new-btn{padding:4px 10px;background:var(--accent);border:none;border-radius:20px;font-size:11px;font-weight:600;color:#fff;cursor:pointer;white-space:nowrap;flex-shrink:0;transition:all .15s;font-family:var(--font)}
.sess-new-btn:hover{background:var(--accent-hover);transform:translateY(-1px)}
.sess-empty{color:var(--muted);font-size:11px;padding:0 4px;white-space:nowrap}
"""


def _esc(t: str) -> str:
    if not t:
        return ""
    return (str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;").replace("\n", "<br>"))


def render_brain_entries(log_list=None):
    bl = log_list if log_list is not None else []
    if not bl:
        return '<div class="empty"><div class="empty-icon">&#x1F9E0;</div><div class="empty-text">AI 大脑等待启动</div><div class="empty-hint">设定目标后点击「启动系统」</div></div>'
    parts = []
    for e in reversed(bl[-50:]):
        ts = e.get("time", "")
        time_str = f' <span style="color:var(--muted);font-weight:400">({ts})</span>' if ts else ""
        is_quality = e.get("observation") == "quality_review"
        entry_cls = 'entry quality-review' if is_quality else 'entry'
        parts.append(f'<div class="{entry_cls}">')
        if is_quality:
            parts.append(f'<div class="entry-round" style="color:var(--warn)">Round {e.get("round","?")} &mdash; 质量评审{time_str}</div>')
        else:
            parts.append(f'<div class="entry-round">Round {e.get("round","?")} &mdash; Brain{time_str}</div>')
        if e.get("thought"):
            parts.append(f'<div class="entry-label brain">思考</div><div class="entry-text">{_esc(e["thought"])}</div>')
        if e.get("observation") and not is_quality:
            parts.append(f'<div class="entry-label brain">观察</div><div class="entry-text">{_esc(e["observation"])}</div>')
        if is_quality and e.get("action"):
            # 质量评审结果显示分数
            parts.append(f'<div class="quality-score">{_esc(e["action"])}</div>')
        elif e.get("action"):
            parts.append(f'<div class="action-box"><div class="action-label">发送给小龙虾</div><div class="action-text">{_esc(e["action"])}</div></div>')
        if e.get("update_memory"):
            parts.append(f'<div class="entry-text" style="color:var(--muted);font-style:italic">{_esc(e["update_memory"])}</div>')
        sc = {"continue":"var(--ok)","milestone":"var(--warn)","blocked":"var(--danger)","pause":"var(--info)","quality_check":"var(--warn)","need_input":"var(--info)"}
        c = sc.get(e.get("status",""),"var(--muted)")
        parts.append(f'<div style="margin-top:8px;font-size:11px;color:{c};font-weight:600">[{e.get("status","?").upper()}]</div>')
        parts.append('</div>')
    return "".join(parts)


def render_claw_entries(log_list=None):
    cl = log_list if log_list is not None else []
    if not cl:
        return '<div class="empty"><div class="empty-icon">&#x1F980;</div><div class="empty-text">小龙虾待命中</div><div class="empty-hint">系统启动后将显示执行记录</div></div>'
    parts = []
    for e in reversed(cl[-50:]):
        icon = "+" if e.get("success") else "x"
        cls = "ok" if e.get("success") else "fail"
        ts = e.get("time", "")
        time_str = f' <span style="color:var(--muted);font-weight:400">({ts})</span>' if ts else ""
        parts.append(f'<div class="entry claw">')
        parts.append(f'<div class="entry-round">Round {e.get("round","?")} &mdash; OpenClaw [{icon}]{time_str}</div>')
        if e.get("instruction"):
            parts.append(f'<div class="entry-label claw">指令</div><div class="entry-text">{_esc(e["instruction"])}</div>')
        if e.get("result"):
            parts.append(f'<div class="entry-label claw">结果</div><div class="result-box result-{cls}">{_esc(e["result"][:500])}</div>')
        parts.append('</div>')
    return "".join(parts)


# render_memory 缓存：避免每次 poll 都读文件
_memory_cache = {"html": "", "ts": 0}
_MEMORY_CACHE_TTL = 3.0  # 3秒缓存

def render_memory(mem_file=None):
    global _memory_cache
    now = time.time()
    if now - _memory_cache["ts"] < _MEMORY_CACHE_TTL:
        return _memory_cache["html"]
    try:
        mem = Memory(mem_file or MEMORY_FILE)
        d = mem.data
    except Exception:
        return '<div class="empty"><div class="empty-text">无法读取记忆</div></div>'
    parts = []
    # 策略
    parts.append(f'<div class="mem-card"><div class="mem-title">&#x1F3AF; 当前策略</div><div class="mem-body">{_esc(d.get("current_strategy","无"))}</div></div>')
    # 里程碑
    ms = d.get("milestones", [])
    if ms:
        parts.append(f'<div class="mem-card"><div class="mem-title">&#x1F31F; 里程碑 ({len(ms)})</div>')
        for m in ms[-5:]:
            parts.append(f'<div class="mem-body">{_esc(m.get("description",""))}</div>')
        parts.append('</div>')
    # 成功模式
    sp = d.get("successful_patterns", [])
    if sp:
        parts.append(f'<div class="mem-card"><div class="mem-title" style="color:var(--ok)">&#x2705; 成功模式</div><div class="mem-body">{"<br>".join(_esc(p) for p in sp[-5:])}</div></div>')
    # 失败
    fl = d.get("failed_attempts", [])
    if fl:
        parts.append(f'<div class="mem-card"><div class="mem-title" style="color:var(--danger)">&#x274C; 失败记录</div><div class="mem-body">{"<br>".join(_esc(f) for f in fl[-5:])}</div></div>')
    if not ms and not sp and not fl:
        parts.append('<div class="empty" style="padding:30px"><div class="empty-text">白板是空的</div></div>')
    html = "".join(parts)
    _memory_cache.update({"html": html, "ts": time.time()})
    return html


def render_outputs():
    """渲染产物列表（支持图片/媒体/网站内联展示）"""
    outputs = output_manager.get_recent_outputs(20)
    # 扫描孤儿文件（在 outputs/ 但未被 manifest 引用）
    orphan_files = output_manager.get_orphan_files()

    if not outputs and not orphan_files:
        return '<div class="empty"><div class="empty-icon">&#x1F4E6;</div><div class="empty-text">暂无产物</div><div class="empty-hint">系统执行任务后，产物会显示在这里</div></div>'

    parts = []

    # ---- 渲染 manifest 中的产物 ----
    for out in outputs:
        type_icons = {
            "code": "&#x1F4BB;", "document": "&#x1F4C4;",
            "image": "&#x1F5BC;", "media": "&#x1F3AC;",
            "data": "&#x1F4CA;", "tool": "&#x1F527;", "website": "&#x1F310;",
        }
        icon = type_icons.get(out["type"], "&#x1F4E6;")
        timestamp = out["timestamp"][:16].replace("T", " ")

        parts.append('<div class="entry">')
        parts.append(f'<div class="entry-round">{icon} {_esc(out["title"])} <span style="color:var(--muted);font-weight:400">({timestamp})</span></div>')
        parts.append(f'<div class="entry-label" style="color:var(--purple-light)">{out["type"].upper()}</div>')

        file_path = out.get("file_path", "")
        fp_name = Path(file_path).name if file_path else ""

        # 如果没有本地 file_path，尝试从 content 中提取 OpenClaw workspace 路径
        ws_name = ""
        ws_url = ""
        if not fp_name and out["type"] in ("image", "media"):
            import re
            content_raw = out.get("content", "")
            # 匹配 /workspace/xxx.png 或 /workspace/sub/xxx.png
            m = re.search(r'/workspace/([\w\-./]+\.(?:png|jpg|jpeg|webp|gif|mp4|webp|mov|mp3|wav))', content_raw, re.IGNORECASE)
            if m:
                ws_name = m.group(1)
                # 转换为 URL 路径（/workspace/sub/file.png → /openclaw-ws/sub/file.png）
                ws_url = "/openclaw-ws/" + ws_name
                # 检查文件是否实际存在
                ws_disk = OPENCLAW_WS / ws_name
                if not ws_disk.is_file():
                    ws_url = ""

        # 图片类型 - 内联展示
        if out["type"] == "image":
            img_src = f"/outputs/{fp_name}" if fp_name else (ws_url if ws_url else "")
            if img_src:
                parts.append(f'<div class="entry-img" style="margin:8px 0;cursor:pointer" onclick="showFullOutput(\'{out["id"]}\')">')
                parts.append(f'<img src="{img_src}" alt="{_esc(out["title"])}" style="max-width:100%;max-height:400px;border-radius:8px;border:1px solid var(--border)" loading="lazy">')
                parts.append('</div>')
                parts.append('<div style="font-size:11px;color:var(--muted);margin-top:4px">点击查看大图</div>')
            else:
                # 既没有本地文件也没有 workspace 文件，显示文本
                content = out.get("content", "")
                if content:
                    preview = content[:500] + ("..." if len(content) > 500 else "")
                    parts.append(f'<div class="entry-text" style="font-size:12px;white-space:pre-wrap">{_esc(preview)}</div>')
                parts.append('<div style="font-size:11px;color:var(--warn);margin-top:4px">原始文件未找到</div>')
        # 媒体类型 - 内联播放
        elif out["type"] == "media":
            media_src = f"/outputs/{fp_name}" if fp_name else (ws_url if ws_url else "")
            if media_src:
                if (fp_name or ws_name or "").endswith(('.mp4', '.webm', '.mov')):
                    parts.append(f'<div class="entry-media" style="margin:8px 0"><video src="{media_src}" controls style="max-width:100%;border-radius:8px;border:1px solid var(--border)"></video></div>')
                else:
                    parts.append(f'<div class="entry-media" style="margin:8px 0"><audio src="{media_src}" controls style="width:100%"></audio></div>')
            else:
                content = out.get("content", "")
                if content:
                    preview = content[:500] + ("..." if len(content) > 500 else "")
                    parts.append(f'<div class="entry-text" style="font-size:12px;white-space:pre-wrap">{_esc(preview)}</div>')
                parts.append('<div style="font-size:11px;color:var(--warn);margin-top:4px">原始文件未找到</div>')
        # 网站类型 - 展示预览或链接
        elif out["type"] == "website" and out.get("full_content"):
            parts.append(f'<div class="entry-text" style="font-size:12px;color:var(--muted);margin-bottom:4px">HTML 页面 ({len(out["full_content"])} 字符)</div>')
            parts.append(f'<button class="btn" style="margin-top:4px;font-size:11px;padding:6px 12px" onclick="showFullOutput(\'{out["id"]}\')">&#x1F310; 查看页面源码</button>')
        # 代码/文档类型 - 截断预览
        elif out["type"] in ["code", "document"]:
            content = out.get("content", "")
            preview = content[:300] + ("..." if len(content) > 300 else "")
            parts.append(f'<div class="entry-text" style="font-family:var(--mono);font-size:12px;white-space:pre-wrap">{_esc(preview)}</div>')
            if out.get("full_content"):
                parts.append(f'<button class="btn" style="margin-top:8px;font-size:11px;padding:6px 12px" onclick="showFullOutput(\'{out["id"]}\')">查看完整内容</button>')
        # 其他类型 - 显示文本描述
        else:
            content = out.get("content", "")
            if content:
                preview = content[:500] + ("..." if len(content) > 500 else "")
                parts.append(f'<div class="entry-text" style="font-size:12px;white-space:pre-wrap">{_esc(preview)}</div>')

        parts.append('</div>')

    # ---- 渲染孤儿文件（未被 manifest 引用的产物文件）----
    if orphan_files:
        parts.append('<div style="margin-top:16px;padding-top:12px;border-top:1px solid var(--border)">')
        parts.append('<div style="font-size:11px;color:var(--muted);margin-bottom:8px;font-weight:600">&#x1F4C1; 输出目录中的其他文件</div>')
        for f in orphan_files[:10]:
            ftime = f["mtime"][:16].replace("T", " ")
            fsize = f["size"] / 1024
            size_str = f"{fsize:.0f} KB" if fsize < 1024 else f"{fsize/1024:.1f} MB"
            icon = "&#x1F5BC;" if f["type"] == "image" else "&#x1F3AC;"
            if f["type"] == "image":
                parts.append(f'<div class="entry" style="padding:10px">')
                parts.append(f'<div style="font-size:12px;margin-bottom:6px">{icon} {_esc(f["name"])} <span style="color:var(--muted)">({size_str}, {ftime})</span></div>')
                parts.append(f'<img src="/outputs/{f["name"]}" style="max-width:100%;max-height:400px;border-radius:8px;border:1px solid var(--border);cursor:pointer" loading="lazy">')
                parts.append('</div>')
            else:
                parts.append(f'<div class="entry" style="padding:10px">')
                parts.append(f'<div style="font-size:12px">{icon} {_esc(f["name"])} <span style="color:var(--muted)">({size_str}, {ftime})</span></div>')
                if f["name"].endswith('.mp4') or f["name"].endswith('.webm'):
                    parts.append(f'<video src="/outputs/{f["name"]}" controls style="max-width:100%;border-radius:8px;margin-top:6px"></video>')
                else:
                    parts.append(f'<audio src="/outputs/{f["name"]}" controls style="width:100%;margin-top:6px"></audio>')
                parts.append('</div>')
        parts.append('</div>')

    return "".join(parts)


def render_tasks():
    """渲染任务列表"""
    tm = get_task_manager()
    tasks = tm.list_tasks(limit=10)
    
    if not tasks:
        return '<div class="task-empty">暂无任务，点击下方创建</div>'
    
    parts = []
    for task in tasks:
        status_class = task["status"]
        status_labels = {
            "pending": "待执行",
            "running": "执行中",
            "completed": "已完成",
            "failed": "失败"
        }
        status_label = status_labels.get(status_class, task["status"])
        
        # 任务图标
        mode_icons = {
            "money": "&#x1F4B0;",
            "dev": "&#x1F6E0;",
            "content": "&#x270F;",
            "research": "&#x1F50D;"
        }
        icon = mode_icons.get(task.get("mode", "money"), "&#x1F4CB;")
        
        parts.append(f'<div class="task-item" data-id="{task["id"]}" onclick="taskEdit(\'{task["id"]}\')">')
        parts.append(f'<div class="task-info">')
        parts.append(f'<span class="task-icon">{icon}</span>')
        parts.append(f'<div class="task-name">{_esc(task["name"])}</div>')
        parts.append(f'</div>')
        parts.append(f'<span class="task-status {status_class}">{status_label}</span>')
        parts.append(f'<div class="task-actions">')
        
        if task["status"] == "pending":
            parts.append(f'<button class="task-btn run" onclick="event.stopPropagation();taskRun(\'{task["id"]}\')">启动</button>')
        
        parts.append(f'<button class="task-btn del" onclick="event.stopPropagation();taskDelete(\'{task["id"]}\',\'{_esc(task["name"])}\')">删除</button>')
        parts.append(f'</div>')
        parts.append(f'</div>')
    
    return "".join(parts)


def build_html():
    agent_opts = "\n".join(f'<option value="{a}">{a}</option>' for a in AGENTS)

    # Script 部分单独构建，避免 f-string 与 JS 花括号冲突
    js_script = """<script>
let running=false,timer=null,pollOnce=false;
const $=id=>document.getElementById(id);

/* === 对话框 === */
let chatOpen=false;
function toggleChat(){
  chatOpen=!chatOpen;
  $('chat-panel').classList.toggle('open',chatOpen);
  $('chat-fab-btn').textContent=chatOpen?'\\u2715':'\\u1F4AC';
  if(chatOpen){var b=$('chat-body');if(b)b.scrollTop=b.scrollHeight;}
}

function renderChatMsgs(msgs){
  if(!msgs||!msgs.length){return '<div class="chat-empty">暂无对话</div>';}
  return msgs.map(function(m){
    var cls=m.role==='usr'?'usr':'sys';
    var lbl=m.role==='usr'?'你':'系统';
    var t=m.text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\\n/g,'<br>');
    return '<div class="chat-msg '+cls+'"><div class="chat-msg-label">'+lbl+'</div><div>'+t+'</div></div>';
  }).join('');
}

function sendChat(){
  var inp=$('chat-input'),txt=inp.value.trim();
  if(!txt)return;
  var msg=txt;inp.value='';
  // 先在本地显示用户消息
  var body=$('chat-body');
  var userDiv=document.createElement('div');
  userDiv.className='chat-msg usr';
  userDiv.innerHTML='<div class="chat-msg-label">你</div><div>'+msg.replace(/</g,'&lt;')+'</div>';
  body.appendChild(userDiv);
  body.scrollTop=body.scrollHeight;

  fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg,task_id:currentTaskId})})
  .then(function(r){return r.json()})
  .then(function(d){
    if(d.error && d.type!=='resumed'){
      var hint=document.createElement('div');
      hint.className='chat-msg sys';
      hint.style.color='var(--warn)';
      hint.innerHTML='<div class="chat-msg-label">系统</div><div>'+d.error+'</div>';
      body.appendChild(hint);
    } else if(d.type==='resumed'){
      var hint=document.createElement('div');
      hint.className='chat-msg sys';
      hint.innerHTML='<div class="chat-msg-label">系统</div><div>已恢复运行，正在处理你的指示...</div>';
      body.appendChild(hint);
      // 更新 UI 为运行中
      running=true;
      $('gobtn').className='btn btn-stop';
      $('gobtn').innerHTML='暂停';
      $('s-dot').className='dot dot-run';
      $('s-text').textContent='运行中（已恢复）';
      if(!timer){timer=setTimeout(poll,2000);}
    } else if(d.quick_action){
      var hint=document.createElement('div');
      hint.className='chat-msg sys';
      hint.innerHTML='<div class="chat-msg-label">系统</div><div>已执行: '+d.quick_action+'</div>';
      body.appendChild(hint);
    } else if(d.messages && d.messages.length>0){
      // 显示系统回复（最后一条）
      var last=d.messages[d.messages.length-1];
      if(last && last.role==='sys'){
        var hint=document.createElement('div');
        hint.className='chat-msg sys';
        hint.innerHTML='<div class="chat-msg-label">系统</div><div>'+last.text+'</div>';
        body.appendChild(hint);
      }
    }
    body.scrollTop=body.scrollHeight;
  }).catch(function(e){
    var hint=document.createElement('div');
    hint.className='chat-msg sys';
    hint.style.color='var(--warn)';
    hint.innerHTML='<div class="chat-msg-label">系统</div><div>发送失败</div>';
    body.appendChild(hint);
    body.scrollTop=body.scrollHeight;
  });
}

/* === Tab 切换 === */
function stab(t){
  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('on'));
  document.querySelectorAll('.board').forEach(p=>p.classList.add('hide'));
  $('t-'+t).classList.add('on');
  $('p-'+t).classList.remove('hide');
}

function setg(el){
  const map={
    'PPT':'用AI自动生成精美的商业PPT模板，研究Gumroad上同类产品的定价和卖点，制作3-5个高质量模板并上架售卖，目标是首周产生第一笔收入。',
    'Prompt':'研究Gumroad上最畅销的AI Prompt Pack类别和定价，针对1-2个高需求场景（如SEO写作、社交媒体内容），批量生成prompt并打包上架。',
    'Notion':'调研Gumroad上热销的Notion模板类型（个人CRM、项目管理、习惯追踪等），用AI生成3个高质量模板并上架售卖。',
    '\\u4ee3\\u5199':'在小红书、知乎、Medium等平台接代写文章订单，用AI批量生成高质量文章，按篇收费，目标单月收入$200+。',
    '\\u8bbe\\u8ba1':'用AI批量生成社交媒体素材包（Instagram模板、YouTube封面、Pinterest图），打包上传到Gumroad/Etsy售卖。'
  };
  const text=el.textContent;
  for(const[k,v]of Object.entries(map)){if(text.includes(k)){$('goal').value=v;return;}}
}

/* === 快速模块切换 === */
function setMode(mode){
  const goals={
    'money':'你是一个全自动创业者。用你的一切能力（搜索调研、商业判断、浏览器操作、代码开发、API调用）去赚钱。第一步：搜索当前市场，找到你能力范围内的真实赚钱机会。然后自主评估、验证、执行。不要等指令，不要只调研不行动，目标是在本次运行中产生真实的收入或可交付的变现产物。遇到需要账号权限时向我要。',
    'dev':'分析用户需求，设计并实现最优技术方案，产出可用的工具或应用。'
  };
  const labels={
    'money':'\\u8D5A\\u94B1\\u6A21\\u5F0F',
    'dev':'\\u5F00\\u53D1\\u6A21\\u5F0F'
  };
  if(goals[mode]){
    $('goal').value=goals[mode];
    document.querySelectorAll('.mode-btn').forEach(function(b){b.classList.remove('mode-active');});
    $('mode-'+mode).classList.add('mode-active');
  }
}

function showFullOutput(id){
  fetch('/api/output/'+id).then(function(r){return r.json()}).then(function(d){
    if(d.error){alert(d.error);return;}
    var modal=document.getElementById('output-modal-bg');
    if(!modal){
      var bg=document.createElement('div');
      bg.id='output-modal-bg';
      bg.className='cred-modal-bg';
      bg.onclick=function(e){if(e.target===bg)bg.style.display='none';};
      document.body.appendChild(bg);
      modal=bg;
    }
    var body='';
    // 提取 OpenClaw workspace 文件路径（/workspace/xxx.png）
    var wsMatch=d.content?d.content.match(/\\/workspace\\/([\\w\\-./]+\\.(?:png|jpg|jpeg|webp|gif|mp4|webp|mov|mp3|wav))/i):null;
    var wsSrc=wsMatch?'/openclaw-ws/'+wsMatch[1]:'';
    if(d.type==='image'&&d.file_path){
      // 图片类型 - 大图展示（本地文件）
      var fname=d.file_path.split('/').pop().split(String.fromCharCode(92)).pop();
      body='<div style="text-align:center"><img src="/outputs/'+fname+'" style="max-width:90vw;max-height:75vh;border-radius:8px" onclick="window.open(this.src)"></div>';
    }else if(d.type==='image'&&wsSrc){
      // 图片类型 - OpenClaw workspace 文件
      body='<div style="text-align:center"><img src="'+wsSrc+'" style="max-width:90vw;max-height:75vh;border-radius:8px" onclick="window.open(this.src)"></div>';
    }else if(d.type==='media'&&d.file_path){
      var fname=d.file_path.split('/').pop().split(String.fromCharCode(92)).pop();
      if(fname.match(/\\.(mp4|webm|mov)/)){
        body='<video src="/outputs/'+fname+'" controls style="max-width:90vw;max-height:75vh"></video>';
      }else{
        body='<audio src="/outputs/'+fname+'" controls style="width:90vw"></audio>';
      }
    }else if(d.type==='media'&&wsSrc){
      if(wsSrc.match(/\\.(mp4|webm|mov)/)){
        body='<video src="'+wsSrc+'" controls style="max-width:90vw;max-height:75vh"></video>';
      }else{
        body='<audio src="'+wsSrc+'" controls style="width:90vw"></audio>';
      }
    }else if(d.type==='website'&&d.content){
      // 网站类型 - HTML 预览
      body='<iframe srcdoc="'+d.content.replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;')+'" style="width:100%;height:60vh;border:none;border-radius:8px;background:white"></iframe>';
    }else{
      body='<pre style="white-space:pre-wrap;font-family:var(--mono);font-size:12px;line-height:1.5">'+d.content.replace(/</g,'&lt;').replace(/>/g,'&gt;')+'</pre>';
    }
    modal.innerHTML='<div class="cred-modal" style="max-width:900px;max-height:85vh;overflow-y:auto"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px"><h3>'+d.title+'</h3><span style="color:var(--muted);font-size:12px;cursor:pointer" onclick="this.closest(\\x27.cred-modal\\x27).parentElement.style.display=\\x27none\\x27">关闭</span></div>'+body+'</div>';
    modal.style.display='flex';
  });
}

function toggle(){
  if(!running){
    const goal=$('goal').value,agent=$('agent').value,
          maxl=parseInt($('maxl').value)||10,
          ival=parseInt($('ival').value)||15;
    if(!goal.trim()){alert('请输入目标');return;}

    $('gobtn').disabled=true;
    $('gobtn').innerHTML='<span class="spinner"></span>启动中...';

    // 不清空面板——继续任务时保留历史，新数据由 poll 刷新
    currentViewSid=null;

    // 构建请求体，如果有 continueFromSid 就带上
    var reqBody={goal:goal,agent:agent,max_loops:maxl,loop_interval:ival};
    if(continueFromSid){reqBody.continue_from=continueFromSid;}

    fetch('/api/start',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(reqBody)
    })
    .then(function(r){
      if(!r.ok) return r.json().then(function(d){throw new Error(d.error||'HTTP '+r.status)});
      return r.json();
    })
    .then(function(d){
      if(d.error){showError(d.error);$('gobtn').disabled=false;$('gobtn').innerHTML='启动新任务';return;}
      running=true;
      currentTaskId=d.task_id;
      // 启动成功后清除 continueFromSid（避免下次启动误续接）
      continueFromSid=null;
      $('gobtn').className='btn btn-stop';
      $('gobtn').innerHTML='停止当前任务';
      $('gobtn').disabled=false;
      $('s-dot').className='dot dot-g';
      $('s-text').textContent='系统运行中';
      $('c-agent').textContent=agent;
      if(!timer) timer=setTimeout(poll,2000);
      poll();
    })
    .catch(function(e){
      showError('启动失败: '+e.message);
      $('gobtn').disabled=false;$('gobtn').innerHTML='启动新任务';
    });
  } else {
    // 停止当前活跃任务
    fetch('/api/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_id:currentTaskId||''})}).then(function(){
      running=false;
      // 不改变按钮文字——保持"继续执行"或"启动新任务"由 poll() 决定
      $('s-dot').className='dot dot-x';
      clearTimeout(timer);timer=null;
      // 最后一次 poll 获取最终状态（包括设置 continueFromSid）
      pollOnce=true;
      poll();
    });
  }
}

/* === 会话管理 === */
var currentViewSid=null; // null = 当前活跃会话，字符串 = 查看历史会话
var continueFromSid=null; // 记住从哪个会话继续，启动时传给后端

function loadSessions(){
  fetch('/api/sessions').then(function(r){return r.json()}).then(function(d){
    var bar=$('sess-bar');
    // 结构：<button 新建> | <div 可滚动会话列表>
    var newBtn='<button class="sess-new-btn" onclick="newSession()">+ 新建</button>';
    if(!d.sessions||d.sessions.length===0){
      bar.innerHTML=newBtn+'<div class="sess-empty" style="margin-left:4px">暂无历史任务</div>';
      return;
    }
    var h='<div class="sess-sessions">';
    // 按时间倒序展示（index 已经是倒序的）
    for(var i=0;i<d.sessions.length;i++){
      var s=d.sessions[i];
      var short=s.goal.length>15?s.goal.slice(0,15)+'...':s.goal;
      var time=s.start_time?s.start_time.slice(11,16):'';
      var isActive=(s.status==='running');
      var dotCls=s.status==='running'?'running':s.status==='error'?'error':'stopped';
      var actCls=isActive&&!currentViewSid?' active':(currentViewSid===s.id?' active':'');
      var loops=s.loop_count||0;
      var isResumable=(!isActive && s.goal && !s.goal.startsWith('(新建'));
      var contBtn=isResumable?`<span class="sess-continue" onclick="event.stopPropagation();continueSession('${s.id}')" title="继续此任务">&#9654;</span>`:'';
      var delBtn=`<span class="sess-del" onclick="event.stopPropagation();deleteSession('${s.id}','${escH(short)}')" title="删除">&#10005;</span>`;
      h+=`<span class="sess-chip${actCls}" data-sid="${s.id}" onclick="viewSession('${s.id}')">`;      h+='<span class="sess-dot '+dotCls+'"></span>';
      h+='<span>'+escH(short)+'</span>';
      h+='<span class="sess-time">'+time+'</span>';
      if(loops>0) h+='<span class="sess-loops">R'+loops+'</span>';
      h+=contBtn;
      h+=delBtn;
      h+='</span>';
    }
    h+='</div>';
    // 保持滚动位置不变
    var scrollWrap=bar.querySelector('.sess-sessions');
    var savedScroll=scrollWrap?scrollWrap.scrollLeft:0;
    bar.innerHTML=newBtn+h;
    var newWrap=bar.querySelector('.sess-sessions');
    if(newWrap&&savedScroll) newWrap.scrollLeft=savedScroll;
  }).catch(function(e){console.error('loadSessions error:',e);});
}

function viewSession(sid){
  if(sid===currentViewSid){
    // 退出历史查看，恢复当前运行视图
    currentViewSid=null;
    poll();
    if(running&&!timer)timer=setTimeout(poll,2000);
    loadSessions();
    return;
  }
  currentViewSid=sid;
  // 停止 poll（查看历史）
  if(timer){clearTimeout(timer);timer=null;}
  fetch('/api/sessions/'+sid).then(function(r){return r.json()}).then(function(d){
    if(d.error){showError(d.error);return;}
    stab('brain');  // 切到 AI大脑 面板
    $('brain-body').innerHTML=d.brain;
    $('claw-body').innerHTML=d.claw;
    $('r-badge').textContent='Round '+d.loop_count;
    $('s-text').textContent=d.status==='running'?'运行中（历史）':'已停止';
    var ob=$('out-body');ob.innerHTML='<div class="empty"></div>';
    var link=document.createElement('a');link.href='javascript:void(0)';link.textContent='点击此处或再次点击该会话返回当前任务';link.style.color='var(--accent)';link.style.cursor='pointer';link.onclick=function(){viewSession(sid);};
    ob.firstChild.className='empty';ob.firstChild.appendChild(link);
    loadSessions();
  });
}

function continueSession(sid){
  fetch('/api/sessions/'+sid+'/continue').then(function(r){return r.json()}).then(function(d){
    if(d.error){showError(d.error);return;}
    // 切回当前会话视图（退出历史查看模式）
    currentViewSid=null;
    // 记住从哪个会话继续，启动时传给后端
    continueFromSid=sid;
    // 保留历史记录显示，而不是清空
    if(d.brain && d.brain.indexOf('AI 大脑等待启动')===-1){ $('brain-body').innerHTML=d.brain; }
    else { $('brain-body').innerHTML='<div class="empty"><div class="empty-icon">&#x1F9E0;</div><div class="empty-text">AI 大脑等待启动</div></div>'; }
    if(d.claw && d.claw.indexOf('小龙虾待命中')===-1){ $('claw-body').innerHTML=d.claw; }
    else { $('claw-body').innerHTML='<div class="empty"><div class="empty-icon">&#x1F980;</div><div class="empty-text">小龙虾待命中</div></div>'; }
    $('out-body').innerHTML='';
    var lc=d.loop_count||0;
    $('r-badge').textContent='Round '+lc;
    $('s-dot').className='dot dot-x';
    $('s-text').textContent='已加载上下文（Round '+lc+'）';
    running=false;
    $('gobtn').className='btn btn-go';
    $('gobtn').innerHTML=lc>0?'继续执行':'启动新任务';
    $('gobtn').disabled=false;
    if(timer){clearTimeout(timer);timer=null;}
    // 只填入原始目标，不拼接上下文
    $('goal').value=d.goal;
    $('goal').focus();
    $('goal').blur();
    loadSessions();
    showNotice('已加载上次任务（已跑'+lc+'轮），点击「'+(lc>0?'继续执行':'启动新任务')+'」');
  }).catch(function(e){showError('加载任务上下文失败: '+e.message);});
}

function deleteSession(sid,name){
  if(!confirm('确定删除任务「'+name+'」？'))return;
  fetch('/api/sessions/'+sid,{method:'DELETE'}).then(function(r){return r.json()}).then(function(d){
    if(d.error){showError(d.error);return;}
    if(currentViewSid===sid){currentViewSid=null;poll();}
    loadSessions();
    showNotice('已删除');
  }).catch(function(e){showError('删除失败: '+e.message);});
}

function showNotice(msg){
  var el=document.createElement('div');
  el.style.cssText='position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:rgba(16,185,129,0.95);color:#fff;padding:12px 24px;border-radius:10px;font-size:13px;z-index:9999;max-width:500px;text-align:center;box-shadow:0 8px 24px rgba(0,0,0,0.4)';
  el.textContent=msg;
  document.body.appendChild(el);
  setTimeout(function(){el.style.opacity='0';el.style.transition='opacity 0.5s';setTimeout(function(){el.remove()},600)},4000);
}

function newSession(){
  currentViewSid=null;
  continueFromSid=null;  // 新建任务不再续接
  currentTaskId='';
  fetch('/api/sessions/new',{method:'POST'}).then(function(r){return r.json()}).then(function(d){
    if(d.error){showError(d.error);return;}
    // 清空右侧
    $('brain-body').innerHTML='<div class="empty"><div class="empty-icon">&#x1F9E0;</div><div class="empty-text">AI 大脑等待启动</div><div class="empty-hint">设定目标后点击「启动系统」</div></div>';
    $('claw-body').innerHTML='<div class="empty"><div class="empty-icon">&#x1F980;</div><div class="empty-text">小龙虾待命中</div><div class="empty-hint">系统启动后将显示执行记录</div></div>';
    $('out-body').innerHTML='';
    $('mem-body').innerHTML='';
    $('r-badge').textContent='Round 0';
    $('c-badge').textContent='0 次执行';
    $('out-badge').textContent='0 个产物';
    $('s-dot').className='dot dot-x';
    $('s-text').textContent='待命中';
    running=false;
    $('gobtn').className='btn btn-go';
    $('gobtn').innerHTML='启动新任务';
    $('gobtn').disabled=false;
    if(timer){clearTimeout(timer);timer=null;}
    $('goal').value='';
    loadSessions();
  }).catch(function(e){showError('新建任务失败: '+e.message);});
}

function showError(msg){
  var el=document.createElement('div');
  el.style.cssText='position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:rgba(239,68,68,0.95);color:#fff;padding:12px 24px;border-radius:10px;font-size:13px;z-index:9999;max-width:500px;text-align:center;box-shadow:0 8px 24px rgba(0,0,0,0.4)';
  el.textContent=msg;
  document.body.appendChild(el);
  setTimeout(function(){el.style.opacity='0';el.style.transition='opacity 0.5s';setTimeout(function(){el.remove()},600)},5000);
}

var currentTaskId='';

function poll(){
  if(!running && !pollOnce){return;}
  pollOnce=false;
  var url='/api/state';
  if(currentTaskId) url+='?task_id='+encodeURIComponent(currentTaskId);
  // AbortController 超时：5秒无响应自动取消，防止连接堆积
  var ctrl=new AbortController();
  var fetchTimer=setTimeout(function(){ctrl.abort();},5000);
  fetch(url,{signal:ctrl.signal}).then(function(r){clearTimeout(fetchTimer);return r.json()}).then(function(d){
    // === 初始化阶段的按钮状态设置（页面刷新后 running=false，需要主动判断） ===
    // 注意：只有非运行状态才执行，running=true 时的按钮由 toggle() 的 .then() 设置
    var isRunning = d.tasks && d.tasks.length > 0 && d.tasks[0].running;
    if(!isRunning && !running){
      // 页面刷新后（running=false 且 Worker 未启动）→ 主动设置按钮状态
      var hasHistory = d.round > 0 && d.task_id;
      var btnText = hasHistory ? '继续执行' : '启动新任务';
      var statusText = hasHistory ? '已停止（Round '+d.round+'）' : '待命中';
      $('gobtn').className='btn btn-go';
      $('gobtn').innerHTML=btnText;
      $('s-dot').className='dot dot-x';
      $('s-text').textContent=statusText;
      if(hasHistory){continueFromSid=d.session_id;}
    }

    // 在替换内容前，保存各面板距底部的距离
    var bb=$('brain-body'),cb=$('claw-body');
    var bbDist=bb?(bb.scrollHeight-bb.scrollTop-bb.clientHeight):0;
    var cbDist=cb?(cb.scrollHeight-cb.scrollTop-cb.clientHeight):0;

    // 只在有实际内容时更新面板（防止空响应覆盖已有历史）
    if(d.task_id || (d.brain && d.brain.indexOf('AI 大脑等待启动')===-1)){
      $('brain-body').innerHTML=d.brain;
    }
    if(d.task_id || (d.claw && d.claw.indexOf('小龙虾待命中')===-1)){
      $('claw-body').innerHTML=d.claw;
    }
    // 只在有活跃任务时才覆盖对话和记忆面板（防止后端重启后清空）
    if(d.task_id){
      $('mem-body').innerHTML=d.memory;
      $('out-body').innerHTML=d.outputs;
      $('chat-body').innerHTML=renderChatMsgs(d.chat_messages);
    }
    // 只在有活跃任务时更新状态文字和计数（防止后端重启后覆盖为"待命中"）
    if(d.task_id){
      $('s-text').textContent=d.status;
      $('r-badge').textContent='Round '+d.round;
      $('c-badge').textContent=d.claw_count+' 次执行';
      $('out-badge').textContent=d.output_count+' 个产物';
    }
    currentTaskId=d.task_id||currentTaskId;

    // 对话框
    // 只在有活跃任务时更新对话框状态
    if(d.task_id && d.has_question){
      $('chat-fab-badge').classList.add('show');
      $('chat-fab-badge').textContent=d.chat_messages.length;
      if(!chatOpen)toggleChat();
    }else if(d.task_id){
      $('chat-fab-badge').classList.remove('show');
    }

    // 更新任务栏
    renderTaskBar(d.tasks||[]);

    // 判断当前任务是否还在运行
    var activeTaskRunning=false;
    if(d.tasks){for(var i=0;i<d.tasks.length;i++){if(d.tasks[i].task_id===currentTaskId&&d.tasks[i].running){activeTaskRunning=true;break;}}}

    if(!activeTaskRunning){
      if(running){
        running=false;
        // 判断是"跑完自动停"还是"手动停止"
        // 如果有当前任务且有轮数记录，说明是跑完了，显示"继续执行"
        var hasCompletedTask = d.round > 0 && currentTaskId;
        $('gobtn').className='btn btn-go';
        $('gobtn').innerHTML = hasCompletedTask ? '继续执行' : '启动新任务';
        $('s-dot').className='dot dot-x';
        $('s-text').textContent = hasCompletedTask ? '已停止（Round '+d.round+'）' : '待命中';
        // 任务完成时记住 session_id，这样点"继续执行"能续接
        if(hasCompletedTask && d.session_id){
          continueFromSid = d.session_id;
        }
        // 如果没有运行中的任务了，停止轮询
        var anyRunning=false;
        if(d.tasks){for(var i=0;i<d.tasks.length;i++){if(d.tasks[i].running){anyRunning=true;break;}}}
        if(!anyRunning){clearTimeout(timer);timer=null;}
        // 任务完成时刷新会话列表，让继续按钮出现
        loadSessions();
      }
    } else {
      running=true;
      $('gobtn').className='btn btn-stop';
      $('gobtn').innerHTML='停止当前任务';
    }

    if(d.status.indexOf('思考')>=0){$('s-dot').className='dot dot-y';$('s-text').textContent=d.status;}
    else if(d.status.indexOf('运行')>=0){$('s-dot').className='dot dot-g';}
    if(d.backends){
      if(d.backends.codex && $('codex-dot')){
        $('codex-dot').className='dot '+(d.backends.codex.ok?'dot-g':'dot-x');
        $('codex-status').textContent=d.backends.codex.label;
      }
      if(d.backends.openclaw && $('gw-dot')){
        $('gw-dot').className='dot '+(d.backends.openclaw.ok?'dot-g':'dot-x');
      }
    }
    // 智能滚动：如果用户之前在底部附近，自动滚到底；否则保持原位
    if(bb && bbDist<100){bb.scrollTop=bb.scrollHeight;}
    if(cb && cbDist<100){cb.scrollTop=cb.scrollHeight;}
    // 更新会话列表（仅运行时）
    if(running){loadSessions();}
    // 递归调度下一次 poll（上一次完成后等 2 秒，避免请求堆积）
    if(running){timer=setTimeout(poll,2000);}
  }).catch(function(e){
    // fetch 超时或网络错误：静默忽略，等 2 秒后重试
    if(timer){clearTimeout(timer);}
    if(running){timer=setTimeout(poll,2000);}
  });
}

function renderTaskBar(tasks){
  var el=$('task-bar');
  if(!el)return;
  if(!tasks||tasks.length===0){el.innerHTML='';el.style.display='none';return;}
  el.style.display='flex';
  var h='';
  for(var i=0;i<tasks.length;i++){
    var t=tasks[i];
    var cls='task-chip'+(t.task_id===currentTaskId?' task-chip-active':'');
    var dot='<span class="task-dot'+(t.running?' task-dot-run':'')+'"></span>';
    h+=`<div class="${cls}" onclick="switchTask('${t.task_id}')" title="${escH(t.goal)}">`;
    h+=dot+'<span class="task-chip-text">'+escH(t.goal.substring(0,25))+(t.goal.length>25?'...':'')+'</span>';
    h+='<span class="task-chip-round">R'+t.round+'</span>';
    h+='</div>';
  }
  el.innerHTML=h;
}

function switchTask(taskId){
  currentTaskId=taskId;
  // 清空面板避免闪烁
  $('brain-body').innerHTML='<div class="empty"><div class="empty-text">切换中...</div></div>';
  $('claw-body').innerHTML='';
  pollOnce=true;
  poll();
}

// 初始加载会话列表
setTimeout(loadSessions,800);

// 初始 poll：检查是否有运行中的任务（如果 Worker 在跑，会被 poll 接管）
pollOnce=true;
poll();

// ========== 凭据管理 ==========
var credTemplates={};
var credEditingId=null;

function credLoad(){
  fetch('/api/credentials').then(function(r){return r.json()}).then(function(d){
    var el=$('cred-list');
    if(!d.accounts||d.accounts.length===0){el.innerHTML='<div class="cred-empty">暂无账号，点击下方添加</div>';return;}
    var h='';
    for(var i=0;i<d.accounts.length;i++){
      var a=d.accounts[i];
      var tpl=credTemplates[a.category]||{label:a.category,icon:'🔧'};
      h+='<div class="cred-item" data-id="'+a.id+'" onclick="credEdit(this.dataset.id)">';
      h+='<div class="cred-info"><span class="cred-icon">'+tpl.icon+'</span><div><div class="cred-name">'+escH(a.name)+'</div><div class="cred-cat">'+escH(tpl.label)+'</div></div></div>';
      h+='<div class="cred-actions"><button class="cred-btn del" data-id="'+a.id+'" data-name="'+escH(a.name)+'" onclick="event.stopPropagation();credDel(this.dataset.id,this.dataset.name)">删除</button></div>';
      h+='</div>';
    }
    el.innerHTML=h;
  });
}

function credLoadTemplates(){
  fetch('/api/credentials/templates').then(function(r){return r.json()}).then(function(d){
    credTemplates=d.templates;
    window._credPresets=d.presets||[];
  });
}

function credOpenModal(editId){
  credEditingId=editId||null;
  var modal=document.getElementById('cred-modal-bg');
  if(!modal){
    var bg=document.createElement('div');
    bg.id='cred-modal-bg';
    bg.className='cred-modal-bg';
    bg.onclick=function(e){if(e.target===bg)credCloseModal();};
    bg.innerHTML='<div class="cred-modal" id="cred-modal"></div>';
    document.body.appendChild(bg);
    modal=bg;
  }else{
    modal.style.display='flex';
  }
  var m=document.getElementById('cred-modal');
  if(editId){
    m.innerHTML='<h3>编辑账号</h3>';
    fetch('/api/credentials/'+editId).then(function(r){return r.json()}).then(function(a){
      if(a.error){credCloseModal();return;}
      credRenderForm(m,a);
    });
  }else{
    m.innerHTML='<h3>添加账号</h3>';
    credRenderForm(m,null);
  }
  modal.style.display='flex';
}

function credRenderForm(container,account){
  var name=account?account.name:'';
  var cat=account?account.category:'';
  var fields=account?account.fields:[];

  var h='<div class="cred-field"><label>账号名称</label><input id="cred-name" placeholder="如: 我的DeepSeek" value="'+escH(name)+'"></div>';
  h+='<div style="font-size:11px;color:var(--muted);margin-bottom:10px">选择类型，自动填入对应字段</div>';
  h+='<div id="cred-cat-btns" style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px"></div>';
  h+='<div class="cred-field"><label>账号信息</label><div class="cred-dynamic-fields" id="cred-fields"></div></div>';
  h+='<div class="cred-modal-btns"><button class="cred-btn-cancel" onclick="credCloseModal()">取消</button><button class="cred-btn-save" onclick="credSave()">保存</button></div>';
  container.innerHTML='<h3>'+(credEditingId?'编辑账号':'添加账号')+'</h3>'+h;

  window._credFields=JSON.parse(JSON.stringify(fields));
  window._credCat=cat;
  credRenderCatBtns();
  credRenderFields();
}

function credRenderCatBtns(){
  var el=document.getElementById('cred-cat-btns');
  if(!el)return;
  var cats=['ai_api','payment','social_media','dev_platform','custom'];
  var h='';
  for(var i=0;i<cats.length;i++){
    var c=cats[i];
    var t=credTemplates[c]||{label:c,icon:'🔧'};
    var active=window._credCat===c?' style="border-color:var(--accent);color:var(--accent)"':'';
    h+='<button type="button" class="cred-btn" data-cat="'+c+'" onclick="credPickCat(this.dataset.cat)"'+active+'>'+t.icon+' '+t.label+'</button>';
  }
  el.innerHTML=h;
}

function credPickCat(c){
  window._credCat=c;
  var tpl=credTemplates[c];
  if(tpl){window._credFields=JSON.parse(JSON.stringify(tpl.fields));}
  credRenderCatBtns();
  credRenderFields();
}

function credRenderFields(){
  var el=document.getElementById('cred-fields');
  if(!el)return;
  if(!window._credFields.length){
    el.innerHTML='<div style="font-size:11px;color:var(--muted);text-align:center;padding:12px">先选择一个类型，或点击下方添加</div><div style="text-align:center"><button type="button" class="cred-btn" onclick="window._credFields=[{key:\"account\",label:\"账号\",value:\"\",type:\"text\"},{key:\"password\",label:\"密码\",value:\"\",type:\"password\"}];credRenderFields();">+ 手动添加</button></div>';
    return;
  }
  var h='';
  for(var i=0;i<window._credFields.length;i++){
    var f=window._credFields[i];
    var lbl=f.label||f.key||'';
    var val=f.value||'';
    var tp=f.type==='password'?'password':'text';
    h+='<div class="cred-field-row" style="margin-bottom:8px">';
    h+='<span style="min-width:90px;font-size:12px;color:var(--muted);padding:8px 0;flex-shrink:0">'+escH(lbl)+'</span>';
    h+='<input type="'+tp+'" placeholder="输入'+escH(lbl)+'" value="'+escH(val)+'" data-fidx="'+i+'" data-fkey="value" oninput="credFieldUpdate(+this.dataset.fidx,this.dataset.fkey,this.value)" style="flex:1">';
    h+='<button onclick="credRemoveField('+i+')" style="background:0 0;border:none;color:var(--muted);cursor:pointer;font-size:16px;padding:4px 8px;flex-shrink:0">×</button>';
    h+='</div>';
  }
  h+='<div style="text-align:center;margin-top:4px"><button type="button" class="cred-btn" onclick="credShowAddMenu()">+ 添加字段</button></div>';
  el.innerHTML=h;
}

function credShowAddMenu(){
  var presets=window._credPresets||[];
  var h='<div style="display:flex;flex-wrap:wrap;gap:4px;justify-content:center;padding:8px 0">';
  for(var i=0;i<presets.length;i++){
    var p=presets[i];
    h+='<button type="button" class="cred-btn" data-pkey="'+escH(p.key)+'" data-plabel="'+escH(p.label)+'" data-ptype="'+escH(p.type)+'" onclick="credAddPreset(this.dataset)">'+escH(p.label)+'</button>';
  }
  h+='</div>';
  var el=document.getElementById('cred-fields');
  el.innerHTML+=h;
}

function credAddPreset(dataset){
  window._credFields.push({key:dataset.pkey,label:dataset.plabel,value:'',type:dataset.ptype});
  credRenderFields();
}

function credFieldUpdate(idx,prop,val){window._credFields[idx][prop]=val;}

function credRemoveField(idx){window._credFields.splice(idx,1);credRenderFields();}

function credSave(){
  var name=$('cred-name').value.trim();
  var cat=window._credCat||'custom';
  var fields=window._credFields.filter(function(f){return f.key&&f.key.trim()!=='';});
  if(!name){alert('请输入账号名称');return;}
  if(fields.length===0){alert('请至少添加一个字段');return;}

  var body=JSON.stringify({name:name,category:cat,fields:fields});
  var url=credEditingId?'/api/credentials/'+credEditingId:'/api/credentials';
  var method=credEditingId?'PUT':'POST';
  fetch(url,{method:method,headers:{'Content-Type':'application/json'},body:body}).then(function(r){return r.json()}).then(function(d){
    if(d.error){alert(d.error);return;}
    credCloseModal();
    credLoad();
  });
}

function credDel(id,name){
  if(!confirm('确定删除账号 "'+name+'" 吗？此操作不可撤销。'))return;
  fetch('/api/credentials/'+id,{method:'DELETE'}).then(function(){credLoad();});
}

function credCloseModal(){
  var el=document.getElementById('cred-modal-bg');
  if(el)el.style.display='none';
  credEditingId=null;
  window._credFields=[];
}

function escH(s){if(!s)return '';return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

credLoadTemplates();
setTimeout(credLoad,500);

// ========== 目标管理 ==========
function taskLoad(){
  fetch('/api/tasks').then(function(r){return r.json()}).then(function(d){
    var el=$('goal-tags');
    if(!d.tasks||d.tasks.length===0){el.innerHTML='';return;}
    var h='';
    for(var i=0;i<d.tasks.length;i++){
      var t=d.tasks[i];
      var short=t.name.length>12?t.name.slice(0,12)+'...':t.name;
      h+=`<span class="goal-tag" onclick="taskEdit('${t.id}')" title="${escH(t.name)}">${escH(short)}<span class="x" onclick="event.stopPropagation();taskDelete('${t.id}','')">x</span></span>`;
    }
    el.innerHTML=h;
  }).catch(function(e){console.error('taskLoad error:',e);});
}

function taskSave(){
  var goal=$('goal').value.trim();
  if(!goal){return;}
  var name=goal.length>20?goal.slice(0,20)+'...':goal;
  fetch('/api/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,goal:goal,description:'',mode:'money'})})
  .then(function(r){return r.json()}).then(function(d){
    if(d.error){return;}
    taskLoad();
  }).catch(function(e){console.error('taskSave error:',e);});
}

function taskRun(id){
  fetch('/api/tasks/'+id).then(function(r){return r.json()}).then(function(t){
    if(t.error){return;}
    $('goal').value=t.goal;
    fetch('/api/tasks/'+id+'/start',{method:'POST'});
    toggle();
  });
}

function taskDelete(id){
  fetch('/api/tasks/'+id,{method:'DELETE'}).then(function(){taskLoad();});
}

function taskEdit(id){
  fetch('/api/tasks/'+id).then(function(r){return r.json()}).then(function(t){
    if(t.error){return;}
    $('goal').value=t.goal;
  });
}

setTimeout(taskLoad,600);

/* === 页面初始化：恢复最近 session 的历史记录 === */
(function restoreLastSession(){
  fetch('/api/last-session').then(function(r){return r.json()}).then(function(d){
    if(d.empty){loadSessions();return;}
    // 如果当前有运行中任务，poll 会接管，不需要恢复
    if(running){loadSessions();return;}
    // 恢复最近 session 的历史到面板
    if(d.brain && d.brain.indexOf('AI 大脑等待启动')===-1){$('brain-body').innerHTML=d.brain;}
    if(d.claw && d.claw.indexOf('小龙虾待命中')===-1){$('claw-body').innerHTML=d.claw;}
    if(d.memory && d.memory.indexOf('白板是空的')===-1){$('mem-body').innerHTML=d.memory;}
    var lc=d.loop_count||0;
    $('r-badge').textContent='Round '+lc;
    $('s-text').textContent='已停止（Round '+lc+'）';
    $('s-dot').className='dot dot-x';
    // 记住 session_id，方便"继续执行"
    if(d.id){continueFromSid=d.id;currentTaskId='';}
    // 只在有实际历史时显示"继续执行"
    if(lc>0){
      $('gobtn').innerHTML='继续执行';
    } else {
      $('gobtn').innerHTML='启动新任务';
    }
    $('gobtn').className='btn btn-go';
    $('gobtn').disabled=false;
    loadSessions();
  }).catch(function(e){console.error('restoreLastSession error:',e);loadSessions();});
})();

document.addEventListener('keydown',function(e){if(e.key==='Enter'&&chatOpen&&document.activeElement===$('chat-input')){e.preventDefault();sendChat();}});
</script>"""

    # 快速目标按钮
    qbtn1 = f'<button class="qbtn" onclick="setg(this)">PPT模板售卖</button>'
    qbtn2 = f'<button class="qbtn" onclick="setg(this)">AI Prompt Pack</button>'
    qbtn3 = f'<button class="qbtn" onclick="setg(this)">Notion模板</button>'
    qbtn4 = f'<button class="qbtn" onclick="setg(this)">AI代写文章</button>'
    qbtn5 = f'<button class="qbtn" onclick="setg(this)">AI设计素材</button>'

    brain_html = render_brain_entries()
    claw_html = render_claw_entries()
    mem_html = render_memory()
    out_html = render_outputs()

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>自主赚钱系统</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{CUSTOM_CSS}</style></head>
<body>
<div id="app">

<!-- 左侧 -->
<div id="left">
  <div class="logo-row"><div class="logo"><svg viewBox="0 0 60 60" width="24" height="24"><path d="M30,2 L35,18 L52,22 L35,26 L38,48 L30,36 L22,48 L25,26 L8,22 L25,18 Z" fill="none" stroke="#fff" stroke-width="3" stroke-linejoin="round"/><circle cx="30" cy="26" r="5" fill="#fff"/></svg></div><div class="logo-text"><h1>claw-brain</h1><p>自主决策系统</p><span class="dev-tag">开发者：楚晴</span></div></div>

  <div class="card"><div class="card-label">系统状态</div>
    <div class="srow"><div class="dot dot-x" id="s-dot"></div><span class="label" id="s-text">待命中</span></div>
    <div class="srow"><div class="dot dot-g" id="brain-dot"></div><span class="label">AI 大脑</span><span class="val" id="brain-model">DeepSeek</span></div>
    <div class="srow"><div class="dot dot-y" id="codex-dot"></div><span class="label">Codex 工程层</span><span class="val" id="codex-status">checking</span></div>
    <div class="srow"><div class="dot dot-g" id="gw-dot"></div><span class="label">小龙虾 Gateway</span><span class="val">:18789</span></div>
    <div id="task-bar" style="display:none;flex-wrap:wrap;gap:6px;margin-top:8px"></div>
  </div>

  <div class="card"><div class="card-label">目标设定</div>
    <textarea class="inp" id="goal" placeholder="输入你的目标..." rows="3"></textarea>
    <div style="display:flex;gap:6px">
      <button class="goal-save" onclick="taskSave()" style="flex:1">+ 保存为目标</button>
    </div>
    <div class="goal-tags" id="goal-tags"></div>
  </div>

  <div class="card"><div class="card-label">快速模式</div>
    <div style="display:flex;gap:8px">
      <button class="btn mode-btn" id="mode-money" onclick="setMode('money')" style="flex:1;font-size:12px">&#x1F4B0; 赚钱模式</button>
      <button class="btn mode-btn" id="mode-dev" onclick="setMode('dev')" style="flex:1;font-size:12px">&#x1F6E0; 开发模式</button>
    </div>
  </div>

  <div class="card"><div class="card-label">参数配置</div>
    <div class="row"><span class="lbl">Agent</span><select class="inp" id="agent">{agent_opts}</select></div>
    <div class="row"><span class="lbl">最大轮数</span><input type="number" class="inp num" id="maxl" value="10" min="1" max="999">
    <span class="lbl" style="margin-left:auto">间隔(秒)</span><input type="number" class="inp num" id="ival" value="15" min="5" max="300"></div>
  </div>

  <button class="btn btn-go" id="gobtn" onclick="toggle()">启动新任务</button>

  <div class="card"><div class="card-label">快速目标模板</div>
    <div style="display:flex;flex-direction:column;gap:6px">
      {qbtn1}
      {qbtn2}
      {qbtn3}
      {qbtn4}
      {qbtn5}
    </div>
  </div>

  <div class="card"><div class="card-label">账号管理</div>
    <div class="cred-list" id="cred-list"><div class="cred-empty">加载中...</div></div>
    <button class="cred-add-btn" onclick="credOpenModal()">+ 添加账号</button>
  </div>
</div>

<!-- 右侧 -->
<div id="right">
  <div class="tabs">
    <button class="tab on" id="t-brain" onclick="stab('brain')">&#x1F9E0; AI 大脑思考板</button>
    <button class="tab" id="t-claw" onclick="stab('claw')">&#x1F980; 小龙虾监控</button>
    <button class="tab" id="t-out" onclick="stab('out')">&#x1F4E6; 产物展示</button>
    <button class="tab" id="t-mem" onclick="stab('mem')">&#x1F4BE; 记忆白板</button>
  </div>
  <div class="sess-bar" id="sess-bar">
    <div class="sess-empty" id="sess-empty">加载中...</div>
  </div>

  <div class="board" id="p-brain">
    <div class="bhead"><div class="bhead-l"><div class="bicon bicon-brain">&#x1F9E0;</div><span class="bname">AI 大脑</span></div><span class="badge" id="r-badge">Round 0</span></div>
    <div class="bbody" id="brain-body">{brain_html}</div>
    <div class="bfoot"><div class="bf-item">模型: <strong>DeepSeek</strong></div><div class="bf-item">API: <strong>DeepSeek 官方</strong></div><div class="bf-item">角色: <strong>策略大脑</strong></div></div>
  </div>

  <div class="board hide" id="p-claw">
    <div class="bhead"><div class="bhead-l"><div class="bicon bicon-claw">&#x1F980;</div><span class="bname">小龙虾 OpenClaw</span></div><span class="badge badge-ok" id="c-badge">0 次执行</span></div>
    <div class="bbody" id="claw-body">{claw_html}</div>
    <div class="bfoot"><div class="bf-item">Agent: <strong id="c-agent">main</strong></div><div class="bf-item">Gateway: <strong>:18789</strong></div><div class="bf-item">模式: <strong>CLI</strong></div></div>
  </div>

  <div class="board hide" id="p-out">
    <div class="bhead"><div class="bhead-l"><div class="bicon" style="background:rgba(59,130,246,0.12);color:#3b82f6">&#x1F4E6;</div><span class="bname">产物展示</span></div><span class="badge" id="out-badge">0 个产物</span></div>
    <div class="bbody" id="out-body">{out_html}</div>
    <div class="bfoot"><div class="bf-item">存储: <strong>outputs/</strong></div><div class="bf-item">类型: <strong>代码 / 文档 / 工具</strong></div></div>
  </div>

  <div class="board hide" id="p-mem">
    <div class="bhead"><div class="bhead-l"><div class="bicon bicon-mem">&#x1F4CB;</div><span class="bname">记忆白板</span></div></div>
    <div class="bbody" id="mem-body">{mem_html}</div>
    <div class="bfoot"><div class="bf-item">存储: <strong>自动保存</strong></div><div class="bf-item">容量: <strong>最近 200 条</strong></div></div>
  </div>
</div>

</div>

<!-- 浮动对话框 -->
<div id="chat-fab">
  <div id="chat-panel">
    <div class="chat-head">
      <div class="chat-head-icon">&#x1F9E0;</div>
      <div><div class="chat-head-title">系统对话</div><div class="chat-head-sub">系统需要你输入时会弹窗通知</div></div>
      <button class="chat-close" onclick="toggleChat()">&times;</button>
    </div>
    <div class="chat-body" id="chat-body"><div class="chat-empty">暂无对话</div></div>
    <div class="chat-foot">
      <input class="chat-input" id="chat-input" placeholder="输入指令或反馈..." autocomplete="off">
      <button class="chat-send" onclick="sendChat()">&#x27A4;</button>
    </div>
  </div>
  <button id="chat-fab-btn" onclick="toggleChat()">&#x1F4AC;</button>
  <div id="chat-fab-badge">0</div>
</div>

{js_script}
</body></html>"""


# ===================== 后台循环 =====================
# 核心逻辑已提取到 core.py，此处仅保留 Web Console 专用的启动封装


# ===================== FastAPI =====================

app = FastAPI(title="自主赚钱系统")

# ===== 闲置检测：更新最后活动时间 =====
def _touch_activity():
    """标记一次用户活动（由关键 API 端点调用）"""
    global LAST_ACTIVITY_TIME
    LAST_ACTIVITY_TIME = time.time()

# 挂载 outputs/ 为静态文件，让浏览器可以直接访问产物文件（图片、视频等）
try:
    app.mount("/outputs", StaticFiles(directory=str(OUTPUT_DIR)), name="outputs")
except Exception as e:
    print(f"[WARN] 静态文件挂载失败: {e}")

# 挂载 OpenClaw workspace 为静态文件，让浏览器可以直接查看 OpenClaw 生成的截图等产物
OPENCLAW_WS = Path.home() / ".openclaw" / "workspace"
if OPENCLAW_WS.is_dir():
    try:
        app.mount("/openclaw-ws", StaticFiles(directory=str(OPENCLAW_WS)), name="openclaw-ws")
        print(f"  [OK] OpenClaw workspace 已挂载: {OPENCLAW_WS}")
    except Exception as e:
        print(f"[WARN] OpenClaw workspace 挂载失败: {e}")


@app.get("/", response_class=HTMLResponse)
async def index():
    return build_html()


@app.post("/api/start")
async def api_start(req: Request):
    _touch_activity()
    try:
        body = await req.json()
    except Exception as e:
        print(f"[API] /api/start JSON 解析失败: {e}")
        return JSONResponse({"error": f"请求格式错误: {e}"})

    goal = body.get("goal", "").strip()
    agent = body.get("agent", "main")
    try:
        max_loops = int(body.get("max_loops", 10))
        interval = int(body.get("loop_interval", 15))
    except (ValueError, TypeError):
        max_loops, interval = 10, 15
    continue_from = body.get("continue_from", "")

    if not goal:
        return JSONResponse({"error": "请输入目标"})

    if not BRAIN_API_KEY:
        return JSONResponse({"error": "未设置 BRAIN_API_KEY 环境变量。请在 .env 文件或系统环境中配置。"})

    # 健康检查
    import urllib.request
    gateway_ok = False
    try:
        urllib.request.urlopen(f"{OPENCLAW_GATEWAY_URL}/health", timeout=5)
        gateway_ok = True
    except Exception as e:
        print(f"[API] Gateway 健康检查失败: {e}，等待重试...")
        time.sleep(3)
        if _ensure_gateway():
            gateway_ok = True
    if not gateway_ok:
        return JSONResponse({"error": "OpenClaw Gateway 离线。请使用桌面启动器重启系统。"})

    # 如果有旧 Worker 在跑，先停止
    global _worker_process
    if _is_worker_alive():
        print("[API] 检测到活跃 Worker，发送停止命令")
        _send_command({"action": "stop"})
        time.sleep(2)
        if _worker_process is not None and _worker_process.poll() is None:
            _worker_process.terminate()
            try:
                _worker_process.wait(timeout=5)
            except Exception:
                _worker_process.kill()
        _worker_process = None

    # 兜底：杀掉任何遗留的 Worker 进程（Web Server 重启后可能存在）
    _kill_orphan_worker()

    # 等到所有 Worker 进程真正消失（最多 3 秒）
    for _ in range(15):
        snap_check = _read_snapshot()
        if not snap_check:
            break
        pid = snap_check.get("pid", 0)
        if not pid or not _pid_exists(pid):
            break
        time.sleep(0.2)

    # 创建任务参数
    task_id = f"t_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}"
    session_key = f"task-{task_id}"
    memory_file = f"memory_{task_id}.json"

    # 继续上次任务
    prev_loop_count = 0
    prev_brain_log = None
    prev_claw_log = None
    current_session_id = ""

    if continue_from:
        old_sess = session_mgr.get_session(continue_from)
        if old_sess:
            old_brain_log = old_sess.get("brain_log", [])
            old_claw_log = old_sess.get("claw_log", [])
            prev_loop_count = old_sess.get("loop_count", 0)
            prev_brain_log = old_brain_log
            prev_claw_log = old_claw_log
            # 复用旧 Session ID——不创建新 Session
            current_session_id = continue_from
            if old_brain_log:
                print(f"[API] 继续任务: 已加载 {len(old_brain_log)} 条 brain_log, {prev_loop_count} 轮历史")
            progress_lines = []
            for entry in old_brain_log[-10:]:
                status = entry.get("status", "")
                thought = entry.get("thought", "")[:100]
                action = entry.get("action", "")[:80]
                if thought or action:
                    progress_lines.append(f"  R{entry.get('round','?')} [{status}]: {thought or action}")
            progress_text = "\n".join(progress_lines) if progress_lines else "（无详细记录）"
            goal = f"{goal}\n\n[继续上次任务：之前已跑{prev_loop_count}轮。以下是最近进展：\n{progress_text}\n请基于以上进展继续推进，不要重复已经做过的事情。]"

    # 归档上一个会话（仅当不是继续同一个会话时）
    if not continue_from and session_mgr.current_id:
        session_mgr.archive_session(session_mgr.current_id, "stopped")

    # 创建新会话（仅当不是继续时）
    if not current_session_id:
        current_session_id = session_mgr.create_session(goal, agent)
    else:
        # 继续旧会话——标记为 running
        session_mgr._current_id = current_session_id
        for entry in session_mgr._index:
            if entry["id"] == current_session_id:
                entry["status"] = "running"
                break
        session_mgr._save_index()

    # 写入启动参数到 pipe/startup.json
    # 如果是继续任务，复用旧的 memory 文件
    if continue_from and prev_brain_log:
        old_memory_files = list(Path(__file__).parent.glob(f"memory_*{continue_from}*.json"))
        if old_memory_files:
            memory_file = str(old_memory_files[0])

    startup_data = {
        "goal": goal, "agent": agent, "max_loops": max_loops, "interval": interval,
        "task_id": task_id, "session_id": current_session_id,
        "session_key": session_key, "memory_file": memory_file,
        "continue_from": continue_from,
        "prev_loop_count": prev_loop_count,
        "prev_brain_log": prev_brain_log,
        "prev_claw_log": prev_claw_log,
    }
    startup_file = PIPE_DIR / "startup.json"
    startup_file.write_text(json.dumps(startup_data, ensure_ascii=False), encoding="utf-8")

    # 清理旧的快照和命令文件
    for f in [SNAPSHOT_FILE, COMMAND_FILE]:
        f.unlink(missing_ok=True)
    for f in PIPE_DIR.glob("*.tmp"):
        f.unlink(missing_ok=True)
    (PIPE_DIR / "inject.json").unlink(missing_ok=True)
    (PIPE_DIR / "answer.json").unlink(missing_ok=True)

    # 启动 Worker 子进程
    python_exe = r"C:\Users\楚\.workbuddy\binaries\python\versions\3.13.12\python.exe"
    worker_script = str(Path(__file__).parent / "worker.py")

    # Worker stdout 写日志文件；保持文件引用防止 GC
    global _worker_log_file
    worker_log_path = PIPE_DIR / "worker.log"
    # 确保文件存在并清空旧内容
    worker_log_path.write_text("", encoding="utf-8")
    _worker_log_file = open(worker_log_path, "a", encoding="utf-8", errors="replace")
    print(f"[API] Worker log: {worker_log_path}, fd={_worker_log_file.fileno()}")
    _worker_process = subprocess.Popen(
        [python_exe, "-u", worker_script],
        stdin=subprocess.DEVNULL,
        stdout=_worker_log_file,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        cwd=str(Path(__file__).parent),
    )
    print(f"[API] Worker Popen: PID={_worker_process.pid}, poll={_worker_process.poll()}")
    # 注意：task_id / session_id 由 Worker 写入快照，前端通过 /api/state 从快照读取
    # 不再依赖全局变量

    # 注入暂存的用户消息
    global pending_user_feedbacks
    if pending_user_feedbacks:
        for fb in pending_user_feedbacks:
            _send_command({
                "action": "inject_feedback",
                "task_id": task_id,
                "text": fb.get("text", ""),
            })
        print(f"[API] 注入 {len(pending_user_feedbacks)} 条暂存用户消息")
        pending_user_feedbacks = []

    print(f"[API] Worker 启动: PID={_worker_process.pid}, task_id={task_id}, goal={goal[:30]}...")

    # 验证 Worker 真正启动：等1秒检查进程是否还活着
    time.sleep(1)
    if _worker_process.poll() is not None:
        # Worker 启动后立刻退出了——读取日志文件
        exit_code = _worker_process.returncode
        err_output = ""
        try:
            err_output = (PIPE_DIR / "worker.log").read_text(encoding="utf-8", errors="replace")[:500]
        except Exception:
            pass
        _worker_process = None
        print(f"[API] Worker 启动后立刻退出 (exit={exit_code}): {err_output[:200]}")
        return JSONResponse({"error": f"Worker 启动失败 (exit={exit_code}): {err_output[:200]}"})

    return JSONResponse({"ok": True, "task_id": task_id, "session_id": current_session_id})


@app.post("/api/stop")
async def api_stop(req: Request):
    """停止当前 Worker"""
    global _worker_process
    _send_command({"action": "stop"})
    # 等待 Worker 退出
    if _worker_process and _worker_process.poll() is None:
        try:
            _worker_process.wait(timeout=5)
        except Exception:
            _worker_process.terminate()
    _worker_process = None

    # 停止后立即从快照保存 Session 日志（Worker 可能没来得及保存）
    snap = _read_snapshot()
    if snap and snap.get("session_id"):
        try:
            bl = snap.get("brain_log", [])
            cl = snap.get("claw_log", [])
            if bl or cl:
                session_mgr.save_session_logs(snap["session_id"], bl, cl)
        except Exception as e:
            print(f"[API] 停止后保存 Session 日志失败: {e}")

    return JSONResponse({"ok": True})


@app.post("/api/clear")
async def api_clear():
    """清空当前快照"""
    # 清空快照文件中的日志
    snap = _read_snapshot()
    if snap:
        snap["brain_log"] = []
        snap["claw_log"] = []
        snap["chat_history"] = []
        snap["loop_count"] = 0
        _atomic_write_snapshot(snap)
    return JSONResponse({"ok": True})


@app.post("/api/answer")
async def api_answer(req: Request):
    _touch_activity()
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": "请求格式错误"})
    answer = body.get("answer", "").strip()
    if not answer:
        return JSONResponse({"error": "回复不能为空"})

    _send_command({"action": "answer", "answer": answer})
    return JSONResponse({"ok": True})


@app.get("/api/messages")
def api_messages():
    _touch_activity()
    return JSONResponse(latest_message_payload(Path(__file__).parent))


@app.post("/api/messages/answer")
async def api_messages_answer(req: Request):
    _touch_activity()
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": "请求格式错误"})
    card_id = body.get("card_id", "").strip()
    answer = body.get("answer", "").strip()
    if not answer:
        return JSONResponse({"error": "回复不能为空"})
    center = open_latest_message_center(Path(__file__).parent)
    if not center:
        return JSONResponse({"error": "暂无消息卡片"}, status_code=404)
    if card_id:
        ok = center.answer_card(card_id, answer)
    else:
        pending = center.get_pending_cards(required_only=True)
        ok = bool(pending) and center.answer_card(pending[0].id, answer)
    if not ok:
        return JSONResponse({"error": "卡片不存在或已处理"}, status_code=404)
    _send_command({"action": "answer", "answer": answer})
    return JSONResponse({"ok": True, "messages": center.to_payload()})


@app.post("/api/chat")
async def api_chat(req: Request):
    """随时接收用户消息 - 支持即时操作和反馈注入"""
    import webbrowser as _wb
    import re as _re

    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": "请求格式错误"})

    text = body.get("message", "").strip()
    # task_id 从快照取，不依赖全局变量
    snap = _read_snapshot()
    task_id = body.get("task_id") or (snap.get("task_id") if snap else "")

    if not text:
        return JSONResponse({"error": "消息不能为空"})

    # === 即时操作：关键词匹配，不经过 Brain ===
    _QUICK_PATTERNS = [
        (r"打开.{0,4}(网页|控制台|浏览器|页面)", lambda: _wb.open("http://127.0.0.1:7860")),
        (r"(再|重新|帮我).{0,4}打开", lambda: _wb.open("http://127.0.0.1:7860")),
        (r"open\s+(browser|page|console|web)", lambda: _wb.open("http://127.0.0.1:7860")),
    ]
    for pattern, action_fn in _QUICK_PATTERNS:
        if _re.search(pattern, text.lower()):
            try:
                action_fn()
                return JSONResponse({"ok": True, "quick_action": "opened_browser", "messages": []})
            except Exception as e:
                return JSONResponse({"ok": False, "error": str(e), "messages": []})

    # === 注入到 Worker ===
    snap = _read_snapshot()
    if _is_worker_alive() and snap and snap.get("running"):
        # Worker 正在运行——注入反馈或回答
        if snap.get("has_question"):
            _send_command({"action": "answer", "answer": text})
            return JSONResponse({"ok": True, "type": "answer"})
        else:
            snap_tid = snap.get("task_id", "") if snap else ""
            _send_command({"action": "inject_feedback", "task_id": snap_tid, "text": text})
            return JSONResponse({"ok": True, "type": "feedback"})
    else:
        # Worker 不在运行——暂存消息
        pending_user_feedbacks.append({"role": "usr", "text": text, "time": time.strftime("%H:%M:%S")})
        return JSONResponse({
            "ok": False,
            "type": "queued",
            "error": "当前没有运行中的任务，消息已暂存。启动新任务后 Brain 会自动看到你的指示。",
            "messages": list(pending_user_feedbacks[-20:]),
        })


def _backend_status_payload() -> dict:
    codex_ok, codex_info = codex_available()
    gateway_ok = False
    try:
        req = urllib.request.urlopen(f"{OPENCLAW_GATEWAY_URL}/health", timeout=2)
        gateway_ok = req.status == 200
    except Exception:
        gateway_ok = False
    return {
        "codex": {
            "ok": codex_ok,
            "label": "online" if codex_ok else "offline",
            "info": codex_info[:120],
        },
        "openclaw": {
            "ok": gateway_ok,
            "label": "online" if gateway_ok else "offline",
            "url": OPENCLAW_GATEWAY_URL,
        },
    }


@app.get("/api/state")
def api_state(task_id: str = ""):
    _touch_activity()
    """读快照文件——唯一真实源。Worker 在不在跑由快照里的 running 字段决定，不依赖全局变量。"""
    snap = _read_snapshot()
    if not snap or not snap.get("task_id"):
        # 没有 Worker 快照——从磁盘读取最后一个有日志的 Session 作为 fallback
        last_sess = _read_last_session_from_disk()
        if last_sess:
            return JSONResponse({
                "brain": render_brain_entries(last_sess.get("brain_log", [])),
                "claw": render_claw_entries(last_sess.get("claw_log", [])),
                "memory": render_memory(),
                "outputs": render_outputs(),
                "status": "已停止",
                "round": last_sess.get("loop_count", 0),
                "claw_count": len(last_sess.get("claw_log", [])),
                "output_count": len(output_manager.get_recent_outputs(100)),
                "chat_messages": last_sess.get("chat_history", [])[-20:],
                "has_question": last_sess.get("has_question", False),
                "session_id": last_sess.get("session_id", ""),
                "task_id": "",
                "tasks": [],
                "last_activity_time": LAST_ACTIVITY_TIME,
                "idle_timeout": IDLE_TIMEOUT_SECONDS,
                "checkpoints": _latest_checkpoint_payload(),
                "task_contract": _latest_task_contract_payload(last_sess.get("task_id", "") or last_sess.get("session_id", "")),
                "messages": latest_message_payload(Path(__file__).parent),
                "backends": _backend_status_payload(),
            })

        return JSONResponse({
            "brain": render_brain_entries(),
            "claw": render_claw_entries(),
            "memory": render_memory(),
            "outputs": render_outputs(),
            "status": "待命中",
            "round": 0,
            "claw_count": 0,
            "output_count": len(output_manager.get_recent_outputs(100)),
            "chat_messages": [],
            "has_question": False,
            "session_id": "",
            "task_id": "",
            "tasks": [],
            "last_activity_time": LAST_ACTIVITY_TIME,
            "idle_timeout": IDLE_TIMEOUT_SECONDS,
            "checkpoints": _latest_checkpoint_payload(),
            "task_contract": _latest_task_contract_payload(),
            "messages": latest_message_payload(Path(__file__).parent),
            "backends": _backend_status_payload(),
        })

    is_alive = _is_worker_alive()
    running = snap.get("running", False)
    status = snap.get("status_text", "运行中")
    if not is_alive and running:
        status = "已停止"
    elif running:
        status = f"运行中 - Round {snap.get('loop_count', 0)}"

    return JSONResponse({
        "brain": render_brain_entries(snap.get("brain_log", [])),
        "claw": render_claw_entries(snap.get("claw_log", [])),
        "memory": render_memory(),
        "outputs": render_outputs(),
        "status": status,
        "round": snap.get("loop_count", 0),
        "claw_count": len(snap.get("claw_log", [])),
        "output_count": len(output_manager.get_recent_outputs(100)),
        "chat_messages": snap.get("chat_history", [])[-20:],
        "has_question": snap.get("has_question", False),
        "session_id": snap.get("session_id", ""),
        "task_id": snap.get("task_id", ""),
        "tasks": [{
            "task_id": snap.get("task_id", ""),
            "goal": snap.get("goal", "")[:60],
            "agent": snap.get("agent", "main"),
            "running": is_alive and running,
            "round": snap.get("loop_count", 0),
            "start_time": snap.get("started_at", ""),
            "active": True,
        }],
        "last_activity_time": LAST_ACTIVITY_TIME,
        "idle_timeout": IDLE_TIMEOUT_SECONDS,
        "checkpoints": _latest_checkpoint_payload(),
        "task_contract": _latest_task_contract_payload(snap.get("task_id", "") or snap.get("session_id", "")),
        "messages": latest_message_payload(Path(__file__).parent),
        "backends": _backend_status_payload(),
    })


@app.get("/api/checkpoints")
def api_checkpoints(session: str = "", limit: int = 8):
    _touch_activity()
    data_dir = Path(__file__).parent / "data" / "checkpoints"
    if not data_dir.exists():
        return JSONResponse({
            "session": session,
            "items": [],
            "review": review_checkpoints([]).to_dict(),
        })

    if session:
        path = data_dir / f"{session}.jsonl"
    else:
        files = sorted(data_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        path = files[0] if files else None

    if not path or not path.exists():
        return JSONResponse({
            "session": session,
            "items": [],
            "review": review_checkpoints([]).to_dict(),
        })

    rows = []
    try:
        safe_limit = max(1, min(limit, 50))
        for line in path.read_text(encoding="utf-8").splitlines()[-safe_limit:]:
            if line.strip():
                rows.append(json.loads(line))
    except Exception:
        rows = []

    return JSONResponse({
        "session": path.stem,
        "items": rows,
        "review": review_checkpoints(rows).to_dict(),
    })


@app.get("/api/task-contract")
def api_task_contract(session: str = ""):
    _touch_activity()
    return JSONResponse(_latest_task_contract_payload(session=session))


@app.get("/api/output/{output_id}")
async def api_get_output(output_id: str):
    """获取产物完整内容"""
    out = output_manager.get_output(output_id)
    if not out:
        return JSONResponse({"error": "产物不存在"}, status_code=404)
    return JSONResponse({
        "id": out["id"],
        "type": out["type"],
        "title": out["title"],
        "content": out.get("full_content") or out["content"],
        "timestamp": out["timestamp"],
        "file_path": out.get("file_path", ""),
    })


@app.get("/api/sessions")
async def api_list_sessions():
    """列出所有会话（索引，不含日志）"""
    return JSONResponse({"sessions": session_mgr.list_sessions()})


@app.get("/api/last-session")
def api_last_session():
    """返回最近一个有日志的会话数据（前端刷新后恢复历史用）"""
    return _get_last_session_sync()


def _get_last_session_sync():
    """同步获取最近 session——优先返回有日志的，其次返回最近的"""
    sessions = session_mgr.list_sessions(30)
    for s in sessions:
        # 找第一个有实际日志的 session（宽松判断：loop_count > 0 或有 brain_log 数据）
        full = session_mgr.get_session(s["id"])
        if not full:
            continue
        bl = full.get("brain_log", [])
        cl = full.get("claw_log", [])
        if bl or cl or s.get("loop_count", 0) > 0:
            return JSONResponse({
                "id": full["id"],
                "goal": full["goal"],
                "status": full["status"],
                "loop_count": full.get("loop_count", 0),
                "brain": render_brain_entries(bl),
                "claw": render_claw_entries(cl),
                "memory": render_memory(),
            })
    return JSONResponse({"empty": True})


@app.get("/api/sessions/{session_id}")
async def api_get_session(session_id: str):
    """获取指定会话的完整日志（渲染为 HTML）"""
    sess = session_mgr.get_session(session_id)
    if not sess:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    # 直接传入历史日志渲染，不再修改全局 state
    brain_html = render_brain_entries(sess.get("brain_log", []))
    claw_html = render_claw_entries(sess.get("claw_log", []))

    return JSONResponse({
        "id": sess["id"],
        "goal": sess["goal"],
        "agent": sess.get("agent", "main"),
        "status": sess["status"],
        "start_time": sess["start_time"],
        "end_time": sess.get("end_time", ""),
        "loop_count": sess.get("loop_count", 0),
        "brain": brain_html,
        "claw": claw_html,
        "brain_plain": sess.get("brain_plain", ""),
    })


@app.delete("/api/sessions/{session_id}")
async def api_delete_session(session_id: str):
    """删除指定会话"""
    import os as _os
    # 不允许删除正在运行的任务
    snap = _read_snapshot()
    if snap and snap.get("session_id") == session_id:
        return JSONResponse({"error": "不能删除正在运行的任务"}, status_code=400)

    sess = session_mgr.get_session(session_id)
    if not sess:
        return JSONResponse({"error": "会话不存在"}, status_code=404)

    # 删除 session 文件
    session_file = _os.path.join(SESSIONS_DIR, f"{session_id}.json")
    if _os.path.exists(session_file):
        _os.remove(session_file)

    # 更新 index.json
    idx_file = _os.path.join(SESSIONS_DIR, "index.json")
    if _os.path.exists(idx_file):
        with open(idx_file, "r", encoding="utf-8") as f:
            idx = json.load(f)
        idx = [e for e in idx if e.get("id") != session_id]
        with open(idx_file, "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=False, indent=2)

    return JSONResponse({"ok": True, "deleted": session_id})


@app.get("/api/sessions/{session_id}/continue")
async def api_continue_session(session_id: str):
    """获取继续上一个任务所需的上下文（原始 goal + 最后几轮 Brain 分析摘要）"""
    sess = session_mgr.get_session(session_id)
    if not sess:
        return JSONResponse({"error": "会话不存在"}, status_code=404)

    # 提取原始 goal（去掉之前拼接的 "[继续上次任务" 后缀）
    raw_goal = sess["goal"]
    cont_marker = "\n[继续上次任务"
    if cont_marker in raw_goal:
        raw_goal = raw_goal[:raw_goal.index(cont_marker)].strip()

    # 提取最后几轮 Brain 的 thought/observation 作为上下文摘要
    brain_log = sess.get("brain_log", [])
    last_rounds = []
    for entry in brain_log[-6:]:  # 最后6轮
        thought = entry.get("thought", "")
        obs = entry.get("observation", "")
        action = entry.get("action", "")
        status = entry.get("status", "")
        if thought:
            last_rounds.append(f"[{status}] {thought[:200]}")
        if action and status not in ("quality_check",):
            last_rounds.append(f"  执行: {action[:100]}")

    # 渲染历史日志为 HTML，让前端能直接显示
    brain_html = render_brain_entries(sess.get("brain_log", []))
    claw_html = render_claw_entries(sess.get("claw_log", []))

    return JSONResponse({
        "goal": raw_goal,
        "loop_count": sess.get("loop_count", 0),
        "context": "\n".join(last_rounds) if last_rounds else "（无分析记录）",
        "brain": brain_html,
        "claw": claw_html,
    })


@app.post("/api/sessions/new")
async def api_new_session():
    _touch_activity()
    """创建新空会话"""
    sid = session_mgr.create_session("(新建任务)", "main")
    return JSONResponse({"ok": True, "session_id": sid})


# ===================== 凭据管理 API =====================

@app.get("/api/credentials")
async def api_cred_list():
    """列出所有账号（脱敏）"""
    return JSONResponse({"accounts": list_accounts(mask=True)})

@app.get("/api/credentials/templates")
async def api_cred_templates():
    """获取账号分类模板"""
    return JSONResponse({"templates": ACCOUNT_TEMPLATES, "presets": PRESET_FIELDS})

@app.get("/api/credentials/{account_id}")
async def api_cred_get(account_id: str):
    """获取单个账号完整信息（不脱敏）"""
    account = get_account(account_id)
    if not account:
        return JSONResponse({"error": "账号不存在"}, status_code=404)
    return JSONResponse(account)

@app.post("/api/credentials")
async def api_cred_add(req: Request):
    """添加新账号"""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": "JSON 解析失败"}, status_code=400)
    name = body.get("name", "").strip()
    category = body.get("category", "custom")
    fields = body.get("fields", [])
    if not name:
        return JSONResponse({"error": "账号名称不能为空"}, status_code=400)
    account = add_account(name, category, fields)
    return JSONResponse(account)

@app.put("/api/credentials/{account_id}")
async def api_cred_update(account_id: str, req: Request):
    """更新账号"""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": "JSON 解析失败"}, status_code=400)
    account = update_account(
        account_id,
        name=body.get("name"),
        category=body.get("category"),
        fields=body.get("fields"),
    )
    if not account:
        return JSONResponse({"error": "账号不存在"}, status_code=404)
    return JSONResponse(account)

@app.delete("/api/credentials/{account_id}")
async def api_cred_delete(account_id: str):
    """删除账号"""
    ok = delete_account(account_id)
    if not ok:
        return JSONResponse({"error": "账号不存在"}, status_code=404)
    return JSONResponse({"ok": True})


# ===================== 任务管理 API =====================

@app.get("/api/tasks")
async def api_task_list():
    """获取任务列表"""
    tm = get_task_manager()
    tasks = tm.list_tasks(limit=50)
    stats = tm.get_stats()
    return JSONResponse({"tasks": tasks, "stats": stats})


@app.get("/api/tasks/{task_id}")
async def api_task_get(task_id: str):
    """获取任务详情"""
    tm = get_task_manager()
    task = tm.get_task(task_id)
    if not task:
        return JSONResponse({"error": "任务不存在"}, status_code=404)
    return JSONResponse(task)


@app.post("/api/tasks")
async def api_task_create(req: Request):
    """创建新任务"""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": "JSON 解析失败"}, status_code=400)

    name = body.get("name", "").strip()
    goal = body.get("goal", "").strip()
    description = body.get("description", "").strip()
    mode = body.get("mode", "money")

    if not name or not goal:
        return JSONResponse({"error": "任务名称和目标不能为空"}, status_code=400)

    tm = get_task_manager()
    task = tm.create_task(name=name, goal=goal, description=description, mode=mode)
    return JSONResponse(task)


@app.post("/api/tasks/{task_id}/start")
async def api_task_start(task_id: str):
    """标记任务为运行中"""
    tm = get_task_manager()
    task = tm.start_task(task_id)
    if not task:
        return JSONResponse({"error": "任务不存在"}, status_code=404)
    return JSONResponse(task)


@app.post("/api/tasks/{task_id}/complete")
async def api_task_complete(task_id: str, req: Request):
    """标记任务为已完成"""
    try:
        body = await req.json()
    except Exception:
        body = {}

    tm = get_task_manager()
    task = tm.complete_task(
        task_id,
        result=body.get("result"),
        outputs=body.get("outputs")
    )
    if not task:
        return JSONResponse({"error": "任务不存在"}, status_code=404)
    return JSONResponse(task)


@app.delete("/api/tasks/{task_id}")
async def api_task_delete(task_id: str):
    """删除任务"""
    tm = get_task_manager()
    ok = tm.delete_task(task_id)
    if not ok:
        return JSONResponse({"error": "任务不存在"}, status_code=404)
    return JSONResponse({"ok": True})


# ===================== 网关自动启动 =====================

def _ensure_gateway(gateway_port: int = 18789, timeout: int = 20) -> bool:
    """检查 OpenClaw 网关是否在运行。返回是否就绪。"""
    # 端口是否已被监听
    for _ in range(3):
        try:
            sock = socket.create_connection(("127.0.0.1", gateway_port), timeout=1)
            sock.close()
            print(f"  [OK] OpenClaw 网关已就绪 (:{gateway_port})")
            return True
        except (OSError, ConnectionRefusedError):
            pass

    print(f"  [!] OpenClaw 网关未就绪 (:{gateway_port})，请先启动网关")
    return False


# ===================== 入口 =====================

# 全局日志：将 print 和 stderr 写入日志文件，崩溃时可追溯
import logging as _logging
_CONSOLE_LOG = str(Path(__file__).parent / "logs" / "web_console_crash.log")
Path(_CONSOLE_LOG).parent.mkdir(exist_ok=True)
_logging.basicConfig(
    filename=_CONSOLE_LOG, level=_logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
_logger = _logging.getLogger("web_console")


class _StderrToLog:
    """将未捕获的 stderr 写入日志"""
    def __init__(self, fallback):
        self._fallback = fallback
    def write(self, msg):
        if msg and msg.strip():
            _logger.error(msg.rstrip())
        self._fallback.write(msg)
    def flush(self):
        self._fallback.flush()


def _ensure_gateway(gateway_port: int = 18789, timeout: int = 20) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", gateway_port), timeout=1):
            print(f"  [OK] OpenClaw gateway ready (:{gateway_port})")
            return True
    except OSError:
        print(f"  [!] OpenClaw gateway offline (:{gateway_port}), trying auto-start...")

    return ensure_openclaw_gateway(
        Path(__file__).parent,
        port=gateway_port,
        max_wait=timeout,
        log=print,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


if __name__ == "__main__":
    import sys as _sys
    import atexit
    import signal as _signal

    _sys.stderr = _StderrToLog(_sys.stderr)
    _logger.info("=== Web 控制台启动 ===")
    print()
    print("  自主赚钱系统 - 控制台")
    print("  http://127.0.0.1:7860")
    print()

    def _graceful_shutdown(signum=None, frame=None):
        """优雅关闭：停止 Worker 进程"""
        print("[SHUTDOWN] 正在停止 Worker...")
        _logger.info("优雅关闭：停止 Worker")
        if _is_worker_alive():
            _send_command({"action": "stop"})
            try:
                _worker_process.wait(timeout=5)
            except Exception:
                _worker_process.terminate()
        _cleanup_dead_worker()

    # 注册退出钩子
    atexit.register(_graceful_shutdown)
    try:
        _signal.signal(_signal.SIGTERM, _graceful_shutdown)
    except (OSError, ValueError):
        pass  # Windows 可能不支持 SIGTERM handler

    _ensure_gateway()

    # Web Server 启动时清理遗留的僵尸 Worker（上次崩溃留下的）
    if _kill_orphan_worker():
        # 清空快照让前端从 0 开始，否则会显示已停止状态
        try:
            snap = _read_snapshot()
            if snap:
                snap["running"] = False
                snap["status_text"] = "已停止"
                snap["pid"] = 0
                _atomic_write_snapshot(snap)
        except Exception:
            pass

    try:
        uvicorn.run(
            app, host="127.0.0.1", port=7860, log_level="warning",
            timeout_keep_alive=5,
            limit_concurrency=50,
        )
    except KeyboardInterrupt:
        print("\n[MAIN] 用户中断，退出")
        _logger.info("用户中断，退出")
        _graceful_shutdown()
    except Exception as e:
        import traceback
        _logger.critical(f"uvicorn 异常退出: {e}\n{traceback.format_exc()}")
        print(f"\n[MAIN] uvicorn 异常退出: {e}")
        _graceful_shutdown()
