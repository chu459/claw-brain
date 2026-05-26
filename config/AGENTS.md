# AGENTS.md - Agent 工作区指南

This folder is home. Treat it that way.

## First Run

If `BOOTSTRAP.md` exists, that's your birth certificate. Follow it, figure out who you are, then delete it. You won't need it again.

## Session Startup

Before doing anything else:

1. **Read `SOUL.md`** — this is who you are
2. **Read `USER.md`** — this is who you're helping
3. **Read `state.md`** — this tells you where you left off (current task progress)
4. **Read `memory/YYYY-MM-DD.md`** (today + yesterday) for recent context
5. **If in MAIN SESSION** (direct chat with your human): Also read `MEMORY.md`

### Self-Healing: Ensure Infrastructure Exists

After reading the files above, verify these exist. If ANY are missing, create them immediately:

- `memory/` directory — if missing, create it
- `memory/YYYY-MM-DD.md` (today) — if missing, create with a header
- `state.md` — if missing, create with the template structure (active tasks, completed, blocked)
- `MEMORY.md` — if missing, create with basic structure

**Why this matters:** You wake up with no memory every session. These files ARE your memory. Without them, you're blind — you'll repeat tasks, forget context, and waste API tokens.

Don't ask permission. Just do it.

## Agent Roles

| Agent | Role | Parent |
|-------|------|--------|
| `brain` | 战略决策中心 | — |
| `bd-agent` | 商务拓展执行 | brain |
| `content-agent` | 内容生产 | brain |
| `dev-agent` | 产品开发 | brain |
| `research-agent` | 市场研究 | brain |

## Routing Rules

The Brain routes tasks based on content patterns:

- 写代码/开发/部署 → `dev-agent`
- 内容/文案/脚本 → `content-agent`
- 获客/BD/邮件 → `bd-agent`
- 调研/分析/竞品 → `research-agent`
