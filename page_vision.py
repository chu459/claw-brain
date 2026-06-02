"""
page_vision.py - 页面视觉感知模块
==================================
截图 -> qwen-vl-plus 识别交互元素 -> 返回编号列表

功能：
  1. 通过 OpenClaw 截取当前浏览器页面
  2. 用百炼 qwen-vl-plus 视觉模型分析页面交互元素
  3. 返回结构化的元素列表（编号、类型、标签）

依赖：
  - openai (pip install openai)
  - 百炼 API Key (环境变量 DASHSCOPE_API_KEY)
"""

import base64
import hashlib
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
SCREENSHOT_PATH = Path(__file__).parent / "page_screenshot.png"

# 缓存：上次分析结果和对应文件 hash
_last_analysis: dict | None = None
_last_image_hash: str | None = None

# 页面状态指纹追踪（用于检测"假成功"——操作执行了但页面没变化）
_last_page_fingerprint: str | None = None
_last_page_hash: str | None = None

# 需要验证页面状态的关键操作关键词（浏览器操作的关键在于改变页面状态）
_STATEFUL_KEYWORDS = (
    "click", "submit", "navigate", "go to", "open ", "select ",
    "fill", "type ", "enter ", "press ", "choose ", "switch ",
    "login", "sign in", "log in", "create", "delete", "save",
)

# SOM system prompt
_SOM_SYSTEM_PROMPT = """你是一个网页元素分析器。看这个截图，找出页面上所有可交互的元素（输入框、按钮、链接、下拉菜单等）。
返回 JSON 格式：{"title": "页面标题", "url": "当前URL（如果可见）", "elements": [{"id": 1, "type": "input|button|link|select|checkbox", "label": "元素上的文字或placeholder", "description": "元素的位置和外观描述"}]}
只列出主要交互元素（最多15个），忽略导航栏和页脚的次要链接。"""


def _make_vision_client():
    """创建 OpenAI 兼容客户端，直连百炼。"""
    import openai
    return openai.OpenAI(api_key=VL_API_KEY, base_url=VL_BASE_URL)


def analyze_page(claw, timeout=30) -> dict:
    """
    截图并用 qwen-vl-plus 识别页面交互元素。
    返回 {"title": str, "elements": [{"id": 1, "type": "input/button/link", "label": str}], "raw_text": str}
    """
    global _last_analysis, _last_image_hash

    if not VL_API_KEY:
        print("[PAGE_VISION] 未配置 DASHSCOPE_API_KEY，无法进行视觉分析")
        return {"title": "", "elements": [], "raw_text": ""}

    # 延迟导入 CLAW_GLOBAL_LOCK，避免循环依赖
    from core import CLAW_GLOBAL_LOCK

    # 截图
    screenshot_path = str(SCREENSHOT_PATH)
    try:
        with CLAW_GLOBAL_LOCK:
            result = claw.execute(
                f"Take a screenshot of the current page and save it to {screenshot_path}.",
                timeout=timeout,
            )
    except Exception as e:
        print(f"[PAGE_VISION] 截图失败: {e}")
        return {"title": "", "elements": [], "raw_text": ""}

    # 读取截图
    if not SCREENSHOT_PATH.exists():
        print("[PAGE_VISION] 截图文件未生成")
        return {"title": "", "elements": [], "raw_text": ""}

    image_data = SCREENSHOT_PATH.read_bytes()
    if len(image_data) < 1000:
        print("[PAGE_VISION] 截图文件过小，可能无效")
        return {"title": "", "elements": [], "raw_text": ""}

    # 计算文件 hash，判断是否需要重新分析
    current_hash = hashlib.md5(image_data).hexdigest()
    if _last_analysis is not None and _last_image_hash == current_hash:
        print("[PAGE_VISION] 页面未变化，使用缓存结果")
        return _last_analysis

    # base64 编码
    image_base64 = base64.b64encode(image_data).decode()

    # 调用 qwen-vl-plus
    try:
        client = _make_vision_client()
        response = client.chat.completions.create(
            model=VL_MODEL,
            messages=[
                {"role": "system", "content": _SOM_SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": "分析这个页面的交互元素。"},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{image_base64}"
                    }},
                ]},
            ],
            temperature=0.1,
            max_tokens=512,
            timeout=30,
        )
        raw_text = response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[PAGE_VISION] qwen-vl-plus 调用失败: {e}")
        return {"title": "", "elements": [], "raw_text": ""}

    # 解析 JSON
    parsed = _parse_result(raw_text)

    # 更新缓存
    _last_analysis = parsed
    _last_image_hash = current_hash

    print(f"[PAGE_VISION] 识别到 {len(parsed['elements'])} 个交互元素, title={parsed['title'][:30]}")
    return parsed


