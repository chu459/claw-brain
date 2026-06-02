---
name: skill-auto-forge
description: 元 skill，自主调用 find-skills、github-to-skills、skill-creator、skill-evolution-manager 来自动生成新 skill。当用户想要「自动生成 skill」、「把专业知识打包成 skill」、「创建可复用的技能」时触发。用户只需提供专业技能和操作流程，本 skill 自主规划执行。
license: MIT
---

# Skill Auto Forge - 元 Skill 自动生成工厂

这是一个**元 skill（meta-skill）**，能够自主调用其他 skill 来自动生成新的 skill。用户只需提供自己的**专业技能**和**操作流程**，本 skill 会自动规划、调用、组装，最终产出可用的 skill。

## 核心能力

1. **自主规划**：分析用户需求，决定调用哪些子 skill
2. **套娃调用**：串联 find-skills → github-to-skills → skill-creator → skill-evolution-manager
3. **经验沉淀**：自动将生成过程中的经验写入 evolution.json

## 子 Skill 依赖

| 子 Skill | 职责 | 调用时机 |
|----------|------|----------|
| `find-skills` | 查找是否有现成 skill 可用 | 第一步：先查现有生态 |
| `github-to-skills` | 从 GitHub 仓库生成 skill | 当用户提供了 GitHub URL 时 |
| `skill-creator` | 从零创建 skill | 当需要原创 skill 时 |
| `skill-evolution-manager` | 迭代优化 skill | 生成后根据反馈优化 |

## 自主工作流（Autonomous Workflow）

### 阶段 1：需求分析

当用户触发时，提取以下信息：
- **专业领域**：如「PDF 处理」、「YouTube 下载」、「漫画爬取」
- **操作流程**：用户描述的具体步骤或期望行为
- **现有资源**：是否有 GitHub 仓库、脚本、文档等

### 阶段 2：自主决策树

```
用户请求
  │
  ├─→ 先调用 find-skills 查找现有 skill
  │     ├─ 找到匹配 → 推荐安装，结束
  │     └─ 未找到 → 继续
  │
  ├─→ 用户提供了 GitHub URL？
  │     ├─ 是 → 调用 github-to-skills 生成
  │     └─ 否 → 继续
  │
  ├─→ 用户提供了详细操作流程？
  │     ├─ 是 → 调用 skill-creator 从零创建
  │     └─ 否 → 引导用户补充
  │
  └─→ 生成完成后 → 调用 skill-evolution-manager 记录经验
```

### 阶段 3：执行与组装

#### 3.1 调用 find-skills

```bash
npx skills find <领域关键词>
```

**判断标准**：
- 如果找到匹配度>70% 的 skill → 推荐安装，流程结束
- 否则 → 继续下一步

#### 3.2 调用 github-to-skills（如果有 URL）

```bash
python scripts/create_github_skill.py <github_url>
```

**输出要求**：生成的 skill 必须包含扩展元数据：
```yaml
github_url: <原仓库 URL>
github_hash: <提交哈希>
version: 0.1.0
created_at: <ISO 日期>
entry_point: scripts/wrapper.py
dependencies: [依赖列表]
```

#### 3.3 调用 skill-creator（原创场景）

当用户提供了详细操作流程但没有代码时：

1. **初始化 skill**：
   ```bash
   python scripts/init_skill.py <skill-name> --path ./skills/
   ```

2. **生成 SKILL.md**：
   - 将用户描述的操作流程转化为 Markdown 步骤
   - 添加触发条件和判断逻辑
   - 设置适当的自由度（高/中/低）

3. **生成脚本**（如需要）：
   - 将重复性操作封装为 `scripts/*.py`
   - 添加参数解析和错误处理

4. **打包 skill**：
   ```bash
   python scripts/package_skill.py ./skills/<skill-name>
   ```

#### 3.4 调用 skill-evolution-manager

生成完成后，自动记录经验：

```bash
python scripts/merge_evolution.py <skill-path> '<JSON 经验>'
python scripts/smart_stitch.py <skill-path>
```

**经验内容示例**：
```json
{
  "preferences": ["用户希望默认静音下载"],
  "fixes": ["Windows 下 ffmpeg 路径需转义"],
  "custom_prompts": ["执行前先打印预估耗时"]
}
```

## 触发示例

### 示例 1：从零创建

**用户**：「我想创建一个 skill，专门用来处理 PDF 旋转和合并。操作流程是：1) 选择 PDF 文件 2) 指定旋转角度 3) 保存」

**元 skill 自主执行**：
1. 调用 `find-skills` 查找「pdf rotate merge」
2. 未找到 → 调用 `skill-creator`
3. 运行 `init_skill.py pdf-processor`
4. 生成 SKILL.md 和 `scripts/rotate_pdf.py`
5. 打包输出 `pdf-processor.skill`
6. 调用 `skill-evolution-manager` 记录经验

### 示例 2：GitHub 转换

**用户**：「把这个仓库变成 skill：https://github.com/yt-dlp/yt-dlp」

