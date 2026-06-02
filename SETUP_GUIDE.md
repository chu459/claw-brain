# OpenClaw 对接指南 - 从零跑通

## 你需要做的事情（按顺序）

### 第 1 步：安装并启动 OpenClaw

```bash
# 安装（需要 Node.js >= 18）
npm install -g openclaw@latest

# 首次引导设置（会让你配置 AI 模型 API Key）
openclaw onboard

# 启动网关
openclaw start
```

启动后，OpenClaw 默认运行在 `http://127.0.0.1:18789`

### 第 2 步：启用 HTTP API

编辑配置文件 `~/.openclaw/openclaw.json`（Windows 下是 `C:\Users\楚\.openclaw\openclaw.json`），添加：

```json
{
  "gateway": {
    "http": {
      "endpoints": {
        "chatCompletions": { "enabled": true }
      }
    }
  }
}
```

如果文件已有内容，只合并 `gateway.http.endpoints` 部分即可。

### 第 3 步：获取 Gateway Token

方式 A：从配置文件查看
```bash
# 查看 openclaw.json 中的 auth 部分
cat ~/.openclaw/openclaw.json
# 找 gateway.auth.token 或 gateway.auth.password
```

方式 B：如果没设置过，设置一个：
```bash
openclaw config set gateway.auth.token your-secret-token-here
```

方式 C：如果认证模式是 `none`，API_KEY 填任意值即可。

### 第 4 步：确认浏览器可用

```bash
openclaw browser doctor
```

如果报错，按提示修复。常见问题：
- 需要安装 Chromium: `openclaw browser start` 会自动下载
- 权限问题：确保不在只读目录下运行

### 第 5 步：运行自主系统

```bash
cd C:\Users\楚\WorkBuddy\2026-05-15-task-28

# 先测试连接
python autonomous_system.py --test

# 交互模式（推荐先用这个调试）
python autonomous_system.py --interactive

# 全自动模式
export BRAIN_API_KEY=你的key
export BRAIN_BASE_URL=https://api.openai.com/v1
export OPENCLAW_TOKEN=你的openclaw-token
python autonomous_system.py
```

## 架构说明

```
┌──────────────┐         ┌──────────────────┐
│              │  自然语言  │                  │
│   大脑(LLM)  │ ───────→ │  OpenClaw Agent  │
│  策略决策    │          │  浏览器执行      │
│              │ ←─────── │  (Playwright)    │
└──────────────┘  执行反馈  │                  │
                           └──────────────────┘

对接协议: OpenAI Chat Completions API 兼容
端点:     POST http://127.0.0.1:18789/v1/chat/completions
认证:     Authorization: Bearer <your-token>
Session:  x-openclaw-session-key: autonomous-money-maker
```

## 关键区别

| 你原来的假设 | 实际情况 |
|-------------|---------|
| OpenClaw 有个 `/execute` 端点直接执行指令 | 没有这个端点。通过 OpenAI 兼容 API 发自然语言 |
| 你需要控制浏览器细节（点击、输入） | 不需要。发给 OpenClaw 自然语言，它自己决定怎么操作浏览器 |
| OpenClaw 只是个浏览器自动化工具 | 它是完整的 AI Agent 网关，浏览器只是其中一个工具 |

## 常见问题

### Q: OpenClaw 启动报错
确保 Node.js >= 18，且 18789 端口没被占用。

### Q: API 返回 401
Token 不对。检查 `openclaw.json` 的 `gateway.auth` 配置。

### Q: 浏览器工具被拒绝
确保 `openclaw.json` 中 `gateway.tools.deny` 列表不包含 `browser`。

### Q: 想要更强的控制力？
用 `invoke_tool()` 方法直接调用 `/tools/invoke` 端点：
```python
# 直接截图
result = system.openclaw.invoke_tool("browser", {"action": "screenshot"})

# 直接获取页面快照
result = system.openclaw.invoke_tool("browser", {"action": "snapshot"})

# 直接导航
result = system.openclaw.invoke_tool("browser", {
    "action": "navigate",
    "url": "https://example.com"
})
```
