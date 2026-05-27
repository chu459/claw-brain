# CloakBrowser 集成工具包

一键为 OpenClaw / Claw-brain 启用反检测隐身浏览器。

## 快速开始

```bash
# 1. 把这三个文件放到你的 claw-brain 项目目录（或任意位置）
#    deploy_cloakbrowser.py
#    cloak_check.py
#    README.md

# 2. 运行部署脚本
python deploy_cloakbrowser.py

# 3. 重启 OpenClaw Gateway
openclaw gateway restart

# 4. 验证
python cloak_check.py --diagnose
```

## 文件说明

| 文件 | 作用 |
|---|---|
| `deploy_cloakbrowser.py` | 一键部署：安装 → 下载二进制 → 改配置 → 验证 |
| `cloak_check.py` | 状态检测模块，可嵌入启动流程 |
| `README.md` | 本文档 |

## 回滚

```bash
python deploy_cloakbrowser.py --rollback
```

## 原理

CloakBrowser 是打了 57 个反检测补丁的 Chromium。OpenClaw 支持通过 `browser.executablePath` 配置任意 Chromium 内核浏览器，因此只需修改 `~/.openclaw/openclaw.json` 即可切换，无需改动 claw-brain 源码。

修改内容：
- `browser.executablePath` → 指向 CloakBrowser Chromium
- `browser.launchArgs` → 添加 `--disable-blink-features=AutomationControlled`
- `browser.viewport` → 默认 1920x1080

## 预期收益

| 检测项 | 原 Playwright | CloakBrowser |
|---|---|---|
| reCAPTCHA v3 分数 | 0.1 | 0.9 |
| Cloudflare | 拦截 | 通过 |
| FingerprintJS | 被检测 | 通过 |
