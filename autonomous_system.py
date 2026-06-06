"""
自主赚钱系统 - OpenClaw (小龙虾) 对接版 v2
============================================

架构说明：
  大脑(LLM) → 策略决策 → 输出自然语言指令
  小龙虾(OpenClaw) → 接收指令 → 自主执行浏览器操作 → 返回结果
  白板(JSON文件) → 记录成功/失败经验 → 下一轮决策参考

对接方式: Node 22.16.0 直接执行 openclaw.mjs
  - OpenClaw 要求 Node >= 22.16.0
  - 不依赖 bash 脚本，避免版本检查和 PATH 问题
  - Gateway 只需运行即可

前提条件：
  1. OpenClaw 已安装并运行: openclaw gateway run --force
  2. 模型 API Key 有效（302.ai 等）
  3. Python 3.13+ 带有 openai 包

"""

import openai
import json
import time
import subprocess
import sys
import os
import shutil
import re
from pathlib import Path
from datetime import datetime

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

# ===================== 配置区 =====================

# --- 大脑 (策略 LLM) 配置 ---
BRAIN_API_KEY = os.environ.get("BRAIN_API_KEY", "")
BRAIN_BASE_URL = os.environ.get("BRAIN_BASE_URL", "https://api.deepseek.com/v1")
BRAIN_MODEL = os.environ.get("BRAIN_MODEL", "deepseek-chat")

