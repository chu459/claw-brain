"""
Task contract for each ClawBrain run.

It freezes the target, minimum validation rule, expected evidence, and tool
policy before the loop starts. The loop can then stay focused instead of
turning into scattered scripts.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from decision_contract import TOOL_STRATEGY, choose_validation_rule


def _safe_session_id(session_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", session_id or "default")


def _pick_phase(goal: str) -> str:
    text = (goal or "").lower()
    if any(word in text for word in ("闲鱼", "卖货", "商品", "上架", "成交", "订单")):
        return "卖货验证"
    if any(word in text for word in ("邮件", "客户", "获客", "触达", "销售", "bd")):
        return "获客验证"
    if any(word in text for word in ("抖音", "视频", "内容", "账号", "小红书")):
        return "内容验证"
    if any(word in text for word in ("网站", "落地页", "产品页", "demo", "表单")):
        return "产品验证"
    if any(word in text for word in ("修复", "bug", "报错", "失败", "问题")):
        return "系统修复"
    if any(word in text for word in ("整理", "文档", "报告", "总结", "归档")):
        return "资料整理"
    return "通用推进"


def _expected_evidence(goal: str, phase: str) -> list[str]:
    if phase == "卖货验证":
        return ["曝光数", "咨询数", "收藏数", "下单数", "截图或平台记录"]
    if phase == "获客验证":
        return ["触达客户数", "回复数", "意向数", "客户名单或发送记录"]
    if phase == "内容验证":
        return ["发布条数", "播放量", "完播或互动数据", "评论或私信反馈"]
    if phase == "产品验证":
        return ["访问数", "点击数", "留资数", "用户反馈", "页面截图"]
    if phase == "系统修复":
        return ["复现记录", "修复文件", "测试结果", "失败日志是否消失"]
    if phase == "资料整理":
        return ["生成文件路径", "整理范围", "缺口清单", "可继续使用的索引"]
    return ["真实动作记录", "数量数据", "结果截图", "下一步判断依据"]


@dataclass
class TaskContract:
    session_id: str
    goal: str
    goal_summary: str
    phase: str
    minimum_validation: str
    expected_evidence: list[str]
    checkpoint_policy: str
    tool_policy: str
    user_risk_policy: str
    created_at: str = ""
    source: str = "task_contract.py"
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def build_prompt_context(self) -> str:
        evidence = "、".join(self.expected_evidence)
        notes = "\n".join(f"- {item}" for item in self.notes) if self.notes else "- 暂无"
        return f"""任务目标契约：
目标：{self.goal_summary}
阶段：{self.phase}
最低验证量：{self.minimum_validation}
预期证据：{evidence}
检查点规则：{self.checkpoint_policy}
工具策略：{self.tool_policy}
安全边界：{self.user_risk_policy}
补充说明：
{notes}

执行要求：
- 每一轮都围绕这个目标契约推进。
- 先做当前最小有效动作，再拿证据。
- 不要只把“命令成功”当成“目标成功”。
- 证据不足时，下一步优先补证据。
- 真实发布、发消息、上架、付款、删除、改价前必须问用户。"""


def create_task_contract(
    base_dir: str | Path,
    session_id: str,
    goal: str,
) -> TaskContract:
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)

    phase = _pick_phase(goal)
    contract = TaskContract(
        session_id=session_id or "default",
        goal=goal or "",
        goal_summary=(goal or "").strip()[:300] or "未写明目标",
        phase=phase,
        minimum_validation=choose_validation_rule(goal or ""),
        expected_evidence=_expected_evidence(goal or "", phase),
        checkpoint_policy="每个关键动作后记录：目标、最小动作、证据、质量判断、下一步。",
        tool_policy="流程、判断、证据、复盘留在核心；爬取、转写、浏览器自动化、文件转换优先接成熟工具。",
        user_risk_policy="只读动作可直接执行；影响账号、客户、钱、发布状态或数据安全的动作必须先确认。",
        created_at=datetime.now().isoformat(timespec="seconds"),
        notes=[
            "这是任务开始前的约束，不是事后复盘。",
            "如果最低验证量没有跑够，不要只凭感觉换方向。",
        ],
    )

    path = base / f"{_safe_session_id(session_id)}.json"
    path.write_text(json.dumps(contract.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    latest = base / "latest.json"
    latest.write_text(json.dumps(contract.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return contract


def read_task_contract(base_dir: str | Path, session_id: str = "") -> dict[str, Any] | None:
    base = Path(base_dir)
    if session_id:
        path = base / f"{_safe_session_id(session_id)}.json"
    else:
        path = base / "latest.json"
        if not path.exists():
            files = sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            files = [p for p in files if p.name != "latest.json"]
            path = files[0] if files else path

    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_file"] = str(path)
        return data
    except Exception:
        return None


def build_task_contract_context(base_dir: str | Path, session_id: str = "") -> str:
    data = read_task_contract(base_dir, session_id=session_id)
    if not data:
        return ""
    evidence = "、".join(data.get("expected_evidence", []))
    return f"""任务目标契约：
目标：{data.get('goal_summary', '')}
阶段：{data.get('phase', '')}
最低验证量：{data.get('minimum_validation', '')}
预期证据：{evidence}
检查点规则：{data.get('checkpoint_policy', '')}
工具策略：{data.get('tool_policy', '')}
安全边界：{data.get('user_risk_policy', '')}"""
