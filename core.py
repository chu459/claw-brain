"""
claw-brain core engine
=====================
Shared run loop, state management, and credential helpers.
Decoupled from any specific UI (CLI / Web / MCP).

Both web_console.py and cli.py import from this module.
"""

import json
import re
import time
import threading
import queue
import traceback
import sys
import io
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from autonomous_system import Brain, Memory, OpenClawClient
from checkpoint_supervisor import build_supervisor_context, review_checkpoints
from cycle_checkpoint import create_checkpoint_journal
from decision_contract import assess_action_risk, build_decision_contract_context
from task_contract import create_task_contract

# Windows 终端默认 gbk 编码，Brain 返回的 emoji/中文会导致 print() 崩溃
# 这会被 except 当成 Brain API 调用失败→触发熔断。强制 utf-8 一劳永逸。
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

# === 延迟导入模块（避免循环依赖）===
# from page_vision import analyze_page    # 在函数内部 import
# from self_heal import attempt_heal      # 在函数内部 import
# from critic import run_critic            # 在函数内部 import

# === 全局 OpenClaw 浏览器锁 ===
# 多任务并发时，OpenClaw 共享同一个浏览器进程（通过 gateway 18789），
# 并发调用会导致浏览器被锁、操作互相干扰。用全局锁串行化执行。
CLAW_GLOBAL_LOCK = threading.Lock()
CLAW_WARMED = False  # 是否已预热（第一次调用启动浏览器很慢）


class SystemState:
    """Thread-safe shared state for the run loop."""

    def __init__(self, task_id: str = "default"):
        self.task_id = task_id
        self.log_file = f"state_logs_{task_id}.json"
        self.lock = threading.Lock()
        self.running = False
        self.loop_count = 0
        self.event_queue: queue.Queue = queue.Queue()
        self.brain_log: list = []
        self.claw_log: list = []
        self.chat_history: list = []
        self.pending_question: str = ""
        self.user_answer: str = ""
        self.answer_event = threading.Event()
        self.injected_feedback: str = ""  # 用户中途注入的消息
        self.feedback_event = threading.Event()
        # 启动时自动加载历史记录
        self.load_logs()

    def reset(self):
        """Reset for a new run — keep history, only clear runtime state."""
        with self.lock:
            self.loop_count = 0
            self.pending_question = ""
            self.user_answer = ""
            self.answer_event.clear()
            while not self.event_queue.empty():
                self.event_queue.get_nowait()

    def load_logs(self):
        """Load brain_log, claw_log, chat_history from JSON file."""
        import json
        from pathlib import Path
        p = Path(self.log_file)
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            with self.lock:
                self.brain_log = data.get("brain_log", [])
                self.claw_log = data.get("claw_log", [])
                self.chat_history = data.get("chat_history", [])
        except Exception as e:
            print(f"[STATE] Failed to load logs: {e}")

    def save_logs(self):
        """Persist brain_log, claw_log, chat_history to JSON file."""
        import json
        from pathlib import Path
        try:
            with self.lock:
                data = {
                    "brain_log": self.brain_log[-200:],  # 最多保留200条
                    "claw_log": self.claw_log[-200:],
                    "chat_history": self.chat_history[-100:],
                }
            Path(self.log_file).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            print(f"[STATE] Failed to save logs: {e}")


class RunLoopConfig:
    """Configuration for a single run."""

    def __init__(
        self,
        goal: str,
        agent: str = "main",
        max_loops: int = 50,
        interval: int = 15,
        brain_api_key: str = "",
        brain_base_url: str = "https://api.deepseek.com/v1",
        brain_model: str = "deepseek-chat",
        gateway_url: str = "http://127.0.0.1:18789",
        session_key: str = "autonomous-money-maker",
        memory_file: str = "system_memory.json",
        use_vector_memory: bool = True,
        output_manager=None,
    ):
        self.goal = goal
        self.agent = agent
        self.max_loops = max_loops
        self.interval = interval
        self.brain_api_key = brain_api_key
        self.brain_base_url = brain_base_url
        self.brain_model = brain_model
        self.gateway_url = gateway_url
        self.session_key = session_key
        self.memory_file = memory_file
        self.use_vector_memory = use_vector_memory
        self.output_manager = output_manager


# ===================== Session Manager =====================

