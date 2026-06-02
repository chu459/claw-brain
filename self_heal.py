"""
self_heal.py - 自愈管道
========================
执行失败 -> 截图诊断 -> 生成修正指令 -> 自动重试（仅一次）

功能：
  1. 截取当前浏览器页面
  2. 用 qwen-vl-plus 分析失败原因
  3. 生成修正指令并用 OpenClaw 重试一次
  4. 返回自愈结果

依赖：
  - openai (pip install openai)
  - 百炼 API Key (环境变量 DASHSCOPE_API_KEY)
"""

import base64
import json
import os
import re
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

# 百炼视觉模型配置
VL_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
VL_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
VL_MODEL = "qwen-vl-plus"

# 截图保存路径
SCREENSHOT_PATH = Path(__file__).parent / "heal_screenshot.png"

# 诊断 prompt 模板
_DIAGNOSIS_PROMPT_TEMPLATE = """上一步执行失败了。分析截图中的当前页面状态，判断失败原因，并生成修正指令。
失败指令：{failed_action}
错误信息：{error_detail}
返回 JSON：{{"diagnosis": "失败原因（一句话）", "corrected_action": "修正后的指令（自然语言，≤40字）", "should_retry": true/false}}
如果页面状态已经正常，只需要换一种操作方式，设置 should_retry=true。
如果页面处于不可恢复状态（如需要重新登录），设置 should_retry=false。"""


def _make_vision_client():
    """创建 OpenAI 兼容客户端，直连百炼。"""
    import openai
    return openai.OpenAI(api_key=VL_API_KEY, base_url=VL_BASE_URL)


def attempt_heal(claw, failed_action: str, error_detail: str, timeout=60) -> dict:
    """
    尝试自愈。截图分析失败原因，生成修正指令并重试一次。
    返回 {"healed": bool, "corrected_action": str, "diagnosis": str, "original_result": dict}
    """
    if not VL_API_KEY:
        return {
            "healed": False,
            "corrected_action": "",
            "diagnosis": "未配置百炼 API Key",
            "original_result": {},
        }

    # 延迟导入 CLAW_GLOBAL_LOCK，避免循环依赖
    from core import CLAW_GLOBAL_LOCK

    # Step 1: 截图
    screenshot_path = str(SCREENSHOT_PATH)
    try:
        with CLAW_GLOBAL_LOCK:
            result = claw.execute(
                f"Take a screenshot of the current page and save it to {screenshot_path}.",
                timeout=30,
            )
    except Exception as e:
        return {
            "healed": False,
            "corrected_action": "",
            "diagnosis": f"截图失败: {e}",
            "original_result": {},
        }

    # 截图失败时 fallback：不截图，直接用错误信息做文本诊断
    if not SCREENSHOT_PATH.exists():
        return _text_only_heal(claw, failed_action, error_detail, timeout)

    image_data = SCREENSHOT_PATH.read_bytes()
    if len(image_data) < 1000:
        return _text_only_heal(claw, failed_action, error_detail, timeout)

    image_base64 = base64.b64encode(image_data).decode()

    # Step 2: 用 qwen-vl-plus 诊断
    diagnosis_prompt = _DIAGNOSIS_PROMPT_TEMPLATE.format(
        failed_action=failed_action[:200],
        error_detail=error_detail[:500],
    )

    try:
        client = _make_vision_client()
        response = client.chat.completions.create(
            model=VL_MODEL,
            messages=[
                {"role": "system", "content": (
                    "你是 Claw-brain 的自愈诊断专家。分析页面截图和错误信息，"
                    "判断失败原因并生成修正指令。输出必须是合法 JSON。"
                )},
                {"role": "user", "content": [
                    {"type": "text", "text": diagnosis_prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{image_base64}"
                    }},
                ]},
            ],
            temperature=0.1,
            max_tokens=256,
            timeout=30,
        )
        raw_text = response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[SELF_HEAL] qwen-vl-plus 调用失败: {e}")
        return {
            "healed": False,
            "corrected_action": "",
            "diagnosis": f"AI 诊断失败: {e}",
            "original_result": {},
        }

    # Step 3: 解析诊断结果
    diagnosis = _parse_diagnosis(raw_text)
    should_retry = diagnosis.get("should_retry", False)
    corrected_action = diagnosis.get("corrected_action", "")

    if not should_retry or not corrected_action:
        print(f"[SELF_HEAL] 不可自愈: diagnosis={diagnosis.get('diagnosis', '')}")
        return {
            "healed": False,
            "corrected_action": corrected_action,
            "diagnosis": diagnosis.get("diagnosis", "未知原因"),
            "original_result": {},
        }

    # Step 4: 重试
    print(f"[SELF_HEAL] 重试: {corrected_action[:60]}")
    try:
        with CLAW_GLOBAL_LOCK:
            retry_result = claw.execute(corrected_action, timeout=timeout)
        healed = retry_result.get("success", False)
        print(f"[SELF_HEAL] 重试结果: success={healed}")
        return {
            "healed": healed,
            "corrected_action": corrected_action,
            "diagnosis": diagnosis.get("diagnosis", ""),
            "original_result": retry_result,
        }
    except Exception as e:
        print(f"[SELF_HEAL] 重试执行失败: {e}")
        return {
            "healed": False,
            "corrected_action": corrected_action,
            "diagnosis": diagnosis.get("diagnosis", ""),
            "original_result": {},
        }


