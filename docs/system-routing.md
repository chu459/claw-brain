# ClawBrain 执行路由设计

目标：提高系统稳定性和智能性，避免所有任务都依赖 OpenClaw 网关。

## 分层

1. Brain：负责判断目标、拆任务、决定下一步。
2. Action Router：负责判断动作该交给谁。
3. Codex：负责代码、测试、仓库、脚本、文档、自我修复。
4. Local Command：负责短命令和轻量验证。
5. OpenClaw：负责浏览器、手机、平台、页面操作。

## 路由规则

- `[CODEX] <任务>`：交给 Codex。
- `[LOCAL_CMD] <命令>`：交给本地命令执行器。
- 普通浏览器/手机/平台动作：交给 OpenClaw。
- `[ADD_CARD:*]`、`[SPAWN_AGENT]`、`[MEMORY_SEARCH]`：继续走系统工具。

## 稳定性策略

- OpenClaw 离线时，系统会尝试自动启动网关。
- 网关仍不可用时，不影响 Codex 和本地命令任务。
- 本地命令会拦截危险操作，如 `git reset --hard`、`Remove-Item`、`shutdown`。
- Codex 以非交互方式运行，有超时、有日志，不阻塞整个系统。

## 智能性提升

以前：Brain 很容易把“修代码、跑测试、改仓库”也发给 OpenClaw。  
现在：系统会自动把工程任务交给 Codex，把真实世界操作交给 OpenClaw。

这让系统更像一个团队：

- Brain 是负责人。
- Codex 是工程师。
- OpenClaw 是外部操作员。
- Local Command 是快速工具箱。
