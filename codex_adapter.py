"""Codex CLI adapter for ClawBrain.

This keeps Codex usage bounded: non-interactive, timed, logged, and limited to
the current project directory by default.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class CodexRunResult:
    success: bool
    content: str
    command: list[str]
    returncode: int | None = None
    duration_sec: float = 0.0
    output_file: str = ""


def find_codex_binary() -> str | None:
    env_bin = os.environ.get("CODEX_BIN", "").strip()
    if env_bin and Path(env_bin).exists():
        return env_bin

    candidates: list[str] = []
    which = shutil.which("codex")
    if which:
        candidates.append(which)

    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["where.exe", "codex"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            candidates.extend(line.strip() for line in proc.stdout.splitlines() if line.strip())
        except Exception:
            pass

    existing = [c for c in candidates if Path(c).exists()]
    for suffix in (".cmd", ".exe", ".ps1", ""):
        for path in existing:
            if path.lower().endswith(suffix):
                return path
    return existing[0] if existing else None


def codex_available() -> tuple[bool, str]:
    codex = find_codex_binary()
    if not codex:
        return False, "codex command not found"
    try:
        proc = subprocess.run(
            [codex, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        return False, f"codex check failed: {exc}"
    text = (proc.stdout or proc.stderr or "").strip()
    return proc.returncode == 0, text or f"exit={proc.returncode}"


def build_codex_prompt(task: str) -> str:
    return f"""请立即执行下面任务，不要只回复“收到”“明白”或行为承诺。

当前必须完成的任务：
{task}

执行要求：
- 只处理代码、测试、仓库、脚本、文档这类工程任务。
- 范围控制在当前项目。
- 能直接修就直接修，修完运行最相关的验证命令。
- 不启动长期前台服务。
- 不改无关文件。
- 不提交 git，除非任务明确要求。
- 如果任务要求只读，就只读，不写文件。
- 如果任务里指定了最终回复文本，最终必须按它回复。
- 最终回答不能只说“我会做”，必须说明已经做了什么。
- 最后用简短中文说明：做了什么、验证结果、还有什么风险。
"""


def run_codex_task(
    task: str,
    project_root: str | Path,
    *,
    timeout: int = 900,
    sandbox: str | None = None,
) -> CodexRunResult:
    if os.environ.get("CLAWBRAIN_CODEX_ENABLED", "1").lower() in {"0", "false", "no"}:
        return CodexRunResult(False, "Codex adapter disabled by CLAWBRAIN_CODEX_ENABLED=0", [])

    codex = find_codex_binary()
    if not codex:
        return CodexRunResult(False, "找不到 codex 命令，无法交给 Codex 执行。", [])

    root = Path(project_root).resolve()
    log_dir = root / "data" / "codex_runs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    output_file = log_dir / f"{stamp}-last-message.txt"
    run_log = log_dir / f"{stamp}-run.json"

    chosen_sandbox = sandbox or os.environ.get("CLAWBRAIN_CODEX_SANDBOX", "workspace-write")
    prompt = build_codex_prompt(task)
    cmd = [
        codex,
        "--ask-for-approval",
        "never",
        "exec",
        "--cd",
        str(root),
        "--sandbox",
        chosen_sandbox,
        "--color",
        "never",
        "--ephemeral",
        "--output-last-message",
        str(output_file),
        "-",
    ]

    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")

    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            env=env,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        duration = time.time() - start
    except subprocess.TimeoutExpired as exc:
        duration = time.time() - start
        result = CodexRunResult(
            False,
            f"Codex 执行超时（{timeout}秒）。stdout={_trim(exc.stdout)} stderr={_trim(exc.stderr)}",
            cmd,
            None,
            duration,
            str(output_file),
        )
        _write_run_log(run_log, result)
        return result
    except Exception as exc:
        duration = time.time() - start
        result = CodexRunResult(False, f"Codex 执行异常：{type(exc).__name__}: {exc}", cmd, None, duration, str(output_file))
        _write_run_log(run_log, result)
        return result

    final_message = ""
    if output_file.exists():
        try:
            final_message = output_file.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            final_message = ""

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    content_parts = []
    if final_message:
        content_parts.append(final_message)
    elif stdout:
        content_parts.append(_trim(stdout, 2500))

    debug = os.environ.get("CLAWBRAIN_CODEX_DEBUG", "0") == "1"
    if stderr and (proc.returncode != 0 or debug):
        content_parts.append("stderr:\n" + _trim(stderr, 1500))
    content = "\n\n".join(content_parts).strip() or "(Codex 无输出)"

    result = CodexRunResult(
        proc.returncode == 0,
        content[:6000],
        cmd,
        proc.returncode,
        duration,
        str(output_file),
    )
    _write_run_log(run_log, result)
    return result


def _trim(value: object, limit: int = 1000) -> str:
    if value is None:
        return ""
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
    return text[:limit]


def _write_run_log(path: Path, result: CodexRunResult) -> None:
    try:
        data = asdict(result)
        data["command"] = _redact_command(data.get("command", []))
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _redact_command(cmd: list[str]) -> list[str]:
    redacted = []
    for part in cmd:
        if "token" in part.lower() or "key" in part.lower():
            redacted.append("<redacted>")
        else:
            redacted.append(part)
    return redacted
