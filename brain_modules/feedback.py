"""
Feedback - 吐槽信箱/评价系统
实现：
1. 每轮结束后的用户评价收集
2. 反馈影响AI后续行为的机制
3. 反馈统计和分析
"""

import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, asdict, field
from enum import Enum


class FeedbackType(Enum):
    """反馈类型"""
    RATING = "rating"      # 评分 (1-5)
    COMMENT = "comment"    # 文字评论
    TAG = "tag"            # 标签选择
    CORRECTION = "correction"  # 纠正/指导


class FeedbackSentiment(Enum):
    """反馈情感"""
    POSITIVE = "positive"   # 正面
    NEUTRAL = "neutral"     # 中性
    NEGATIVE = "negative"   # 负面


@dataclass
class RoundFeedback:
    """单轮反馈"""
    id: str
    round_number: int
    session_id: str
    
    # 评分 (1-5星)
    rating: int = 0  # 0表示未评分
    
    # 文字反馈
    comment: str = ""
    
    # 标签
    tags: List[str] = field(default_factory=list)
    
    # 纠正/指导 (用户直接告诉AI该怎么做)
    correction: str = ""
    
    # 情感分析结果
    sentiment: str = "neutral"
    
    # 关联的行动
    action_taken: str = ""
    action_result: str = ""
    
    # 是否已应用到AI
    applied: bool = False
    application_note: str = ""
    
    created_at: str = ""
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> "RoundFeedback":
        return cls(**data)


@dataclass
class FeedbackSummary:
    """反馈汇总"""
    session_id: str
    total_rounds: int = 0
    rated_rounds: int = 0
    average_rating: float = 0.0
    
    # 标签统计
    tag_counts: Dict[str, int] = field(default_factory=dict)
    
    # 常见纠正
    common_corrections: List[str] = field(default_factory=list)
    
    # 改进建议汇总
    improvement_suggestions: str = ""
    
    updated_at: str = ""
    
    def to_dict(self) -> Dict:
        return asdict(self)


