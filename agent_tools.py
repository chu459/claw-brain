"""System-level action tools for the ClawBrain loop.

These tools let Brain use structured prefixes instead of sending everything to
OpenClaw as plain browser instructions.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from action_router import build_action_router_context, execute_routed_action
from message_center import MessageCenter


@dataclass
class SystemActionResult:
    handled: bool
    success: bool
    content: str
    action_type: str = "system"


def build_system_tools_context(project_root: str | Path) -> str:
    root = Path(project_root)
    web_access = root / "workspace_templates" / "tools" / "web-access" / "SKILL.md"
    adapter_skill = root / "workspace_templates" / "skills" / "opencli-adapter-author" / "SKILL.md"

    lines = [
        "## 系统级工具协议",
        "当普通浏览器指令不合适时，action_to_openclaw 可以填写以下前缀，系统会拦截处理。",
        "",
        "1. [ADD_CARD:choice] title:问题 content:补充 options:选项A,选项B",
        "   用于让用户做关键选择。高风险、方向选择、缺少偏好时使用。",
        "2. [ADD_CARD:proposal] title:提案 content:原因 options:同意,调整,取消",
        "   用于主动提出改计划，不直接执行有风险的改动。",
        "3. [SPAWN_AGENT name:web-access] <任务>",
        "   用于网页调研、登录态页面、动态页面、需要看页面证据的任务。",
        "4. [CREATE_AGENT name:agent-name] <这个子Agent负责什么>",
        "   用于把反复出现的专业任务沉淀成可复用子Agent。",
        "5. [MEMORY_SEARCH] <关键词>",
        "   用于检索 OpenClaw 记忆。",
        "",
        "只允许一个 action 使用一个系统前缀。不要把多个前缀塞在同一条 action 里。",
    ]
    if web_access.exists():
        lines.append("web-access 模板已安装，可优先用于复杂网页任务。")
    if adapter_skill.exists():
        lines.append("opencli-adapter-author 模板已安装，可用于把网站能力沉淀成命令行适配器。")
    lines.append("")
    lines.append(build_action_router_context(root))
    return "\n".join(lines)


def handle_system_action(
    action: str,
    *,
    project_root: str | Path,
    message_center: MessageCenter,
    claw: Any,
    config: Any,
    loop_count: int,
) -> SystemActionResult:
    text = (action or "").strip()
    if not text.startswith("["):
        return SystemActionResult(False, False, "")

    if text.startswith("[ADD_CARD:"):
        return _handle_add_card(text, message_center)
    if text.startswith("[CREATE_AGENT"):
        return _handle_create_agent(text, Path(project_root))
    if text.startswith("[SPAWN_AGENT"):
        return _handle_spawn_agent(text, Path(project_root), claw, config, loop_count)
    if text.startswith("[MEMORY_SEARCH]"):
        query = text[len("[MEMORY_SEARCH]"):].strip()
        return _handle_memory_search(query, Path(project_root))
    if text.startswith("[CODEX]") or text.startswith("[LOCAL_CMD]") or text.startswith("[SHELL]") or text.startswith("[RUN_CMD]"):
        result = execute_routed_action(
            text,
            project_root=project_root,
            claw=claw,
            timeout=180,
        )
        return SystemActionResult(True, bool(result.get("success")), str(result.get("content", "")), str(result.get("route", "router")))

    return SystemActionResult(
        True,
        False,
        f"未知系统前缀：{text[:80]}。请改用普通 OpenClaw 指令或已支持的系统前缀。",
        "unknown_prefix",
    )


def _handle_add_card(text: str, center: MessageCenter) -> SystemActionResult:
    m = re.match(r"\[ADD_CARD:(\w+)\](.*)", text, flags=re.S)
    if not m:
        return SystemActionResult(True, False, "ADD_CARD 格式错误", "message_card")
    card_type = m.group(1).strip()
    body = m.group(2).strip()
    fields = _parse_loose_fields(body)
    title = fields.get("title") or "需要你确认"
    content = fields.get("content") or body[:500]
    options = _split_options(fields.get("options", ""))
    required = card_type in {"choice", "proposal", "plan", "fill"}
    card = center.add_card(
        card_type=card_type,
        title=title,
        content=content,
        options=options,
        required=required,
        timeout=0,
        priority="high" if required else "normal",
        source="brain",
    )
    return SystemActionResult(
        True,
        True,
        f"已创建消息卡片 {card.id}：{card.title}。等待用户回答后继续。",
        "message_card",
    )


def _handle_create_agent(text: str, project_root: Path) -> SystemActionResult:
    m = re.match(r"\[CREATE_AGENT\s+name:([\w-]+)\](.*)", text, flags=re.S)
    if not m:
        return SystemActionResult(True, False, "CREATE_AGENT 格式错误，应为 [CREATE_AGENT name:xxx] 描述", "agent")
    name = m.group(1).strip()
    description = m.group(2).strip() or f"{name} 子Agent"
    agents_dir = project_root / "data" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    agent_file = agents_dir / f"{name}.md"
    if agent_file.exists():
        return SystemActionResult(True, True, f"子Agent 已存在：{name}", "agent")
    content = f"""# {name}

