"""
KnowledgeBase - 知识库系统
扫描 workspace/knowledge/*.md，加载为 AI 可引用的知识文档。
每份文档有 frontmatter 元数据，正文按原样提供给 AI。
"""

import json
import os
from pathlib import Path
from typing import Dict, List, Optional


class KnowledgeDoc:
    def __init__(self, name: str, filepath: str):
        self.name = name
        self.filepath = filepath
        self.title = name
        self.description = ""
        self.tags: List[str] = []
        self.priority = "medium"
        self.content = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "tags": self.tags,
            "priority": self.priority,
            "filepath": self.filepath,
        }

    def format_for_prompt(self) -> str:
        lines = [
            f"### {self.title}",
            f"标签: {', '.join(self.tags) if self.tags else '无'}",
            f"优先级: {self.priority}",
            "",
            self.content,
        ]
        return "\n".join(lines)


class KnowledgeBase:
    def __init__(self, knowledge_dir: str):
        self.knowledge_dir = Path(knowledge_dir)
        self.docs: Dict[str, KnowledgeDoc] = {}
        self._scan()

    def _scan(self):
        self.docs = {}
        if not self.knowledge_dir.exists():
            return
        for md_file in self.knowledge_dir.glob("*.md"):
            doc = self._parse_file(md_file)
            self.docs[doc.name] = doc

    def _parse_file(self, filepath: Path) -> KnowledgeDoc:
        name = filepath.stem
        doc = KnowledgeDoc(name=name, filepath=str(filepath))
        try:
            raw = filepath.read_text(encoding="utf-8")
        except Exception:
            return doc
        if raw.startswith("---"):
            end = raw.find("---", 3)
            if end != -1:
                frontmatter = raw[3:end].strip()
                raw = raw[end + 3:].strip()
                for line in frontmatter.split("\n"):
                    line = line.strip()
                    if line.startswith("title:"):
                        doc.title = line.split(":", 1)[1].strip()
                    elif line.startswith("description:"):
                        doc.description = line.split(":", 1)[1].strip()
                    elif line.startswith("tags:"):
                        tags_str = line.split(":", 1)[1].strip()
                        doc.tags = [t.strip() for t in tags_str.split(",") if t.strip()]
                    elif line.startswith("priority:"):
                        doc.priority = line.split(":", 1)[1].strip()
        doc.content = raw.strip()
        return doc

    def reload(self):
        self._scan()

    def get_catalog(self) -> str:
        if not self.docs:
            return "(知识库为空)"
        lines = []
        for doc in self.docs.values():
            lines.append(f"- **{doc.title}** ({doc.name}.md) — {doc.description or '无描述'} [优先级:{doc.priority}]")
        return "\n".join(lines)

    def format_for_prompt(self) -> str:
        if not self.docs:
            return ""

        parts = [
            "## ══════ 知识库（参考文档，按需引用） ══════",
            "",
            "以下知识文档在你决策时可随时参考：",
            "",
            self.get_catalog(),
            "",
            "---",
            "",
        ]

        sorted_docs = sorted(
            self.docs.values(),
            key=lambda d: {"high": 0, "medium": 1, "low": 2}.get(d.priority, 1),
        )
        for doc in sorted_docs:
            parts.append(doc.format_for_prompt())
            parts.append("")

        parts.append("## ══════ 知识库结束 ══════")
        return "\n".join(parts)

    def get_doc(self, name: str) -> Optional[KnowledgeDoc]:
        return self.docs.get(name)
