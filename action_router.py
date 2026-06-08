"""Route ClawBrain actions to the right execution backend."""

from __future__ import annotations

import os
import re
import subprocess
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from codex_adapter import codex_available, run_codex_task
from gateway_runtime import ensure_gateway


@dataclass
class ActionRoute:
    route: str
    payload: str
    reason: str


@dataclass
class ActionExecutionResult:
    success: bool
    content: str
    route: str

    def as_dict(self) -> dict[str, Any]:
        return {"success": self.success, "content": self.content, "route": self.route}


CODEX_PREFIX = "[CODEX]"
LOCAL_PREFIXES = ("[LOCAL_CMD]", "[SHELL]", "[RUN_CMD]")


def build_action_router_context(project_root: str | Path) -> str:
    codex_ok, codex_info = codex_available()
    gateway_hint = "unknown"
    try:
        from gateway_runtime import port_open

        gateway_hint = "online" if port_open(18789) else "offline"
    except Exception:
        pass
    return f"""## 动作路由规则
系统现在不是所有动作都交给 OpenClaw。
- 代码、测试、仓库、脚本、文档、自我修复：优先填 `[CODEX] <任务>`。
- 简短本地命令：填 `[LOCAL_CMD] <命令>`，例如 `[LOCAL_CMD] python -m py_compile core.py`。
- 浏览器、手机、平台发布、登录页面操作：继续填普通动作，系统会交给 OpenClaw。
- 需要用户确认：用 `[ADD_CARD:choice]` 或 status=need_input。

当前能力状态：
- Codex: {"online" if codex_ok else "offline"} ({codex_info})
- OpenClaw Gateway: {gateway_hint}
"""


def classify_action(action: str) -> ActionRoute:
    text = (action or "").strip()
    if not text:
        return ActionRoute("none", "", "empty")

    upper = text.upper()
    if upper.startswith(CODEX_PREFIX):
        return ActionRoute("codex", text[len(CODEX_PREFIX) :].strip(), "explicit_codex")
    for prefix in LOCAL_PREFIXES:
        if upper.startswith(prefix):
            return ActionRoute("local_cmd", text[len(prefix) :].strip(), "explicit_local_cmd")

    command = _extract_command(text)
    if command:
        return ActionRoute("local_cmd", command, "command_like")

    if _looks_browser_or_phone(text):
        return ActionRoute("openclaw", text, "browser_or_phone")

    if _looks_engineering_task(text):
        return ActionRoute("codex", text, "engineering_task")

    return ActionRoute("openclaw", text, "default_openclaw")


def execute_routed_action(
    action: str,
    *,
    project_root: str | Path,
    claw: Any | None = None,
    emit: Callable[[str, str], None] | None = None,
    lock: Any | None = None,
    timeout: int = 180,
) -> dict[str, Any]:
    route = classify_action(action)
    root = Path(project_root)

    if route.route == "none":
        return ActionExecutionResult(False, "没有可执行动作。", "none").as_dict()

    if route.route == "codex":
        if emit:
            emit("status", "Codex 正在执行工程任务...")
        result = run_codex_task(route.payload, root, timeout=int(os.environ.get("CLAWBRAIN_CODEX_TIMEOUT", "900")))
        prefix = "[Codex]"
        return ActionExecutionResult(result.success, f"{prefix} {result.content}", "codex").as_dict()

    if route.route == "local_cmd":
        if emit:
            emit("status", "本地命令正在执行...")
        return _run_local_command(route.payload, root, timeout=timeout).as_dict()

    if route.route == "openclaw":
        if claw is None:
            return ActionExecutionResult(False, "OpenClaw 客户端不存在。", "openclaw").as_dict()
        if not _ensure_openclaw_ready(claw, root):
            return ActionExecutionResult(
                False,
                "OpenClaw Gateway 离线，且自动启动失败。代码/测试类任务可改用 [CODEX]，简短命令可改用 [LOCAL_CMD]。",
                "openclaw",
            ).as_dict()
        if emit:
            emit("status", "OpenClaw 正在执行浏览器/手机动作...")
        context = lock if lock is not None else nullcontext()
        try:
            with context:
                result = claw.execute(route.payload)
        except Exception as exc:
            return ActionExecutionResult(False, f"OpenClaw 执行异常：{type(exc).__name__}: {exc}", "openclaw").as_dict()
        return {"success": bool(result.get("success")), "content": result.get("content", ""), "route": "openclaw"}

    return ActionExecutionResult(False, f"未知路由：{route.route}", route.route).as_dict()


def _extract_command(text: str) -> str:
    patterns = [
        r"^(?:运行|执行|测试)命令[:：]\s*(.+)$",
        r"^(?:run|exec|shell|cmd)[:：]\s*(.+)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, text, flags=re.I | re.S)
        if match:
            return match.group(1).strip()

    first_word = text.split(maxsplit=1)[0].lower() if text.split() else ""
    if first_word in {"python", "py", "pytest", "git", "npm", "node", "pnpm", "uvicorn"}:
        return text
    return ""


def _looks_engineering_task(text: str) -> bool:
    lower = text.lower()
    keywords = [
        "代码",
        "仓库",
        "测试",
        "修复",
        "bug",
        "编译",
        "脚本",
        "文件",
        "模块",
        "接口",
        "函数",
        "重构",
        "提交",
        "git",
        "pytest",
        "py_compile",
        "npm",
        "readme",
        "文档",
        "codex",
    ]
    return any(k in lower for k in keywords)


def _looks_browser_or_phone(text: str) -> bool:
    lower = text.lower()
    keywords = [
        "打开",
        "点击",
        "输入",
        "浏览器",
        "网页",
        "页面",
        "登录",
        "截图",
        "手机",
        "发布",
        "抖音",
        "闲鱼",
        "小红书",
        "淘宝",
        "chrome",
        "url",
        "http",
    ]
    return any(k in lower for k in keywords)


def _run_local_command(command: str, root: Path, timeout: int = 180) -> ActionExecutionResult:
    command = command.strip()
    if not command:
        return ActionExecutionResult(False, "LOCAL_CMD 缺少命令。", "local_cmd")
    if _is_dangerous_command(command):
        return ActionExecutionResult(False, f"本地命令被安全策略拦截：{command[:120]}", "local_cmd")

    try:
        proc = subprocess.run(
            command,
            cwd=str(root),
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return ActionExecutionResult(False, f"本地命令超时（{timeout}秒）：{command[:120]}", "local_cmd")
    except Exception as exc:
        return ActionExecutionResult(False, f"本地命令异常：{type(exc).__name__}: {exc}", "local_cmd")

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    content = "\n".join(part for part in [stdout, stderr] if part).strip() or "(命令无输出)"
    return ActionExecutionResult(proc.returncode == 0, f"[LOCAL_CMD] {content[:5000]}", "local_cmd")


def _is_dangerous_command(command: str) -> bool:
    if os.environ.get("CLAWBRAIN_ALLOW_DANGEROUS_LOCAL_CMD", "0") == "1":
        return False
    lower = command.lower()
    dangerous = [
        "remove-item",
        " rm -rf",
        "del /s",
        "rmdir /s",
        "format ",
        "shutdown",
        "restart-computer",
        "git reset --hard",
        "git clean -fd",
        "taskkill /f /im",
    ]
    return any(item in f" {lower}" for item in dangerous)


def _ensure_openclaw_ready(claw: Any, root: Path) -> bool:
    try:
        if claw.check_health():
            return True
    except Exception:
        pass
    try:
        ensure_gateway(root, max_wait=20)
    except Exception:
        pass
    try:
        return bool(claw.check_health())
    except Exception:
        return False