{description}

## 工作方式
- 先理解任务目标。
- 给出可执行结果，不写空话。
- 失败时说明原因和下一步。

## 输出
用简洁中文输出：
【结果】实际结果
【完成】
"""
    agent_file.write_text(content, encoding="utf-8")
    return SystemActionResult(True, True, f"已创建子Agent：{name}", "agent")


def _handle_spawn_agent(
    text: str,
    project_root: Path,
    claw: Any,
    config: Any,
    loop_count: int,
) -> SystemActionResult:
    m = re.match(r"\[SPAWN_AGENT\s+name:([\w-]+)\](.*)", text, flags=re.S)
    if not m:
        return SystemActionResult(True, False, "SPAWN_AGENT 格式错误，应为 [SPAWN_AGENT name:xxx] 任务", "agent")
    name = m.group(1).strip()
    task = m.group(2).strip() or "执行当前子任务"
    prompt = _load_agent_prompt(project_root, name)
    message = f"""你是 ClawBrain 子Agent：{name}

你的说明：
{prompt or "无专门说明，按通用执行助手工作。"}

父任务：
{task}

要求：
- 只做这个子任务，不扩展成大项目。
- 优先给证据、路径、结果。
- 输出用：
【结果】...
【完成】
"""
    try:
        result = claw.execute(message, timeout=180)
    except Exception as e:
        result = {"success": False, "content": f"子Agent 执行异常: {e}"}

    _record_agent_task(project_root, name, task, result, loop_count)
    status = "完成" if result.get("success") else "失败"
    return SystemActionResult(
        True,
        bool(result.get("success")),
        f"子Agent {name} {status}：{str(result.get('content', ''))[:2000]}",
        "agent",
    )


def _handle_memory_search(query: str, project_root: Path) -> SystemActionResult:
    if not query:
        return SystemActionResult(True, False, "MEMORY_SEARCH 缺少关键词", "memory")
    cmd = _get_openclaw_cmd(project_root) + [
        "memory", "search", "--query", query, "--json", "--max-results", "5",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "NODE_OPTIONS": ""},
        )
        if result.returncode == 0:
            return SystemActionResult(True, True, result.stdout.strip()[:3000] or "没有检索结果", "memory")
        return SystemActionResult(True, False, (result.stderr or result.stdout or "记忆检索失败")[:1000], "memory")
    except Exception as e:
        return SystemActionResult(True, False, f"记忆检索异常: {e}", "memory")


def _parse_loose_fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for key in ("title", "content", "options"):
        m = re.search(rf"{key}\s*:\s*(.*?)(?=\s+(?:title|content|options)\s*:|$)", body, flags=re.I | re.S)
        if m:
            fields[key] = m.group(1).strip()
    return fields


def _split_options(raw: str) -> list[str]:
    if not raw:
        return []
    return [x.strip().strip("\"'") for x in re.split(r"[,，/|]", raw) if x.strip()]


def _load_agent_prompt(project_root: Path, name: str) -> str:
    candidates = [
        project_root / "data" / "agents" / f"{name}.md",
        project_root / "workspace_templates" / "agents" / f"{name}.md",
    ]
    if name == "web-access":
        candidates.append(project_root / "workspace_templates" / "tools" / "web-access" / "SKILL.md")
    if name == "opencli-adapter-author":
        candidates.append(project_root / "workspace_templates" / "skills" / "opencli-adapter-author" / "SKILL.md")
    for path in candidates:
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8")
                return text[:6000]
            except Exception:
                continue
    return ""


def _record_agent_task(project_root: Path, name: str, task: str, result: dict[str, Any], loop_count: int) -> None:
    data_dir = project_root / "data" / "agent_tasks"
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{int(time.time())}_{name}.json"
    payload = {
        "name": name,
        "task": task,
        "loop_count": loop_count,
        "success": bool(result.get("success")),
        "content": str(result.get("content", ""))[:5000],
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    path.write_text(__import__("json").dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_openclaw_cmd(project_root: Path) -> list[str]:
    try:
        from autonomous_system import _build_direct_openclaw_cmd

        direct = _build_direct_openclaw_cmd()
        if direct:
            return direct
    except Exception:
        pass
    openclaw = shutil.which("openclaw")
    if openclaw:
        return [openclaw]
    npx = shutil.which("npx")
    if npx:
        return [npx, "openclaw"]
    return ["openclaw"]
