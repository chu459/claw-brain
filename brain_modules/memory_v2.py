"""
MemoryV2 - 增强记忆系统
实现三级记忆：短期记忆 + 压缩记忆 + 反思记忆
"""

import json
import time
import os
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from datetime import datetime


@dataclass
class CompressedMemory:
    """压缩记忆结构"""
    topic: str
    key_findings: str
    success_patterns: str
    failure_lessons: str
    confidence: float  # 0-1
    timestamp: str
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> "CompressedMemory":
        return cls(**data)
    
    def to_text(self, max_length: int = 200) -> str:
        """转换为文本格式，限制字数"""
        text = f"主题：{self.topic}\n"
        text += f"关键发现：{self.key_findings}\n"
        text += f"成功模式：{self.success_patterns}\n"
        text += f"失败教训：{self.failure_lessons}\n"
        text += f"置信度：{self.confidence}"
        
        if len(text) > max_length:
            text = text[:max_length-3] + "..."
        return text


@dataclass
class Reflection:
    """反思记忆结构"""
    action: str
    key_factor: str  # 成功/失败的关键
    reusable_pattern: str  # 可复用模式
    avoid_next_time: str  # 下次避免
    timestamp: str
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> "Reflection":
        return cls(**data)
    
    def format(self) -> str:
        """格式化反思输出"""
        return f"""【反思】
行动：{self.action}
关键：{self.key_factor}
模式：{self.reusable_pattern}
避免：{self.avoid_next_time}
"""


