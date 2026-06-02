"""
captcha_solver.py - AI 视觉验证码解决模块
=========================================
使用阿里百炼 qwen-vl-plus 视觉模型分析验证码截图，返回可执行指令。
零额外成本（利用已有的百炼 API Key）。

工作流程:
  1. 检测到验证码/超时 → 触发截图
  2. OpenClaw 截取当前页面截图
  3. 截图发给 qwen-vl-plus 分析
  4. 返回 OpenClaw 可执行的自然语言指令（点击、输入等）
"""

import base64
import json
import os
import re
import time
import threading
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

# 百炼 API 不需要代理（国内直连），但保险起见不走代理
VL_PROXY = None  # 强制直连


def _make_vision_client():
    """创建 OpenAI 兼容客户端，直连百炼（不走代理）。"""
    import openai
    return openai.OpenAI(api_key=VL_API_KEY, base_url=VL_BASE_URL)


def take_screenshot(openclaw_client, timeout: int = 30) -> str:
    """
    通过 OpenClaw 截取当前浏览器页面截图。
    返回截图的 base64 编码字符串（不含 data: 前缀）。
    失败返回空字符串。
    """
    # 让 OpenClaw 截图并保存
    screenshot_path = str(Path(__file__).parent / "captcha_screenshot.png")
    try:
        from core import CLAW_GLOBAL_LOCK
        with CLAW_GLOBAL_LOCK:
            result = openclaw_client.execute(
                f"Take a screenshot of the current page and save it to {screenshot_path}. "
                f"If you can't save to file, just describe what you see on the page including any captcha or verification elements.",
                timeout=timeout,
            )
        # 尝试读取保存的截图
        if os.path.exists(screenshot_path):
            with open(screenshot_path, "rb") as f:
                data = f.read()
            if len(data) > 1000:  # 有效图片至少几 KB
                # 清理临时文件
                try:
                    os.remove(screenshot_path)
                except Exception:
                    pass
                return base64.b64encode(data).decode()
    except Exception as e:
        print(f"[CAPTCHA] Screenshot failed: {e}")

    # 截图失败，尝试从 OpenClaw 的描述中获取信息
    return ""


