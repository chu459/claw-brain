# Claw-brain 🧠

> Give OpenClaw a brain that decides for itself.

OpenClaw gave AI hands and feet — browser automation that follows instructions.
**Claw-brain gives it a brain that decides what to do next.**

This is the missing piece between "AI can execute tasks" and "AI can figure out what tasks to execute." An autonomous decision engine that runs a self-correcting loop: **Think → Decide → Act → Observe → Think again.**

## The Big Picture

```
┌─────────────────────────────────────────────────────────┐
│                    CLAW-BRAIN                            │
│                                                         │
│   ┌──────────┐    decision     ┌──────────────────┐     │
│   │  BRAIN   │ ────────────→   │   OPENCLAW       │     │
│   │  (LLM)   │ ←────────────   │  (Browser Agent) │     │
│   │          │    feedback     │                  │     │
│   └────┬─────┘                  └──────────────────┘     │
│        │                                                 │
│        │  writes experience      ┌──────────────────┐    │
│        └──────────────────────→  │  MEMORY          │    │
│                                  │  (Whiteboard)    │    │
│                                  └──────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

Most AI agents today are **reactive** — they wait for human instructions and execute them. Claw-brain is **proactive** — it sets its own goals, evaluates outcomes, adjusts strategy, and keeps going. autonomously.

## How It Works

Every cycle, the Brain:

1. **Reflects** on the current goal, accumulated memory, and feedback from the last action
2. **Decides** what to do next — as a natural language instruction
3. **Sends** the instruction to OpenClaw, which autonomously operates the browser
4. **Receives** the execution result (success or failure)
5. **Records** the experience — what worked, what didn't, and why
6. **Loops back** to step 1 with updated context

If a step fails, the Brain doesn't just retry — it **analyzes the failure and adjusts its entire strategy**. This is not a script with error handling. This is an agent that learns from its own mistakes in real time.

When the Brain needs human input (a phone number, a verification code, a strategic decision), it pauses and asks. Otherwise, it runs.

## What Can You Do With This?

Claw-brain is not a tool for technical people. It's a decision engine for **anyone who has a goal.**

You don't need to know how AI works. You don't need to write prompts, chain tools, or design workflows. You just tell it what you want — in plain language, the way you'd tell a capable friend — and it figures out the rest.

Some fundamental things people want:

### "Help me make money."
You set a revenue goal. The system researches what's possible, validates real opportunities, tests approaches, learns from failures, and iterates until something works. You don't plan the steps. You just set the destination.

### "Help me get [N] customers."
Tell it who your target customer is. It finds where they hang out, studies how they make purchasing decisions, tests different approaches, and iterates until leads start flowing. You review the results, not the process.

### "Help me grow my [business/project]."
Feed it your current situation. It studies your market, finds your bottleneck, and starts working on it — not reporting about it, actually working on it. Every cycle moves the needle.

### "Help me cut my costs to [target]."
Give it access to your spending data. It identifies waste, finds cheaper alternatives, negotiates better deals on your behalf, and tracks savings in real time. You see the number go down.

### "Help me build and launch [X]."
Describe what you want to build. It researches the market, validates the idea, finds the fastest path to a working product, and starts executing. You steer; it builds.

### "Help me find a [job/client/opportunity] that matches [criteria]."
Set your requirements — salary, location, type, industry. It scans every relevant platform, applies on your behalf, follows up, and brings you only the matches worth your time. You pick. It hunts.

The pattern is the same in every case: **you state a concrete goal with a measurable outcome, the system turns it into action and closes the gap.**

The most powerful application is the one you'll think of when you realize you don't need to understand AI to use it.

## Philosophy

There's a kind of anxiety spreading right now — "AI anxiety." People feel like they need to learn prompting, understand agents, master tools, or get left behind.

**That's wrong.**

AI should serve you the way electricity does. You don't need to understand alternating current to turn on a light. You shouldn't need to understand transformer architecture to make AI work for you.

Claw-brain is built on a belief: **a person with no technical background should be able to walk up to this system, say "help me make money," and become a one-person powerhouse — a super individual — just by stating what they want.**

No learning curve. No prompt engineering. No tool chaining. Just your goal, in your words.

I've watched too many smart people spend their time on operational tasks — copying data, filling forms, checking dashboards, doing things that machines should do. And I've watched too many ordinary people feel paralyzed by AI, convinced it's too complex for them.

Claw-brain exists for both of them. **Your time should be spent on thinking, not clicking.** And you should be able to harness AI without becoming an AI expert.

The final goal isn't "AI that does things." It's **restoring time and freedom to every person** — achieving your goals with the least possible effort, freeing you to live your life instead of operating a computer.

## Quick Start

### Prerequisites
- [OpenClaw](https://github.com/nicepkg/openclaw) installed and running (`openclaw gateway run --force`)
- Python 3.13+
- An LLM API key (DeepSeek, OpenAI, or any OpenAI-compatible API)

### Install

```bash
git clone https://github.com/chu459/claw-brain.git
cd claw-brain
pip install openai fastapi uvicorn
```

### Configure

Set your environment variables:

```bash
export BRAIN_API_KEY="your-api-key"
export BRAIN_BASE_URL="https://api.deepseek.com/v1"
export BRAIN_MODEL="deepseek-chat"
export OPENCLAW_GATEWAY_URL="http://127.0.0.1:18789"
```

### Run

**Web Console (recommended):**
```bash
python web_console.py
# Open http://127.0.0.1:7860
```

**CLI:**
```bash
python autonomous_system.py --interactive
# or
python autonomous_system.py --goal "Your goal here" --loops 50
```

## Architecture

| Component | Role |
|-----------|------|
| **Brain** | LLM-powered strategy engine. Receives context, outputs structured decisions (JSON). |
| **OpenClaw Client** | Bridges to OpenClaw. Translates Brain's natural language decisions into browser actions. |
| **Memory** | Persistent whiteboard. Records successful patterns, failed attempts, and strategy evolution. |
| **Web Console** | FastAPI-based dashboard. Real-time monitoring, human-in-the-loop chat, configurable parameters. |

## Key Design Decisions

- **Natural language as the interface between Brain and execution** — no brittle API coupling. The Brain thinks in concepts; OpenClaw acts on instructions. This makes the system resilient to UI changes on target websites.
- **Failure is data, not an error** — the Brain treats every failed action as input for the next decision, not as a reason to stop.
- **Human-in-the-loop by exception** — the system only interrupts you when it genuinely can't proceed. Otherwise, it runs.
- **Memory is the real product** — over time, the accumulated experience becomes more valuable than any single decision.

## License

[AGPL-3.0](LICENSE) — This project uses the GNU Affero General Public License v3.0.

If you use this code (even over a network), you must share your modifications under the same license. This protects the open-source ecosystem from being wrapped into closed SaaS products without giving back.

## Author

Built by [Claw-brain](https://twitter.com/Claw_brain) — 19, building AI that decides for itself.

---

*"The next step for AI isn't better execution. It's autonomous decision-making."*
