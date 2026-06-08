"""Lightweight message cards for ClawBrain.

This module keeps human/agent interaction structured without changing the main
run-loop architecture. Cards are persisted under data/messages, which is
runtime-only and ignored by git.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


CARD_TYPES = {"choice", "fill", "feedback", "proposal", "announcement", "plan", "todo"}


@dataclass
class MessageCard:
    id: str
    type: str
    title: str
    content: str = ""
    options: list[str] = field(default_factory=list)
    required: bool = True
    timeout: int = 0
    status: str = "pending"
    answer: Any = None
    source: str = "system"
    priority: str = "normal"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    answered_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MessageCard":
        clean = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**clean)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def question_text(self) -> str:
        parts = [self.title.strip()]
        if self.content.strip():
            parts.append(self.content.strip())
        if self.options:
            parts.append("可选项：" + " / ".join(str(o) for o in self.options))
        return "\n".join(p for p in parts if p)


class MessageCenter:
    def __init__(self, base_dir: str | Path, session_id: str):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id or "default"
        self.path = self.base_dir / f"{self.session_id}.json"
        self.latest_file = self.base_dir / "latest_session.txt"
        self.cards: dict[str, MessageCard] = {}
        self._load()
        self._save_latest()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.cards = {
                k: MessageCard.from_dict(v)
                for k, v in raw.get("cards", {}).items()
            }
        except Exception:
            self.cards = {}

    def _save_latest(self) -> None:
        try:
            self.latest_file.write_text(self.session_id, encoding="utf-8")
        except Exception:
            pass

    def _save(self) -> None:
        data = {
            "session_id": self.session_id,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cards": {k: v.to_dict() for k, v in self.cards.items()},
        }
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._save_latest()

    def add_card(
        self,
        card_type: str,
        title: str,
        content: str = "",
        options: list[str] | None = None,
        required: bool = True,
        timeout: int = 0,
        source: str = "system",
        priority: str = "normal",
        metadata: dict[str, Any] | None = None,
    ) -> MessageCard:
        if card_type not in CARD_TYPES:
            card_type = "fill"
        card_id = f"card_{card_type}_{uuid.uuid4().hex[:10]}"
        card = MessageCard(
            id=card_id,
            type=card_type,
            title=title.strip() or "需要确认",
            content=content.strip(),
            options=list(options or []),
            required=required,
            timeout=timeout,
            source=source,
            priority=priority,
            metadata=metadata or {},
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.cards[card_id] = card
        self._save()
        return card

    def answer_card(self, card_id: str, answer: Any) -> bool:
        card = self.cards.get(card_id)
        if not card:
            return False
        card.answer = answer
        card.status = "answered"
        card.answered_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save()
        return True

    def dismiss_card(self, card_id: str, reason: str = "") -> bool:
        card = self.cards.get(card_id)
        if not card:
            return False
        card.status = "dismissed"
        card.answer = reason
        card.answered_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save()
        return True

    def expire_old_cards(self) -> None:
        now = time.time()
        changed = False
        for card in self.cards.values():
            if card.status != "pending" or not card.timeout:
                continue
            try:
                created = time.mktime(time.strptime(card.created_at, "%Y-%m-%d %H:%M:%S"))
            except Exception:
                continue
            if now - created > card.timeout:
                card.status = "expired"
                card.answered_at = time.strftime("%Y-%m-%d %H:%M:%S")
                changed = True
        if changed:
            self._save()

    def get_pending_cards(self, required_only: bool = True) -> list[MessageCard]:
        self.expire_old_cards()
        cards = [c for c in self.cards.values() if c.status == "pending"]
        if required_only:
            cards = [c for c in cards if c.required]
        return sorted(cards, key=lambda c: (c.priority != "urgent", c.created_at))

    def build_prompt_context(self, limit: int = 8) -> str:
        if not self.cards:
            return ""
        rows = []
        for card in sorted(self.cards.values(), key=lambda c: c.created_at)[-limit:]:
            answer = "" if card.answer is None else f" answer={str(card.answer)[:120]}"
            rows.append(
                f"- [{card.status}] {card.type}: {card.title[:80]}{answer}"
            )
        pending = self.get_pending_cards(required_only=True)
        if pending:
            rows.append("")
            rows.append("当前有待用户确认的卡片，回答前不要继续需要该答案的动作。")
        return "\n".join(rows)

    def to_payload(self, limit: int = 50) -> dict[str, Any]:
        cards = sorted(self.cards.values(), key=lambda c: c.created_at)[-limit:]
        pending = self.get_pending_cards(required_only=True)
        return {
            "session_id": self.session_id,
            "cards": [c.to_dict() for c in cards],
            "pending_count": len(pending),
            "pending": [c.to_dict() for c in pending],
        }


def create_message_center(project_root: str | Path, session_id: str) -> MessageCenter:
    return MessageCenter(Path(project_root) / "data" / "messages", session_id)


def open_latest_message_center(project_root: str | Path) -> MessageCenter | None:
    base_dir = Path(project_root) / "data" / "messages"
    latest = base_dir / "latest_session.txt"
    if not latest.exists():
        return None
    session_id = latest.read_text(encoding="utf-8").strip()
    if not session_id:
        return None
    return MessageCenter(base_dir, session_id)


def latest_message_payload(project_root: str | Path) -> dict[str, Any]:
    center = open_latest_message_center(project_root)
    if not center:
        return {"session_id": "", "cards": [], "pending_count": 0, "pending": []}
    return center.to_payload()