def analyze_captcha(image_base64: str, page_context: str = "") -> dict:
    """
    用 qwen-vl-plus 分析验证码截图。
    返回解析结果 dict:
      - type: captcha 类型 (image_click, slider, text_input, recaptcha, etc.)
      - instruction: 给 OpenClaw 的具体执行指令
      - answer: 验证码答案（如果可直接确定）
      - description: 验证码描述
    """
    if not VL_API_KEY:
        return {
            "type": "unknown",
            "instruction": "",
            "answer": "",
            "description": "未配置百炼 API Key，无法使用 AI 视觉分析",
        }

    if not image_base64:
        return {
            "type": "unknown",
            "instruction": "",
            "answer": "",
            "description": "无法获取截图",
        }

    prompt = """分析这张网页截图，判断是否存在验证码/人机验证。

如果存在验证码，请详细描述：
1. 验证码类型（图片点选、滑块、文字输入、reCAPTCHA、hCaptcha等）
2. 验证码要求用户做什么
3. 如果是图片点选，描述需要按什么顺序点击什么内容
4. 如果是滑块，描述缺口位置
5. 如果是文字输入，识别图片中的文字
6. 给出具体的浏览器操作建议

请用以下 JSON 格式回复（不要输出其他内容）：
{
  "has_captcha": true/false,
  "captcha_type": "image_click/slider/text_input/recaptcha/hcaptcha/none",
  "description": "验证码的详细描述",
  "answer": "如果可以直接确定答案，写在这里",
  "instruction": "给浏览器自动化工具的具体操作指令（自然语言）"
}

如果没有验证码，设置 has_captcha 为 false。"""

    # 如果有页面上下文，附加到 prompt
    if page_context:
        prompt = f"页面操作上下文: {page_context}\n\n" + prompt

    try:
        client = _make_vision_client()
        response = client.chat.completions.create(
            model=VL_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{image_base64}"
                    }},
                ],
            }],
            max_tokens=500,
            temperature=0.1,  # 低温度，要精确答案
            timeout=30,
        )
        content = response.choices[0].message.content.strip()

        # 尝试提取 JSON
        json_match = re.search(r'\{[^{}]+\}', content, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
            result.setdefault("has_captcha", False)
            result.setdefault("captcha_type", "unknown")
            result.setdefault("description", "")
            result.setdefault("answer", "")
            result.setdefault("instruction", "")
            return result
        else:
            return {
                "type": "unknown",
                "instruction": "",
                "answer": "",
                "description": f"AI 分析结果无法解析为 JSON: {content[:200]}",
            }

    except Exception as e:
        return {
            "type": "unknown",
            "instruction": "",
            "answer": "",
            "description": f"AI 视觉分析失败: {e}",
        }


def solve_captcha(openclaw_client, last_action: str = "", max_retries: int = 2) -> dict:
    """
    完整的验证码解决流程。
    返回 dict:
      - solved: bool - 是否成功解决
      - instruction: str - 给 OpenClaw 的下一步指令
      - description: str - 过程描述
    """
    print("[CAPTCHA] 开始验证码解决流程...")

    # Step 1: 截图
    print("[CAPTCHA] 正在截图...")
    image_b64 = take_screenshot(openclaw_client)
    if not image_b64:
        return {
            "solved": False,
            "instruction": "",
            "description": "截图失败，无法分析验证码",
        }

    # Step 2: AI 分析
    print("[CAPTCHA] AI 分析验证码中...")
    result = analyze_captcha(image_b64, page_context=last_action)
    print(f"[CAPTCHA] 分析结果: type={result.get('captcha_type')}, desc={result.get('description', '')[:100]}")

    if not result.get("has_captcha"):
        return {
            "solved": False,
            "instruction": "",
            "description": "AI 未检测到验证码，可能是其他问题导致超时",
        }

    captcha_type = result.get("captcha_type", "unknown")
    instruction = result.get("instruction", "")

    # Step 3: 根据类型生成执行指令
    if instruction:
        # AI 已经给出了具体指令
        full_instruction = f"页面上出现了验证码（{captcha_type}）。请按以下方式处理: {instruction}"
    else:
        # AI 没给出具体指令，生成通用指令
        full_instruction = _generate_fallback_instruction(captcha_type, result)

    if not full_instruction:
        return {
            "solved": False,
            "instruction": "",
            "description": f"无法生成验证码解决指令 (type={captcha_type})",
        }

    # Step 4: 让 OpenClaw 执行解决指令
    print(f"[CAPTCHA] 执行解决指令: {full_instruction[:100]}...")
    from core import CLAW_GLOBAL_LOCK
    with CLAW_GLOBAL_LOCK:
        exec_result = openclaw_client.execute(full_instruction, timeout=60)

    if exec_result.get("success"):
        print("[CAPTCHA] 解决指令执行成功")
        return {
            "solved": True,
            "instruction": "",
            "description": f"验证码({captcha_type})已处理: {exec_result['content'][:200]}",
        }
    else:
        print(f"[CAPTCHA] 解决指令执行失败: {exec_result['content'][:100]}")
        # 重试一次
        if max_retries > 1:
            print("[CAPTCHA] 重试中...")
            time.sleep(3)
            return solve_captcha(openclaw_client, last_action, max_retries - 1)
        return {
            "solved": False,
            "instruction": "",
            "description": f"验证码解决失败: {exec_result['content'][:200]}",
        }


def _generate_fallback_instruction(captcha_type: str, result: dict) -> str:
    """当 AI 无法给出具体指令时，根据类型生成通用指令。"""
    desc = result.get("description", "")
    answer = result.get("answer", "")

    if captcha_type == "image_click" and answer:
        return f"页面上有图片点选验证码。请按以下顺序点击: {answer}"
    elif captcha_type == "slider":
        return "页面上有滑块验证码。请找到滑块缺口位置，拖动滑块到缺口处完成验证。"
    elif captcha_type == "text_input" and answer:
        return f"页面上有文字验证码。请在输入框中输入: {answer}"
    elif captcha_type in ("recaptcha", "hcaptcha"):
        return f"页面上有{captcha_type}验证框。请点击验证框中的'我不是机器人'复选框。"
    elif desc:
        return f"页面上出现验证码: {desc}。请尝试完成验证。"
    else:
        return ""


# ===================== 独立测试 =====================

if __name__ == "__main__":
    import sys

    print("=== 验证码解决模块测试 ===\n")

    # 1. 检查 API 配置
    print(f"百炼 API Key: {'已配置' if VL_API_KEY else '未配置'}")
    print(f"视觉模型: {VL_MODEL}")

    if VL_API_KEY:
        # 2. 测试 Vision API
        print("\n测试 Vision API 连通性...")
        import struct, zlib

        def make_png():
            def chunk(ctype, data):
                c = ctype + data
                return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
            raw = b''
            for _ in range(50):
                raw += b'\x00' + bytes([0, 128, 255]) * 50
            return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 50, 50, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')

        b64 = base64.b64encode(make_png()).decode()
        result = analyze_captcha(b64)
        print(f"测试结果: {json.dumps(result, ensure_ascii=False, indent=2)}")
    else:
        print("\n未配置百炼 API Key，跳过 API 测试")