class SessionManager:
    """管理运行会话（session）的创建、归档、查询。每个会话保存独立的 brain/claw 日志。"""

    def __init__(self, base_dir: str = None):
        import json
        if base_dir is None:
            base_dir = str(Path(__file__).parent)
        self.base_dir = Path(base_dir)
        self.sessions_dir = self.base_dir / "sessions"
        self.sessions_dir.mkdir(exist_ok=True)
        self.index_file = self.sessions_dir / "index.json"
        self._index = self._load_index()
        self._clean_stale_sessions()
        self._current_id = None

    def _load_index(self) -> list:
        """加载会话索引（不含日志数据）"""
        if self.index_file.exists():
            try:
                return json.loads(self.index_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return []

    def _clean_stale_sessions(self):
        """启动时清理上次被强制终止的僵尸会话"""
        import os
        dirty = False
        for s in self._index:
            if s.get("status") == "running":
                s["status"] = "stopped"
                dirty = True
            # 删除无数据文件的残留索引
            sid = s["id"]
            fpath = self.sessions_dir / (sid + ".json")
            if not fpath.exists():
                self._index.remove(s)
                dirty = True
            elif fpath.stat().st_size < 10:
                os.remove(fpath)
                self._index.remove(s)
                dirty = True
        if dirty:
            self._save_index()

    def _save_index(self):
        """保存会话索引"""
        # 只保留最近 100 个
        self._index = self._index[-100:]
        self.index_file.write_text(
            json.dumps(self._index, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _sess_id(self) -> str:
        return f"sess_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    def create_session(self, goal: str, agent: str = "main", mode: str = "money") -> str:
        """创建新会话，返回 session_id。同时归档上一个活跃会话。"""
        # 归档上一个会话
        if self._current_id:
            self.archive_session(self._current_id, "stopped")

        sid = self._sess_id()
        entry = {
            "id": sid,
            "goal": goal,
            "agent": agent,
            "mode": mode,
            "start_time": datetime.now().isoformat(),
            "end_time": "",
            "status": "running",
            "loop_count": 0,
        }
        self._index.append(entry)
        self._save_index()
        # 创建会话日志文件（空）
        self.sessions_dir.mkdir(exist_ok=True)
        (self.sessions_dir / f"{sid}.json").write_text(
            json.dumps({"brain_log": [], "claw_log": []}, ensure_ascii=False), encoding="utf-8"
        )
        self._current_id = sid
        return sid

    def archive_session(self, session_id: str, status: str = "stopped"):
        """归档指定会话，保存 state 中的日志到文件"""
        # 更新索引
        for entry in self._index:
            if entry["id"] == session_id:
                entry["end_time"] = datetime.now().isoformat()
                entry["status"] = status
                break
        self._save_index()

    def save_session_logs(self, session_id: str, brain_log: list, claw_log: list):
        """保存会话的日志到文件"""
        f = self.sessions_dir / f"{session_id}.json"
        f.write_text(
            json.dumps({
                "brain_log": brain_log[-200:],
                "claw_log": claw_log[-200:],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        # 更新索引中的 loop_count
        for entry in self._index:
            if entry["id"] == session_id:
                entry["loop_count"] = len(claw_log)
                break

    def get_session(self, session_id: str) -> dict | None:
        """获取指定会话的完整数据（含日志）"""
        # 从索引找元数据
        meta = None
        for entry in self._index:
            if entry["id"] == session_id:
                meta = entry
                break
        if not meta:
            return None
        # 加载日志
        f = self.sessions_dir / f"{session_id}.json"
        logs = {"brain_log": [], "claw_log": []}
        if f.exists():
            try:
                logs = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {**meta, **logs}

    def list_sessions(self, limit: int = 50) -> list:
        """返回会话索引列表（不含日志，按时间倒序）"""
        return list(reversed(self._index[-limit:]))

    @property
    def current_id(self) -> str | None:
        return self._current_id

    def migrate_old_logs(self, brain_log: list, claw_log: list):
        """首次使用时，将旧的 state_logs 导入为一个历史会话"""
        if self._index:
            return  # 已经有会话历史，不需要迁移
        if not brain_log and not claw_log:
            return  # 没有旧日志
        sid = self._sess_id()
        entry = {
            "id": sid,
            "goal": "(历史运行记录)",
            "agent": "main",
            "mode": "money",
            "start_time": brain_log[0].get("time", "") if brain_log else "",
            "end_time": brain_log[-1].get("time", "") if brain_log else "",
            "status": "stopped",
            "loop_count": len(claw_log),
        }
        self._index.append(entry)
        self._save_index()
        self.sessions_dir.mkdir(exist_ok=True)
        (self.sessions_dir / f"{sid}.json").write_text(
            json.dumps({
                "brain_log": brain_log[-200:],
                "claw_log": claw_log[-200:],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"[SESSION] 迁移旧日志到会话 {sid}（{len(brain_log)} 条大脑日志，{len(claw_log)} 条执行日志）")


# ===================== Credential Helpers =====================

def build_cred_summary() -> str:
    """Build credential summary for Brain context. Keys are NOT masked so Brain can pass them to OpenClaw."""
    from credential_store import list_accounts, ACCOUNT_TEMPLATES
    try:
        accounts = list_accounts(mask=False)
        if not accounts:
            return "\u7528\u6237\u5c1a\u672a\u5b58\u50a8\u4efb\u4f55\u8d26\u53f7\u4fe1\u606f"
        lines = ["\u4ee5\u4e0b\u662f\u7528\u6237\u5df2\u5b58\u50a8\u7684\u8d26\u53f7\u4fe1\u606f\uff08\u53ef\u76f4\u63a5\u4f7f\u7528\uff0c\u65e0\u9700\u518d\u95ee\u7528\u6237\u8981\uff09\uff1a"]
        for acc in accounts:
            cat = acc.get("category", "custom")
            tpl = ACCOUNT_TEMPLATES.get(cat, {"label": "\u5176\u4ed6", "icon": "?"})
            name = acc.get("name", "")
            lines.append(f"  - [{tpl['icon']} {tpl['label']}] {name}")
            for f in acc.get("fields", []):
                if f.get("has_value"):
                    lines.append(f"      {f['label']}: {f['value']}")
                else:
                    lines.append(f"      {f['label']}: \uff08\u672a\u586b\u5199\uff09")
        lines.append("\u5982\u679c\u67d0\u9879\u672a\u586b\u5199\u4e14\u4efb\u52a1\u9700\u8981\uff0c\u624d\u901a\u8fc7 need_input \u5411\u7528\u6237\u7d22\u53d6\u3002")
        return "\n".join(lines)
    except Exception:
        return ""


def try_save_user_input(answer: str, question: str):
    """Auto-save detected phone/email from user input to credential store."""
    from credential_store import add_account, update_account, list_accounts
    try:
        answer = answer.strip()
        # Detect phone number (China mainland, 11 digits starting with 1)
        phone_match = re.search(r'1[3-9]\d{9}', answer)
        if phone_match:
            phone = phone_match.group()
            accounts = list_accounts(mask=False)
            updated = False
            for acc in accounts:
                for f in acc.get("fields", []):
                    if f.get("key") == "phone" and not f.get("value"):
                        f["value"] = phone
                        update_account(acc["id"], fields=acc["fields"])
                        print(f"[CRED] Auto-saved phone to: {acc['name']}")
                        updated = True
                        break
                if updated:
                    break
            if not updated:
                add_account("\u7528\u6237\u624b\u673a\u53f7", "custom", [
                    {"key": "phone", "label": "\u624b\u673a\u53f7", "value": phone, "type": "text"},
                ])
                print("[CRED] Created phone credential entry")

        # Detect email
        email_match = re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', answer)
        if email_match:
            email = email_match.group()
            accounts = list_accounts(mask=False)
            for acc in accounts:
                for f in acc.get("fields", []):
                    if f.get("key") == "email" and not f.get("value"):
                        f["value"] = email
                        update_account(acc["id"], fields=acc["fields"])
                        print(f"[CRED] Auto-saved email to: {acc['name']}")
                        break
    except Exception as e:
        print(f"[CRED] Auto-save failed: {e}")


# ===================== Credential Auto-Answer =====================

# 凭据相关关键词：用于检测 Brain 是否在问凭据问题
_CRED_KEYWORDS = [
    "api key", "api_key", "apikey", "密钥", "api token", "token",
    "密码", "password", "账号", "account", "凭据", "credential",
    "登录信息", "手机号", "邮箱", "email", "phone",
    "base url", "base_url", "接口地址",
]

_NON_CRED_KEYWORDS = [
    "验证码", "verification code", "vcode", "短信验证码", "邮箱验证码",
    "confirm code", "auth code",
]

def _is_credential_question(question: str) -> bool:
    """判断问题是否在请求凭据/API Key 等信息。
    验证码等动态一次性信息不是凭据，凭据库里不可能有，不应触发自动应答。"""
    q = question.lower()
    # 先排除验证码类问题
    if any(kw in q for kw in _NON_CRED_KEYWORDS):
        return False
    return any(kw in q for kw in _CRED_KEYWORDS)


def _build_full_cred_answer() -> str:
    """构建包含所有凭据的完整应答文本。"""
    from credential_store import list_accounts
    accounts = list_accounts(mask=False)
    if not accounts:
        return ""

    lines = ["凭据库中已有以下账号信息，请直接使用："]
    for acc in accounts:
        name = acc.get("name", "")
        fields = acc.get("fields", [])
        filled = [f for f in fields if f.get("has_value")]
        if not filled:
            continue
        lines.append(f"\n【{name}】")
        for f in filled:
            lines.append(f"  {f['label']}: {f['value']}")
    if len(lines) <= 1:
        return ""
    lines.append("\n以上凭据均已就绪，请直接在指令中使用，不要再向用户索取。")
    return "\n".join(lines)


def _try_auto_answer_credentials(question: str) -> str:
    """
    When Brain asks for credentials/API keys, auto-answer from credential store
    instead of blocking for user input. Returns empty string if no match found.
    """
    try:
        # 检测是否是凭据相关的问题
        if not _is_credential_question(question):
            print(f"[CRED-AUTO] 非凭据问题，跳过: {question[:80]}")
            return ""

        # 构建完整凭据应答
        answer = _build_full_cred_answer()
        if answer:
            print(f"[CRED-AUTO] 检测到凭据问题，自动应答")
            return answer

        return ""
    except Exception as e:
        print(f"[CRED-AUTO] Failed to auto-answer: {e}")
        return ""


def _try_smart_answer(answer: str, question: str) -> str:
    """
    智能理解用户自然语言回复。
    如果用户说"已经填好了"/"凭据库里有"/"已经配了"等，自动查凭据库补全。
    返回增强后的应答文本，或原样返回。
    """
    if not answer:
        return answer

    # 不需要增强的直接回答（包含 sk- / pk_ / token 等实际密钥格式）
    if any(marker in answer.lower() for marker in ["sk-", "pk_", "key-", "token-", "bearer "]):
        return answer

    # 智能识别：用户表示凭据已准备好
    smart_triggers = [
        "已经填", "已经配", "已经设", "已经输入", "已经配置",
        "填好了", "配好了", "设好了", "输好了", "有", "存了",
        "凭据库", "credentials", "credential store",
        "你看一下", "去查", "已经准备好了", "不用再问", "都有了",
        "自己去", "查一下", "之前填过", "已经提供",
    ]
    answer_lower = answer.lower().strip()
    if any(trigger in answer_lower for trigger in smart_triggers):
        # 用户暗示凭据已在系统中，自动补全
        cred_info = _build_full_cred_answer()
        if cred_info:
            print(f"[CRED-SMART] 用户表示凭据已就绪，自动补全完整凭据信息")
            return f"{answer}（系统已自动从凭据库获取：{cred_info}）"

    return answer


def _shrink_overpacked_action(action: str, goal: str = "") -> tuple[str, str]:
    """把明显塞了太多步骤的测试动作压小，避免 OpenClaw 长时间卡住。"""
    text = f"{goal}\n{action}".lower()
    if "about:blank" in text:
        risky_parts = ("截图", "screenshot", "等待", "wait", "提取", "extract", "保存", "save")
        if any(part in text for part in risky_parts):
            return (
                "打开 about:blank，并只报告页面标题。不要截图，不要等待，不要做其他操作。",
                "about:blank 测试动作过大，已压成单步只读动作。",
            )
    return action, ""


def _quality_review(
    brain, action: str, result_content: str, goal: str, loop_count: int,
    quality_threshold: float = 7.0, max_retries: int = 2,
) -> dict:
    """
    质量门控：让 Brain 评判 OpenClaw 产出物的质量，决定是否需要改进。
    评审结果包含详细的思考过程和具体问题分析。

    Returns:
        {"passed": bool, "score": float, "feedback": str, "reasoning": str,
         "improve_action": str or "", "problems": str, "suggestions": str}
    """
    prompt = f"""你是一个有商业判断力的产品评估者。你评估的不是一个产品"功能做完了没"，而是"这个东西有没有市场价值"。

## 当前赚钱目标
{goal}

## 执行的动作
{action}

## 执行结果
{result_content[:3000]}

## 评估流程（必须按此顺序思考）

### 第一步：这个产品/服务解决什么问题？
具体描述它解决了谁的问题，痛点是什么。如果说不清，这就是最大的问题。

### 第二步：有人愿意为此付费吗？
- 目标用户是谁？
- 他们现在怎么解决这个问题？（竞品/替代方案）
- 这个结果比竞品好在哪？凭什么让人用？
- 竞品收费多少？这个产品值这个价吗？

### 第三步：能全自动闭环吗？
- 从交付到收款，全链路是否不需要人介入？
- 如果需要人介入，是哪个环节？有没有自动化方案？

### 第四步：综合判断
这个产品值得继续投入还是应该调整方向？为什么？

输出 JSON:
{{
    "reasoning": "你的完整思考过程（200-400字，涵盖上面四个步骤的分析）",
    "score": 1到10的整数（1=没人会买单, 10=明确有付费意愿且能闭环），
    "passed": true或false（7分及以上：有市场价值，值得继续），
    "market_risk": "最大的市场风险是什么（一句话）",
    "improve_action": "如果不通过，下一步应该做什么来提升市场价值（≤50字）",
    "pivot": false或true（是否应该换方向而非改进当前产品）
}}
"""
    try:
        response = brain.client.chat.completions.create(
            model=brain.model,
            messages=[
                    {"role": "system", "content": (
                        "你是一个有商业判断力的产品评估者。你不关心'功能做没做完'，"
                        "你关心的是'有人愿意为这个付费吗'。"
                        "评分标准：1=没人买单，7=有市场价值值得继续，10=明确付费意愿且能全自动闭环。"
                        "不要因为'做出来了'就给高分。必须展示完整思考。输出必须是合法JSON。"
                    )},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,  # 低温度，更客观
            max_tokens=1024,  # 增大 token 限制以容纳思考过程
            timeout=30,
        )
        raw = response.choices[0].message.content
        review = json.loads(raw)
        score = float(review.get("score", 5))
        passed = review.get("passed", score >= quality_threshold)
        market_risk = review.get("market_risk", "")
        improve_action = review.get("improve_action", "")
        should_pivot = review.get("pivot", False)
        reasoning = review.get("reasoning", "")

        print(f"[MARKET] Round {loop_count} - score={score}/10, passed={passed}, pivot={should_pivot}")

        # 构建反馈（市场评估视角）
        feedback = f"市场价值评分: {score}/10"
        if reasoning:
            feedback += f"\n\n评估思考过程:\n{reasoning}"
        if market_risk:
            feedback += f"\n最大市场风险: {market_risk}"
        if not passed:
            if should_pivot:
                feedback += f"\n建议换方向: 当前产品/服务市场价值不足，应重新评估方向"
            elif improve_action:
                feedback += f"\n下一步建议: {improve_action}"

        return {
            "passed": passed,
            "score": score,
            "feedback": feedback,
            "reasoning": reasoning,
            "market_risk": market_risk,
            "improve_action": improve_action,
            "should_pivot": should_pivot,
        }
    except Exception as e:
        print(f"[MARKET] 评估失败: {e}")
        return {"passed": True, "score": 0, "feedback": f"评估异常: {e}", "reasoning": "", "market_risk": "", "improve_action": "", "should_pivot": False}


# ===== LLM Wiki: 跨任务商业认知积累 =====

WIKI_DIR = Path(__file__).parent / "wiki"
WIKI_INDEX = WIKI_DIR / "index.md"
WIKI_LOG = WIKI_DIR / "log.md"
IDENTITY_PATH = WIKI_DIR / "identity.md"
TRAINING_ACCUMULATOR = Path(__file__).parent / "training_accumulator.jsonl"


def _accumulate_training_data(user_message: str, thought: str = "", action: str = ""):
    """积累训练数据：用户反馈→Brain应该怎么想的样本对。
    后续用于云上微调。"""
    try:
        sample = {
            "instruction": "你是Claw-brain。用户给你反馈，请反思并内化。",
            "input": user_message,
            "output": "",  # Brain的反思结果会在下一轮填入
            "_timestamp": datetime.now().isoformat(),
            "_type": "user_feedback",
        }
        with open(TRAINING_ACCUMULATOR, 'a', encoding='utf-8') as f:
            f.write(json.dumps(sample, ensure_ascii=False) + '\n')
        print(f"[TRAIN] 积累训练样本: {user_message[:50]}...")
    except Exception as e:
        print(f"[TRAIN] 积累训练数据失败: {e}")


def _trigger_identity_update(brain, user_message: str):
    """用Brain API消化用户反馈，更新identity.md。
    这是一个独立的API调用（不受512 token限制），让Brain自己提炼认知变化。"""
    try:
        # 读取当前identity内容
        current_identity = ""
        if IDENTITY_PATH.exists():
            current_identity = IDENTITY_PATH.read_text(encoding="utf-8").strip()

        prompt = f"""用户（楚）对你说了以下内容：

"{user_message}"

请你反思这段反馈，提炼出你的认知变化。

当前你的identity.md内容：
{current_identity if current_identity else "（空白，尚未训练）"}

请输出更新后的identity.md完整内容。格式要求：
1. 用Markdown
2. 分三个区域：## 我的能力推理、## 我的方向判断标准、## 我被纠正过的错误模式
3. 每个区域下用简洁的要点（不是长篇大论）
4. 如果某个区域已经有内容，保留仍然有效的，合并新认知
5. 不要超过500字
6. 直接输出Markdown内容，不要加```标记"""

        response = brain.client.chat.completions.create(
            model=brain.model,
            messages=[
                {"role": "system", "content": "你正在更新自己的人格文件。把用户反馈消化成你自己的思维本能，不是规则，是你真正理解的认知。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=1024,
            timeout=30,
        )

        new_identity = response.choices[0].message.content.strip()
        if new_identity and len(new_identity) > 50:
            IDENTITY_PATH.write_text(new_identity, encoding="utf-8")
            print(f"[IDENTITY] 已更新: {new_identity[:80]}...")
            _emit("status", "人格训练完成 — Brain已消化用户反馈并更新认知")
        else:
            print(f"[IDENTITY] 更新结果太短，跳过")
    except Exception as e:
        print(f"[IDENTITY] 更新失败（非致命）: {e}")


def _load_wiki_summary(max_chars: int = 2000) -> str:
    """加载 Wiki index 内容 + identity.md（Brain的人格文件），作为 Brain 的上下文。"""
    parts = []
    try:
        # 优先加载 identity.md（人格训练结果）
        identity_path = WIKI_DIR / "identity.md"
        if identity_path.exists():
            identity = identity_path.read_text(encoding="utf-8").strip()
            if identity and "等待通过对话训练填充" not in identity:
                # 提取非空的非模板内容
                lines = [l for l in identity.split("\n") if l.strip() and not l.startswith("#") and not l.startswith(">") and not l.startswith("---") and "等待通过对话训练填充" not in l]
                if lines:
                    parts.append("【我的人格（通过训练获得）】\n" + "\n".join(lines))
    except Exception:
        pass
    try:
        if WIKI_INDEX.exists():
            content = WIKI_INDEX.read_text(encoding="utf-8").strip()
            if content:
                parts.append(content[:max_chars])
    except Exception:
        pass
    return "\n\n".join(parts) if parts else ""


def _wiki_write_page(category: str, title: str, content: str, source: str = "system"):
    """写入一个 Wiki 页面，同时更新 index.md 和 log.md。"""
    try:
        cat_dir = WIKI_DIR / category
        cat_dir.mkdir(parents=True, exist_ok=True)
        # 标题转文件名（安全处理）
        safe_title = re.sub(r'[<>:"/\\|?*]', '_', title).strip()
        page_path = cat_dir / f"{safe_title}.md"
        page_path.write_text(content, encoding="utf-8")
        print(f"[WIKI] 写入页面: {category}/{safe_title}.md")

        # 更新 log.md
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        log_entry = f"## [{ts}] {source} | {category}/{safe_title}\n"
        if WIKI_LOG.exists():
            existing_log = WIKI_LOG.read_text(encoding="utf-8")
            WIKI_LOG.write_text(existing_log + log_entry, encoding="utf-8")
        else:
            WIKI_LOG.write_text(f"# Wiki 操作日志\n\n{log_entry}", encoding="utf-8")

        # 更新 index.md — 追加条目到对应分类
        _refresh_wiki_index()
    except Exception as e:
        print(f"[WIKI] 写入失败: {e}")


def _refresh_wiki_index():
    """重新生成 index.md，基于 wiki/ 下的实际文件。"""
    try:
        lines = ["# Claw-brain 商业认知 Wiki\n",
                 "> Brain 维护的结构化知识库。跨任务、跨会话持续积累。\n"]
        categories = {
            "directions": "方向分析",
            "competitors": "竞品研究",
            "failures": "失败经验",
            "capabilities": "能力边界",
        }
        for cat_dir_name, cat_label in categories.items():
            cat_path = WIKI_DIR / cat_dir_name
            if not cat_path.exists():
                continue
            md_files = sorted(cat_path.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not md_files:
                continue
            lines.append(f"\n## {cat_label} ({cat_dir_name}/)\n")
            for f in md_files[:10]:  # 每个分类最多显示10个
                # 读取第一行作为摘要
                try:
                    first_line = f.read_text(encoding="utf-8").split("\n")[0].lstrip("# ")
                    lines.append(f"- [{first_line}]({cat_dir_name}/{f.name})")
                except Exception:
                    lines.append(f"- {f.name}")
        lines.append(f"\n---\n*最后更新：{datetime.now().strftime('%Y-%m-%d %H:%M')}*\n")
        WIKI_INDEX.write_text("\n".join(lines), encoding="utf-8")
    except Exception as e:
        print(f"[WIKI] index 刷新失败: {e}")


def _wiki_brain_summary(brain, goal: str, action: str, result: str,
                        loop_count: int, wiki_type: str) -> str:
    """调用 Brain 生成 Wiki 页面内容。返回 markdown 文本。"""
    if wiki_type == "direction":
        prompt = f"""基于以下调研和执行结果，写一个结构化的商业方向分析页。

任务目标：{goal}
执行动作：{action}
执行结果：{result[:2000]}

要求（输出纯 Markdown，不要 JSON）：
# [方向名称]
## 市场分析
（目标用户、痛点、市场规模、付费意愿）
## 竞品格局
（主要竞品、定价、优劣势）
## 闭环可行性
（从产品到收款，每步能否全自动？风险在哪？）
## 我的优势
（AI+自动化的差异化在哪？）
## 结论
（值得投入吗？下一步应该做什么？）"""
    elif wiki_type == "failure":
        prompt = f"""基于以下执行失败，写一个结构化的失败经验页。

任务目标：{goal}
执行动作：{action}
失败结果：{result[:2000]}

要求（输出纯 Markdown，不要 JSON）：
# [失败模式名称]
## 现象
（具体表现）
## 根因分析
（真正的失败原因，不是表面现象）
## 误区
（容易误判为什么——比如超时被误判为验证码）
## 正确应对
（下次遇到类似情况应该怎么做）
## 适用范围
（这个经验在什么场景下有用）"""
    elif wiki_type == "capability":
        prompt = f"""基于以下执行结果，写一个系统能力边界认知页。

任务目标：{goal}
执行动作：{action}
执行结果：{result[:2000]}

要求（输出纯 Markdown，不要 JSON）：
# [能力认知标题]
## 系统能做什么
（已验证的能力）
## 系统不能做什么（或做得不好）
（能力边界）
## 建议用法
（怎样最好地利用这个能力）
## 避坑指南
（常见的使用误区）"""
    else:
        return ""

    try:
        resp = brain.client.chat.completions.create(
            model=brain.model,
            messages=[
                {"role": "system", "content": "你是 Claw-brain 的知识管理者。你把执行经验写成结构化的 Wiki 页面，帮助未来的自己避免重复犯错、积累商业认知。只输出 Markdown，不要其他格式。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=1024,
            temperature=0.3,
            timeout=30,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[WIKI] Brain 生成 Wiki 内容失败: {e}")
        return ""


def _get_artifacts_summary(output_manager=None, max_chars: int = 600) -> str:
    """生成已有产物清单摘要，供 Brain 在新任务启动时了解之前的产出。
    避免重复创建已有的东西，鼓励在现有产物上迭代改进。
    同时扫描最近 session 的里程碑操作（外部平台创建的 Bot/账号等）。"""
    lines = []

    # 1. 本地产物（从 output_manager manifest）
    if output_manager:
        try:
            outputs = output_manager.get_recent_outputs(limit=20)
            if outputs:
                seen_titles = set()
                for o in reversed(outputs):  # 旧的在前，新的在后
                    title = o.get("title", "")
                    otype = o.get("type", "")
                    if title in seen_titles:
                        continue
                    seen_titles.add(title)
                    ts = o.get("timestamp", "")[:10]
                    fp = o.get("file_path", "")
                    content_preview = (o.get("content") or "")[:80]
                    line = f"- [{ts}] {title}"
                    if fp:
                        line += f" ({fp})"
                    elif content_preview:
                        line += f" — {content_preview}"
                    lines.append(line)
                    if len("\n".join(lines)) > max_chars:
                        break
        except Exception:
            pass

    # 2. 外部平台操作里程碑（从最近 session 的 brain_log 提取）
    try:
        sessions_dir = Path(__file__).parent / "sessions"
        if sessions_dir.exists():
            idx_file = sessions_dir / "index.json"
            if idx_file.exists():
                import json
                idx = json.loads(idx_file.read_text(encoding="utf-8"))
                # 取最近5个有 brain_log 的 session
                sessions_list = idx if isinstance(idx, list) else idx.get("sessions", [])
                for sess in sessions_list[-5:]:  # 最新的5个 session
                    sess_id = sess.get("id", "")
                    sess_file = sessions_dir / f"{sess_id}.json"
                    if not sess_file.exists():
                        continue
                    try:
                        sess_data = json.loads(sess_file.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    brain_log = sess_data.get("brain_log", [])
                    if not brain_log:
                        continue
                    sess_date = sess_id.replace("sess_", "")[:10]
                    # 提取里程碑和成功创建类操作
                    for entry in brain_log:
                        action = entry.get("action", "")
                        obs = entry.get("observation", "")
                        status = entry.get("status", "")
                        # 关键词匹配：创建、注册、搭建、发布、配置
                        milestone_kw = ["创建", "注册", "搭建", "发布", "配置完成", "成功"]
                        if any(kw in action for kw in milestone_kw) and "成功" in obs:
                            line = f"- [{sess_date}] 外部操作: {action[:50]}"
                            if line not in lines:
                                lines.append(line)
                            if len("\n".join(lines)) > max_chars:
                                break
                    if len("\n".join(lines)) > max_chars:
                        break
    except Exception:
        pass

    if lines:
        return "\n".join(lines)
    return ""


def _auto_detect_outputs(action: str, result_content: str, output_manager=None):
    """自动检测 OpenClaw 执行结果中的产物（代码、文件、网站等）并收录。"""
    if not output_manager or not result_content:
        return
    try:
        from autonomous_system import OutputManager
        if not isinstance(output_manager, OutputManager):
            return
        content_lower = result_content.lower()
        # 检测产物类型
        output_type = None
        title = ""
        extracted_file_path = None
        # 代码/文件创建
        file_keywords = ["已创建", "已保存", "创建成功", "写入文件", "saved", "created file", "echo"]
        code_keywords = ["html", "python", "javascript", "css", "json", "bash", "shell script"]
        website_keywords = ["http-server", "localhost:", "前端页面", "web 服务", "server started"]
        # 检查是否创建了文件
        for kw in file_keywords:
            if kw in result_content:
                # 尝试提取文件路径
                import re as _re
                # 匹配路径模式如 index.html, app.py, *.html 等
                file_match = _re.search(r'(\S+\.(?:html|htm|py|js|css|json|sh|bat|md|txt|java|go|rs|png|jpg|jpeg|gif|webp|svg|pdf|mp4|mp3|wav))', result_content)
                if file_match:
                    fname = file_match.group(1).split("/")[-1].split("\\")[-1]
                    # 清理文件名中的 markdown 标记
                    fname_clean = fname.strip().lstrip("`").rstrip("`")
                    title = f"文件: {fname_clean}"
                    if any(ext in fname for ext in [".html", ".htm"]):
                        output_type = "website"
                        title = f"网站: {fname_clean}"
                    elif any(ext in fname for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"]):
                        output_type = "image"
                        title = f"图片: {fname_clean}"
                    elif any(ext in fname for ext in [".mp4", ".mp3", ".wav"]):
                        output_type = "media"
                        title = f"媒体: {fname_clean}"
                    elif any(ext in fname for ext in [".py", ".js", ".css", ".sh", ".bat", ".java", ".go"]):
                        output_type = "code"
                    elif any(ext in fname for ext in [".json", ".md", ".txt"]):
                        output_type = "document"
                    else:
                        output_type = "tool"

                    # 尝试定位实际文件（图片/媒体/HTML需要）
                    if output_type in ["image", "media", "website"]:
                        extracted_file_path = _try_locate_file(result_content, fname_clean)
                    break
        # 检查是否启动了网站服务
        if not output_type:
            for kw in website_keywords:
                if kw in content_lower or kw in result_content:
                    port_match = _re.search(r'(\d{4,5})', result_content)
                    port = port_match.group(1) if port_match else "?"
                    output_type = "website"
                    title = f"本地服务 (端口 {port})"
                    break
        if output_type and title:
            output_manager.add_output(
                output_type=output_type,
                title=title,
                content=result_content[:2000],
                metadata={"action": action[:100], "auto": True},
                file_path=extracted_file_path,
            )
            print(f"[OUTPUT] 自动收录产物: {title}")
    except Exception as e:
        print(f"[OUTPUT] 自动检测产物失败: {e}")


def _try_locate_file(result_content: str, target_fname: str) -> str:
    """尝试从结果文本中定位实际文件路径，返回找到的路径或 None。"""
    import re as _re
    from pathlib import Path as _Path

    # 从结果文本中提取所有可能的路径
    path_patterns = _re.findall(r'[`\s](/[^\s`]+\.' + target_fname.split('.')[-1] + r')[`\s]?', result_content)
    # 也尝试匹配 Windows 路径
    win_patterns = _re.findall(r'[A-Za-z]:\\[^\s"]+\.' + target_fname.split('.')[-1], result_content)

    candidate_paths = list(set(path_patterns + win_patterns))

    # 也直接用文件名搜索几个常见位置
    search_dirs = [
        _Path(__file__).parent,  # 项目目录
        _Path(__file__).parent / "outputs",  # outputs 目录
    ]

    # 尝试从结果中提取的路径
    for p in candidate_paths:
        try:
            pp = _Path(p)
            if pp.exists():
                return str(pp)
            # 尝试去掉 Linux 路径前缀，只看文件名
        except Exception:
            continue

    # 用文件名搜索常见目录
    for d in search_dirs:
        if d.exists():
            for f in d.rglob(target_fname):
                if f.is_file():
                    return str(f)

    return None


# ===================== Core Run Loop =====================

def run_loop(
    state: SystemState,
    config: RunLoopConfig,
    on_input_needed: Optional[Callable[[str], str]] = None,
    on_event: Optional[Callable[[str, str], None]] = None,
):
    """
    The canonical run loop. Works in any context (CLI, Web, MCP).

    Args:
        state: Shared SystemState instance.
        config: RunLoopConfig with all parameters.
        on_input_needed: Callback when Brain needs user input.
            Receives the question string, returns the answer string.
            If None, uses state.answer_event / state.user_answer (web mode).
        on_event: Callback for status events (type, message).
    """
    goal = config.goal
    agent = config.agent
    max_loops = config.max_loops
    interval = config.interval

    # 调试日志（写到文件，不受 Hidden 窗口影响）
    _dbg_path = Path(__file__).parent / "logs" / "run_loop_debug.log"
    _dbg_path.parent.mkdir(exist_ok=True)
    def _dbg(msg):
        try:
            with open(_dbg_path, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
        except Exception:
            pass

    def _emit(event_type: str, msg: str):
        state.event_queue.put((event_type, msg))
        if on_event:
            try:
                on_event(event_type, msg)
            except Exception:
                pass

    def _wait(seconds: int):
        for _ in range(seconds):
            with state.lock:
                if not state.running:
                    return
            time.sleep(1)

    print(f"[LOOP] start: goal={goal[:30]}..., agent={agent}, max_loops={max_loops}")

    _dbg("run_loop: start")
    mem = Memory(config.memory_file)
    _dbg("run_loop: Memory init ok")
    checkpoint_journal = create_checkpoint_journal(
        Path(__file__).parent / "data" / "checkpoints",
        session_id=getattr(state, "task_id", config.session_key),
    )
    task_contract = create_task_contract(
        Path(__file__).parent / "data" / "task_contracts",
        session_id=getattr(state, "task_id", config.session_key),
        goal=goal,
    )
    brain = Brain(config.brain_api_key, config.brain_base_url, config.brain_model)
    _dbg(f"run_loop: Brain init ok, model={config.brain_model}")
    try:
        from brain_v2 import create_brain_v2_context
        brain_v2 = create_brain_v2_context(Path(__file__).parent)
    except Exception as e:
        print(f"[LOOP] BrainV2 init failed (non-fatal): {e}")
        brain_v2 = None

    # Initialize vector memory
    vec_mem = None
    if config.use_vector_memory:
        _dbg("run_loop: init VectorMemory...")
        try:
            from vector_memory import VectorMemory, format_search_results
            vec_mem = VectorMemory()
            stats = vec_mem.get_stats()
            print(f"[LOOP] Vector memory: {stats['total']} memories, api={stats['api_available']}")
            _dbg(f"run_loop: VectorMemory ok, {stats['total']} memories")
        except Exception as e:
            print(f"[LOOP] Vector memory init failed (non-fatal): {e}")
            _dbg(f"run_loop: VectorMemory failed: {e}")

    try:
        claw = OpenClawClient(agent, config.session_key, config.gateway_url)
    except Exception as e:
        print(f"[LOOP] OpenClaw init failed: {e}")
        traceback.print_exc()
        _dbg(f"run_loop: OpenClaw init FAILED: {e}")
        state.brain_log.append({
            "round": 0, "time": datetime.now().strftime("%H:%M:%S"), "thought": f"OpenClaw init failed: {e}",
            "observation": "system_error", "action": "",
            "update_memory": "", "status": "blocked",
        })
        with state.lock:
            state.running = False
        return

    last_fb = "System just started, take the first action."
    print("[LOOP] OpenClaw ready, entering main loop")
    _dbg("run_loop: entering main loop")

    # === OpenClaw 冷启动预热 ===
    # 第一次调用 openclaw 需要启动浏览器进程，可能需要 60-90 秒。
    # 预热一次避免第一轮正式执行超时。
    global CLAW_WARMED
    if not CLAW_WARMED:
        CLAW_WARMED = True
        print("[LOOP] OpenClaw cold start warmup...")
        _emit("status", "OpenClaw 浏览器预热中...")
        with CLAW_GLOBAL_LOCK:
            try:
                warmup = claw.execute("navigate to about:blank", timeout=120)
                print(f"[LOOP] Warmup done: success={warmup['success']}")
            except Exception as e:
                print(f"[LOOP] Warmup failed (non-fatal): {e}")

    # === 防死循环：连续失败/空转感知 ===
    _brain_api_fail_count = 0       # Brain API 连续调用失败次数
    _empty_action_count = 0         # 连续空 action 次数
    _recent_actions = []            # 最近 N 次指令指纹 (action, success)
    _recent_fail_types = []         # 最近 N 次失败类型分类
    _MAX_BRAIN_API_FAILS = 3        # Brain API 连续失败 N 次后熔断
    _MAX_EMPTY_ACTIONS = 3          # 连续空 action N 次后熔断
    _MAX_SAME_ACTION_FAILS = 2      # 同一 action 连续失败 N 次后强制换策略
    _MAX_SAME_FAIL_TYPE = 3         # 同一失败类型连续出现 N 次后强制换策略（防"换方向同失败"循环）
    _MAX_TOTAL_FAILS = 6            # 最近 N 轮中总失败次数超过此值后降低温度

    def _classify_failure(content: str) -> str:
        """将失败内容分类，用于检测同类失败循环"""
        c = content.lower()
        if any(kw in c for kw in ["验证码", "captcha", "cap_solver", "人机验证", "安全验证"]):
            return "captcha"
        if any(kw in c for kw in ["403", "forbidden", "blocked", "封禁", "拦截"]):
            return "blocked"
        if any(kw in c for kw in ["限流", "rate limit", "too many", "频率", "频繁"]):
            return "rate_limit"
        if any(kw in c for kw in ["超时", "timeout", "timed out", "卡住", "stuck"]):
            return "timeout"
        if any(kw in c for kw in ["登录", "login", "sign in", "认证", "auth", "token"]):
            return "auth"
        if any(kw in c for kw in ["参数", "parameter", "invalid", "400", "格式错误"]):
            return "param_error"
        if any(kw in c for kw in ["api", "接口", "api key", "密钥"]):
            return "api_error"
        if any(kw in c for kw in ["network", "网络", "connection", "连接", "dns"]):
            return "network"
        if any(kw in c for kw in ["文件锁", "file lock", "permission", "权限", "eacces"]):
            return "file_error"
        return "unknown"

    def _build_fail_warning() -> str:
        """检查是否陷入同类失败循环，生成警告注入 Brain prompt"""
        if len(_recent_fail_types) < _MAX_SAME_FAIL_TYPE:
            return ""
        # 统计最近失败类型频率
        from collections import Counter
        type_counts = Counter(_recent_fail_types)
        most_common_type, most_common_count = type_counts.most_common(1)[0]
        if most_common_type == "unknown" or most_common_count < _MAX_SAME_FAIL_TYPE:
            return ""

        type_names = {
            "captcha": "验证码/人机验证", "blocked": "网站封禁/拦截",
            "rate_limit": "访问限流/频率限制", "timeout": "超时/卡住",
            "auth": "登录/认证问题", "param_error": "API参数错误",
            "api_error": "API调用失败", "network": "网络连接问题",
            "file_error": "文件权限/锁问题",
        }
        name = type_names.get(most_common_type, most_common_type)
        return (
            f"\n\n🚨 严重警告：最近 {most_common_count} 次失败全是同一个类型【{name}】！"
            f"\n换不同的网站/指令/方向并不能解决这个问题，因为根因是 [{name}]。"
            f"\n你必须先解决这个根因，或者彻底放弃需要这个能力的路径："
            f"\n- 验证码问题 → 用不需要登录的公开接口/绕过方式"
            f"\n- 限流问题 → 换平台或等一段时间再试"
            f"\n- API参数问题 → 仔细检查参数格式，参考API文档"
            f"\n- 网络问题 → 检查连接状态"
            f"\n如果无法解决根因，使用 status=blocked 放弃这条路径。"
        )

    while True:
        with state.lock:
            if not state.running:
                print("[LOOP] state.running=False, exiting")
                break
        state.loop_count += 1
        lc = state.loop_count
        if 0 < max_loops < lc:
            print(f"[LOOP] Max loops {max_loops} reached")
            break

        print(f"[LOOP] Round {lc} - start")
        _emit("status", f"Round {lc} - Brain thinking...")

        # Brain thinks
        # === 上下文构建（失败不应该计入 Brain API 熔断） ===
        try:
            cred_summary = build_cred_summary()
        except Exception as e:
            print(f"[LOOP] Round {lc} - 凭据构建失败: {e}")
            cred_summary = ""

        try:
            vector_context = ""
            if vec_mem:
                # Search for memories related to the goal and last feedback
                search_queries = [goal[:100]]
                if last_fb and last_fb != "System just started, take the first action.":
                    search_queries.append(last_fb[:100])

                all_results = []
                for q in search_queries:
                    results = vec_mem.search(q, n_results=3, verified_first=True)
                    all_results.extend(results)

                # Deduplicate by text
                seen = set()
                unique_results = []
                for r in all_results:
                    if r["text"] not in seen:
                        seen.add(r["text"])
                        unique_results.append(r)

                if unique_results:
                    vector_context = format_search_results(unique_results[:5])
        except Exception as e:
            print(f"[LOOP] Round {lc} - 向量搜索失败: {e}")
            vector_context = ""

        try:
            _stuck_warning = _build_fail_warning()
        except Exception as e:
            print(f"[LOOP] Round {lc} - 失败警告构建失败: {e}")
            _stuck_warning = ""

        # === Critic 触发函数：在以下任一条件满足时引入独立诊断 ===
        _critic_triggered = False
        def _try_trigger_critic(reason: str):
            """尝试触发 Critic 诊断，成功返回 True"""
            nonlocal _critic_triggered
            if _critic_triggered:
                return False
            print(f"[LOOP] Round {lc} - 触发 Critic ({reason})")
            try:
                from critic import run_critic
                from page_vision import analyze_page

                similar_cases = []
                if vec_mem:
                    similar_cases = vec_mem.search_failure_cases(last_fb, n_results=3)

                page_elements = {}
                try:
                    page_elements = analyze_page(claw, timeout=20)
                except Exception:
                    pass

                critic_result = run_critic(
                    brain_log=state.brain_log[-10:],
                    failure_cases=similar_cases,
                    current_elements=page_elements,
                    consecutive_fails=0,  # 由 Critic 自行判断严重程度
                )

                confidence = critic_result.get("confidence", 0)
                if confidence >= 0.8:
                    _stuck_warning_ref = (
                        f"\n\n[Critic 诊断 - 置信度 {confidence:.0%}]\n"
                        f"诊断: {critic_result['diagnosis']}\n"
                        f"根因: {critic_result['root_cause']}\n"
                        f"建议: {critic_result['suggested_action']}"
                    )
                    print(f"[LOOP] Critic 强注入: {critic_result['diagnosis'][:60]}")
                    _critic_triggered = True
                    return _stuck_warning_ref
                elif confidence >= 0.5:
                    _stuck_warning_ref = (
                        f"\n\n[Critic 参考 - 置信度 {confidence:.0%}]\n"
                        f"建议: {critic_result['suggested_action']}"
                    )
                    print(f"[LOOP] Critic 弱注入: {critic_result['suggested_action'][:60]}")
                    _critic_triggered = True
                    return _stuck_warning_ref
            except Exception as crit_err:
                print(f"[LOOP] Critic 失败: {crit_err}")
            return None

        # 条件1：同类失败类型 >= 2次
        try:
            if len(_recent_fail_types) >= 2:
                from collections import Counter as _Counter
                _type_counts = _Counter(_recent_fail_types)
                _most_type, _most_count = _type_counts.most_common(1)[0]
                if _most_count >= 2 and _most_type in {"timeout", "unknown", "auth"}:
                    _critic_result = _try_trigger_critic(f"同类失败 {_most_count} 次")
                    if _critic_result:
                        _stuck_warning += _critic_result
        except Exception as e:
            print(f"[LOOP] Round {lc} - Critic 触发检查失败: {e}")

        # action 指纹去重：历史中同指令已失败过 → 追加警告（独立于 Critic 检测）
        try:
            if _recent_actions and action:
                current_fp = action.strip()[:80]
                same_fp_fails = sum(1 for fp, suc in _recent_actions if fp == current_fp and not suc)
                if same_fp_fails >= _MAX_SAME_ACTION_FAILS:
                    _stuck_warning += (
                        f"\n\n⚠️ 重复指令警告：\"{current_fp[:50]}\" 已失败 {same_fp_fails} 次！"
                        f"必须换策略，不能再用同样的指令。"
                    )
        except Exception as e:
            print(f"[LOOP] Round {lc} - 指纹去重检查失败: {e}")

        # 搜索相似失败案例（仅在上一步失败时）
        failure_cases_context = ""
        try:
            if not _recent_actions or (len(_recent_actions) > 0 and not _recent_actions[-1][1]):
                # 上一轮失败了，搜索相似案例
                if vec_mem:
                    cases = vec_mem.search_failure_cases(last_fb, n_results=1)
                    if cases:
                        failure_cases_context = cases[0]["text"][:300]
        except Exception as e:
            print(f"[LOOP] Round {lc} - 失败案例搜索失败: {e}")

        # 构建近期行动摘要：让 Brain 看到自己最近做了什么，自己发现模式
        action_history_summary = ""
        try:
            if len(_recent_actions) >= 3:
                lines = []
                for i, (fp, suc) in enumerate(_recent_actions[-10:], start=1):
                    mark = "OK" if suc else "FAIL"
                    lines.append(f"  {i}. [{mark}] {fp[:60]}")
                action_history_summary = "\n".join(lines)
        except Exception as e:
            print(f"[LOOP] Round {lc} - 行动摘要构建失败: {e}")

        # === 构建 context（所有字段都已安全降级） ===
        recent_checkpoints = checkpoint_journal.recent(limit=5)
        supervisor_context = build_supervisor_context(recent_checkpoints)
        try:
            ctx = {
                "goal": goal,
                "memory_summary": mem.get_summary(),
                "vector_memory": vector_context,
                "knowledge_base": mem.get_knowledge_summary(),
                "wiki_summary": _load_wiki_summary(),
                "last_feedback": last_fb + _stuck_warning,
                "history_summary": mem.get_summary(3),
                "loop_count": lc,
                "credentials": cred_summary,
                "current_date": datetime.now().strftime("%Y年%m月%d日 %A"),
                "artifacts_summary": _get_artifacts_summary(output_manager=getattr(config, 'output_manager', None)),
                "failure_cases": failure_cases_context,
                "action_history": action_history_summary,
                "task_contract": task_contract.build_prompt_context(),
                "decision_contract": build_decision_contract_context(goal, last_fb, lc),
                "checkpoint_context": checkpoint_journal.build_prompt_context(goal, lc),
                "supervisor_context": supervisor_context,
            }
        except Exception as e:
            print(f"[LOOP] Round {lc} - context 构建失败: {e}")
            ctx = {
                "goal": goal, "memory_summary": "", "vector_memory": "",
                "knowledge_base": "", "wiki_summary": "",
                "last_feedback": last_fb, "loop_count": lc,
                "credentials": "", "current_date": str(datetime.now()),
                "task_contract": task_contract.build_prompt_context(),
                "decision_contract": build_decision_contract_context(goal, last_fb, lc),
                "checkpoint_context": checkpoint_journal.build_prompt_context(goal, lc),
                "supervisor_context": supervisor_context,
            }

        # === 检查用户中途注入的消息 ===
        if state.feedback_event.is_set():
            with state.lock:
                fb = state.injected_feedback
                state.injected_feedback = ""
                state.feedback_event.clear()
            if fb:
                print(f"[LOOP] Round {lc} - 收到用户消息: {fb[:80]}")
                last_fb = f"[用户消息] {fb}\n\n{last_fb}" if last_fb else f"[用户消息] {fb}"
                _emit("status", f"Round {lc} - 收到用户消息，已注入Brain上下文")

                # === 训练闭环：积累训练数据 + 触发identity更新 ===
                _accumulate_training_data(fb, thought="", action="")
                # 异步触发identity更新（让Brain消化用户反馈）
                _trigger_identity_update(brain, fb)

        # === 自我反思机制：每5轮触发一次思考过程审视 ===
        _self_reflection_hint = ""
        if lc > 1 and lc % 5 == 0:
            _self_reflection_hint = (
                "\n\n[自我反思提醒] 你已经执行了多轮。在思考下一步之前，"
                "先审视自己最近的决策过程：方向选对了吗？推理链有没有漏洞？"
                "是不是被惯性思维带着走了？有没有可以优化的地方？"
                "把这个反思融入你当前的思考中。"
            )
            print(f"[LOOP] Round {lc} - 触发自我反思")
            _emit("status", f"Round {lc} - 自我反思中...")
        if _self_reflection_hint:
            ctx["last_feedback"] = (ctx.get("last_feedback", "") or "") + _self_reflection_hint

        if brain_v2:
            try:
                v2_context = brain_v2.build_prompt_context(
                    goal=goal,
                    last_feedback=last_fb,
                    loop_count=lc,
                    session_id=getattr(state, "task_id", "default"),
                )
                if v2_context:
                    ctx["knowledge_base"] = (
                        (ctx.get("knowledge_base", "") or "")
                        + "\n\n## Brain V2 增强上下文\n"
                        + v2_context
                    ).strip()
            except Exception as e:
                print(f"[LOOP] BrainV2 context failed (non-fatal): {e}")

        # === Brain API 调用（只有这里失败才计入熔断） ===
        try:
            _dbg(f"Round {lc}: calling brain.think...")
            dec = brain.think(ctx)
            _dbg(f"Round {lc}: brain.think returned, status={dec.get('status')}")
            _brain_api_fail_count = 0  # 调用成功，重置计数
            print(f"[LOOP] Round {lc} - Brain: status={dec.get('status')}, action={dec.get('action_to_openclaw','')[:50]}")
        except Exception as e:
            _brain_api_fail_count += 1
            print(f"[LOOP] Round {lc} - Brain API error: {e} (连续失败 {_brain_api_fail_count}/{_MAX_BRAIN_API_FAILS})")
            traceback.print_exc()

            if _brain_api_fail_count >= _MAX_BRAIN_API_FAILS:
                # 熔断：Brain API 连续失败，停止循环
                print(f"[LOOP] Brain API 连续失败 {_brain_api_fail_count} 次，熔断停止")
                state.brain_log.append({
                    "round": lc, "time": datetime.now().strftime("%H:%M:%S"),
                    "thought": f"Brain API 连续 {_brain_api_fail_count} 次调用失败: {e}",
                    "observation": "brain_api_down",
                    "action": "",
                    "update_memory": "",
                    "status": "blocked",
                })
                state.save_logs()
                _emit("status", f"大脑 API 连续 {_brain_api_fail_count} 次调用失败，系统已停止。请检查网络连接或 API Key。")
                break
            else:
                # 等待更长时间再重试
                print(f"[LOOP] 等待 {_brain_api_fail_count * 10}s 后重试...")
                _wait(_brain_api_fail_count * 10)
                continue

        thought = dec.get("thought", "")
        observation = dec.get("observation", "")
        action = dec.get("action_to_openclaw", "").strip()
        upd = dec.get("update_memory", "")
        st = dec.get("status", "continue")
        if action:
            action, shrink_note = _shrink_overpacked_action(action, goal)
            if shrink_note:
                dec["action_to_openclaw"] = action
                observation = (observation + "\n" if observation else "") + f"[动作压缩] {shrink_note}"
                print(f"[LOOP] Round {lc} - action shrunk: {action}")

        if action:
            try:
                action_risk = assess_action_risk(
                    action=action,
                    thought=thought,
                    goal=goal,
                    last_feedback=last_fb,
                )
            except Exception as e:
                print(f"[LOOP] Round {lc} - action risk check failed (non-fatal): {e}")
                action_risk = {"needs_user": False}

            if action_risk.get("needs_user"):
                st = "need_input"
                dec["status"] = "need_input"
                dec["question_for_user"] = action_risk.get("question", "这个动作需要你确认后再执行。是否允许执行？")
                dec["approval_required"] = True
                observation = (observation + "\n" if observation else "") + f"[执行前确认] {action_risk.get('reason', '')}"
                print(f"[LOOP] Round {lc} - action requires approval: {action[:80]}")
                _emit("status", f"Round {lc} - 高风险动作需要确认")

        # 补全训练数据：如果上轮有用户消息，把Brain的反思也记入训练数据
        if state.injected_feedback == "" and last_fb and "[用户消息]" in (last_fb or ""):
            try:
                # 更新最后一条训练样本的output
                if TRAINING_ACCUMULATOR.exists():
                    lines = TRAINING_ACCUMULATOR.read_text(encoding="utf-8").strip().split("\n")
                    if lines:
                        last_sample = json.loads(lines[-1])
                        if last_sample.get("_type") == "user_feedback" and not last_sample.get("output"):
                            last_sample["output"] = json.dumps({
                                "thought": thought[:300],
                                "action": action[:80],
                            }, ensure_ascii=False)
                            lines[-1] = json.dumps(last_sample, ensure_ascii=False)
                            TRAINING_ACCUMULATOR.write_text("\n".join(lines), encoding="utf-8")
            except Exception:
                pass

        state.brain_log.append({
            "round": lc, "time": datetime.now().strftime("%H:%M:%S"),
            "thought": thought, "observation": observation,
            "action": action, "update_memory": upd, "status": st,
        })
        state.save_logs()

        # Handle need_input
        if st == "need_input":
            question = dec.get("question_for_user", thought) or "System needs your input"
            print(f"[LOOP] Round {lc} - need_input: {question}")
            try:
                checkpoint_journal.record(
                    goal=goal,
                    loop_count=lc,
                    action=action or "need_input",
                    result=question,
                    success=False,
                    thought=thought,
                    status="need_input",
                )
            except Exception as e:
                print(f"[CHECKPOINT] need_input record failed: {e}")

            # 如果 Brain 给了有效的 action 且不是纯等待指令，先执行 action
            # 例如"输入手机号18817378624，等待验证码"——应先执行输入手机号
            _WAIT_ONLY_KEYWORDS = ["无需执行", "等待", "暂停", "不需要操作"]
            is_wait_only = any(kw in action for kw in _WAIT_ONLY_KEYWORDS)
            if action and not is_wait_only and not dec.get("approval_required"):
                print(f"[LOOP] Round {lc} - need_input 但有可执行action，先执行: {action[:60]}...")
                _emit("status", f"Round {lc} - OpenClaw executing... (等待浏览器锁)")
                with CLAW_GLOBAL_LOCK:
                    _emit("status", f"Round {lc} - OpenClaw executing...")
                    try:
                        result = claw.execute(action)
                        print(f"[LOOP] Round {lc} - OpenClaw: success={result['success']}")
                        last_fb = result["content"]
                        # 记录 claw 日志
                        state.claw_log.append({
                            "round": lc, "time": datetime.now().strftime("%H:%M:%S"),
                            "instruction": action, "result": last_fb[:2000],
                            "success": result["success"],
                        })
                        state.save_logs()
                    except Exception as e:
                        print(f"[LOOP] Round {lc} - OpenClaw exception: {e}")
                        last_fb = f"Exception: {e}"

            # === 凭据自动应答 ===
            # 仅当问题是凭据相关时才自动应答，验证码等动态信息不匹配
            auto_answer = _try_auto_answer_credentials(question)

            if auto_answer:
                print(f"[LOOP] Round {lc} - 自动应答: {auto_answer[:80]}...")
                state.chat_history.append({"role": "sys", "text": question})
                state.chat_history.append({"role": "usr", "text": f"[自动应答] {auto_answer}"})
                state.save_logs()
                # 如果 action 已执行，last_fb 是 claw 真实结果，不应被凭据信息覆盖
                if not (action and not is_wait_only):
                    last_fb = f"用户回复（自动）: {auto_answer}"
                _wait(2)
                continue

            if on_input_needed:
                # Direct callback mode (CLI / MCP)
                try:
                    answer = on_input_needed(question)
                except Exception:
                    answer = ""
                # 智能理解用户回复
                smart_answer = _try_smart_answer(answer, question)
                state.chat_history.append({"role": "sys", "text": question})
                state.chat_history.append({"role": "usr", "text": smart_answer})
                state.save_logs()
                last_fb = f"User replied: {smart_answer}" if smart_answer else "User timeout"
                print(f"[LOOP] Round {lc} - User: {smart_answer}")
                try_save_user_input(answer, question)
            else:
                # Event-based mode (Web Console)
                with state.lock:
                    state.pending_question = question
                state.chat_history.append({"role": "sys", "text": question})
                state.save_logs()
                _emit("status", f"Round {lc} - Waiting for user input...")

                state.answer_event.clear()
                state.answer_event.wait(timeout=300)

                with state.lock:
                    state.pending_question = ""
                if not state.user_answer:
                    last_fb = "User timeout"
                else:
                    # 智能理解用户回复：如果用户说"已经填好了"等，自动补全凭据
                    smart_answer = _try_smart_answer(state.user_answer, question)
                    last_fb = f"User replied: {smart_answer}"
                    print(f"[LOOP] Round {lc} - User: {smart_answer}")
                    try_save_user_input(state.user_answer, question)
                    state.user_answer = ""

            _wait(2)
            continue

        # Handle stop/pause
        if st in ("blocked", "pause"):
            print(f"[LOOP] Round {lc} - Brain requested stop: {st}")
            # blocked 时写入失败经验到 Wiki + 失败案例库
            if st == "blocked" and action:
                try:
                    wiki_content = _wiki_brain_summary(
                        brain, goal, action,
                        last_fb[:1000] + "\n" + (result["content"] if result else ""),
                        lc, "failure")
                    if wiki_content:
                        title_line = wiki_content.split("\n")[0].lstrip("# ").strip()
                        _wiki_write_page("failures", title_line, wiki_content,
                                         source=f"blocked round={lc}")
                except Exception as wiki_err:
                    print(f"[WIKI] 失败经验写入失败: {wiki_err}")

                # 写入失败案例库（即使没有自愈，Brain 主动放弃也是宝贵经验）
                _NOTEXIST_KEYWORDS = ["不存在", "找不到", "没有这个", "没有找到", "没有该",
                                      "does not exist", "not found", "not available",
                                      "不存在于", "平台上没有", "无法找到"]
                thought_lower = thought.lower()
                if any(kw in thought_lower for kw in _NOTEXIST_KEYWORDS) and vec_mem:
                    try:
                        vec_mem.add_failure_case(
                            failure_type="feature_not_exist",
                            action=action[:100],
                            error=thought[:200],
                            diagnosis=f"Brain 确认目标不存在: {thought[:100]}",
                            fix=observation[:200] if observation else "需换方向",
                        )
                        print(f"[CASE] 失败案例已写入: feature_not_exist - {action[:50]}")
                    except Exception as case_err:
                        print(f"[CASE] 失败案例写入失败: {case_err}")
            break

        # Update memory
        if upd:
            mem.update_strategy(upd)
        if st == "milestone" and upd:
            mem.add_milestone(upd)
            # milestone 时写入能力认知到 Wiki
            try:
                wiki_content = _wiki_brain_summary(
                    brain, goal, action,
                    result["content"] if result else last_fb[:1000],
                    lc, "capability")
                if wiki_content:
                    title_line = wiki_content.split("\n")[0].lstrip("# ").strip()
                    _wiki_write_page("capabilities", title_line, wiki_content,
                                     source=f"milestone round={lc}")
            except Exception as wiki_err:
                print(f"[WIKI] 能力认知写入失败: {wiki_err}")

        # No action
        if not action:
            _empty_action_count += 1
            print(f"[LOOP] Round {lc} - No action (连续空转 {_empty_action_count}/{_MAX_EMPTY_ACTIONS})")
            if _empty_action_count >= _MAX_EMPTY_ACTIONS:
                print(f"[LOOP] 连续 {_MAX_EMPTY_ACTIONS} 次空 action，停止空转")
                state.brain_log.append({
                    "round": lc, "time": datetime.now().strftime("%H:%M:%S"),
                    "thought": f"大脑连续 {_MAX_EMPTY_ACTIONS} 次未给出可执行指令",
                    "observation": "brain_stuck",
                    "action": "",
                    "update_memory": "",
                    "status": "blocked",
                })
                state.save_logs()
                _emit("status", f"大脑连续 {_MAX_EMPTY_ACTIONS} 次未给出有效指令，系统已停止。")
                break
            # 不覆盖 last_fb，保留上一轮 claw 的真实结果，让 Brain 重试时仍能看到
            _wait(interval)
            continue
        else:
            _empty_action_count = 0  # 有 action，重置空转计数

        # Execute via OpenClaw
        _emit("status", f"Round {lc} - OpenClaw executing... (等待浏览器锁)")
        print(f"[LOOP] Round {lc} - OpenClaw: {action[:60]}...")
        with CLAW_GLOBAL_LOCK:
            _emit("status", f"Round {lc} - OpenClaw executing...")
            try:
                result = claw.execute(action)
                print(f"[LOOP] Round {lc} - OpenClaw: success={result['success']}")
            except Exception as e:
                print(f"[LOOP] Round {lc} - OpenClaw exception: {e}")
                traceback.print_exc()
                result = {"success": False, "content": f"Exception: {e}"}

        # ===== 自愈管道（在验证码检测之前）=====
        if not result["success"]:
            fail_type = _classify_failure(result["content"])
            healable_types = {"timeout", "unknown", "param_error"}
            if fail_type in healable_types and lc > 1:  # 第一轮不自愈（可能是冷启动）
                print(f"[LOOP] Round {lc} - 尝试自愈 (type={fail_type})...")
                _emit("status", f"Round {lc} - 自愈中...")
                try:
                    from self_heal import attempt_heal
                    heal_result = attempt_heal(claw, action, result["content"][:500])
                    if heal_result.get("healed"):
                        print(f"[LOOP] Round {lc} - 自愈成功: {heal_result['diagnosis'][:80]}")
                        result = {"success": True, "content": f"[自愈成功] {heal_result['diagnosis']} | {heal_result['corrected_action']}"}
                        # 记录成功案例
                        if vec_mem:
                            vec_mem.add_failure_case(
                                action=action, error=result["content"][:200],
                                failure_type=fail_type, diagnosis=heal_result.get("diagnosis", ""),
                                fix=heal_result.get("corrected_action", ""),
                            )
                except Exception as heal_err:
                    print(f"[LOOP] Round {lc} - 自愈失败: {heal_err}")

        # ===== 验证码自动检测与解决 =====
        if not result["success"]:
            content_lower = result["content"].lower()
            # 只有真正的验证码关键词才触发验证码解决流程
            # 超时/封禁/网络错误不是验证码，误触发会导致截图失败→误判循环
            captcha_keywords = [
                "验证码", "captcha", "cap_solver", "人机验证", "安全验证", "滑动验证",
                "图片验证", "短信验证", "图形验证", "vcode", "verify",
            ]
            timeout_keywords = ["超时", "timeout", "timed out", "卡住", "stuck"]
            block_keywords = ["403", "forbidden", "blocked", "拦截"]

            has_captcha_signal = any(kw in content_lower for kw in captcha_keywords)
            has_timeout_signal = any(kw in content_lower for kw in timeout_keywords)
            has_block_signal = any(kw in content_lower for kw in block_keywords)

            if has_captcha_signal and not has_timeout_signal:
                # 真正的验证码：截图 + AI 视觉分析
                print(f"[LOOP] Round {lc} - 检测到验证码，启动 AI 视觉分析...")
                _emit("status", f"Round {lc} - 检测到验证码，AI 分析中...")
                try:
                    from captcha_solver import solve_captcha
                    captcha_result = solve_captcha(claw, last_action=action, max_retries=1)
                    if captcha_result["solved"]:
                        print(f"[LOOP] Round {lc} - 验证码已处理: {captcha_result['description'][:100]}")
                        _emit("status", f"Round {lc} - 验证码已解决")
                        result = {
                            "success": True,
                            "content": f"[验证码已解决] {captcha_result['description']}",
                        }
                    else:
                        print(f"[LOOP] Round {lc} - 验证码未解决: {captcha_result['description'][:100]}")
                        _emit("status", f"Round {lc} - 验证码未能自动解决")
                        result["content"] = f"[验证码未解决] {captcha_result['description']}. {result['content']}"
                except Exception as cap_err:
                    print(f"[LOOP] Round {lc} - 验证码解决模块异常: {cap_err}")
            elif has_timeout_signal or has_block_signal:
                # 超时/封禁：不是验证码，附加诊断上下文帮助 Brain 理解真正原因
                diag_parts = []
                if has_timeout_signal:
                    has_partial = "已有部分输出" in result["content"]
                    if has_partial:
                        diag_parts.append("超时但有部分结果——操作可能已成功，只是后续步骤卡住了。先尝试用简短指令获取当前页面内容（如'总结页面内容'），不要放弃浏览器换其他方式")
                    else:
                        diag_parts.append("超时无输出——可能是指令过于复杂、页面加载慢、或环境问题")
                if has_block_signal:
                    diag_parts.append("访问被拦截——可能是网站反自动化机制、需要登录、或IP被限制")
                # 告诉 Brain 这个指令本身是什么，让它判断是"方法问题"还是"环境问题"
                diag_parts.append(f"原始指令: {action[:80]}")
                result["content"] = result["content"] + f" [诊断: {'; '.join(diag_parts)}]"
        # ===== 验证码处理结束 =====

        state.claw_log.append({
            "round": lc, "time": datetime.now().strftime("%H:%M:%S"),
            "instruction": action,
            "result": result["content"], "success": result["success"],
        })
        try:
            checkpoint = checkpoint_journal.record(
                goal=goal,
                loop_count=lc,
                action=action,
                result=result["content"],
                success=result["success"],
                thought=thought,
                status=st,
            )
            print(
                f"[CHECKPOINT] R{lc} phase={checkpoint.phase} "
                f"evidence={checkpoint.evidence_type} quality={checkpoint.quality} "
                f"next={checkpoint.next_decision}"
            )
        except Exception as e:
            print(f"[CHECKPOINT] record failed: {e}")

        # 记录 action 指纹 + 失败类型
        _action_fingerprint = action.strip()[:80]
        _recent_actions.append((_action_fingerprint, result["success"]))
        if len(_recent_actions) > 8:
            _recent_actions = _recent_actions[-8:]

        # 记录失败类型用于同类失败检测
        if not result["success"]:
            _fail_type = _classify_failure(result["content"])
            _recent_fail_types.append(_fail_type)
            if len(_recent_fail_types) > 8:
                _recent_fail_types = _recent_fail_types[-8:]
            print(f"[LOOP] Round {lc} - 失败类型: {_fail_type}")
        else:
            # 成功则清空失败类型历史（重新开始计数）
            _recent_fail_types.clear()

        mem.add_action(action, result["content"], result["success"])
        if brain_v2:
            try:
                brain_v2.remember_round(
                    action=action,
                    result=result["content"],
                    success=result["success"],
                    key_factor=upd or observation,
                )
            except Exception as e:
                print(f"[LOOP] BrainV2 remember failed (non-fatal): {e}")
        last_fb = result["content"] if result["success"] else f"Failed: {result['content']}"
        # 每轮执行后自动持久化
        state.save_logs()

        # 自动检测并收录产物
        if result["success"]:
            _auto_detect_outputs(action, result["content"], output_manager=getattr(config, 'output_manager', None))

        # ===== 市场价值评估：产物做出后评估是否有付费价值 =====
        quality_retries = getattr(config, '_quality_retries', 0)
        max_quality_retries = 2
        if result["success"] and quality_retries < max_quality_retries:
            has_deliverable = any(kw in result["content"] for kw in
                ["已创建", "创建成功", "写入文件", "saved", "created file",
                 ".html", ".py", "index.", "server started", "localhost:"])
            if has_deliverable:
                _emit("status", f"Round {lc} - 市场价值评估中...")
                review = _quality_review(brain, action, result["content"], goal, lc)
                # 记录评估结果
                state.brain_log.append({
                    "round": lc, "time": datetime.now().strftime("%H:%M:%S"),
                    "thought": f"[市场评估] {review['feedback']}",
                    "observation": "market_review",
                    "action": f"score={review['score']}/10, passed={review['passed']}, pivot={review.get('should_pivot', False)}",
                    "update_memory": "", "status": "quality_check",
                })
                state.save_logs()

                if not review["passed"]:
                    if review.get("should_pivot"):
                        # 方向本身市场价值不足，让 Brain 重新评估方向
                        print(f"[LOOP] Round {lc} - 市场价值不足({review['score']}/10)，建议换方向")
                        _emit("status", f"Round {lc} - 市场价值 {review['score']}/10 不足，重新评估方向")
                        last_fb = f"[市场评估未通过 {review['score']}/10] {review['feedback']}\n评估结论：当前产品/服务市场价值不足，应重新评估方向——不是改进产品，而是思考方向本身的问题。"
                        config._quality_retries = max_quality_retries  # 不再重试，直接让Brain重新评估
                    elif review.get("improve_action"):
                        # 有改进空间，构建市场导向的改进指令
                        print(f"[LOOP] Round {lc} - 市场价值偏低({review['score']}/10)，改进中...")
                        _emit("status", f"Round {lc} - 市场价值 {review['score']}/10 偏低，优化中...")
                        last_fb = f"[市场评估未通过 {review['score']}/10] {review['feedback']}\n改进方向: {review['improve_action']}"

                        if vec_mem:
                            try:
                                vec_mem.add_memory(
                                    f"[市场评估低] score={review['score']}/10, action={action[:100]}, "
                                    f"risk={review.get('market_risk', '')[:200]}",
                                    category="market_fail",
                                    source="market_review",
                                    metadata={"loop": lc, "score": review['score']},
                                    verified=True,
                                )
                            except Exception:
                                pass

                    _wait(interval)
                    continue
                else:
                    # 市场评估通过，将方向分析写入 Wiki
                    print(f"[LOOP] Round {lc} - 市场价值 {review['score']}/10 通过，写入 Wiki")
                    try:
                        wiki_content = _wiki_brain_summary(
                            brain, goal, action, result["content"], lc, "direction")
                        if wiki_content:
                            # 用评估结果的第一行作为标题
                            title_line = wiki_content.split("\n")[0].lstrip("# ").strip()
                            _wiki_write_page("directions", title_line, wiki_content,
                                             source=f"market_review score={review['score']}")
                    except Exception as wiki_err:
                        print(f"[WIKI] 方向分析写入失败: {wiki_err}")
        # ===== 市场价值评估结束 =====

        # Store to vector memory for semantic retrieval
        if vec_mem:
            try:
                result_text = f"[执行] {action}\n[结果] {'成功' if result['success'] else '失败'}: {result['content'][:200]}"
                # 成功执行的记忆默认未验证，等待 auto_promote 自动提升
                vec_mem.add_memory(
                    result_text,
                    category="action_result" if result["success"] else "error",
                    source="openclaw",
                    metadata={"loop": lc, "success": result["success"]},
                    verified=False,
                )
                # 每10轮自动检查一次是否有可提升的已验证记忆
                if lc % 10 == 0 and result["success"]:
                    try:
                        promoted = vec_mem.auto_promote_verified(min_occurrences=3)
                        if promoted > 0:
                            _emit("status", f"Round {lc} - Auto-promoted {promoted} memories to verified")
                    except Exception:
                        pass
            except Exception as e:
                print(f"[LOOP] Vector store failed (non-fatal): {e}")
        else:
            print(f"[LOOP] Vector memory disabled, skipping store")

        print(f"[LOOP] Round {lc} - waiting {interval}s...")
        _wait(interval)
        print(f"[LOOP] Round {lc} - wait done, loop_count={state.loop_count}, max_loops={max_loops}")

    with state.lock:
        state.running = False
    _emit("status", "Stopped")
    state.save_logs()
    print("[LOOP] run_loop ended")


# ===================== Health Check =====================

def run_health_check(
    brain_api_key: str = "",
    brain_base_url: str = "https://api.deepseek.com/v1",
    brain_model: str = "deepseek-chat",
    gateway_url: str = "http://127.0.0.1:18789",
    node_dir: str = "",
) -> list:
    """
    Run system health checks. Returns list of (check_name, passed, message).
    """
    results = []

    # 1. API key
    if brain_api_key:
        try:
            import openai
            client = openai.OpenAI(api_key=brain_api_key, base_url=brain_base_url)
            models = client.models.list()
            results.append(("Brain API", True, f"Connected, {len(list(models))} models available"))
        except Exception as e:
            results.append(("Brain API", False, str(e)[:80]))
    else:
        results.append(("Brain API", False, "No API key configured"))

    # 2. Gateway
    try:
        import urllib.request
        req = urllib.request.urlopen(f"{gateway_url}/health", timeout=5)
        results.append(("OpenClaw Gateway", req.status == 200, f"HTTP {req.status}"))
    except Exception as e:
        results.append(("OpenClaw Gateway", False, str(e)[:80]))

    # 3. Credential store
    try:
        from credential_store import list_accounts
        accs = list_accounts(mask=True)
        results.append(("Credential Store", True, f"{len(accs)} account(s) stored"))
    except Exception as e:
        results.append(("Credential Store", False, str(e)[:80]))

    # 4. Memory file
    from pathlib import Path
    mem_file = Path("system_memory.json")
    if mem_file.exists():
        try:
            import json
            data = json.loads(mem_file.read_text(encoding="utf-8"))
            n = len(data.get("actions_history", []))
            results.append(("Memory File", True, f"OK, {n} action(s) recorded"))
        except Exception as e:
            results.append(("Memory File", False, str(e)[:80]))
    else:
        results.append(("Memory File", True, "Not created yet (will be created on first run)"))

    # 5. Vector memory
    try:
        from vector_memory import get_vector_memory
        vm = get_vector_memory()
        vm_stats = vm.get_stats()
        vm_available = vm_stats.get("available", True)
        vm_detail = (
            f"{vm_stats.get('total', 0)} memories, "
            f"api={'ok' if vm_stats.get('api_available') else 'no key'}, "
            f"fallback={vm_stats.get('fallback', False)}"
        )
        if vm_stats.get("error"):
            vm_detail += f", {vm_stats.get('error')}"
        results.append(("Vector Memory", vm_available, vm_detail))
    except Exception as e:
        results.append(("Vector Memory", False, str(e)[:80]))

    return results