def _parse_diagnosis(raw_text: str) -> dict:
    """解析 qwen-vl-plus 返回的诊断 JSON。"""
    default = {"diagnosis": "", "corrected_action": "", "should_retry": False}

    json_match = re.search(r'\{[^{}]*\}', raw_text, re.DOTALL)
    if not json_match:
        return default

    try:
        data = json.loads(json_match.group())
    except json.JSONDecodeError:
        return default

    return {
        "diagnosis": data.get("diagnosis", ""),
        "corrected_action": str(data.get("corrected_action", ""))[:40],
        "should_retry": bool(data.get("should_retry", False)),
    }


def _text_only_heal(claw, failed_action: str, error_detail: str, timeout=60) -> dict:
    """
    截图不可用时的 fallback：纯文本诊断。
    用 qwen-vl-plus 的文本模式分析错误信息，生成修正指令并重试一次。
    """
    print("[SELF_HEAL] 截图不可用，切换到纯文本诊断模式")

    if not VL_API_KEY:
        return {
            "healed": False,
            "corrected_action": "",
            "diagnosis": "截图不可用且未配置 API Key",
            "original_result": {},
        }

    try:
        client = _make_vision_client()
        response = client.chat.completions.create(
            model=VL_MODEL,
            messages=[
                {"role": "system", "content": (
                    "你是 Claw-brain 的自愈诊断专家。没有截图可用，"
                    "仅根据错误信息判断失败原因并生成修正指令。输出必须是合法 JSON。"
                )},
                {"role": "user", "content": _DIAGNOSIS_PROMPT_TEMPLATE.format(
                    failed_action=failed_action[:200],
                    error_detail=error_detail[:500],
                )},
            ],
            temperature=0.1,
            max_tokens=256,
            timeout=30,
        )
        raw_text = response.choices[0].message.content.strip()
    except Exception as e:
        return {
            "healed": False,
            "corrected_action": "",
            "diagnosis": f"文本诊断也失败: {e}",
            "original_result": {},
        }

    diagnosis = _parse_diagnosis(raw_text)
    corrected_action = diagnosis.get("corrected_action", "")

    if not diagnosis.get("should_retry") or not corrected_action:
        print(f"[SELF_HEAL] 文本诊断: 不可自愈 ({diagnosis.get('diagnosis', '')})")
        return {
            "healed": False,
            "corrected_action": corrected_action,
            "diagnosis": diagnosis.get("diagnosis", ""),
            "original_result": {},
        }

    from core import CLAW_GLOBAL_LOCK
    print(f"[SELF_HEAL] 文本诊断重试: {corrected_action[:60]}")
    try:
        with CLAW_GLOBAL_LOCK:
            retry_result = claw.execute(corrected_action, timeout=timeout)
        healed = retry_result.get("success", False)
        print(f"[SELF_HEAL] 重试结果: success={healed}")
        return {
            "healed": healed,
            "corrected_action": corrected_action,
            "diagnosis": diagnosis.get("diagnosis", ""),
            "original_result": retry_result,
        }
    except Exception as e:
        return {
            "healed": False,
            "corrected_action": corrected_action,
            "diagnosis": diagnosis.get("diagnosis", ""),
            "original_result": {},
        }
