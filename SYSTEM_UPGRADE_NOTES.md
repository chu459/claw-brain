# ClawBrain 系统升级说明

日期：2026-06-03

## 结论

本次最新版以本机主线系统为基础：

`C:\Users\楚\WorkBuddy\2026-05-15-task-28`

同时吸收压缩包里的模块化优化：

`claw-brain-main(1).zip`

没有直接整包覆盖。原因是压缩包里的 `core/` 目录会和主线的 `core.py` 冲突。

## 本机主线保留的能力

- Web 控制台
- Worker 进程隔离
- 快照通信
- 会话管理
- OpenClaw 执行闭环
- 失败诊断
- 自愈
- 页面视觉分析
- 质量复盘
- Wiki 经验沉淀
- 向量记忆
- 微调数据生成
- AutoDL 微调脚本

## 从压缩包吸收的优化

- `brain_modules/soul.py`
  - 给 Brain 加人格和呼吸节奏。

- `brain_modules/memory_v2.py`
  - 增加压缩记忆和反思记忆。

- `brain_modules/planner.py`
  - 增加阶段、任务、待办结构。

- `brain_modules/knowledge.py`
  - 支持读取知识库文档。

- `brain_modules/interaction.py`
  - 支持提问、公告、讨论上下文。

- `brain_modules/feedback.py`
  - 支持每轮评分、标签、纠正建议。

- `brain_modules/space_parser.py`
  - 支持解析 Space 文档任务。

- `workspace_templates/`
  - 保留压缩包里的 Agent 和知识模板。

## 实际接入方式

新增 `brain_v2.py`。

它是一个适配层，作用是：

- 每轮运行前，把 Soul、MemoryV2、Knowledge、Feedback 汇总成上下文。
- 注入到主循环的 Brain prompt。
- 每轮执行后，把行动结果写入 V2 记忆。
- 出错时自动降级，不影响主系统继续跑。

## 为什么这样合并

主线系统更稳定，适合继续赚钱闭环。

压缩包版本更模块化，适合学习和增强。

所以这次采用：

主线系统做底座，压缩包模块做增强。

## 未直接合并的部分

- 压缩包的整版 `web_console.py`
  - 原因：会覆盖主线的 Worker 架构。

- 压缩包的 `core/` 目录名
  - 原因：会和主线 `core.py` 冲突。

- 压缩包里的缓存文件
  - 原因：不是源码。