class FeedbackManager:
    """
    反馈管理器 (吐槽信箱)
    
    职责：
    1. 收集每轮的用户评价
    2. 分析反馈情感
    3. 生成改进建议
    4. 将反馈传递给Brain影响后续决策
    """
    
    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)
        
        self.feedback_file = self.data_dir / "feedback.json"
        self.summary_file = self.data_dir / "feedback_summary.json"
        
        self.feedbacks: Dict[str, RoundFeedback] = {}
        self.summaries: Dict[str, FeedbackSummary] = {}
        
        # 回调
        self.on_feedback_received: Optional[Callable[[RoundFeedback], None]] = None
        
        self._load()
    
    def _load(self):
        """加载数据"""
        if self.feedback_file.exists():
            with open(self.feedback_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.feedbacks = {k: RoundFeedback.from_dict(v) for k, v in data.items()}
        
        if self.summary_file.exists():
            with open(self.summary_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.summaries = {k: FeedbackSummary(**v) for k, v in data.items()}
    
    def _save(self):
        """保存数据"""
        with open(self.feedback_file, "w", encoding="utf-8") as f:
            json.dump({k: v.to_dict() for k, v in self.feedbacks.items()}, f, indent=2, ensure_ascii=False)
        
        with open(self.summary_file, "w", encoding="utf-8") as f:
            json.dump({k: v.to_dict() for k, v in self.summaries.items()}, f, indent=2, ensure_ascii=False)
    
    def submit_feedback(self, session_id: str, round_number: int,
                       rating: int = 0, comment: str = "", 
                       tags: List[str] = None, correction: str = "",
                       action_taken: str = "", action_result: str = "") -> RoundFeedback:
        """
        提交反馈
        
        Args:
            session_id: 会话ID
            round_number: 轮次
            rating: 评分 1-5
            comment: 评论
            tags: 标签列表
            correction: 纠正/指导
            action_taken: 执行的行动
            action_result: 行动结果
            
        Returns:
            RoundFeedback 对象
        """
        fid = f"fb_{session_id}_{round_number}"
        
        # 分析情感
        sentiment = self._analyze_sentiment(rating, comment, correction)
        
        feedback = RoundFeedback(
            id=fid,
            round_number=round_number,
            session_id=session_id,
            rating=rating,
            comment=comment,
            tags=tags or [],
            correction=correction,
            sentiment=sentiment,
            action_taken=action_taken,
            action_result=action_result,
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        
        self.feedbacks[fid] = feedback
        
        # 更新汇总
        self._update_summary(session_id)
        
        self._save()
        
        # 触发回调
        if self.on_feedback_received:
            self.on_feedback_received(feedback)
        
        return feedback
    
    def _analyze_sentiment(self, rating: int, comment: str, correction: str) -> str:
        """分析反馈情感"""
        # 基于评分
        if rating >= 4:
            return "positive"
        elif rating <= 2 and rating > 0:
            return "negative"
        
        # 基于关键词
        positive_words = ["好", "棒", "不错", "满意", "赞", "优秀", "完美"]
        negative_words = ["差", "糟", "烂", "失望", "垃圾", "不行", "错误", "不对"]
        
        text = (comment + correction).lower()
        
        pos_count = sum(1 for w in positive_words if w in text)
        neg_count = sum(1 for w in negative_words if w in text)
        
        if pos_count > neg_count:
            return "positive"
        elif neg_count > pos_count:
            return "negative"
        
        return "neutral"
    
    def _update_summary(self, session_id: str):
        """更新反馈汇总"""
        # 收集该会话的所有反馈
        session_feedbacks = [
            f for f in self.feedbacks.values() 
            if f.session_id == session_id
        ]
        
        if not session_feedbacks:
            return
        
        # 计算统计
        rated = [f for f in session_feedbacks if f.rating > 0]
        avg_rating = sum(f.rating for f in rated) / len(rated) if rated else 0
        
        # 标签统计
        tag_counts = {}
        for f in session_feedbacks:
            for tag in f.tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        
        # 收集纠正
        corrections = [f.correction for f in session_feedbacks if f.correction]
        
        # 生成改进建议
        suggestions = self._generate_improvement_suggestions(session_feedbacks)
        
        summary = FeedbackSummary(
            session_id=session_id,
            total_rounds=len(session_feedbacks),
            rated_rounds=len(rated),
            average_rating=round(avg_rating, 2),
            tag_counts=tag_counts,
            common_corrections=corrections[-5:],  # 最近5条
            improvement_suggestions=suggestions,
            updated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        
        self.summaries[session_id] = summary
    
    def _generate_improvement_suggestions(self, feedbacks: List[RoundFeedback]) -> str:
        """生成改进建议"""
        suggestions = []
        
        # 低评分分析
        low_ratings = [f for f in feedbacks if f.rating <= 2]
        if low_ratings:
            suggestions.append(f"最近 {len(low_ratings)} 轮评分较低，需要调整策略")
        
        # 纠正分析
        corrections = [f.correction for f in feedbacks if f.correction]
        if corrections:
            suggestions.append(f"用户提供了 {len(corrections)} 条纠正意见")
        
        # 负面标签
        negative_tags = ["太慢", "不对", "错误", "重复", "无效"]
        neg_count = sum(
            1 for f in feedbacks 
            for tag in f.tags 
            if tag in negative_tags
        )
        if neg_count > 0:
            suggestions.append(f"收到 {neg_count} 个负面标签，需要改进")
        
        return "; ".join(suggestions) if suggestions else "暂无改进建议"
    
    def get_feedback_for_round(self, session_id: str, round_number: int) -> Optional[RoundFeedback]:
        """获取指定轮次的反馈"""
        fid = f"fb_{session_id}_{round_number}"
        return self.feedbacks.get(fid)
    
    def get_session_feedbacks(self, session_id: str) -> List[RoundFeedback]:
        """获取会话的所有反馈"""
        return [
            f for f in self.feedbacks.values()
            if f.session_id == session_id
        ]
    
    def get_summary(self, session_id: str) -> Optional[FeedbackSummary]:
        """获取反馈汇总"""
        return self.summaries.get(session_id)
    
    def mark_applied(self, feedback_id: str, note: str = ""):
        """标记反馈已应用"""
        if feedback_id in self.feedbacks:
            self.feedbacks[feedback_id].applied = True
            self.feedbacks[feedback_id].application_note = note
            self._save()
    
    def build_context_for_brain(self, session_id: str, last_n_rounds: int = 3) -> Dict:
        """
        为 Brain 构建反馈上下文
        
        包含：
        - 最近N轮的反馈
        - 汇总统计
        - 改进建议
        - 用户的纠正意见
        """
        feedbacks = self.get_session_feedbacks(session_id)
        feedbacks.sort(key=lambda x: x.round_number, reverse=True)
        
        recent = feedbacks[:last_n_rounds]
        summary = self.get_summary(session_id)
        
        context = {
            "has_feedback": len(feedbacks) > 0,
            "recent_feedbacks": [f.to_dict() for f in recent],
            "summary": summary.to_dict() if summary else None,
            "latest_correction": "",
            "improvement_hints": [],
        }
        
        # 提取最新的纠正
        for f in recent:
            if f.correction:
                context["latest_correction"] = f.correction
                break
        
        # 提取改进提示
        if summary:
            context["improvement_hints"] = summary.improvement_suggestions.split("; ")
        
        return context
    
    def format_for_prompt(self, session_id: str, last_n_rounds: int = 3) -> str:
        """格式化为 Prompt 文本"""
        feedbacks = self.get_session_feedbacks(session_id)
        if not feedbacks:
            return ""
        
        feedbacks.sort(key=lambda x: x.round_number, reverse=True)
        recent = feedbacks[:last_n_rounds]
        summary = self.get_summary(session_id)
        
        parts = ["【用户反馈】"]
        
        # 汇总
        if summary:
            parts.append(f"平均评分: {summary.average_rating}/5 ({summary.rated_rounds}轮评价)")
        
        # 最新纠正（最重要）
        for f in recent:
            if f.correction:
                parts.append(f"\n[用户指导] {f.correction}")
                break
        
        # 最近反馈
        if recent:
            parts.append("\n最近评价:")
            for f in recent:
                if f.rating > 0:
                    stars = "★" * f.rating + "☆" * (5 - f.rating)
                    parts.append(f"  Round {f.round_number}: {stars}")
                if f.comment:
                    parts.append(f"    评论: {f.comment}")
                if f.tags:
                    parts.append(f"    标签: {', '.join(f.tags)}")
        
        # 改进建议
        if summary and summary.improvement_suggestions:
            parts.append(f"\n[改进方向] {summary.improvement_suggestions}")
        
        parts.append("\n注意：根据用户反馈调整你的决策。如果用户给出了纠正，优先遵循用户的指导。")
        
        return "\n".join(parts)
    
    def get_feedback_prompt_template(self) -> str:
        """获取反馈收集的提示模板"""
        return """
本轮执行完成。请对AI的表现进行评价：

1. 评分（1-5星）：
   ⭐ 1星 - 完全不对，浪费我时间
   ⭐⭐ 2星 - 有明显问题
   ⭐⭐⭐ 3星 - 一般般
   ⭐⭐⭐⭐ 4星 - 还不错
   ⭐⭐⭐⭐⭐ 5星 - 完美，超出预期

2. 标签（可多选）：
   - 太慢了
   - 方向错误
   - 重复劳动
   - 没有进展
   - 思路清晰
   - 执行到位
   - 有创意

3. 吐槽/建议：
   （直接告诉AI哪里做得不好，或者该怎么改进）

4. 纠正指导（可选）：
   （如果你知道正确的做法，直接告诉AI）
"""


# 便捷函数
def create_feedback_manager(data_dir: str = "data") -> FeedbackManager:
    """创建 FeedbackManager 实例的工厂函数"""
    return FeedbackManager(data_dir)
