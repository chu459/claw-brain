"""
Segment supervisor for ClawBrain checkpoints.

This is intentionally not a real-time blocker. It reviews a small batch of
checkpoints and tells the main loop whether to continue, verify, change method,
or wait for the user.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SupervisorReview:
    decision: str
    severity: str
    reason: str
    suggestions: list[str] = field(default_factory=list)
    evidence_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "severity": self.severity,
            "reason": self.reason,
            "suggestions": self.suggestions,
            "evidence_summary": self.evidence_summary,
        }


def review_checkpoints(rows: list[dict[str, Any]]) -> SupervisorReview:
    if not rows:
        return SupervisorReview(
            decision="plan_minimal_action",
            severity="info",
            reason="还没有检查点。下一步必须先定义最小动作和预期证据。",
            suggestions=["先做一个只读或低风险动作，并说明做完后拿什么证据。"],
        )

    recent = rows[-5:]
    bad = [r for r in recent if r.get("quality") == "bad"]
    weak = [r for r in recent if r.get("quality") == "weak"]
    needs_user = [r for r in recent if r.get("quality") == "needs_user"]
    evidence_types = [r.get("evidence_type", "") for r in recent]

    evidence_summary = "; ".join(
        f"R{r.get('loop_count')}:{r.get('quality')}/{r.get('evidence_type')}"
        for r in recent
    )

    if needs_user and needs_user[-1] == recent[-1]:
        return SupervisorReview(
            decision="wait_for_user",
            severity="high",
            reason="最新检查点需要用户确认，不能继续执行真实影响动作。",
            suggestions=["等待用户确认，或改成搜索、截图、分析等只读动作。"],
            evidence_summary=evidence_summary,
        )

    if len(bad) >= 2:
        return SupervisorReview(
            decision="change_method",
            severity="high",
            reason="最近几步出现多次失败，继续重复执行会浪费时间。",
            suggestions=[
                "先缩小动作，只验证通道是否正常。",
                "换成熟工具或更简单工具，不要继续堆脚本。",
                "把失败原因写入记忆，再换方法。",
            ],
            evidence_summary=evidence_summary,
        )

    if len(weak) >= 2:
        return SupervisorReview(
            decision="verify_result",
            severity="medium",
            reason="最近多次证据偏弱，只知道命令执行了，不知道目标是否达成。",
            suggestions=[
                "下一步只做验证动作：截图、读取页面、统计数量、检查文件是否存在。",
                "拿到数量证据后再继续推进。",
            ],
            evidence_summary=evidence_summary,
        )

    if recent[-1].get("next_decision") == "continue_until_minimum_sample":
        return SupervisorReview(
            decision="continue_until_minimum_sample",
            severity="info",
            reason="已有真实动作数据，但还要跑够最低验证量。",
            suggestions=["继续扩大样本，但每次都记录曝光、触达、咨询、回复或成交数量。"],
            evidence_summary=evidence_summary,
        )

    if len(set(evidence_types[-3:])) == 1 and evidence_types[-1] == "execution_result":
        return SupervisorReview(
            decision="collect_stronger_evidence",
            severity="medium",
            reason="连续检查点都只是执行结果，缺少外部世界证据。",
            suggestions=["下一步改成读取结果、截图、统计数量或检查真实平台状态。"],
            evidence_summary=evidence_summary,
        )

    return SupervisorReview(
        decision="continue",
        severity="info",
        reason="最近检查点没有明显风险，可以继续主循环。",
        suggestions=["继续做当前最小动作，动作后保存证据。"],
        evidence_summary=evidence_summary,
    )


def build_supervisor_context(rows: list[dict[str, Any]]) -> str:
    review = review_checkpoints(rows)
    suggestions = "\n".join(f"- {item}" for item in review.suggestions)
    return f"""分段监督结论：
decision={review.decision}
severity={review.severity}
reason={review.reason}
evidence={review.evidence_summary or "暂无"}
建议：
{suggestions}

执行要求：
- severity=high 时，下一步不要继续原动作；先验证、换方法、缩小动作或等待用户。
- severity=medium 时，下一步优先补强证据。
- severity=info 时，继续主循环，但动作后必须记录证据。"""
