#!/usr/bin/env python3
"""
CloakBrowser 状态检测模块
可嵌入 OpenClaw 启动检查或 Brain 诊断流程
"""

import json
import sys
from pathlib import Path


def check_cloakbrowser() -> dict:
    """
    检测 CloakBrowser 是否就绪，返回状态字典。

    Returns:
        {
            "available": bool,
            "binary_ready": bool,
            "binary_path": str | None,
            "configured": bool,  # openclaw.json 是否指向 CloakBrowser
            "config_path": str,
            "message": str,
        }
    """
    result = {
        "available": False,
        "binary_ready": False,
        "binary_path": None,
        "configured": False,
        "config_path": str(Path.home() / ".openclaw" / "openclaw.json"),
        "message": "",
    }

    # 1. 检查 Python 包
    try:
        import cloakbrowser
        result["available"] = True
    except ImportError:
        result["message"] = "cloakbrowser 包未安装。运行: pip install cloakbrowser"
        return result

    # 2. 检查二进制
    try:
        info = cloakbrowser.binary_info()
        result["binary_path"] = info.get("path")
        result["binary_ready"] = info.get("status") == "ready"
        if not result["binary_ready"]:
            result["message"] = f"二进制未就绪: {info}"
    except Exception as e:
        result["message"] = f"获取二进制信息失败: {e}"
        return result

    # 3. 检查 openclaw.json 配置
    config_path = Path(result["config_path"])
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            browser_cfg = cfg.get("browser", {})
            exe = browser_cfg.get("executablePath", "")
            if result["binary_path"] and exe == result["binary_path"]:
                result["configured"] = True
                result["message"] = "CloakBrowser 已安装且已配置"
            else:
                result["message"] = (
                    f"CloakBrowser 已安装但未配置为默认浏览器。"
                    f"当前配置: {exe}, CloakBrowser: {result['binary_path']}"
                )
        except Exception as e:
            result["message"] = f"读取配置失败: {e}"
    else:
        result["message"] = f"配置文件不存在: {config_path}"

    return result


def startup_check() -> bool:
    """
    启动时调用，打印状态并返回是否可继续。

    用法:
        from cloak_check import startup_check
        if not startup_check():
            print("警告: 浏览器反检测未启用")
    """
    status = check_cloakbrowser()
    icon = "✓" if status["configured"] else "⚠"
    print(f"{icon} CloakBrowser: {status['message']}")
    return status["configured"]


def diagnose():
    """诊断模式：详细打印所有信息。"""
    print("=" * 40)
    print("CloakBrowser 诊断报告")
    print("=" * 40)
    status = check_cloakbrowser()
    for k, v in status.items():
        print(f"  {k:15s}: {v}")
    print("=" * 40)
    return status


if __name__ == "__main__":
    if "--diagnose" in sys.argv:
        diagnose()
    else:
        ok = startup_check()
        sys.exit(0 if ok else 1)
