"""
SpaceParser - Space 文档解析器
只负责：读取 md 文件，传给 AI 自己解析
"""

from pathlib import Path
from typing import Dict, List, Optional, Any


class SpaceTask:
    """Space 文档 — 只存原始内容，让 AI 自己理解"""
    goal: str = ""
    raw_content: str = ""
    source_file: str = ""
    
    def __init__(self, raw_content: str = "", source_file: str = ""):
        self.raw_content = raw_content
        self.source_file = source_file
        # 尝试提取第一行作为目标摘要
        lines = raw_content.strip().split("\n")
        if lines and lines[0].startswith("#"):
            self.goal = lines[0].lstrip("#").strip()[:100]
        elif lines:
            self.goal = lines[0][:100]
    
    def is_valid(self) -> bool:
        return bool(self.raw_content.strip())


class SpaceParser:
    """极简解析器 — 不做字段拆解，原样传给 AI"""
    
    def parse(self, content: str, source_file: str = "") -> SpaceTask:
        return SpaceTask(raw_content=content, source_file=source_file)
    
    def parse_file(self, filepath: str) -> SpaceTask:
        path = Path(filepath)
        if not path.exists():
            return SpaceTask()
        content = path.read_text(encoding="utf-8")
        return self.parse(content, source_file=filepath)


# 便捷函数
def parse_space_file(filepath: str) -> SpaceTask:
    return SpaceParser().parse_file(filepath)
