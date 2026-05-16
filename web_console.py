"""
自主赚钱系统 - Web 控制台
===========================
FastAPI 后端 + 自定义 HTML 前端
参考 OpenClaw Control UI 深色主题风格

启动: python web_console.py
访问: http://127.0.0.1:7860
"""

import json
import threading
import time
import queue
import os
import sys
from pathlib import Path

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
from autonomous_system import OpenClawClient, Brain, Memory

# ===================== 配置 =====================
BRAIN_API_KEY = os.environ.get("BRAIN_API_KEY", "")
BRAIN_BASE_URL = os.environ.get("BRAIN_BASE_URL", "https://api.deepseek.com/v1")
BRAIN_MODEL = os.environ.get("BRAIN_MODEL", "deepseek-chat")
OPENCLAW_GATEWAY_URL = os.environ.get("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
SESSION_KEY = "autonomous-money-maker"
MEMORY_FILE = str(Path(__file__).parent / "system_memory.json")

AGENTS = ["main", "brain", "content-agent", "research-agent", "dev-agent", "bd-agent"]

# ===================== 全局状态 =====================
state_lock = threading.Lock()
system_running = False
loop_count = 0
event_queue: queue.Queue = queue.Queue()
brain_log: list = []
claw_log: list = []
chat_history: list = []       # 对话历史 [{"role":"sys"|"usr", "text":"..."}]
pending_question: str = ""    # 当前等待回答的问题（空=无）
answer_event = threading.Event()  # 通知 run_loop 用户已回复
user_answer: str = ""         # 用户的回复内容


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
.logo-text p{color:var(--muted);font-size:11px;margin-top:2px}

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

/* === 快速目标 === */
.qbtn{background:var(--bg-hover);border:1px solid var(--border-strong);border-radius:8px;padding:8px 12px;color:var(--text);font-size:12px;cursor:pointer;text-align:left;transition:all .2s;font-family:var(--font);width:100%}
.qbtn:hover{border-color:rgba(255,92,92,.3);background:var(--bg-elevated)}

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
"""


def _esc(t: str) -> str:
    if not t:
        return ""
    return (str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;").replace("\n", "<br>"))


def render_brain_entries():
    if not brain_log:
        return '<div class="empty"><div class="empty-icon">&#x1F9E0;</div><div class="empty-text">AI 大脑等待启动</div><div class="empty-hint">设定目标后点击「启动系统」</div></div>'
    parts = []
    for e in reversed(brain_log[-50:]):
        parts.append(f'<div class="entry">')
        parts.append(f'<div class="entry-round">Round {e.get("round","?")} &mdash; Brain</div>')
        if e.get("thought"):
            parts.append(f'<div class="entry-label brain">思考</div><div class="entry-text">{_esc(e["thought"])}</div>')
        if e.get("observation"):
            parts.append(f'<div class="entry-label brain">观察</div><div class="entry-text">{_esc(e["observation"])}</div>')
        if e.get("action"):
            parts.append(f'<div class="action-box"><div class="action-label">发送给小龙虾</div><div class="action-text">{_esc(e["action"])}</div></div>')
        if e.get("update_memory"):
            parts.append(f'<div class="entry-text" style="color:var(--muted);font-style:italic">{_esc(e["update_memory"])}</div>')
        sc = {"continue":"var(--ok)","milestone":"var(--warn)","blocked":"var(--danger)","pause":"var(--info)"}
        c = sc.get(e.get("status",""),"var(--muted)")
        parts.append(f'<div style="margin-top:8px;font-size:11px;color:{c};font-weight:600">[{e.get("status","?").upper()}]</div>')
        parts.append('</div>')
    return "".join(parts)


def render_claw_entries():
    if not claw_log:
        return '<div class="empty"><div class="empty-icon">&#x1F980;</div><div class="empty-text">小龙虾待命中</div><div class="empty-hint">系统启动后将显示执行记录</div></div>'
    parts = []
    for e in reversed(claw_log[-50:]):
        icon = "+" if e.get("success") else "x"
        cls = "ok" if e.get("success") else "fail"
        parts.append(f'<div class="entry claw">')
        parts.append(f'<div class="entry-round">Round {e.get("round","?")} &mdash; OpenClaw [{icon}]</div>')
        if e.get("instruction"):
            parts.append(f'<div class="entry-label claw">指令</div><div class="entry-text">{_esc(e["instruction"])}</div>')
        if e.get("result"):
            parts.append(f'<div class="entry-label claw">结果</div><div class="result-box result-{cls}">{_esc(e["result"][:500])}</div>')
        parts.append('</div>')
    return "".join(parts)


def render_memory():
    try:
        mem = Memory(MEMORY_FILE)
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
    return "".join(parts)


def build_html():
    agent_opts = "\n".join(f'<option value="{a}">{a}</option>' for a in AGENTS)

    # Script 部分单独构建，避免 f-string 与 JS 花括号冲突
    js_script = """<script>
let running=false,timer=null;
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
  inp.value='';
  fetch('/api/answer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({answer:txt})})
  .then(function(r){return r.json()})
  .then(function(d){
    $('chat-body').innerHTML=renderChatMsgs(d.messages);
    var b=$('chat-body');if(b)b.scrollTop=b.scrollHeight;
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
    '\\u83b7\\u5ba2':'\\u5206\\u6790\\u5f53\\u524d\\u83b7\\u5ba2\\u6e20\\u9053\\uff0c\\u627e\\u5230\\u8f6c\\u5316\\u7387\\u6700\\u9ad8\\u7684\\u65b9\\u5f0f\\u5e76\\u6301\\u7eed\\u653e\\u5927\\uff0c\\u76f4\\u5230\\u4ea7\\u751f\\u771f\\u5b9e\\u6536\\u5165\\u3002',
    '\\u5b9a\\u4ef7':'\\u8c03\\u7814 Gumroad \\u4e0a\\u5356 AI prompt \\u7684\\u6700\\u4f73\\u5b9a\\u4ef7\\u7b56\\u7565\\u548c\\u7ade\\u54c1\\u5206\\u6790\\u3002',
    'Reddit':'\\u641c\\u7d22 Reddit \\u4e0a\\u5173\\u4e8e AI automation \\u8d5a\\u94b1\\u7684\\u6700\\u65b0\\u5e16\\u5b50\\uff0c\\u603b\\u7ed3\\u6210\\u529f\\u6848\\u4f8b\\u3002'
  };
  const text=el.textContent;
  for(const[k,v]of Object.entries(map)){if(text.includes(k)){$('goal').value=v;return;}}
}

function toggle(){
  if(!running){
    const goal=$('goal').value,agent=$('agent').value,
          maxl=parseInt($('maxl').value)||10,
          ival=parseInt($('ival').value)||15;
    if(!goal.trim()){alert('请输入目标');return;}

    $('gobtn').disabled=true;
    $('gobtn').innerHTML='<span class="spinner"></span>启动中...';

    fetch('/api/start',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({goal:goal,agent:agent,max_loops:maxl,loop_interval:ival})
    })
    .then(function(r){
      if(!r.ok) return r.json().then(function(d){throw new Error(d.error||'HTTP '+r.status)});
      return r.json();
    })
    .then(function(d){
      if(d.error){showError(d.error);$('gobtn').disabled=false;$('gobtn').innerHTML='启动系统';return;}
      running=true;
      $('gobtn').className='btn btn-stop';
      $('gobtn').innerHTML='停止系统';
      $('gobtn').disabled=false;
      $('s-dot').className='dot dot-g';
      $('s-text').textContent='系统运行中';
      $('c-agent').textContent=agent;
      timer=setInterval(poll,2000);
    })
    .catch(function(e){
      showError('启动失败: '+e.message);
      $('gobtn').disabled=false;$('gobtn').innerHTML='启动系统';
    });
  } else {
    fetch('/api/stop',{method:'POST'}).then(function(){
      running=false;
      $('gobtn').className='btn btn-go';
      $('gobtn').innerHTML='启动系统';
      $('s-dot').className='dot dot-x';
      clearInterval(timer);timer=null;
      poll();
    });
  }
}

function showError(msg){
  var el=document.createElement('div');
  el.style.cssText='position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:rgba(239,68,68,0.95);color:#fff;padding:12px 24px;border-radius:10px;font-size:13px;z-index:9999;max-width:500px;text-align:center;box-shadow:0 8px 24px rgba(0,0,0,0.4)';
  el.textContent=msg;
  document.body.appendChild(el);
  setTimeout(function(){el.style.opacity='0';el.style.transition='opacity 0.5s';setTimeout(function(){el.remove()},600)},5000);
}

function poll(){
  fetch('/api/state').then(function(r){return r.json()}).then(function(d){
    $('brain-body').innerHTML=d.brain;
    $('claw-body').innerHTML=d.claw;
    $('mem-body').innerHTML=d.memory;
    $('s-text').textContent=d.status;
    $('r-badge').textContent='Round '+d.round;
    $('c-badge').textContent=d.claw_count+' 次执行';
    /* 对话框 */
    $('chat-body').innerHTML=renderChatMsgs(d.chat_messages);
    if(d.has_question){
      $('chat-fab-badge').classList.add('show');
      $('chat-fab-badge').textContent=d.chat_messages.length;
      if(!chatOpen)toggleChat();
    }else{
      $('chat-fab-badge').classList.remove('show');
    }
    if(d.status.indexOf('停止')>=0||d.status.indexOf('待命')>=0){
      if(running){
        running=false;
        $('gobtn').className='btn btn-go';
        $('gobtn').innerHTML='启动系统';
        $('s-dot').className='dot dot-x';
        clearInterval(timer);timer=null;
      }
    }
    if(d.status.indexOf('思考')>=0){$('s-dot').className='dot dot-y';$('s-text').textContent=d.status;}
    else if(d.status.indexOf('运行')>=0){$('s-dot').className='dot dot-g';}
    var bb=$('brain-body'),cb=$('claw-body');
    if(bb.lastChild)bb.scrollTop=bb.scrollHeight;
    if(cb.lastChild)cb.scrollTop=cb.scrollHeight;
  });
}

poll();
document.addEventListener('keydown',function(e){if(e.key==='Enter'&&chatOpen&&document.activeElement===$('chat-input')){e.preventDefault();sendChat();}});
</script>"""

    # 快速目标按钮（不用 unicode escape，直接中文）
    qbtn1 = f'<button class="qbtn" onclick="setg(this)">&#x1F50D; 获客分析 &mdash; 找到转化率最高的渠道</button>'
    qbtn2 = f'<button class="qbtn" onclick="setg(this)">&#x1F4B0; 定价调研 &mdash; Gumroad AI prompt 竞品分析</button>'
    qbtn3 = f'<button class="qbtn" onclick="setg(this)">&#x1F4AC; Reddit 挖掘 &mdash; AI automation 赚钱成功案例</button>'

    brain_html = render_brain_entries()
    claw_html = render_claw_entries()
    mem_html = render_memory()

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
  <div class="logo-row"><div class="logo">M</div><div class="logo-text"><h1>自主赚钱系统</h1><p>Autonomous Money Maker v2</p></div></div>

  <div class="card"><div class="card-label">系统状态</div>
    <div class="srow"><div class="dot dot-x" id="s-dot"></div><span class="label" id="s-text">待命中</span></div>
    <div class="srow"><div class="dot dot-g" id="brain-dot"></div><span class="label">AI 大脑</span><span class="val" id="brain-model">DeepSeek</span></div>
    <div class="srow"><div class="dot dot-g" id="gw-dot"></div><span class="label">小龙虾 Gateway</span><span class="val">:18789</span></div>
  </div>

  <div class="card"><div class="card-label">目标设定</div>
    <textarea class="inp" id="goal" placeholder="输入你的终极目标...">分析当前获客渠道，找到转化率最高的方式并持续放大，直到产生真实收入。</textarea>
  </div>

  <div class="card"><div class="card-label">参数配置</div>
    <div class="row"><span class="lbl">Agent</span><select class="inp" id="agent">{agent_opts}</select></div>
    <div class="row"><span class="lbl">最大轮数</span><input type="number" class="inp num" id="maxl" value="10" min="1" max="999">
    <span class="lbl" style="margin-left:auto">间隔(秒)</span><input type="number" class="inp num" id="ival" value="15" min="5" max="300"></div>
  </div>

  <button class="btn btn-go" id="gobtn" onclick="toggle()">启动系统</button>

  <div class="card"><div class="card-label">快速目标模板</div>
    <div style="display:flex;flex-direction:column;gap:6px">
      {qbtn1}
      {qbtn2}
      {qbtn3}
    </div>
  </div>
</div>

<!-- 右侧 -->
<div id="right">
  <div class="tabs">
    <button class="tab on" id="t-brain" onclick="stab('brain')">&#x1F9E0; AI 大脑思考板</button>
    <button class="tab" id="t-claw" onclick="stab('claw')">&#x1F980; 小龙虾监控</button>
    <button class="tab" id="t-mem" onclick="stab('mem')">&#x1F4BE; 记忆白板</button>
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

  <div class="board hide" id="p-mem">
    <div class="bhead"><div class="bhead-l"><div class="bicon bicon-mem">&#x1F4CB;</div><span class="bname">记忆白板</span></div></div>
    <div class="bbody" id="mem-body">{mem_html}</div>
    <div class="bfoot"><div class="bf-item">存储: <strong>JSON 文件</strong></div><div class="bf-item">容量: <strong>最近 50 条</strong></div></div>
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
      <input class="chat-input" id="chat-input" placeholder="输入你的回复..." autocomplete="off">
      <button class="chat-send" onclick="sendChat()">&#x27A4;</button>
    </div>
  </div>
  <button id="chat-fab-btn" onclick="toggleChat()">&#x1F4AC;</button>
  <div id="chat-fab-badge">0</div>
</div>

{js_script}
</body></html>"""


# ===================== 后台循环 =====================

def run_loop(goal: str, agent: str, max_loops: int, interval: int):
    global system_running, loop_count, pending_question, user_answer
    import traceback

    print(f"[LOOP] 启动 run_loop: goal={goal[:30]}..., agent={agent}, max_loops={max_loops}")

    mem = Memory(MEMORY_FILE)
    brain = Brain(BRAIN_API_KEY, BRAIN_BASE_URL, BRAIN_MODEL)

    try:
        claw = OpenClawClient(agent, SESSION_KEY, OPENCLAW_GATEWAY_URL)
    except Exception as e:
        print(f"[LOOP] OpenClaw 初始化失败: {e}")
        traceback.print_exc()
        brain_log.append({
            "round": 0, "thought": f"OpenClaw 初始化失败: {e}",
            "observation": "system_error", "action": "",
            "update_memory": "", "status": "blocked",
        })
        with state_lock:
            system_running = False
        return

    last_fb = "系统刚刚启动，请开始第一步行动。"
    print(f"[LOOP] OpenClaw 初始化成功，进入主循环")

    while True:
        with state_lock:
            if not system_running:
                print("[LOOP] system_running=False, 退出循环")
                break
        loop_count += 1
        if 0 < max_loops < loop_count:
            print(f"[LOOP] 达到最大轮数 {max_loops}, 退出")
            break

        print(f"[LOOP] Round {loop_count} - 开始")
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
            print(f"[LOOP] Round {loop_count} - Brain 返回: status={dec.get('status')}, action={dec.get('action_to_openclaw','')[:50]}")
        except Exception as e:
            print(f"[LOOP] Round {loop_count} - Brain 错误: {e}")
            traceback.print_exc()
            brain_log.append({
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

        brain_log.append({
            "round": loop_count, "thought": thought, "observation": observation,
            "action": action, "update_memory": upd, "status": st,
        })

        if st == "need_input":
            # Brain 需要用户输入，暂停等待
            question = dec.get("question_for_user", thought) or "系统需要你的输入"
            print(f"[LOOP] Round {loop_count} - 需要用户输入: {question}")
            with state_lock:
                pending_question = question
            chat_history.append({"role": "sys", "text": question})
            event_queue.put(("status", f"Round {loop_count} - 等待用户输入..."))

            # 等待用户回复
            answer_event.clear()
            answer_event.wait(timeout=300)  # 最多等5分钟

            with state_lock:
                pending_question = ""
            if not user_answer:
                last_fb = "用户超时未回复"
            else:
                last_fb = f"用户回复: {user_answer}"
                print(f"[LOOP] Round {loop_count} - 用户回复: {user_answer}")
                user_answer = ""
            _wait(2)
            continue

        if st in ("blocked", "pause"):
            print(f"[LOOP] Round {loop_count} - 大脑要求停止: {st}")
            break
        if upd:
            mem.update_strategy(upd)
        if st == "milestone" and upd:
            mem.add_milestone(upd)

        if not action:
            last_fb = "大脑未给出指令"
            print(f"[LOOP] Round {loop_count} - 无指令，等待 {interval}s")
            _wait(interval)
            continue

        event_queue.put(("status", f"Round {loop_count} - 小龙虾执行中..."))
        print(f"[LOOP] Round {loop_count} - 调用 OpenClaw: {action[:60]}...")
        try:
            result = claw.execute(action)
            print(f"[LOOP] Round {loop_count} - OpenClaw 返回: success={result['success']}")
        except Exception as e:
            print(f"[LOOP] Round {loop_count} - OpenClaw 执行异常: {e}")
            traceback.print_exc()
            result = {"success": False, "content": f"执行异常: {e}"}

        claw_log.append({
            "round": loop_count, "instruction": action,
            "result": result["content"], "success": result["success"],
        })

        mem.add_action(action, result["content"], result["success"])
        last_fb = result["content"] if result["success"] else f"失败: {result['content']}"

        _wait(interval)

    with state_lock:
        system_running = False
    event_queue.put(("status", "已停止"))
    print("[LOOP] run_loop 结束")


def _wait(seconds):
    for _ in range(seconds):
        with state_lock:
            if not system_running:
                return
        time.sleep(1)


# ===================== FastAPI =====================

app = FastAPI(title="自主赚钱系统")


@app.get("/", response_class=HTMLResponse)
async def index():
    return build_html()


@app.post("/api/start")
async def api_start(req: Request):
    global system_running, loop_count, brain_log, claw_log
    with state_lock:
        if system_running:
            return JSONResponse({"error": "系统已在运行中"})
    try:
        body = await req.json()
    except Exception as e:
        print(f"[API] /api/start JSON 解析失败: {e}")
        return JSONResponse({"error": f"请求格式错误: {e}"})

    goal = body.get("goal", "").strip()
    agent = body.get("agent", "main")
    max_loops = int(body.get("max_loops", 10))
    interval = int(body.get("loop_interval", 15))

    if not goal:
        return JSONResponse({"error": "请输入目标"})

    if not BRAIN_API_KEY:
        return JSONResponse({"error": "未设置 BRAIN_API_KEY 环境变量。请在 .env 文件或系统环境中配置。"})

    # 健康检查
    try:
        import urllib.request
        urllib.request.urlopen(f"{OPENCLAW_GATEWAY_URL}/health", timeout=5)
        print(f"[API] OpenClaw Gateway 健康检查通过")
    except Exception as e:
        print(f"[API] OpenClaw Gateway 健康检查失败: {e}")
        return JSONResponse({"error": f"OpenClaw Gateway 离线: {e}。请先运行 openclaw gateway run --force"})

    with state_lock:
        system_running = True
        loop_count = 0
        brain_log = []
        claw_log = []
    while not event_queue.empty():
        event_queue.get_nowait()

    print(f"[API] 启动系统: goal={goal[:30]}..., agent={agent}, max_loops={max_loops}")
    t = threading.Thread(target=run_loop, args=(goal, agent, max_loops, interval), daemon=True)
    t.daemon = True
    t.start()
    print(f"[API] 后台线程已启动: {t.name}, is_alive={t.is_alive()}")
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

    # 消费事件
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

    status = f"运行中 - Round {lc}" if sr else "已停止"

    return JSONResponse({
        "brain": render_brain_entries(),
        "claw": render_claw_entries(),
        "memory": render_memory(),
        "status": status,
        "round": lc,
        "claw_count": len(claw_log),
        "chat_messages": ch,
        "has_question": bool(pq),
    })


# ===================== 入口 =====================

if __name__ == "__main__":
    print()
    print("  自主赚钱系统 - 控制台")
    print("  http://127.0.0.1:7860")
    print()
    uvicorn.run(app, host="127.0.0.1", port=7860, log_level="warning")