def _parse_result(raw_text: str) -> dict:
    """解析 qwen-vl-plus 返回的 JSON 结果。"""
    default = {"title": "", "elements": [], "raw_text": raw_text}

    # 尝试提取 JSON
    json_match = re.search(r'\{[\s\S]*\}', raw_text)
    if not json_match:
        return default

    try:
        data = json.loads(json_match.group())
    except json.JSONDecodeError:
        return default

    title = data.get("title", "")
    elements = data.get("elements", [])

    # 验证 elements 格式
    valid_elements = []
    for elem in elements[:15]:
        if isinstance(elem, dict) and "id" in elem and "type" in elem:
            valid_elements.append({
                "id": elem["id"],
                "type": elem.get("type", "unknown"),
                "label": elem.get("label", ""),
                "description": elem.get("description", ""),
            })

    return {"title": title, "elements": valid_elements, "raw_text": raw_text}


def get_page_fingerprint(claw, timeout=20) -> dict:
    """
    获取当前页面的简洁状态指纹，用于判断操作是否真正推进了页面状态。
    返回 {"fingerprint": str, "hash": str, "title": str, "element_count": int, "changed": bool}

    changed=True 表示与上次指纹不同（页面有变化）。
    changed=False 表示页面没变化（可能是假成功）。
    """
    global _last_page_fingerprint, _last_page_hash

    if not VL_API_KEY:
        return {"fingerprint": "", "hash": "", "title": "", "element_count": 0,
                "changed": _last_page_fingerprint is None}

    # 先取截图计算 image hash（快速判断页面像素是否变化）
    screenshot_path = str(SCREENSHOT_PATH)
    try:
        from core import CLAW_GLOBAL_LOCK
        with CLAW_GLOBAL_LOCK:
            result = claw.execute(
                f"Take a screenshot of the current page and save it to {screenshot_path}.",
                timeout=timeout,
            )
    except Exception as e:
        print(f"[FINGERPRINT] 截图失败: {e}")
        return {"fingerprint": "", "hash": "", "title": "", "element_count": 0,
                "changed": _last_page_fingerprint is None}

    if not SCREENSHOT_PATH.exists():
        return {"fingerprint": "", "hash": "", "title": "", "element_count": 0,
                "changed": _last_page_fingerprint is None}

    image_data = SCREENSHOT_PATH.read_bytes()
    if len(image_data) < 1000:
        return {"fingerprint": "", "hash": "", "title": "", "element_count": 0,
                "changed": _last_page_fingerprint is None}

    current_hash = hashlib.md5(image_data).hexdigest()

    # 像素没变 → 页面一定没变（快速路径，跳过 VL 调用）
    if _last_page_hash is not None and current_hash == _last_page_hash:
        print(f"[FINGERPRINT] 页面像素未变化（MD5相同），可能是假成功")
        return {
            "fingerprint": _last_page_fingerprint or "",
            "hash": current_hash,
            "title": _last_analysis.get("title", "") if _last_analysis else "",
            "element_count": len(_last_analysis.get("elements", [])) if _last_analysis else 0,
            "changed": False,
        }

    # 像素变了 → 用已有的 analyze_page 判断具体状态（复用缓存）
    analysis = analyze_page(claw, timeout=timeout)
    title = analysis.get("title", "")
    elements = analysis.get("elements", [])

    # 构建指纹：标题 + 元素类型和标签的摘要
    elem_summary = "|".join(
        f"{e['type']}:{e['label'][:20]}" for e in elements[:10]
    )
    fingerprint = f"{title} || {elem_summary}"

    changed = fingerprint != _last_page_fingerprint
    _last_page_fingerprint = fingerprint
    _last_page_hash = current_hash

    print(f"[FINGERPRINT] title={title[:30]}, elements={len(elements)}, changed={changed}")
    return {
        "fingerprint": fingerprint,
        "hash": current_hash,
        "title": title,
        "element_count": len(elements),
        "changed": changed,
    }


def is_stateful_action(action: str) -> bool:
    """判断一个操作是否是会改变页面状态的关键操作（需要截图验证）。"""
    if not action:
        return False
    action_lower = action.lower()
    # 排除截图、分析等只读操作
    readonly_kw = ("screenshot", "scroll", "wait", "hover", "describe",
                   "总结", "分析", "列出", "获取", "检查", "查看", "搜索")
    if any(kw in action_lower for kw in readonly_kw):
        return False
    return any(kw in action_lower for kw in _STATEFUL_KEYWORDS)


def reset_fingerprint():
    """重置页面指纹（在导航到新页面时调用，避免新旧页面误判）。"""
    global _last_page_fingerprint, _last_page_hash
    _last_page_fingerprint = None
    _last_page_hash = None
