#!/usr/bin/env python3
"""
CloakBrowser 一键部署脚本 for OpenClaw / Claw-brain
自动完成：安装 → 下载二进制 → 修改配置 → 验证

用法:
    python deploy_cloakbrowser.py
    python deploy_cloakbrowser.py --rollback  # 回滚配置
"""

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

# ---------- 配置 ----------
OPENCLAW_CONFIG = Path.home() / ".openclaw" / "openclaw.json"
BACKUP_SUFFIX = ".backup"


def run(cmd, capture=True, check=False):
    """执行 shell 命令，返回输出字符串。"""
    kwargs = {
        "shell": True,
        "capture_output": capture,
        "text": True,
    }
    if check:
        kwargs["check"] = True
    result = subprocess.run(cmd, **kwargs)
    return result.stdout.strip() if capture else ""


def step(msg):
    print(f"\n[>] {msg}")


def ok(msg):
    print(f"  ✓ {msg}")


def fail(msg):
    print(f"  ✗ {msg}", file=sys.stderr)


def install_cloakbrowser():
    """安装 cloakbrowser Python 包。"""
    step("1/5 安装 cloakbrowser Python 包")
    try:
        import cloakbrowser
        ok("cloakbrowser 已安装")
        return True
    except ImportError:
        print("  正在安装 cloakbrowser...")
        run(f'"{sys.executable}" -m pip install cloakbrowser', check=True)
        import importlib
        importlib.invalidate_caches()
        try:
            import cloakbrowser
            ok("cloakbrowser 安装成功")
            return True
        except ImportError:
            fail("安装后仍无法导入 cloakbrowser")
            return False


def download_binary():
    """确保 CloakBrowser 二进制已下载。"""
    step("2/5 下载 CloakBrowser Chromium 二进制")
    try:
        import cloakbrowser
        info = cloakbrowser.binary_info()
        if info.get("status") == "ready":
            ok(f"二进制已就绪: {info.get('path', 'unknown')}")
            return info["path"]
    except Exception:
        pass

    try:
        import cloakbrowser
        print("  正在下载 CloakBrowser Chromium (约 150MB)...")
        cloakbrowser.ensure_binary()
        info = cloakbrowser.binary_info()
        ok(f"下载完成: {info['path']}")
        return info["path"]
    except Exception as e:
        fail(f"下载失败: {e}")
        return None


def modify_config(binary_path):
    """修改 openclaw.json，加入 CloakBrowser 配置。"""
    step("3/5 修改 OpenClaw 配置")
    config_path = Path(OPENCLAW_CONFIG)
    if not config_path.exists():
        fail(f"找不到 OpenClaw 配置: {config_path}")
        print("  提示: 请先运行 openclaw gateway init 生成配置")
        return False

    # 读取现有配置
    with open(config_path, "r", encoding="utf-8") as f:
        content = f.read()
    config = json.loads(content)

    # 备份
    backup_path = config_path.with_suffix(BACKUP_SUFFIX)
    with open(backup_path, "w", encoding="utf-8") as f:
        f.write(content)
    ok(f"配置已备份: {backup_path}")

    # 修改: 添加 customBrowser 和 launchArgs
    modified = False
    browser_cfg = config.get("browser", {})
    if not isinstance(browser_cfg, dict):
        config["browser"] = browser_cfg = {}

    # 保存原始 executablePath (用于回滚)
    original = browser_cfg.get("executablePath")
    if original and original != binary_path:
        browser_cfg["_originalExecutablePath"] = original

    browser_cfg["executablePath"] = binary_path

    # 添加反检测启动参数
    launch_args = browser_cfg.get("launchArgs", [])
    if not isinstance(launch_args, list):
        launch_args = []
    stealth_flags = [
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
    ]
    for flag in stealth_flags:
        if flag not in launch_args:
            launch_args.append(flag)
            modified = True
    browser_cfg["launchArgs"] = launch_args

    # 添加 viewport / userAgent 建议 (不强制覆盖，只在缺失时添加)
    if "viewport" not in browser_cfg:
        browser_cfg["viewport"] = {"width": 1920, "height": 1080}
        modified = True

    config["browser"] = browser_cfg

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    ok(f"配置已更新: {config_path}")
    return True


def verify():
    """验证 CloakBrowser 是否可用。"""
    step("4/5 验证 CloakBrowser")
    try:
        import cloakbrowser
        info = cloakbrowser.binary_info()
        if info.get("status") == "ready":
            ok("CloakBrowser 状态正常")
        else:
            fail(f"状态异常: {info}")
            return False

        # 尝试启动一次快速检测
        print("  正在启动隐身浏览器做快速检测...")
        result = run(f'"{sys.executable}" -c "import cloakbrowser; b=cloakbrowser.launch(); print(b.evaluate(\\\"navigator.webdriver\\\")); b.close()"', timeout=60)
        if "undefined" in result or "false" in result.lower():
            ok("navigator.webdriver = undefined (反检测生效)")
        else:
            print(f"  检测结果: {result}")
        return True
    except Exception as e:
        fail(f"验证失败: {e}")
        return False


def restart_gateway():
    """提示重启 OpenClaw Gateway。"""
    step("5/5 重启 OpenClaw Gateway")
    print("  请手动执行: openclaw gateway restart")
    print("  或杀掉 gateway 进程后重新启动")
    return True


def rollback():
    """回滚配置到备份。"""
    config_path = Path(OPENCLAW_CONFIG)
    backup_path = config_path.with_suffix(BACKUP_SUFFIX)
    if not backup_path.exists():
        fail(f"找不到备份文件: {backup_path}")
        return False
    with open(backup_path, "r", encoding="utf-8") as f:
        original = f.read()
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(original)
    ok("配置已回滚")
    return True


def main():
    if "--rollback" in sys.argv:
        rollback()
        sys.exit(0)

    print("=" * 50)
    print("CloakBrowser 一键部署 for OpenClaw")
    print("=" * 50)

    success = True
    success = install_cloakbrowser() and success

    binary_path = None
    if success:
        binary_path = download_binary()
        success = binary_path is not None

    if success:
        success = modify_config(binary_path)

    if success:
        success = verify()

    restart_gateway()

    print("\n" + "=" * 50)
    if success:
        print("✓ 部署完成。重启 gateway 后生效。")
        print(f"✓ 备份配置: {OPENCLAW_CONFIG}{BACKUP_SUFFIX}")
        print("✓ 回滚命令: python deploy_cloakbrowser.py --rollback")
    else:
        print("✗ 部署过程中出现错误，请查看上方日志。")
        sys.exit(1)


if __name__ == "__main__":
    main()