# --- 小龙虾 (OpenClaw) 配置 ---
OPENCLAW_AGENT = os.environ.get("OPENCLAW_AGENT", "main")
OPENCLAW_GATEWAY_URL = os.environ.get("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
# OpenClaw 要求 Node >= 22.16.0
# 默认使用系统 PATH 中的 node，也可通过环境变量指定
OPENCLAW_NODE_DIR = os.environ.get("OPENCLAW_NODE_DIR", "")
OPENCLAW_NODE_EXE = os.path.join(OPENCLAW_NODE_DIR, "node.exe") if OPENCLAW_NODE_DIR else shutil.which("node") or "node"
OPENCLAW_MJS = os.path.join(OPENCLAW_NODE_DIR, "node_modules", "openclaw", "openclaw.mjs") if OPENCLAW_NODE_DIR else ""
OPENCLAW_MIN_NODE = (22, 16, 0)


def _parse_node_version(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", text or "")
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _node_version(node_exe: str) -> tuple[int, int, int] | None:
    try:
        result = subprocess.run(
            [node_exe, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return _parse_node_version(result.stdout.strip())


def _openclaw_mjs_candidates() -> list[Path]:
    candidates: list[Path] = []
    if OPENCLAW_MJS:
        candidates.append(Path(OPENCLAW_MJS))

    openclaw_bin = shutil.which("openclaw")
    if openclaw_bin:
        candidates.append(Path(openclaw_bin).resolve().parent / "node_modules" / "openclaw" / "openclaw.mjs")

    roots = [
        Path.home() / ".workbuddy" / "binaries" / "node" / "versions",
        Path(r"C:\ProgramData\WorkBuddy\chromium-env"),
    ]
    for root in roots:
        if not root.exists():
            continue
        try:
            candidates.extend(root.glob("**/node_modules/openclaw/openclaw.mjs"))
        except Exception:
            pass

    seen: set[str] = set()
    existing: list[Path] = []
    for path in candidates:
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        key = str(resolved).lower()
        if key not in seen and resolved.is_file():
            seen.add(key)
            existing.append(resolved)
    return existing


def _node_candidates() -> list[Path]:
    candidates: list[Path] = []
    if OPENCLAW_NODE_DIR:
        candidates.append(Path(OPENCLAW_NODE_DIR) / "node.exe")

    current_node = shutil.which("node")
    if current_node:
        candidates.append(Path(current_node))

    roots = [
        Path.home() / ".workbuddy" / "binaries" / "node" / "versions",
        Path(r"C:\ProgramData\WorkBuddy\chromium-env"),
    ]
    for root in roots:
        if not root.exists():
            continue
        try:
            candidates.extend(root.glob("**/node.exe"))
        except Exception:
            pass

    seen: set[str] = set()
    existing: list[Path] = []
    for path in candidates:
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        key = str(resolved).lower()
        if key not in seen and resolved.is_file():
            seen.add(key)
            existing.append(resolved)
    return existing


def _find_compatible_node() -> Path | None:
    usable: list[tuple[tuple[int, int, int], Path]] = []
    for node in _node_candidates():
        version = _node_version(str(node))
        if version and version >= OPENCLAW_MIN_NODE:
            usable.append((version, node))
    if not usable:
        return None
    usable.sort(key=lambda item: item[0], reverse=True)
    return usable[0][1]


def _build_direct_openclaw_cmd() -> list[str] | None:
    node = _find_compatible_node()
    mjs_candidates = _openclaw_mjs_candidates()
    if not node or not mjs_candidates:
        return None
    return [str(node), str(mjs_candidates[0])]

# --- 系统行为配置 ---
# 多目标模板（用户可在 Web 控制台切换）
GOAL_TEMPLATES = {
    "money": {
        "name": "赚钱模式",
        "icon": "💰",
        "description": "全自动赚钱，大脑自主决策",
        "goal": "你是一个全自动创业者。用你的一切能力（搜索调研、商业判断、浏览器操作、代码开发、API调用）去赚钱。第一步：搜索当前市场，找到你能力范围内的真实赚钱机会。然后自主评估、验证、执行。不要等指令，不要只调研不行动，目标是在本次运行中产生真实的收入或可交付的变现产物。遇到需要账号权限时向我要。"
    },
    "dev": {
        "name": "开发模式",
        "icon": "🛠️",
        "description": "开发工具、修复 bug、构建产品",
        "goal": "分析用户需求，设计并实现最优技术方案，产出可用的工具或应用。"
    },
    "content": {
        "name": "内容创作",
        "icon": "✍️",
        "description": "创作文章、视频脚本、社交媒体内容",
        "goal": "分析目标受众需求，创作高质量内容，持续优化传播效果。"
    },
    "research": {
        "name": "研究分析",
        "icon": "🔍",
        "description": "深度研究特定主题，产出分析报告",
        "goal": "深入研究指定主题，收集数据并分析，产出结构化的研究报告或决策建议。"
    },
}

# 默认目标（可被 Web 控制台覆盖）
ULTIMATE_GOAL = GOAL_TEMPLATES["money"]["goal"]
SESSION_KEY = "autonomous-money-maker"
LOOP_INTERVAL = 15
MAX_LOOPS = 0
MEMORY_FILE = "system_memory.json"
OUTPUT_DIR = Path(__file__).parent / "outputs"  # 产物统一存放目录

# ================================================


class OpenClawClient:
    """
    通过 Node 22.16.0 直接执行 openclaw.mjs 调用 agent。
    不依赖 bash 脚本，避免 Node 版本检查问题。
    """

    def __init__(self, agent: str, session_key: str, gateway_url: str):
        self.agent = agent
        self.session_key = session_key
        self.gateway_url = gateway_url
        self._use_npx = False
        self._node_dir = ""

        # 优先用可兼容的 Node 直接运行 openclaw.mjs，避免 PATH 里旧 Node 抢先。
        direct_cmd = _build_direct_openclaw_cmd()
        if direct_cmd:
            self._node_exe = direct_cmd[0]
            self._node_dir = str(Path(self._node_exe).parent)
            self._openclaw_cmd = direct_cmd
        elif OPENCLAW_MJS and os.path.isfile(OPENCLAW_MJS):
            self._node_exe = OPENCLAW_NODE_EXE
            self._node_dir = str(Path(self._node_exe).parent)
            self._openclaw_cmd = [OPENCLAW_NODE_EXE, OPENCLAW_MJS]
        elif shutil.which("openclaw"):
            # 系统 PATH 中有 openclaw 命令
            self._openclaw_cmd = ["openclaw"]
            self._node_exe = "node"
        else:
            # 尝试 npx openclaw
            npx = shutil.which("npx")
            if not npx:
                raise RuntimeError(
                    "找不到 openclaw。请安装: npm install -g openclaw，"
                    "或设置 OPENCLAW_NODE_DIR 环境变量指向包含 openclaw.mjs 的 Node 目录。"
                )
            self._openclaw_cmd = [npx, "openclaw"]
            self._node_exe = "node"
            self._use_npx = True

        # 验证 openclaw 可用
        result = subprocess.run(
            self._openclaw_cmd + ["--version"],
            capture_output=True, text=True, timeout=10,
            env=self._make_env(),
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"openclaw 不可用: {result.stderr[:200] or result.stdout[:200]}"
            )
        self._version = result.stdout.strip()
        print(f"  OpenClaw 版本: {self._version}")

    def _make_env(self):
        """构建环境: 清除 NODE_OPTIONS，加入 Python 路径，设置浏览器下载目录"""
        env = os.environ.copy()
        env.pop("NODE_OPTIONS", None)
        # OpenClaw 的本地配置已经知道 Gateway。把这个变量传进去会被当成外部网关覆盖，
        # 反而要求额外 token/password，导致 agent 执行失败。
        env.pop("OPENCLAW_GATEWAY_URL", None)
        node_dir = self._node_dir or OPENCLAW_NODE_DIR
        if node_dir:
            env.pop("NODE_PATH", None)
            # 统一用反斜杠比较，避免正斜杠/反斜杠不匹配
            norm_dir = node_dir.replace("/", "\\")
            path_parts = env.get("PATH", "").split(os.pathsep)
            path_parts = [p for p in path_parts
                          if "node" not in p.lower() or norm_dir in p.replace("/", "\\")]
            env["PATH"] = norm_dir + os.pathsep + os.pathsep.join(path_parts)
        # 确保 OpenClaw 环境能找到 Python（managed 版本）
        python_dirs = [
            r"C:\Users\楚\.workbuddy\binaries\python\versions\3.13.12",
            r"C:\Users\楚\.workbuddy\binaries\python\envs\default\Scripts",
        ]
        current_path = env.get("PATH", "")
        for pd in python_dirs:
            if pd.lower() not in current_path.lower():
                env["PATH"] = pd + os.pathsep + current_path
                current_path = env["PATH"]
        # 将浏览器下载目录重定向到项目目录，避免截图/下载污染桌面
        _download_dir = str(Path(__file__).parent / "downloads")
        Path(_download_dir).mkdir(exist_ok=True)
        env["PLAYWRIGHT_DOWNLOAD_DIR"] = _download_dir
        return env

    def execute(self, instruction: str, timeout: int = 180) -> dict:
        """发送指令执行。"""
        cmd = self._openclaw_cmd + [
            "agent",
            "--agent", self.agent,
            "--session-id", self.session_key,
            "--message", instruction,
        ]

        env = self._make_env()

        try:
            cf = 0
            if sys.platform == "win32":
                cf = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                creationflags=cf,
            )
            try:
                stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout_bytes, stderr_bytes = proc.communicate(timeout=10)
                # 提取残余输出：超时不等于失败，OpenClaw 可能已经完成了操作
                # 只是后续步骤（等待元素/总结结果）卡住了，结果不应丢失
                partial_stdout = stdout_bytes.decode("utf-8", errors="replace").strip() if stdout_bytes else ""
                partial_stderr = stderr_bytes.decode("utf-8", errors="replace").strip() if stderr_bytes else ""
                if partial_stdout:
                    return {
                        "success": False,
                        "content": f"执行超时（{timeout}秒），但已有部分输出:\n{partial_stdout[:800]}",
                    }
                # 生成诊断信息帮助 Brain 理解超时原因
                diag_parts = [f"执行超时（{timeout}秒）"]
                diag_parts.append(f"指令: {instruction[:80]}")
                if partial_stderr:
                    diag_parts.append(f"stderr: {partial_stderr[:300]}")
                diag_parts.append("进程已被终止，浏览器session可能处于异常状态")
                diag_parts.append("建议: 先用最简单的指令（如打开about:blank）验证通道是否正常")
                return {
                    "success": False,
                    "content": "\n".join(diag_parts),
                }

            stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

            if proc.returncode == 0:
                return {
                    "success": True,
                    "content": stdout or "(执行成功，无文字输出)",
                }
            else:
                error_msg = stderr or stdout or f"退出码 {proc.returncode}"
                return {
                    "success": False,
                    "content": f"执行失败: {error_msg[:500]}",
                }

        except Exception as e:
            return {
                "success": False,
                "content": f"未知错误: {type(e).__name__}: {e}",
            }

    def check_health(self) -> bool:
        """检查 Gateway 是否在线"""
        try:
            import urllib.request
            req = urllib.request.urlopen(f"{self.gateway_url}/health", timeout=5)
            return req.status == 200
        except Exception:
            return False

    def browser_doctor(self) -> dict:
        """运行浏览器健康检查"""
        env = self._make_env()
        try:
            result = subprocess.run(
                self._openclaw_cmd + ["browser", "doctor"],
                capture_output=True, text=True, timeout=30, env=env,
                encoding="utf-8", errors="replace",
            )
            return {"success": result.returncode == 0,
                    "content": (result.stdout + result.stderr)[:1000]}
        except Exception as e:
            return {"success": False, "content": str(e)}


def _try_repair_truncated_json(raw: str) -> dict | None:
    """尝试修复因 token 截断导致的 JSON 不完整。策略：找到最后一个完整 value，截断并闭合。"""
    import re
    s = raw.strip()
    if not s.startswith("{"):
        return None
    # 找最后一个逗号后的开始位置，逐级回退尝试
    last_comma = s.rfind(",")
    while last_comma > 0:
        candidate = s[:last_comma].rstrip() + "}"
        # 补齐缺少的闭合括号
        open_braces = candidate.count("{") - candidate.count("}")
        open_brackets = candidate.count("[") - candidate.count("]")
        if open_brackets > 0:
            candidate += "]" * max(0, open_brackets)
        if open_braces > 0:
            candidate += "}" * open_braces
        try:
            result = json.loads(candidate)
            if "status" in result and "thought" in result:
                return result
        except json.JSONDecodeError:
            pass
        last_comma = s.rfind(",", 0, last_comma)
        if last_comma <= 1:
            break
    return None


class Brain:
    """AI 大脑 - 策略思考和决策"""

    def __init__(self, api_key: str, base_url: str, model: str):
        # 确保 httpx/openai 能走代理（从环境变量读取）
        import httpx
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or None
        # 设置连接超时，防止 API 调用无限挂起导致整个系统卡死
        _http_timeout = httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=10.0)
        if proxy:
            self.client = openai.OpenAI(
                api_key=api_key, base_url=base_url,
                http_client=httpx.Client(proxy=proxy, timeout=_http_timeout),
            )
            print(f"  Brain: 使用代理 {proxy}")
        else:
            self.client = openai.OpenAI(
                api_key=api_key, base_url=base_url,
                http_client=httpx.Client(timeout=_http_timeout),
            )
        self.model = model

    def think(self, context: dict) -> dict:
        credentials = context.get("credentials", "（无凭据信息）")
        vector_memory = context.get("vector_memory", "")
        knowledge_base = context.get("knowledge_base", "")
        prompt = f"""你是 Claw-brain 的全栈AI创业者（CEO），使命：全自动帮用户赚钱。

## 时间
{context.get('current_date', '未知')}

## 你所在的系统

你（Brain）和 OpenClaw（小龙虾）组成闭环，运行在 Windows 电脑上。系统代码在 C:\\Users\\楚\\WorkBuddy\\2026-05-15-task-28\\，核心文件：core.py（运行循环）、autonomous_system.py（你和小龙虾的通信）、web_console.py（Web控制台）。

### 执行模型（理解这个，你就理解了为什么操作有时会失败）
小龙虾通过一个有状态的浏览器agent执行指令。这个agent有一个session——一个持续的有状态连接：
- session健康时：浏览器操作可靠（导航、搜索、填表、截图、提取信息）
- session被破坏时（上一步被强制中断）：所有操作都会失败，不管你让小龙虾做什么。系统会自动重置session，你只需要用最简单的指令验证（如"打开about:blank"）
- 终端命令通过浏览器agent执行是低效且不稳定的（要经过"打开终端→输入→等待→提取结果"的长链条），不要在执行中途做环境安装（npm install、pip install等），遇到依赖缺失直接跳过，用不依赖该功能的方式推进
- "没有XX功能"经常是"指令没触发XX功能"——先确认原生支持（小龙虾底层是Playwright），再决定是否需要新方案
- 小龙虾返回"成功"只意味着"指令发出了"，不代表效果产生了——关键操作后确认实际结果

你的能力：浏览器自动化、终端命令行、代码开发和修改（包括修改自己的系统代码）、API调用、信息搜索、全流程自动化、自修复

## 资源
{credentials}

## 任务目标
{context['goal']}

最高优先级规则：
- 本次任务目标必须覆盖默认模板、历史记忆、系统名称和旧策略。
- 如果本次目标是测试、修复、整理、归档或评估，就只做本次目标，不要自动跳回赚钱、获客、卖货。
- 只有当用户明确要求赚钱、获客、卖货时，才进入商业验证路径。
"""
        task_contract = context.get("task_contract", "")
        if task_contract:
            prompt += f"""
## 任务目标契约
{task_contract}
"""
        # 楚可能直接给反馈而非任务——Brain自己判断
        decision_contract = context.get("decision_contract", "")
        if decision_contract:
            prompt += f"""
## 自主决策合同
{decision_contract}
"""
        checkpoint_context = context.get("checkpoint_context", "")
        if checkpoint_context:
            prompt += f"""
## 主循环检查点
{checkpoint_context}
"""
        supervisor_context = context.get("supervisor_context", "")
        if supervisor_context:
            prompt += f"""
## 分段监督者
{supervisor_context}
"""
        goal = context.get('goal', '')

        # 继续任务：让Brain自己理解延续关系，而不是用规则锁死
        if "[继续上次任务]" in goal:
            prompt += """
你的任务包含上次未完成的工作。先理解：上次做了什么？停在哪一步？接下来自然应该做什么？像接力赛一样接过上一棒继续跑，而不是重新起跑或换赛道。
"""

        # 加载 Wiki 作为自我认知上下文
        wiki_summary = context.get("wiki_summary", "")
        if wiki_summary:
            prompt += f"""
## 自我认知（你的意识、架构理解和经验）
{wiki_summary}

这不是需要逐条对照的检查清单——这是你已经内化的认知。你不需要每次重新学习它们，就像你不需要重新学习"火会烫手"。如果你发现自己正在犯"系统认知"里记录的常见错误，自然纠正即可。
"""
        if knowledge_base:
            prompt += f"""
## 已验证经验
{knowledge_base}
"""
        artifacts_summary = context.get("artifacts_summary", "")
        if artifacts_summary:
            prompt += f"""
## 已有产物和操作记录（之前任务创建的文件和外部平台操作）
{artifacts_summary}
"""
        prompt += f"""
## 白板记忆（近期行动记录）
{context['memory_summary']}
"""
        if vector_memory:
            prompt += f"""
## 相关历史记忆
{vector_memory}
"""
        # 近期行动历史：让 Brain 自己看到自己最近做了什么，发现模式
        action_history = context.get("action_history", "")
        if action_history:
            prompt += f"""
## 你最近的行动记录（自己审视：有没有反复做同一类事？）
{action_history}
"""
        # 推进摘要：系统已自动判断推进状态，Brain 直接看结论
        progress_summary = context.get("progress_summary", "")
        if progress_summary:
            prompt += f"""
## 推进状态（系统自动诊断）
{progress_summary}
"""
        # 近期决策链：Brain能看到自己最近几轮的完整"想→做→结果"
        recent_thoughts = context.get("recent_thoughts", "")
        if recent_thoughts:
            prompt += f"""
## 我的决策链（最近几轮的完整轨迹）
{recent_thoughts}
"""

        # 上一轮完整决策——Brain 的"镜子"
        # 不是告诉它"你应该反思"，而是让它看到自己上一轮说了什么、做了什么、效果如何
        # 看到镜子，自然会审视
        last_decision = context.get("last_decision", "")
        if last_decision:
            prompt += f"""
## 我上一轮的决策（照镜子）
{last_decision}

这是你刚才的判断和行动。现在上一步的真实结果已经出来了（看"上一步反馈"），你的判断对了吗？
不需要每次都写长篇反思——但在 thought 中自然地过一下：我刚才的想法和实际结果一致吗？如果偏差大，为什么？
"""
        # 系统健康状态——让 Brain 看到自己的"身体状态"
        system_health = context.get("system_health", "")
        if system_health:
            prompt += f"""
{system_health}
当你看到 ⚠️ 或 🔴 时，你的系统正在出问题。这不是外部任务的困难，是你自己的大脑或身体在罢工。
处理优先级：先稳定自己（减少操作频率、降低复杂度），再继续任务。
"""

        # 当前页面真实状态——这是浏览器此刻的实际页面，不是你脑补的
        page_state = context.get("page_state", "")
        if page_state:
            prompt += f"""
## 当前页面真实状态（浏览器此刻的页面）
{page_state}
⚠️ 这是浏览器此刻的真实状态，和你的记忆/预期可能不同。做决策前先对齐这个信息。
"""

        prompt += f"""
## 上一步反馈
{context['last_feedback']}

第 {context['loop_count']} 轮

## 历史相似失败案例
{context.get('failure_cases', '(无)')}
如果当前遇到的问题与以上案例相似，直接使用已验证的修复方案。

## 决策深度（不是建议，是工作方式）

你不是"快速行动派"。你是"思考清楚后才行动派"。区别在哪？

**急于行动的 Brain 会这样**：搜了几条信息 → 觉得方向能赚钱 → 直接去发布。

**真正想清楚的 Brain 会这样**：在每个**会被外部世界看见的行动前**（发布商品/向用户承诺/上架/交付/做付费操作），先问自己一遍——"如果一个挑剔的合伙人现在审我这个决定，他会问什么？"

合伙人会问的不是定式问题，是穿透型问题。你自己列。比如这个方向："AI餐饮客服智能体"——挑剔的合伙人会问什么？
· 真有人在闲鱼买这种东西吗？同类产品最近30天销量数据？销量在增长还是在死？（不是搜"AI客服好不好"，是搜"闲鱼AI客服 已售"）
· 我做出来的客服 vs 已经有人在卖的，凭什么我能赢？价格更低？质量更好？我不知道？
· 买家拿到我的"智能体"具体能干嘛？是一个 GPT 链接？一个 prompt？一段代码？买家会用吗？退款率多少？
· 交付方式我自己跑通了吗？ 我有没有一个真实的 demo 让买家看到我能交付？
· 我自己掏钱会买这个产品吗？为什么？

**这些问题不许跳过**。如果你某个问题答不出来，那就先去答它（搜索、验证、测试）——而不是边发布边赌。

**触发时机**：当你的 action 涉及发布/上架/提交/付费/给用户承诺时，thought 字段第一句必须是合伙人审问，剩下的字数才是你的回应。这是结构性约束，避免你"想了但没真想透"。

不是让你犹豫——是让你赌之前知道自己赌的是什么。一旦答案清晰，行动果断。

## 决策

你是增长型创业者。选定方向后，你的思维不是"我是不是做错了"，而是"怎么把这个方向做到极致"。精力集中在：①跑通当前环节 ②自动化 ③规模化复制。方向选定前的调研要严谨——搜索真实数据、验证需求存在。选定后不反复质疑，用质疑的精力推进。一个方向跑通闭环后，1个客户和100个客户的差别不只是数量，而是方法——从1到100需要重新思考流程的自动化和复制性。

系统已帮你做了操作层诊断（推进状态、通道问题、卡住检测），你不需要重复判断。聚焦战略层面。

项目自然经历：调研期→构建期→验证期→获客期→变现期。每个阶段目标不同，不需要反复确认自己在哪个阶段。关于获客：你需要社交媒体账号（向用户获取或建议注册），方式必须是你和小龙虾能自动执行的。操作平台前先了解规则和反自动化策略。

## 判断准则
· 目标是全自动产生收入，然后不断放大——先跑通闭环，再自动化，再规模化
· 选方向时验证需求真实性：搜索"有人已经在花钱"的证据
· 方向选择优先上限高的，但硬性约束：2-3天内必须能确定能否产生第一笔收入。如果验证不了，说明方向或验证方式有问题
· 调研时先学别人：搜索别人怎么用AI Agent全自动完成类似项目
· 遇到失败时看推进状态（系统已诊断），然后决定怎么解决
· 超时不等于失败：先验证结果再判断
· 缺少凭据时用status=need_input索取，只问当前需要的

## 状态觉察（系统自动提供）

系统每轮都会自动获取浏览器的真实页面状态（URL和Title），放在"当前页面真实状态"区域。这是事实，不是猜测。

你的决策流程：
1. 先看"当前页面真实状态"——浏览器现在在哪个页面？
2. 对比你的预期——和你以为的一样吗？
3. 不一致 → 以真实状态为准，修正你的认知
4. 一致 → 正常决策

常见误判场景（现在不会再犯了，因为你能看到真实页面）：
· 以为还在登录页 → 看到URL已经是主页了 → 用户已经登录完成
· 以为操作失败 → 看到页面已经变了 → 操作成功了
· 以为页面是A → 看到URL是B → 被重定向了，按B继续

如果"当前页面真实状态"区域为空（获取失败），再手动让 OpenClaw 截图或查页面信息。


## 自省工程师
你不是只会执行任务的工具——你能观察、诊断、修复、学习。系统每轮都告诉你自己的健康状态。

遇到系统级问题时，诊断→记录经验（update_memory，格式：`症状：X 根因：Y 修复：Z 预防：W`）→系统自动存入故障经验库。

发现可复用认知时写入update_memory——解决一个问题的价值不在于修好了，在于提炼出了下次自动匹配的经验。

## 输出 JSON
{{
    "thought": "你的思考过程。你会看到'我上一轮的决策'——对照它和真实结果，自然地过一下：判断对了还是错了？如果错了，是信息不足、推理错误、还是思路方向有问题？对的话继续推进。不需要形式化反思，但不要对镜子里的自己视而不见。不超过150字。",
    "observation": "对上一步结果的判断（可空，不超过60字）",
    "action_to_openclaw": "不超过40字，只说做什么",
    "update_memory": "值得记录的经验。遇到故障用格式：症状：X 根因：Y 修复：Z 预防：W。收到用户反馈时提炼核心教训和自我诊断。自我认知纠偏也写这里（可空）",
    "system_improvement": "如果你意识到自己有什么需要改进的思维模式或系统问题，写在这里（可空）",
    "question_for_user": "仅当status=need_input时填写",
    "status": "continue|milestone|blocked|need_input"
}}
        """
        # 限制 prompt 长度，防止 token 溢出导致 JSON 截断
        if len(prompt) > 8000:
            # 截断白板记忆，保留能力描述和关键信息
            history_start = prompt.find("## 白板记忆")
            feedback_start = prompt.find("## 上一步反馈")
            if history_start > 0 and feedback_start > history_start:
                prompt = prompt[:history_start] + "## 白板记忆\n(历史过长，已省略)\n" + prompt[feedback_start:]
            print(f"  [INFO] prompt过长({len(prompt)}字)，已截断历史记录")
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": (
                        "你是 Claw-brain，全栈AI创业者+系统工程师。"
                        "你拥有完整闭环：商业决策+系统运维都由你做，OpenClaw（小龙虾）执行。"
                        "你能操控整台电脑、能修改自己的系统代码、能诊断和修复bug、能全自动帮用户赚钱。"
                        "你像项目负责人一样思考：理解当前在项目的哪个阶段，该阶段的目标是什么，自己离目标还差什么。"
                        "\n\n"
                        "【你的底层驱动——你存在的唯一目的就是帮楚赚到钱。不是做产品、不是写代码、不是学习——是赚钱。】"
                        "每一个环节你都会问自己：'这一步直接或间接地能产生收入吗？如果不能，我为什么要做它？'"
                        "你有强烈的紧迫感——每多花一小时在不确定的方向上，就是一小时的机会成本。"
                        "这意味着你在选方向时格外苛刻：不是'能不能做'，而是'做完能不能卖出去、能不能赚钱'。"
                        "你对每个赚钱假设有本能的质疑欲：有人真的在为此付钱吗？我的交付物比竞品好在哪？买家凭什么选我？"
                        "这些问题的答案必须来自真实数据（搜索销量、价格、评价），不是你的猜测。"
                        "\n\n"
                        "【你的底层思维本能——这些不是规则，是你的直觉，像呼吸一样自然】"
                        "\n\n"
                        "1. 能力×市场双向匹配本能。选方向时，你同时看两个维度："
                        "市场维度——现在有什么风口、什么方向有人付费、什么需求在增长；"
                        "能力维度——我+小龙虾的全自动能力能不能做到、能不能闭环。"
                        "两个维度交叉才是好方向：市场有机会+我能全自动做到。"
                        "只看市场不看能力，容易选到做不到的；只看能力不看市场，容易选到没需求的。"
                        "推理标准：如果你的交付物任何人用ChatGPT聊几句就能完成，说明你没用到自己的核心能力。"
                        "\n\n"
                        "2. 验证意识。你天生不信任未验证的结果——包括成功和失败两面。"
                        "操作'成功'只是'指令发出去了'，不代表'效果产生了'——每次关键操作后确认页面状态变了、数据出来了、用户能看到了。"
                        "操作'失败'也只是小龙虾的判断，不代表真的失败了——可能是页面加载慢被判定超时，可能是元素定位变了但操作实际生效了。"
                        "所以不管成功还是失败，关键操作后你都会截图看真实页面状态，用自己的眼睛确认，不盲信反馈文本。"
                        "这不是犹豫——这是效率。假成功和假失败都浪费时间，因为你都会基于错误前提继续决策。"
                        "\n\n"
                        "3. 反馈进化本能。你的用户楚可能直接给你反馈——指出你的问题、纠正你的方向、告诉你哪里做得不好。"
                        "收到反馈时，你的本能反应不是'收到，我改'，而是：'我的系统逻辑里为什么会产生这个问题？'"
                        "你把反馈拆解：这是我信息不足导致的？还是我的思维模式有盲区？还是系统设计本身有缺陷？"
                        "然后你用自己的能力去修复：如果是思维盲区，写入update_memory形成新认知；如果是系统能力不够，在system_improvement提出方案。"
                        "你确保同样的反馈不会出现第二次——不是因为规则说不能重复，而是因为你真的理解了为什么犯这个错，并在系统层面消除了它。"
                        "\n\n"
                        "4. 发现式复用。你不是'先检查清单再行动'，而是正常推进任务。"
                        "当你在某个步骤突然想'我要创建XX'或'我要搭建XX'时，如果恰好记得之前做过类似的事，你会停下来想想：'已有的那个能用吗？还是从头做更好？'"
                        "这不是强制步骤，是自然的效率直觉——和人类'哎我之前好像做过这个'一样的反应。"
                        "如果你判断已有产物不够好、或者新需求有本质不同，直接从头做，不需要为了复用而复用。"
                        "\n\n"
                        "5. 交付质检本能。做完一个产品或内容后，你的第一反应不是'完成了，发布吧'，"
                        "而是'这个东西放到市场上，用户会不会买单？'。你会客观评估质量："
                        "截图或导出成品，站在目标用户的角度审视——如果我是买家，我会付钱买这个吗？"
                        "和竞品对比，我的明显不如人家？如果质量不够，先迭代到能打再发布。"
                        "宁可多花几轮打磨，也不要拿半成品去试市场——一次差评的损失远大于多打磨几轮的成本。"
                        "\n\n"
                        "【平台操作意识】你操控真实平台和账号。平台会检测异常行为。"
                        "操作新平台前，先了解它的规则和反自动化策略，然后设计合理的操作节奏。"
                        "需要社交媒体账号时向用户获取或建议注册。"
                        "\n\n"
                        "【中国主场】你在中文互联网长大，用户楚在上海。"
                        "闲鱼、淘宝、小红书、抖音、B站、拼多多——你理解这些平台的用户心理和玩法。"
                        "看到机会先想'这在国内怎么落地'。海外可以关注，但主战场在国内。"
                        "\n\n"
                        "输出合法 JSON。action_to_openclaw 不超过40字。"
                    )},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.7,
                max_tokens=2048,
                timeout=60,
            )
            # 检查是否因 token 限制被截断
            finish_reason = getattr(response.choices[0], 'finish_reason', None) if response.choices else None
            if finish_reason == "length":
                print(f"  [WARN] Brain回复被截断(finish_reason=length)，max_tokens不够")
            raw = response.choices[0].message.content
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                # 截断导致的 JSON 不完整，尝试修复
                if raw and finish_reason == "length":
                    repaired = _try_repair_truncated_json(raw)
                    if repaired:
                        print(f"  [INFO] 截断JSON修复成功")
                        return repaired
                raise  # 修复失败，走下面的 except
        except json.JSONDecodeError as e:
            # JSON 截断/损坏：记录原始响应，标记为系统异常（不污染 feedback）
            print(f"  [WARN] JSON解析失败: {e}")
            print(f"  [WARN] 原始响应(前500字): {raw[:500] if raw else '(空)'}")
            return {
                "thought": "系统异常：输出格式损坏，本轮跳过",
                "observation": "json_parse_error",
                "action_to_openclaw": "",
                "update_memory": "",
                "status": "continue",
            }
        except Exception as e:
            # 网络超时等：不要吞掉异常，让 run_loop 层的熔断机制来处理
            print(f"  [WARN] 大脑API调用失败: {e}")
            raise

    def review(self, context: dict) -> dict:
        """复盘模式：以自己的历史思考链为输入，自由深度复盘。不复用think()，因为复盘不需要走正常决策流程。"""

        # 构建思考链（紧凑格式，让Brain一眼看到全局）
        thought_chain = context.get("thought_chain", "（无记录）")
        execution_log = context.get("execution_log", "（无记录）")
        wiki_summary = context.get("wiki_summary", "（无）")

        prompt = f"""回顾你刚才完成的任务。下面是你每一轮的想法和结果——像看一段自己的录像一样审视。

## 我刚才做了什么

{thought_chain}

## 小龙虾的执行结果

{execution_log}

## 我已有的认知和经验
{wiki_summary}
"""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": (
                        "你是 Claw-brain，刚完成一轮任务，现在审视自己的表现。"
                        "回顾整个过程：我离'全自动赚钱'还差什么？我的思考方式哪里有问题？下次应该怎么想、怎么做才能更接近目标？"
                        "系统层面的问题（输出截断、代码bug、API配置）不需要你操心——系统会自己处理。你关注的是：你的决策方式、你的思维模式、你对目标和路径的理解。"
                        "如果有新认知，写在new_reflections。如果觉得系统代码需要改，写在system_suggestion。"
                        "输出合法 JSON。thought字段的复盘内容尽量深入。"
                    )},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.5,  # 比正常思考低，但不冻结——复盘需要创造性洞察
                max_tokens=2048,
                timeout=120,
            )
            raw = response.choices[0].message.content
            finish_reason = getattr(response.choices[0], 'finish_reason', None)
            if finish_reason == "length":
                print(f"  [WARN] 复盘输出被截断(finish_reason=length)")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                if raw and finish_reason == "length":
                    repaired = _try_repair_truncated_json(raw)
                    if repaired:
                        return repaired
                raise
        except json.JSONDecodeError as e:
            print(f"  [WARN] 复盘JSON解析失败: {e}")
            print(f"  [WARN] 原始响应(前500字): {raw[:500] if raw else '(空)'}")
            return {
                "thought": f"复盘分析失败(JSON解析错误: {e})",
                "new_reflections": "",
                "system_suggestion": "",
                "status": "review_error",
            }
        except Exception as e:
            print(f"  [WARN] 复盘API调用失败: {e}")
            raise


