from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Callable


GATEWAY_PORT = 18789
MIN_NODE_VERSION = (22, 16, 0)


def port_open(port: int = GATEWAY_PORT, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _parse_version(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _node_version(node: str) -> tuple[int, int, int] | None:
    try:
        proc = subprocess.run(
            [node, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    return _parse_version((proc.stdout or proc.stderr or "").strip())


def _where_node() -> list[Path]:
    paths: list[Path] = []
    try:
        proc = subprocess.run(
            ["where.exe", "node"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line:
                paths.append(Path(line))
    except Exception:
        pass
    return paths


def _known_node_paths() -> list[Path]:
    userprofile = Path(os.environ.get("USERPROFILE", str(Path.home())))
    program_data = Path(os.environ.get("ProgramData", "C:/ProgramData"))
    paths: list[Path] = []

    env_node = os.environ.get("OPENCLAW_NODE")
    if env_node:
        paths.append(Path(env_node))

    paths.extend(_where_node())

    user_node_root = userprofile / ".workbuddy" / "binaries" / "node" / "versions"
    if user_node_root.exists():
        paths.extend(user_node_root.glob("*/node.exe"))

    chromium_root = program_data / "WorkBuddy" / "chromium-env"
    if chromium_root.exists():
        paths.extend(chromium_root.glob("*/.workbuddy/binaries/node/versions/*/node.exe"))

    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path).lower()
        if key not in seen and path.exists():
            seen.add(key)
            unique.append(path)
    return unique


def best_node() -> str | None:
    candidates: list[tuple[tuple[int, int, int], Path]] = []
    for path in _known_node_paths():
        version = _node_version(str(path))
        if version and version >= MIN_NODE_VERSION:
            candidates.append((version, path))
    if not candidates:
        return None
    candidates.sort(reverse=True, key=lambda item: item[0])
    return str(candidates[0][1])


def _openclaw_mjs_candidates() -> list[Path]:
    paths: list[Path] = []
    openclaw = shutil.which("openclaw")
    if openclaw:
        paths.append(Path(openclaw).resolve().parent / "node_modules" / "openclaw" / "openclaw.mjs")

    userprofile = Path(os.environ.get("USERPROFILE", str(Path.home())))
    program_data = Path(os.environ.get("ProgramData", "C:/ProgramData"))
    user_node_root = userprofile / ".workbuddy" / "binaries" / "node" / "versions"
    if user_node_root.exists():
        paths.extend(user_node_root.glob("*/node_modules/openclaw/openclaw.mjs"))

    chromium_root = program_data / "WorkBuddy" / "chromium-env"
    if chromium_root.exists():
        paths.extend(chromium_root.glob("*/.workbuddy/binaries/node/versions/*/node_modules/openclaw/openclaw.mjs"))

    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path).lower()
        if key not in seen and path.exists():
            seen.add(key)
            unique.append(path)
    return unique


def gateway_command() -> list[str] | None:
    node = best_node()
    for mjs in _openclaw_mjs_candidates():
        if node:
            return [node, str(mjs), "gateway", "run", "--force"]

    npx = shutil.which("npx")
    if npx:
        return [npx, "openclaw", "gateway", "run", "--force"]

    openclaw = shutil.which("openclaw")
    if openclaw:
        return [openclaw, "gateway", "run", "--force"]

    return None


def gateway_env() -> dict[str, str]:
    env = os.environ.copy()
    env["NODE_OPTIONS"] = ""
    env.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
    node = best_node()
    if node:
        env["PATH"] = str(Path(node).parent) + os.pathsep + env.get("PATH", "")
    return env


def ensure_gateway(
    cwd: str | Path,
    port: int = GATEWAY_PORT,
    max_wait: int = 30,
    log: Callable[[str], None] | None = None,
    creationflags: int = 0,
    stdout=None,
    stderr=None,
) -> bool:
    if port_open(port):
        if log:
            log(f"OpenClaw gateway already running on {port}")
        return True

    cmd = gateway_command()
    if not cmd:
        if log:
            log("OpenClaw gateway command not found")
        return False

    if log:
        log("Starting OpenClaw gateway: " + " ".join(cmd))

    subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=gateway_env(),
        creationflags=creationflags,
        stdout=stdout,
        stderr=stderr,
    )

    for _ in range(max_wait):
        time.sleep(1)
        if port_open(port):
            if log:
                log(f"OpenClaw gateway ready on {port}")
            return True

    if log:
        log(f"OpenClaw gateway start timeout after {max_wait}s")
    return False
