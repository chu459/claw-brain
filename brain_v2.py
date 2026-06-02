"""
Optional ClawBrain V2 context adapter.

This keeps the current core.py engine stable while reusing the modular
brain pieces from brain_modules: soul, memory_v2, knowledge, and feedback.
All failures are soft failures, so the money loop can keep running.
"""

from pathlib import Path
from typing import Optional


class BrainV2Context:
    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.data_dir = self.base_dir / "data" / "brain_v2"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        from brain_modules.soul import create_soul
        from brain_modules.memory_v2 import create_memory_v2
        from brain_modules.knowledge import KnowledgeBase
        from brain_modules.feedback import create_feedback_manager

        self.soul = create_soul()
        self.memory = create_memory_v2(str(self.data_dir / "system_memory_v2.json"))
        self.feedback = create_feedback_manager(str(self.data_dir))

        knowledge_dirs = [
            self.base_dir / "workspace" / "knowledge",
            self.base_dir / "workspace_templates" / "knowledge",
            self.base_dir / "wiki",
        ]
        self.knowledge_bases = [
            KnowledgeBase(str(path)) for path in knowledge_dirs if path.exists()
        ]

    def build_prompt_context(
        self,
        goal: str,
        last_feedback: str = "",
        loop_count: int = 0,
        session_id: str = "default",
        max_chars: int = 1800,
    ) -> str:
        parts: list[str] = []

        try:
            cycle = self.soul.get_current_cycle()
            parts.append("[Soul]")
            parts.append(self.soul.get_cycle_description(cycle))
            parts.append(self.soul.get_strategy_reference({"goal": goal, "loop_count": loop_count}))
        except Exception:
            pass

        try:
            memory_text = self.memory.get_full_summary()
            if memory_text:
                parts.append("[MemoryV2]")
                parts.append(memory_text)
        except Exception:
            pass

        try:
            feedback_text = self.feedback.format_for_prompt(session_id, last_n_rounds=3)
            if feedback_text:
                parts.append("[Feedback]")
                parts.append(feedback_text)
        except Exception:
            pass

        for kb in self.knowledge_bases:
            try:
                text = kb.format_for_prompt()
                if text:
                    parts.append("[Knowledge]")
                    parts.append(text)
            except Exception:
                pass

        text = "\n\n".join(parts).strip()
        if len(text) > max_chars:
            text = text[: max_chars - 20] + "\n...(trimmed)"
        return text

    def remember_round(
        self,
        action: str,
        result: str,
        success: bool,
        key_factor: str = "",
    ) -> None:
        try:
            self.memory.add_action(action=action, result=result, success=success)
            if key_factor:
                self.memory.add_reflection(
                    action=action,
                    key_factor=key_factor,
                    reusable_pattern=key_factor,
                    avoid_next_time="" if success else result[:300],
                )
        except Exception:
            pass


def create_brain_v2_context(base_dir: Optional[str | Path] = None) -> Optional[BrainV2Context]:
    try:
        return BrainV2Context(base_dir or Path(__file__).parent)
    except Exception as exc:
        print(f"[BrainV2] disabled: {exc}")
        return None