class MemoryV2:
    """
    增强记忆系统 V2
    
    三级记忆：
    1. 短期记忆：固定 Session Key，共享上下文
    2. 压缩记忆：主题/关键发现/成功模式/失败教训/置信度（200字内）
    3. 反思记忆：每次行动后的关键/模式/避免
    
    保留 V1 功能：
    - actions_history
    - failed_attempts
    - successful_patterns
    - current_strategy
    - milestones
    """
    
    SESSION_KEY = "autonomous-money-maker"
    COMPRESS_THRESHOLD = 10  # 多少条行动后触发压缩
    MAX_ACTIONS = 50
    MAX_PATTERNS = 5
    MAX_REFLECTIONS = 30
    MAX_COMPRESSED = 10
    
    def __init__(self, filepath: str = "system_memory_v2.json"):
        """
        初始化记忆系统
        
        Args:
            filepath: 记忆文件路径
        """
        self.filepath = filepath
        self.data = self._load()
        
    def _load(self) -> Dict:
        """加载记忆数据"""
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        
        # 初始化默认结构
        return {
            # V1 兼容字段
            "actions_history": [],
            "failed_attempts": [],
            "successful_patterns": [],
            "current_strategy": "初步市场调研",
            "milestones": [],
            # V2 新增字段
            "compressed_memories": [],  # 压缩记忆列表
            "reflections": [],  # 反思记忆列表
            "session_data": {  # 短期记忆数据
                "session_key": self.SESSION_KEY,
                "context": {},
                "last_updated": "",
            },
            "version": "2.0",
        }
    
    def save(self):
        """保存记忆数据"""
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
    
    # ========== V1 兼容方法 ==========
    
    def add_action(self, action: str, result: str, success: bool):
        """
        添加行动记录（V1兼容）
        
        Args:
            action: 行动描述
            result: 结果
            success: 是否成功
        """
        self.data["actions_history"].append({
            "action": action,
            "result": result[:500],
            "success": success,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        
        # 限制历史记录数量
        if len(self.data["actions_history"]) > self.MAX_ACTIONS:
            self.data["actions_history"] = self.data["actions_history"][-self.MAX_ACTIONS:]
        
        # 更新成功/失败模式（截断+去重，避免巨型指令撑爆上下文）
        pattern_snippet = action[:120].replace('\n', ' ').strip()
        if success:
            if pattern_snippet not in self.data["successful_patterns"]:
                self.data["successful_patterns"].append(pattern_snippet)
            if len(self.data["successful_patterns"]) > self.MAX_PATTERNS:
                self.data["successful_patterns"] = self.data["successful_patterns"][-self.MAX_PATTERNS:]
        else:
            if pattern_snippet not in self.data["failed_attempts"]:
                self.data["failed_attempts"].append(pattern_snippet)
            if len(self.data["failed_attempts"]) > self.MAX_PATTERNS:
                self.data["failed_attempts"] = self.data["failed_attempts"][-self.MAX_PATTERNS:]
        
        # 检查是否需要压缩
        if len(self.data["actions_history"]) % self.COMPRESS_THRESHOLD == 0:
            self._auto_compress()
        
        self.save()
    
    def get_summary(self, max_items: int = 5) -> str:
        """
        获取记忆摘要（V1兼容）
        
        Args:
            max_items: 最近行动数量
            
        Returns:
            记忆摘要文本
        """
        lines = []
        
        # 最近的压缩记忆
        if self.data["compressed_memories"]:
            latest = self.data["compressed_memories"][-1]
            lines.append(f"[压缩记忆] {latest.get('topic', '无主题')}")
        
        # 最近的行动
        recent = self.data["actions_history"][-max_items:]
        for i, item in enumerate(recent, 1):
            status = "OK" if item["success"] else "FAIL"
            lines.append(f"{i}. [{status}] {item['action']} -> {item['result'][:100]}...")
        
        # 成功/失败模式
        if self.data["successful_patterns"]:
            patterns = [p[:80] for p in self.data["successful_patterns"][-3:]]
            lines.append(f"成功模式: {', '.join(patterns)}")
        if self.data["failed_attempts"]:
            patterns = [p[:80] for p in self.data["failed_attempts"][-3:]]
            lines.append(f"失败记录: {', '.join(patterns)}")
        
        return "\n".join(lines) if lines else "(空白板，刚开始)"
    
    def update_strategy(self, strategy: str):
        """更新当前策略（V1兼容）"""
        self.data["current_strategy"] = strategy
        self.save()
    
    def add_milestone(self, description: str):
        """添加里程碑（V1兼容）"""
        self.data["milestones"].append({
            "description": description,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        self.save()
    
    # ========== V2 新增方法：压缩记忆 ==========
    
    def compress_memory(
        self,
        topic: str,
        key_findings: str,
        success_patterns: str,
        failure_lessons: str,
        confidence: float,
    ) -> CompressedMemory:
        """
        创建压缩记忆
        
        Args:
            topic: 主题
            key_findings: 关键发现
            success_patterns: 成功模式
            failure_lessons: 失败教训
            confidence: 置信度 0-1
            
        Returns:
            CompressedMemory 对象
        """
        compressed = CompressedMemory(
            topic=topic,
            key_findings=key_findings,
            success_patterns=success_patterns,
            failure_lessons=failure_lessons,
            confidence=confidence,
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        
        self.data["compressed_memories"].append(compressed.to_dict())
        
        # 限制数量
        if len(self.data["compressed_memories"]) > self.MAX_COMPRESSED:
            self.data["compressed_memories"] = self.data["compressed_memories"][-self.MAX_COMPRESSED:]
        
        self.save()
        return compressed
    
    def _auto_compress(self):
        """自动压缩记忆（内部调用）"""
        # 获取最近的行动
        recent_actions = self.data["actions_history"][-self.COMPRESS_THRESHOLD:]
        
        if not recent_actions:
            return
        
        # 分析成功/失败
        successes = [a for a in recent_actions if a["success"]]
        failures = [a for a in recent_actions if not a["success"]]
        
        # 生成压缩记忆
        topic = f"最近{self.COMPRESS_THRESHOLD}轮行动总结"
        key_findings = f"成功{len(successes)}次，失败{len(failures)}次"
        success_patterns = "; ".join([a["action"][:30] for a in successes[:2]]) if successes else "无"
        failure_lessons = "; ".join([a["action"][:30] for a in failures[:2]]) if failures else "无"
        confidence = len(successes) / len(recent_actions) if recent_actions else 0.5
        
        self.compress_memory(
            topic=topic,
            key_findings=key_findings,
            success_patterns=success_patterns,
            failure_lessons=failure_lessons,
            confidence=round(confidence, 2),
        )
    
    def get_compressed_summary(self, max_items: int = 3) -> str:
        """
        获取压缩记忆摘要
        
        Args:
            max_items: 最多返回几条
            
        Returns:
            压缩记忆文本
        """
        memories = self.data["compressed_memories"][-max_items:]
        if not memories:
            return "(暂无压缩记忆)"
        
        lines = []
        for i, m in enumerate(memories, 1):
            cm = CompressedMemory.from_dict(m)
            lines.append(f"{i}. {cm.to_text(100)}")
        
        return "\n".join(lines)
    
    # ========== V2 新增方法：反思记忆 ==========
    
    def add_reflection(
        self,
        action: str,
        key_factor: str,
        reusable_pattern: str,
        avoid_next_time: str,
    ) -> Reflection:
        """
        添加反思记忆
        
        Args:
            action: 行动描述
            key_factor: 成功/失败的关键
            reusable_pattern: 可复用模式
            avoid_next_time: 下次避免
            
        Returns:
            Reflection 对象
        """
        reflection = Reflection(
            action=action,
            key_factor=key_factor,
            reusable_pattern=reusable_pattern,
            avoid_next_time=avoid_next_time,
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        
        self.data["reflections"].append(reflection.to_dict())
        
        # 限制数量
        if len(self.data["reflections"]) > self.MAX_REFLECTIONS:
            self.data["reflections"] = self.data["reflections"][-self.MAX_REFLECTIONS:]
        
        self.save()
        return reflection
    
    def get_reflections(self, max_items: int = 5) -> List[Reflection]:
        """
        获取反思记忆列表
        
        Args:
            max_items: 最多返回几条
            
        Returns:
            Reflection 对象列表
        """
        reflections = self.data["reflections"][-max_items:]
        return [Reflection.from_dict(r) for r in reflections]
    
    def get_reflections_text(self, max_items: int = 3) -> str:
        """
        获取反思记忆文本
        
        Args:
            max_items: 最多返回几条
            
        Returns:
            格式化的反思文本
        """
        reflections = self.get_reflections(max_items)
        if not reflections:
            return "(暂无反思记录)"
        
        return "\n---\n".join([r.format() for r in reflections])
    
    # ========== V2 新增方法：短期记忆 ==========
    
    def get_session_key(self) -> str:
        """获取固定的 Session Key"""
        return self.SESSION_KEY
    
    def update_session_context(self, key: str, value: Any):
        """
        更新短期记忆上下文
        
        Args:
            key: 键
            value: 值
        """
        self.data["session_data"]["context"][key] = value
        self.data["session_data"]["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.save()
    
    def get_session_context(self, key: str, default: Any = None) -> Any:
        """
        获取短期记忆上下文
        
        Args:
            key: 键
            default: 默认值
            
        Returns:
            值或默认值
        """
        return self.data["session_data"]["context"].get(key, default)
    
    def clear_session_context(self):
        """清空短期记忆上下文"""
        self.data["session_data"]["context"] = {}
        self.data["session_data"]["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.save()
    
    def get_session_summary(self) -> str:
        """获取短期记忆摘要"""
        context = self.data["session_data"]["context"]
        if not context:
            return "(短期记忆为空)"
        
        lines = [f"Session: {self.SESSION_KEY}"]
        for k, v in list(context.items())[:5]:  # 最多显示5条
            v_str = str(v)[:50]
            lines.append(f"  {k}: {v_str}")
        
        return "\n".join(lines)
    
    # ========== 综合记忆摘要 ==========
    
    def get_full_summary(self) -> str:
        """获取简明记忆摘要（Brain 能快速读懂）"""
        sections = []
        
        # 最近行动（最重要）
        recent = self.data["actions_history"][-5:]
        if recent:
            lines = []
            for a in recent:
                s = "OK" if a["success"] else "FAIL"
                lines.append(f"[{s}] {a['action'][:60]} → {a['result'][:60]}")
            sections.append("最近行动:\n" + "\n".join(lines))
        
        # 成功模式（截断输出）
        if self.data["successful_patterns"]:
            patterns = [p[:80] for p in self.data["successful_patterns"][-3:]]
            sections.append(f"成功模式: {', '.join(patterns)}")
        
        # 失败教训（截断输出）
        if self.data["failed_attempts"]:
            patterns = [p[:80] for p in self.data["failed_attempts"][-3:]]
            sections.append(f"失败教训: {', '.join(patterns)}")
        
        # 反思
        reflections = self.data["reflections"][-2:]
        if reflections:
            r_lines = []
            for r in reflections:
                r_lines.append(f"  {r.get('action','')[:40]}: {r.get('key_factor','')[:40]}")
            sections.append("反思:\n" + "\n".join(r_lines))
        
        return "\n".join(sections) if sections else "(空白板)"

    def get_summary(self, max_items: int = 5) -> str:
        """获取记忆摘要（兼容旧接口）"""
        return self.get_full_summary()


# 便捷函数
def create_memory_v2(filepath: str = "system_memory_v2.json") -> MemoryV2:
    """创建 MemoryV2 实例的工厂函数"""
    return MemoryV2(filepath)
