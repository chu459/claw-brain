"""
critic.py - 独立批判者
======================
Brain 连续失败时，独立 LLM 调用提供第三方诊断。

功能：
  1. 接收 Brain 最近日志、历史失败案例、当前页面元素
  2. 用 DeepSeek API 独立分析问题根因
  3. 返回结构化诊断 + 修正建议 + 置信度

依赖：
  - openai (pip install openai)
  - DeepSeek API (环境变量 DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL)
"""

import json
import os
from pathlib import Path

# 自动加载 .env
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

# DeepSeek API 配置
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.environ.get("BRAIN_MODEL", "deepseek-chat")

# Critic system prompt
_CRITIC_SYSTEM_PROMPT = """你是 Claw-brain 系统的独立诊断专家。Brain（策略大脑）连续失败了，你从外部视角分析问题。
你的输入：Brain 最近的决策日志、历史相似失败案例、当前页面状态。
你的输出：精准诊断 + 可执行的修正建议 + 置信度评分。

关键原则：
1. Brain 看不到页面，它的诊断可能基于猜测——你要基于实际页面状态判断
2. 历史案例是已验证的修复方案——优先参考
3. 如果页面元素和 Brain 预期不符，说明页面状态已变化
4. 给出具体的、可执行的修正指令（≤40字）
5. 置信度 >0.8 才值得采纳

特别关注——"功能不存在"模式：
如果 Brain 的日志显示它在反复搜索/查找同一类东西（如插件、API、功能模块），每次换词但目标一致，这通常意味着这个东西在当前平台不存在。
此时你应该：
- 明确指出"目标功能可能不存在于当前平台"
- 建议替代方案：自己写代码实现、换平台、用原生API、用其他工具链
- 不要建议"再搜一次"或"换个搜索词"

输出 JSON 格式：
{"diagnosis": "一句话诊断", "root_cause": "根因分析", "suggested_action": "≤40字修正指令", "confidence": 0.0-1.0, "reasoning": "推理过程"}"""


def run_critic(
    brain_log: list,
    failure_cases: list,
    current_elements: dict,
    consecutive_fails: int,
) -> dict:
    """
    运行独立批判者。
    返回 {"diagnosis": str, "root_cause": str, "suggested_action": str, "confidence": float, "reasoning": str}
    """
    if not DEEPSEEK_API_KEY:
        print("[CRITIC] 未配置 DEEPSEEK_API_KEY")
        return _empty_result()

    # 构建输入
    prompt = _build_prompt(brain_log, failure_cases, current_elements, consecutive_fails)

    try:
        import openai
        import httpx

        # 代理设置（与 Brain 一致）
        proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                 or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or None)
        if proxy:
            client = openai.OpenAI(
                api_key=DEEPSEEK_API_KEY,
                base_url=DEEPSEEK_BASE_URL,
                http_client=httpx.Client(proxy=proxy),
            )
        else:
            client = openai.OpenAI(
                api_key=DEEPSEEK_API_KEY,
                base_url=DEEPSEEK_BASE_URL,
            )

        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": _CRITIC_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
            max_tokens=256,
            timeout=30,
        )
        raw = response.choices[0].message.content.strip()
        return _parse_result(raw)
    except Exception as e:
        print(f"[CRITIC] DeepSeek API 调用失败: {e}")
        return _empty_result()


def _build_prompt(brain_log: list, failure_cases: list, current_elements: dict, consecutive_fails: int) -> str:
    """构建 Critic 的输入 prompt。"""
    # Brain 日志（最近10轮，让 Critic 自己发现跨轮次模式）
    log_lines = []
    for entry in brain_log[-10:]:
        round_num = entry.get("round", "?")
        thought = entry.get("thought", "")[:100]
        action = entry.get("action", "")[:80]
        status = entry.get("status", "")
        log_lines.append(f"Round {round_num}: action={action}, status={status}, thought={thought}")
    brain_log_text = "\n".join(log_lines) if log_lines else "(无日志)"

    # 历史失败案例
    cases_text = ""
    if failure_cases:
        case_lines = []
        for c in failure_cases[:3]:
            case_lines.append(f"- {c.get('text', '')[:200]}")
        cases_text = "\n".join(case_lines)

    # 当前页面元素
    elements_text = ""
    if current_elements and current_elements.get("elements"):
        title = current_elements.get("title", "")
        elem_lines = [f"页面标题: {title}"]
        for elem in current_elements["elements"][:10]:
            elem_lines.append(f"  [{elem['id']}] {elem['type']}: {elem.get('label', '')} ({elem.get('description', '')[:50]})")
        elements_text = "\n".join(elem_lines)

    prompt = f"""## 连续失败次数
{consecutive_fails}

## Brain 最近决策日志
{brain_log_text}

## 历史相似失败案例
{cases_text if cases_text else "(无相似案例)"}

## 当前页面交互元素
{elements_text if elements_text else "(无法获取页面元素)"}

请分析以上信息，给出诊断和修正建议。"""

    return prompt


def _parse_result(raw: str) -> dict:
    """解析 Critic 返回的 JSON 结果。"""
    default = _empty_result()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 尝试提取 JSON
        import re
        json_match = re.search(r'\{[\s\S]*\}', raw)
        if json_match:
            try:
                data = json.loads(json_match.group())
            except json.JSONDecodeError:
                return default
        else:
            return default

    confidence = float(data.get("confidence", 0))
    confidence = max(0.0, min(1.0, confidence))  # 限制在 0-1

    return {
        "diagnosis": str(data.get("diagnosis", "")),
        "root_cause": str(data.get("root_cause", "")),
        "suggested_action": str(data.get("suggested_action", ""))[:40],
        "confidence": confidence,
        "reasoning": str(data.get("reasoning", "")),
    }


def _empty_result() -> dict:
    return {
        "diagnosis": "",
        "root_cause": "",
        "suggested_action": "",
        "confidence": 0.0,
        "reasoning": "Critic 未能生成有效诊断",
    }
