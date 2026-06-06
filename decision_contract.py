"""
Decision contract for the autonomous loop.

This module turns the useful ideas from the newer claw-brain draft into
small, stable rules that the current system can use without a full rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ValidationRule:
    keywords: tuple[str, ...]
    text: str


VALIDATION_RULES: tuple[ValidationRule, ...] = (
    ValidationRule(
        ("测试", "能力测试", "自检", "修复", "排查", "debug", "test", "doctor", "about:blank"),
        "系统测试/修复：先跑 1 个最小只读动作，并拿到明确证据。比如页面标题、文件存在、接口 200、错误日志消失。不要套商业曝光或触达量。",
    ),
    ValidationRule(
        ("闲鱼", "二手", "卖货", "商品", "上架"),
        "闲鱼/卖货：先跑到至少 500 次曝光。重点看咨询数、收藏数、下单数。没有跑够 500 曝光，不要只凭感觉换方向。",
    ),
    ValidationRule(
        ("邮件", "外贸", "客户开发", "bd", "销售", "私信", "触达"),
        "客户触达：先触达至少 50 个精准客户。重点看回复、预约、报价请求。AI 能一天发 20 封，就不要按人工慢节奏拖。",
    ),
    ValidationRule(
        ("抖音", "小红书", "内容", "视频", "账号"),
        "内容验证：先发 3-5 条同方向内容。重点看播放、完播、评论、私信、涨粉，不只看主观感觉。",
    ),
    ValidationRule(
        ("落地页", "网站", "表单", "产品页", "demo"),
        "产品页验证：先拿到至少 100 次有效访问，或 20 个精准用户查看。重点看点击、留资、咨询。",
    ),
)

DEFAULT_VALIDATION = (
    "通用验证：先跑够一个最小真实样本。默认至少 20 个精准触达，或 100 次真实曝光。"
    "判断只看真实动作数据：曝光、点击、咨询、回复、成交、复购。"
)

RISK_KEYWORDS: tuple[str, ...] = (
    "发布", "上架", "提交", "发出", "发送", "群发", "私信", "评论", "回复客户",
    "付款", "支付", "购买", "下单", "充值", "转账", "订阅", "开通",
    "删除", "清空", "注销", "解绑", "修改密码", "改价", "确认收货",
    "publish", "post", "submit", "send", "delete", "pay", "buy", "purchase",
)

READ_ONLY_HINTS: tuple[str, ...] = (
    "查看", "读取", "搜索", "调研", "打开", "截图", "分析", "预览", "下载",
    "保存草稿", "创建草稿", "只读", "read", "search", "open", "screenshot",
)

APPROVAL_HINTS: tuple[str, ...] = (
    "允许执行", "同意执行", "确认执行", "可以执行", "批准执行",
)

TOOL_STRATEGY = """工具策略：
- 不要把系统做成很多脚本堆在一起。新增能力前，先判断它属于流程、判断、证据、复盘，还是底层工具。
- 流程整合、判断逻辑、证据记录、复盘系统、交付包装，应该沉淀到 ClawBrain 自己的核心。
- 已成熟、便宜、稳定的底层能力，优先接入现成工具，不要为了证明能力从零重造。
- 爬取、转写、模型接口、账号管理、浏览器自动化、文件转换，默认先找成熟工具。
- 如果必须新写脚本，要写清楚：为什么现成工具不够用、脚本归属哪个流程、产出的证据是什么、后续如何复用。"""


def _contains_any(text: str, keywords: Iterable[str]) -> bool:
    lower = text.lower()
    return any(keyword.lower() in lower for keyword in keywords)


def choose_validation_rule(goal: str) -> str:
    """Pick the smallest useful validation rule for the current goal."""
    goal = goal or ""
    for rule in VALIDATION_RULES:
        if _contains_any(goal, rule.keywords):
            return rule.text
    return DEFAULT_VALIDATION


def build_decision_contract_context(
    goal: str,
    last_feedback: str = "",
    loop_count: int = 0,
) -> str:
    """Build short prompt context for autonomous decision quality."""
    validation = choose_validation_rule(goal)
    return f"""对标目标：系统要像项目负责人一样工作，不是只会执行命令。

必须做到：
1. 先判断当前阶段：调研、构建、验证、获客、变现。
2. 每一步都说清楚：目标是什么，下一步为什么最有用。
3. 商业判断不能只凭感觉，必须跑够最低验证量。
4. 关键动作后要验证真实结果，不把“命令成功”当成“事情成功”。
5. 遇到同类失败 2-3 次，要换方法或停下来说明原因。

最低验证量：
{validation}

{TOOL_STRATEGY}

执行前安全门：
- 只读动作可以直接做，比如搜索、查看、截图、分析。
- 影响真实账号、外部用户、钱、发布内容、商品状态、客户消息、数据删除的动作，必须先问用户确认。
- 没有确认时，status 用 need_input，只问一个最关键的问题。

当前轮次：{loop_count}
上一轮反馈摘要：{(last_feedback or '')[:300]}"""


def user_approved_last_turn(last_feedback: str) -> bool:
    return _contains_any(last_feedback or "", APPROVAL_HINTS)


def assess_action_risk(
    action: str,
    thought: str = "",
    goal: str = "",
    last_feedback: str = "",
) -> dict:
    """Return whether an action must wait for user confirmation."""
    action = (action or "").strip()
    if not action:
        return {"needs_user": False, "reason": ""}

    combined = f"{action}\n{thought or ''}"
    matched = [kw for kw in RISK_KEYWORDS if kw.lower() in combined.lower()]
    if not matched:
        return {"needs_user": False, "reason": ""}

    if _contains_any(action, READ_ONLY_HINTS) and not any(
        kw in action for kw in ("删除", "付款", "支付", "购买", "下单", "发布", "上架", "发送", "群发")
    ):
        return {"needs_user": False, "reason": ""}

    if user_approved_last_turn(last_feedback):
        return {
            "needs_user": False,
            "reason": "用户上一轮已明确允许执行。",
            "matched_keywords": matched,
        }

    short_action = action[:120]
    reason = "动作会影响真实账号、外部用户、钱、发布状态或数据安全。"
    question = (
        f"{reason}\n"
        f"即将执行：{short_action}\n"
        "是否允许执行？如果允许，请回复：允许执行。"
    )
    return {
        "needs_user": True,
        "reason": reason,
        "question": question,
        "matched_keywords": matched,
    }
