"""
Checkpoint journal for the ClawBrain main loop.

It keeps the system centered on one loop:
goal -> minimal action -> evidence -> judgment -> next decision.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class CycleCheckpoint:
    session_id: str
    loop_count: int
    goal: str
    phase: str
    minimal_action: str
    evidence_type: str
    evidence: str
    success: bool
    quality: str
    next_decision: str
    warnings: list[str] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


PHASE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("research", ("搜索", "调研", "查看", "分析", "竞品", "市场", "search", "research")),
    ("build", ("创建", "生成", "写入", "开发", "修改", "实现", "build", "create", "write")),
    ("validate", ("验证", "测试", "曝光", "点击", "咨询", "回复", "反馈", "test", "verify")),
    ("acquire", ("触达", "私信", "邮件", "客户", "获客", "send", "email", "dm")),
    ("monetize", ("成交", "付款", "下单", "收入", "订单", "pay", "order", "revenue")),
)

EVIDENCE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("file_created", ("已创建", "写入文件", "saved", "created", ".html", ".md", ".py", ".json")),
    ("page_state", ("url", "title", "页面", "截图", "screenshot", "浏览器状态", "snapshot", "page title")),
    ("customer_touch", ("触达", "私信", "邮件", "发送", "客户", "email", "sent")),
    ("exposure", ("曝光", "浏览量", "播放量", "访问量", "views", "impressions")),
    ("inquiry", ("咨询", "回复", "留言", "私信", "comment", "reply")),
    ("payment", ("付款", "成交", "订单", "收入", "paid", "order", "revenue")),
    ("user_feedback", ("用户", "反馈", "评价", "rating", "feedback")),
)

WEAK_SUCCESS_HINTS = (
    "执行成功",
    "无文字输出",
    "指令发出",
    "success",
    "done",
)

FAIL_HINTS = (
    "失败",
    "错误",
    "超时",
    "exception",
    "error",
    "failed",
    "timeout",
)


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    lower = (text or "").lower()
    return any(keyword.lower() in lower for keyword in keywords)


def infer_phase(goal: str, action: str, result: str = "") -> str:
    text = f"{goal}\n{action}\n{result}"
    for phase, keywords in PHASE_KEYWORDS:
        if _contains_any(text, keywords):
            return phase
    return "decide"


def infer_evidence_type(action: str, result: str) -> str:
    text = f"{action}\n{result}"
    for evidence_type, keywords in EVIDENCE_KEYWORDS:
        if _contains_any(text, keywords):
            return evidence_type
    return "execution_result"


def has_numeric_evidence(text: str) -> bool:
    return bool(re.search(r"\d+", text or ""))


def judge_quality(success: bool, evidence_type: str, evidence: str) -> tuple[str, list[str]]:
    warnings: list[str] = []
    evidence = evidence or ""

    if not success or _contains_any(evidence, FAIL_HINTS):
        return "bad", ["执行失败，需要先修复、换工具或缩小动作。"]

    if evidence_type in {"exposure", "inquiry", "payment", "customer_touch"} and not has_numeric_evidence(evidence):
        warnings.append("缺少数量证据。只知道做了，不知道效果。")
        return "weak", warnings

    if _contains_any(evidence, WEAK_SUCCESS_HINTS) and evidence_type == "execution_result":
        warnings.append("证据偏弱。命令成功不等于目标成功。")
        return "weak", warnings

    return "good", warnings


def choose_next_decision(quality: str, evidence_type: str) -> str:
    if quality == "bad":
        return "fix_or_change_method"
    if quality == "weak":
        return "verify_result"
    if evidence_type in {"payment", "inquiry", "customer_touch", "exposure"}:
        return "continue_until_minimum_sample"
    return "continue"


class CheckpointJournal:
    def __init__(self, data_dir: str | Path, session_id: str = "default"):
        self.data_dir = Path(data_dir)
        self.session_id = session_id or "default"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        safe_session = re.sub(r"[^a-zA-Z0-9_.-]+", "_", self.session_id)
        self.jsonl_file = self.data_dir / f"{safe_session}.jsonl"
        self.latest_file = self.data_dir / f"{safe_session}.latest.json"

    def record(
        self,
        goal: str,
        loop_count: int,
        action: str,
        result: str,
        success: bool,
        thought: str = "",
        status: str = "",
    ) -> CycleCheckpoint:
        phase = infer_phase(goal, action, result)
        evidence_type = infer_evidence_type(action, result)
        evidence = (result or "").strip()[:1200]
        quality, warnings = judge_quality(success, evidence_type, evidence)
        next_decision = choose_next_decision(quality, evidence_type)
        if status == "need_input":
            quality = "needs_user"
            next_decision = "wait_for_user"

        checkpoint = CycleCheckpoint(
            session_id=self.session_id,
            loop_count=loop_count,
            goal=(goal or "")[:500],
            phase=phase,
            minimal_action=(action or "")[:500],
            evidence_type=evidence_type,
            evidence=evidence,
            success=success,
            quality=quality,
            next_decision=next_decision,
            warnings=warnings,
            created_at=datetime.now().isoformat(timespec="seconds"),
        )

        payload = checkpoint.to_dict()
        if thought:
            payload["thought"] = thought[:500]
        if status:
            payload["status"] = status

        with open(self.jsonl_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.latest_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return checkpoint

    def recent(self, limit: int = 5) -> list[dict[str, Any]]:
        if not self.jsonl_file.exists():
            return []
        try:
            lines = self.jsonl_file.read_text(encoding="utf-8").splitlines()
            rows = []
            for line in lines[-limit:]:
                if line.strip():
                    rows.append(json.loads(line))
            return rows
        except Exception:
            return []

    def build_prompt_context(self, goal: str, loop_count: int, limit: int = 4) -> str:
        rows = self.recent(limit=limit)
        lines = [
            "主循环检查点：系统必须按“目标 -> 最小动作 -> 证据 -> 判断 -> 下一步”推进。",
            f"当前目标：{(goal or '')[:160]}",
            f"当前轮次：{loop_count}",
        ]

        if not rows:
            lines.append("暂无检查点。本轮要先明确最小动作，并计划做完后拿什么证据。")
            return "\n".join(lines)

        lines.append("最近检查点：")
        for row in rows:
            warn = "; ".join(row.get("warnings", []))
            lines.append(
                f"- R{row.get('loop_count')}: phase={row.get('phase')}, "
                f"action={row.get('minimal_action', '')[:80]}, "
                f"evidence={row.get('evidence_type')}, quality={row.get('quality')}, "
                f"next={row.get('next_decision')}"
                + (f", warning={warn}" if warn else "")
            )

        if loop_count > 0 and loop_count % 3 == 0:
            lines.append("本轮是检查点轮：先检查最近几步证据够不够，再决定继续、改方向或停止。")

        return "\n".join(lines)


def create_checkpoint_journal(
    base_dir: str | Path,
    session_id: str = "default",
) -> CheckpointJournal:
    return CheckpointJournal(Path(base_dir), session_id=session_id)