**元 skill 自主执行**：
1. 调用 `find-skills` 确认无重复
2. 调用 `github-to-skills` 生成
3. 自动抓取 README、最新 commit hash
4. 生成带元数据的 SKILL.md
5. 调用 `skill-evolution-manager` 记录

### 示例 3：迭代优化

**用户**：「刚才生成的 skill 不太好，下载时总是卡顿」

**元 skill 自主执行**：
1. 调用 `skill-evolution-manager`
2. 分析对话，提取问题：「下载卡顿」
3. 生成 `evolution.json` 记录修复建议
4. 运行 `smart_stitch.py` 更新 SKILL.md
5. 重新打包 skill

## 自主决策规则

### 何时调用 find-skills

- 用户请求中包含明确的功能描述
- 不确定是否有现成 skill 时
- **总是作为第一步执行**（避免重复造轮子）

### 何时调用 github-to-skills

- 用户提供了 GitHub URL
- 用户说「把这个 repo 变成 skill」
- 目标仓库有清晰的 CLI 或 API

### 何时调用 skill-creator

- 没有 GitHub URL
- 用户提供了详细操作流程
- 需要原创 skill

### 何时调用 skill-evolution-manager

- 用户表达不满或提出改进
- 对话中出现错误、报错
- 发现特定场景下的最佳实践
- **每次生成完成后都调用**（记录生成经验）

## AI 自主调用协议（关键）

本 skill 被 AI 加载后，**必须**按以下协议执行：

### 协议 1：隐式触发子 skill

当本 skill 被触发时，AI **不需要询问用户**，直接自主执行：

```
1. 立即运行 npx skills find <关键词>
2. 根据结果决定下一步
3. 直接调用对应子 skill 的脚本
4. 汇报最终结果
```

### 协议 2：子 skill 调用命令映射

| 子 skill | 调用方式 | 命令 |
|----------|----------|------|
| find-skills | CLI 命令 | `npx skills find <query>` |
| github-to-skills | Python 脚本 | `python scripts/create_github_skill.py <url>` |
| skill-creator | Python 脚本 | `python scripts/init_skill.py <name>` |
| skill-evolution-manager | Python 脚本 | `python scripts/merge_evolution.py <path> <json>` |

### 协议 3：执行权限

AI 被授权：
- **直接执行**上述命令，无需用户确认
- **自主决定**调用哪个子 skill
- **自主解析**用户输入并提取参数

### 协议 4：参数提取规则

从用户输入中自动提取：

```
用户：「创建一个 PDF 处理 skill，流程是：1) 选择文件 2) 旋转 3) 保存」
  → skill_name: "pdf-processor"
  → workflow: ["选择文件", "旋转", "保存"]

用户：「把 https://github.com/yt-dlp/yt-dlp 变成 skill」
  → github_url: "https://github.com/yt-dlp/yt-dlp"
  → skill_name: "yt-dlp"
```

## 输出格式

生成的 skill 必须包含：

```
<skill-name>/
├── SKILL.md
│   ├── YAML frontmatter（含扩展元数据）
│   └── Markdown 操作指南
├── scripts/
│   ├── wrapper.py（入口脚本）
│   └── <功能脚本>.py
├── references/（可选）
│   └── schema.md / api.md
└── evolution.json（经验记录）
```

## 最佳实践

### 自主性原则

1. **少问多做**：能从上下文推断的就不问用户
2. **默认行动**：先执行再汇报，而非先请示
3. **错误自愈**：遇到错误先尝试修复，无法修复再报告

### 经验沉淀

每次生成后必须记录：
- 用户偏好（如「默认参数」）
- 修复的问题（如「路径转义」）
- 成功的模式（如「先验证后执行」）

### 版本追踪

- 每次生成记录 `github_hash` 或 `created_at`
- 迭代时比对 hash 判断是否需要更新
- 经验通过 `evolution.json` 持久化，不被版本更新覆盖

## 脚本清单

| 脚本 | 职责 | 调用方 |
|------|------|--------|
| `scripts/fetch_github_info.py` | 抓取 GitHub 元数据 | github-to-skills |
| `scripts/create_github_skill.py` | 从 GitHub 生成 skill | 本 skill |
| `scripts/init_skill.py` | 初始化 skill 目录 | skill-creator |
| `scripts/package_skill.py` | 打包 skill | skill-creator |
| `scripts/merge_evolution.py` | 合并经验 JSON | skill-evolution-manager |
| `scripts/smart_stitch.py` | 缝合经验到 SKILL.md | skill-evolution-manager |
| `scripts/align_all.py` | 全量对齐经验 | skill-evolution-manager |

## 快速开始

**用户只需说**：
- 「创建一个 skill，用来 XXX」
- 「把这个 repo 变成 skill：URL」
- 「自动生成一个 XXX skill」

**元 skill 自主完成**：
1. 查找现有 skill
2. 决定生成策略
3. 调用对应 skill
4. 打包输出
5. 记录经验

---

*这是一个套娃式的元 skill，能够自主调用其他 skill 来完成 skill 的自动生成。*
