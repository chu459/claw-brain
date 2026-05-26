# Contributing to Claw-brain

感谢你对 Claw-brain 的兴趣！无论你是来提 Bug、提建议还是提交代码，都欢迎。

## 快速开始

1. Fork 本仓库
2. Clone 你的 fork：`git clone https://github.com/YOUR_USERNAME/claw-brain.git`
3. 创建分支：`git checkout -b feature/your-feature-name`
4. 提交更改：`git commit -m "feat: add something cool"`
5. Push 到 fork：`git push origin feature/your-feature-name`
6. 提交 Pull Request

## 开发环境

```bash
# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
pip install -r requirements-dev.txt  # 开发依赖
```

## 代码规范

- 遵循 PEP 8
- 关键逻辑添加类型注解
- 新功能必须包含测试

## Commit 规范

使用以下前缀：

- `feat:` 新功能
- `fix:` Bug 修复
- `docs:` 文档更新
- `refactor:` 重构
- `test:` 测试相关
- `chore:` 构建/工具相关

## 路线图中的优先项

当前最需要贡献的方向：

1. **多 Agent 系统** — 将本地已有的 Brain + BD + Content + Dev + Research agents 同步到 GitHub
2. **向量记忆** — 用向量数据库替代 JSON 文件白板
3. **Docker 部署** — 一键部署脚本
4. **错误处理优化** — 更智能的自我修复逻辑

## 提问和讨论

- 有 Bug 或功能请求 → 开 [Issue](https://github.com/chu459/claw-brain/issues/new/choose)
- 一般性讨论 → 在 [Discussions](https://github.com/chu459/claw-brain/discussions) 中发起

## 许可证

提交 PR 即表示你同意你的代码将在 AGPL-3.0 许可证下发布。