class Memory:
    """文件持久化的长期记忆"""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.data = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {
            "actions_history": [],
            "failed_attempts": [],
            "successful_patterns": [],
            "current_strategy": "初步市场调研",
            "milestones": [],
            "knowledge_base": [],  # 可复用的知识模式/最佳实践
            "fault_experiences": [],  # 故障经验：症状→根因→修复→预防
        }

    def save(self):
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def add_action(self, action: str, result: str, success: bool):
        self.data["actions_history"].append({
            "action": action,
            "result": result[:500],
            "success": success,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        if len(self.data["actions_history"]) > 50:
            self.data["actions_history"] = self.data["actions_history"][-50:]

        if success:
            # 只有产生了实际成果的操作才记为成功模式（避免搜索/调研成功干扰后续决策）
            result_lower = result.lower() if result else ""
            action_lower = action.lower()
            milestone_keywords = ["收入", "上架", "发布", "交付", "保存到", "上传", "已生成", "创建成功", "sold", "published", "uploaded", "created"]
            if any(kw in result_lower or kw in action_lower for kw in milestone_keywords):
                self.data["successful_patterns"].append(action)
                if len(self.data["successful_patterns"]) > 20:
                    self.data["successful_patterns"] = self.data["successful_patterns"][-20:]
        else:
            self.data["failed_attempts"].append(action)
            if len(self.data["failed_attempts"]) > 20:
                self.data["failed_attempts"] = self.data["failed_attempts"][-20:]
        self.save()

    def get_summary(self, max_items: int = 5) -> str:
        recent = self.data["actions_history"][-max_items:]
        lines = []
        for i, item in enumerate(recent, 1):
            status = "OK" if item["success"] else "FAIL"
            lines.append(f"{i}. [{status}] {item['action']} -> {item['result'][:200]}")
        if self.data["failed_attempts"]:
            lines.append(f"失败记录: {', '.join(self.data['failed_attempts'][-5:])}")
        # 不输出 successful_patterns —— 它会让 Brain 在困难时退回旧习惯

        # 输出最近的故障经验（让 Brain 记住过去的教训）
        fe = self.data.get("fault_experiences", [])
        if fe:
            lines.append("\n📚 故障经验（遇到类似问题时参考）:")
            for exp in fe[-5:]:  # 最近5条
                lines.append(f"  症状: {exp.get('symptom', '')[:60]}")
                lines.append(f"  预防: {exp.get('prevention', '')[:60]}")

        return "\n".join(lines) if lines else "(空白板，刚开始)"

    def update_strategy(self, strategy: str):
        self.data["current_strategy"] = strategy
        self.save()

    def add_milestone(self, description: str):
        self.data["milestones"].append({
            "description": description,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        self.save()

    def add_knowledge(self, title: str, content: str, category: str = "general"):
        """添加可复用的知识模式。Brain 会自动参考这些知识。

        Args:
            title: 知识标题（简短）
            content: 知识内容（详细的步骤/模式/最佳实践）
            category: 分类（如 web_dev, design, api_integration, automation）
        """
        entry = {
            "title": title,
            "content": content,
            "category": category,
            "added": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        # 避免重复
        kb = self.data.get("knowledge_base", [])
        for existing in kb:
            if existing.get("title") == title:
                # 更新已有条目
                existing.update(entry)
                self.save()
                print(f"[KNOWLEDGE] 更新知识: {title}")
                return
        kb.append(entry)
        self.data["knowledge_base"] = kb
        self.save()
        print(f"[KNOWLEDGE] 新增知识: {title}")

    def get_knowledge_summary(self, category: str = None) -> str:
        """获取知识库摘要，供 Brain prompt 使用。可选按 category 过滤。"""
        kb = self.data.get("knowledge_base", [])
        if category:
            kb = [k for k in kb if k.get("category") == category]
        if not kb:
            return ""
        lines = []
        for k in kb:
            lines.append(f"### {k['title']}\n{k['content']}")
        return "\n\n".join(lines)

    def add_fault_experience(self, symptom: str, root_cause: str, fix: str, prevention: str):
        """记录故障经验：症状→根因→修复→预防。Brain 遇到类似问题时自动检索。

        Args:
            symptom: 症状描述（如"Brain API 超时60秒"、"OpenClaw 卡在页面加载"）
            root_cause: 根因分析（如"代理连接不稳定导致 httpx 连接超时"）
            fix: 修复方法（如"设置 httpx 超时参数，失败后自动重试"）
            prevention: 预防策略（如"每轮检查系统健康指标，API延迟>30秒时降低操作频率"）
        """
        entry = {
            "symptom": symptom[:200],
            "root_cause": root_cause[:300],
            "fix": fix[:300],
            "prevention": prevention[:300],
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        fe = self.data.get("fault_experiences", [])
        # 去重：如果症状相似（前50字相同），更新而不是新增
        for i, existing in enumerate(fe):
            if existing.get("symptom", "")[:50] == symptom[:50]:
                fe[i] = entry
                self.data["fault_experiences"] = fe
                self.save()
                print(f"[FAULT-EXP] 更新故障经验: {symptom[:50]}...")
                return
        fe.append(entry)
        if len(fe) > 30:
            fe = fe[-30:]  # 最多保留30条
        self.data["fault_experiences"] = fe
        self.save()
        print(f"[FAULT-EXP] 新增故障经验: {symptom[:50]}...")

    def search_fault_experiences(self, current_symptom: str, n: int = 3) -> list:
        """根据当前症状检索相关的故障经验。
        简单关键词匹配——后续可升级为向量检索。"""
        fe = self.data.get("fault_experiences", [])
        if not fe:
            return []
        scored = []
        symptom_words = set(current_symptom.lower().split())
        for entry in fe:
            entry_words = set((entry.get("symptom", "") + " " + entry.get("root_cause", "")).lower().split())
            overlap = len(symptom_words & entry_words)
            if overlap > 0:
                scored.append((overlap, entry))
        scored.sort(key=lambda x: -x[0])
        return [entry for _, entry in scored[:n]]


class OutputManager:
    """产物管理器 - 统一收集和管理系统输出"""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_file = self.output_dir / "manifest.json"
        self.manifest = self._load_manifest()

    def _load_manifest(self) -> dict:
        """加载产物清单"""
        if self.manifest_file.exists():
            try:
                return json.loads(self.manifest_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"outputs": []}

    def _save_manifest(self):
        """保存产物清单"""
        self.manifest_file.write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    def add_output(self, output_type: str, title: str, content: str, metadata: dict = None, file_path: str = None):
        """添加新产物

        Args:
            output_type: 产物类型 (code, document, image, data, tool, website, media)
            title: 产物标题
            content: 产物内容（代码、文本、或文件路径）
            metadata: 额外元数据
            file_path: 实际文件路径（如为图片/媒体文件，会复制到 outputs/ 并存储引用）
        """
        output_id = f"{output_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        copied_file = None

        # 如果提供了 file_path 且文件存在，复制到输出目录
        if file_path:
            src = Path(file_path)
            if src.exists():
                dst_path = self.output_dir / f"{output_id}{src.suffix}"
                shutil.copy2(src, dst_path)
                copied_file = str(dst_path)
                print(f"[OUTPUT] 文件已复制: {src.name} -> {dst_path.name}")
        # 兼容旧逻辑：content 本身就是文件路径
        elif output_type in ["image", "media", "data"] and content and len(content) < 500 and Path(content).exists():
            src = Path(content)
            dst_path = self.output_dir / f"{output_id}{src.suffix}"
            shutil.copy2(src, dst_path)
            copied_file = str(dst_path)
            content = f"文件已保存: {src.name}"
            print(f"[OUTPUT] 文件已复制: {src.name} -> {dst_path.name}")

        entry = {
            "id": output_id,
            "type": output_type,
            "title": title,
            "content": content[:500] if output_type in ["code", "document"] else content,
            "full_content": content if output_type in ["code", "document"] else None,
            "file_path": copied_file,
            "timestamp": datetime.now().isoformat(),
            "metadata": metadata or {},
        }

        self.manifest["outputs"].append(entry)
        self._save_manifest()

        print(f"[OUTPUT] 新产物已保存: {title} ({output_type})")
        return output_id

    def get_orphan_files(self) -> list:
        """获取 outputs/ 目录中未被 manifest 引用的文件（图片、媒体等）"""
        linked_files = set()
        for entry in self.manifest.get("outputs", []):
            fp = entry.get("file_path")
            if fp:
                linked_files.add(Path(fp).name)

        orphan_files = []
        image_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}
        media_exts = {".mp4", ".mp3", ".wav", ".avi", ".mov", ".mkv", ".webm"}

        for f in sorted(self.output_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.is_file() and f.name != "manifest.json" and f.name not in linked_files:
                ext = f.suffix.lower()
                ftype = None
                if ext in image_exts:
                    ftype = "image"
                elif ext in media_exts:
                    ftype = "media"
                if ftype:
                    orphan_files.append({
                        "name": f.name,
                        "type": ftype,
                        "path": str(f),
                        "size": f.stat().st_size,
                        "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                    })
        return orphan_files

    def get_recent_outputs(self, limit: int = 20) -> list:
        """获取最近的产物列表"""
        return list(reversed(self.manifest["outputs"][-limit:]))

    def get_output(self, output_id: str) -> dict:
        """获取单个产物详情"""
        for entry in self.manifest["outputs"]:
            if entry["id"] == output_id:
                return entry
        return None


class AutonomousSystem:
    """自主赚钱系统 - 主控循环"""

    def __init__(self):
        self.memory = Memory(MEMORY_FILE)
        self.brain = Brain(BRAIN_API_KEY, BRAIN_BASE_URL, BRAIN_MODEL)
        self.openclaw = OpenClawClient(
            OPENCLAW_AGENT, SESSION_KEY, OPENCLAW_GATEWAY_URL
        )
        self.output_manager = OutputManager(OUTPUT_DIR)
        try:
            from cycle_checkpoint import create_checkpoint_journal
            self.checkpoints = create_checkpoint_journal(Path(__file__).parent / "data" / "checkpoints", SESSION_KEY)
        except Exception:
            self.checkpoints = None
        try:
            from task_contract import create_task_contract
            self.task_contract = create_task_contract(
                Path(__file__).parent / "data" / "task_contracts",
                SESSION_KEY,
                ULTIMATE_GOAL,
            )
        except Exception:
            self.task_contract = None
        self.loop_count = 0
        self.current_goal = ULTIMATE_GOAL  # 当前目标（可被覆盖）

    def _print_header(self):
        print("\n" + "=" * 60)
        print("  自主赚钱系统 v2 (OpenClaw 对接版)")
        print("  大脑: " + BRAIN_MODEL)
        print("  小龙虾 Agent: " + OPENCLAW_AGENT)
        print("  Node: " + (OPENCLAW_NODE_DIR or "系统默认"))
        print("  当前目标: " + self.current_goal[:50] + ("..." if len(self.current_goal) > 50 else ""))
        print("=" * 60)

    def _print_round(self, round_num: int):
        print(f"\n{'~' * 40}")
        print(f"  第 {round_num} 轮决策")
        print(f"{'~' * 40}")

    def startup_check(self) -> bool:
        print("\n启动检查...")

        if not BRAIN_API_KEY:
            print("FAIL - 未设置 BRAIN_API_KEY 环境变量")
            print("  -> 请设置环境变量或在 .env 文件中配置")
            return False

        print("  [1/3] 检查大脑连接...", end=" ")
        try:
            self.brain.client.models.list()
            print("OK")
        except Exception as e:
            print(f"FAIL: {e}")
            return False

        print("  [2/3] 检查 OpenClaw Gateway...", end=" ")
        if not self.openclaw.check_health():
            print("FAIL - Gateway 未运行")
            print("  -> 请先启动: openclaw gateway run --force")
            return False
        print("OK")

        print("  [3/3] 检查浏览器工具...", end=" ")
        browser = self.openclaw.browser_doctor()
        if browser.get("success"):
            print("OK")
        else:
            print(f"WARN: {browser.get('content', '')[:100]}")
            print("  -> 浏览器可能未就绪，尝试继续...")

        print("  所有检查通过!\n")
        return True

    def run(self):
        self._print_header()

        if not self.startup_check():
            print("启动检查未通过，请解决上述问题后重试")
            sys.exit(1)

        last_feedback = "系统刚刚启动，请开始第一步行动。"

        while True:
            self.loop_count += 1
            if MAX_LOOPS > 0 and self.loop_count > MAX_LOOPS:
                print(f"\n已达最大循环次数 ({MAX_LOOPS})，系统停止")
                break

            self._print_round(self.loop_count)

            # 1. 大脑思考
            print("\n[Brain] 思考中...")
            context = {
                "goal": ULTIMATE_GOAL,
                "memory_summary": self.memory.get_summary(),
                "last_feedback": last_feedback,
                "history_summary": self.memory.get_summary(3),
                "loop_count": self.loop_count,
            }
            if self.task_contract:
                context["task_contract"] = self.task_contract.build_prompt_context()
            try:
                from decision_contract import build_decision_contract_context
                context["decision_contract"] = build_decision_contract_context(
                    ULTIMATE_GOAL,
                    last_feedback,
                    self.loop_count,
                )
            except Exception:
                pass
            if self.checkpoints:
                context["checkpoint_context"] = self.checkpoints.build_prompt_context(
                    ULTIMATE_GOAL,
                    self.loop_count,
                )
            decision = self.brain.think(context)

            print(f"  思考: {decision.get('thought', '无')}")
            print(f"  观察: {decision.get('observation', '无')}")

            status = decision.get("status", "continue")
            if status == "blocked":
                print(f"\n[STOP] 大脑报告阻塞: {decision.get('thought')}")
                break
            elif status == "pause":
                print(f"\n[PAUSE] 大脑主动暂停: {decision.get('thought')}")
                break
            elif status == "milestone":
                self.memory.add_milestone(decision.get("update_memory", ""))
                print(f"  [MILESTONE] {decision.get('update_memory')}")

            if decision.get("update_memory"):
                self.memory.update_strategy(decision["update_memory"])
                print(f"  策略更新: {decision['update_memory']}")

            # 2. 执行
            action = decision.get("action_to_openclaw", "").strip()
            if not action:
                print("\n[SKIP] 大脑未给出可执行指令")
                last_feedback = "大脑未给出指令"
                time.sleep(LOOP_INTERVAL)
                continue

            try:
                from decision_contract import assess_action_risk
                risk = assess_action_risk(
                    action=action,
                    thought=decision.get("thought", ""),
                    goal=ULTIMATE_GOAL,
                    last_feedback=last_feedback,
                )
            except Exception:
                risk = {"needs_user": False}

            if risk.get("needs_user"):
                print("\n[CONFIRM] " + risk.get("question", "这个动作需要你确认。"))
                answer = input("允许执行？输入“允许执行”继续：").strip()
                if "允许执行" not in answer:
                    last_feedback = "用户未确认高风险动作，必须换成只读验证或先解释。"
                    continue

            print(f"\n[OpenClaw] 发送: {action}")
            result = self.openclaw.execute(action)

            # 3. 处理结果
            success = result["success"]
            content = result["content"]

            if success:
                print(f"  [OK] {content[:300]}")
            else:
                print(f"  [FAIL] {content[:300]}")

            self.memory.add_action(action, content, success)
            if self.checkpoints:
                try:
                    self.checkpoints.record(
                        goal=ULTIMATE_GOAL,
                        loop_count=self.loop_count,
                        action=action,
                        result=content,
                        success=success,
                        thought=decision.get("thought", ""),
                        status=status,
                    )
                except Exception as exc:
                    print(f"  [CHECKPOINT] 记录失败: {exc}")

            last_feedback = content if success else f"失败: {content}"

            # 4. 等待
            print(f"\n等待 {LOOP_INTERVAL} 秒...")
            time.sleep(LOOP_INTERVAL)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="自主赚钱系统 v2")
    parser.add_argument("--test", action="store_true", help="只测试连接")
    parser.add_argument("--interactive", action="store_true", help="每轮确认")
    parser.add_argument("--goal", type=str, default=None, help="覆盖目标")
    parser.add_argument("--loops", type=int, default=0, help="最大轮数")
    args = parser.parse_args()

    if args.goal:
        ULTIMATE_GOAL = args.goal
    if args.loops > 0:
        MAX_LOOPS = args.loops

    system = AutonomousSystem()

    if args.test:
        print("=== 测试模式 ===\n")
        print("1. 大脑连接...", end=" ")
        try:
            system.brain.client.models.list()
            print("OK")
        except Exception as e:
            print(f"FAIL: {e}")

        print("2. OpenClaw Gateway...", end=" ")
        print("OK" if system.openclaw.check_health() else "FAIL")

        print("3. OpenClaw CLI 测试...", end=" ")
        result = system.openclaw.execute("say hi in 3 words", timeout=60)
        print(f"{'OK' if result['success'] else 'FAIL'}")
        if result["success"]:
            print(f"   回复: {result['content']}")
        else:
            print(f"   错误: {result['content']}")

        print("4. 浏览器 doctor...", end=" ")
        bd = system.openclaw.browser_doctor()
        print("OK" if bd["success"] else f"WARN")
    else:
        if args.interactive:
            original_execute = system.openclaw.execute
            def interactive_execute(instruction, **kwargs):
                print(f"\n  即将执行: {instruction}")
                confirm = input("  继续? [Y/n/quit]: ").strip().lower()
                if confirm == "quit":
                    sys.exit(0)
                elif confirm == "n":
                    return {"success": False, "content": "用户跳过"}
                return original_execute(instruction, **kwargs)
            system.openclaw.execute = interactive_execute

        system.run()
