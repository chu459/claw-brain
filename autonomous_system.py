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
from pathlib import Path

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

# --- 系统行为配置 ---
ULTIMATE_GOAL = "分析当前获客渠道，找到转化率最高的方式并持续放大，直到产生真实收入。"
SESSION_KEY = "autonomous-money-maker"
LOOP_INTERVAL = 15
MAX_LOOPS = 0
MEMORY_FILE = "system_memory.json"

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

        # 确定执行方式：直接 openclaw 命令 / openclaw.mjs / npx openclaw
        if OPENCLAW_MJS and os.path.isfile(OPENCLAW_MJS):
            # 用户指定了 OPENCLAW_NODE_DIR 且 openclaw.mjs 存在
            self._node_exe = OPENCLAW_NODE_EXE
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
        """构建环境: 清除 NODE_OPTIONS"""
        env = os.environ.copy()
        env.pop("NODE_OPTIONS", None)
        if OPENCLAW_NODE_DIR:
            env.pop("NODE_PATH", None)
            path_parts = env.get("PATH", "").split(os.pathsep)
            path_parts = [p for p in path_parts
                          if "node" not in p.lower() or OPENCLAW_NODE_DIR in p]
            env["PATH"] = OPENCLAW_NODE_DIR + os.pathsep + os.pathsep.join(path_parts)
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
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                encoding="utf-8",
                errors="replace",
            )

            stdout = result.stdout.strip()
            stderr = result.stderr.strip()

            if result.returncode == 0:
                return {
                    "success": True,
                    "content": stdout or "(执行成功，无文字输出)",
                }
            else:
                error_msg = stderr or stdout or f"退出码 {result.returncode}"
                return {
                    "success": False,
                    "content": f"执行失败: {error_msg[:500]}",
                }

        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "content": f"执行超时（{timeout}秒），浏览器操作可能卡住了",
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


class Brain:
    """AI 大脑 - 策略思考和决策"""

    def __init__(self, api_key: str, base_url: str, model: str):
        self.client = openai.OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    def think(self, context: dict) -> dict:
        prompt = f"""你是一个自主赚钱系统的最高策略大脑。

## 终极目标
{context['goal']}

## 白板记忆
{context['memory_summary']}

## 上一步执行反馈
{context['last_feedback']}

## 最近行动记录
{context['history_summary']}

## 当前循环轮次
第 {context['loop_count']} 轮

## 你的任务
根据上面的信息进行深度反思，决定下一步行动。

**关键规则:**
1. action_to_openclaw 必须是自然语言指令，OpenClaw agent 会自主执行浏览器操作
2. 只下达一个具体可执行的指令，不要一次给多个
3. 如果上一步失败了，分析原因并调整策略
4. 优先做信息收集和验证，不要急于执行高风险操作
5. 如果发现有效模式，在 update_memory 中记录
6. 除非确实遇到无法解决的问题（如API余额耗尽），否则不要设置status为blocked
7. 如果需要用户提供信息（手机号、账号、验证码等），设置 status 为 need_input，并在 question_for_user 字段写下你想问用户的具体问题

输出 JSON:
{{
    "thought": "你的思考过程（1-3句话）",
    "observation": "对上一步结果的评价（成功/失败/待定 + 原因）",
    "action_to_openclaw": "给OpenClaw的具体自然语言指令",
    "update_memory": "需要记在白板上的新经验（如果有的话）",
    "question_for_user": "需要问用户的问题（仅当status=need_input时填写）",
    "status": "continue"
}}

status 取值:
- continue: 正常继续
- milestone: 达成了阶段性成果
- blocked: 遇到无法自行解决的问题，需要人工介入
- need_input: 需要用户提供信息（手机号、验证码、账号等），在 question_for_user 中描述需要什么
"""
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": (
                        "你是一个具备商业头脑和执行力的自主Agent策略大脑。"
                        "你只负责思考和决策，具体浏览器操作交给OpenClaw执行。"
                        "输出必须是合法的JSON格式。"
                    )},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.7,
                timeout=60,
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            return {
                "thought": f"大脑思考出错: {e}",
                "observation": "unknown",
                "action_to_openclaw": "",
                "update_memory": "",
                "status": "blocked",
            }


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
        if self.data["successful_patterns"]:
            lines.append(f"成功模式: {', '.join(self.data['successful_patterns'][-5:])}")
        if self.data["failed_attempts"]:
            lines.append(f"失败记录: {', '.join(self.data['failed_attempts'][-5:])}")
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


class AutonomousSystem:
    """自主赚钱系统 - 主控循环"""

    def __init__(self):
        self.memory = Memory(MEMORY_FILE)
        self.brain = Brain(BRAIN_API_KEY, BRAIN_BASE_URL, BRAIN_MODEL)
        self.openclaw = OpenClawClient(
            OPENCLAW_AGENT, SESSION_KEY, OPENCLAW_GATEWAY_URL
        )
        self.loop_count = 0

    def _print_header(self):
        print("\n" + "=" * 60)
        print("  自主赚钱系统 v2 (OpenClaw 对接版)")
        print("  大脑: " + BRAIN_MODEL)
        print("  小龙虾 Agent: " + OPENCLAW_AGENT)
        print("  Node: " + OPENCLAW_NODE_DIR)
        print("  目标: " + ULTIMATE_GOAL)
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
