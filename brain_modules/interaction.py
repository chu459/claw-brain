"""
Interaction - 交互式提问与公告系统
实现：
1. 选择题/填空题提问机制
2. 会话级公告/指令系统
3. 与 Brain 决策流程集成
"""

import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, asdict, field
from enum import Enum


class QuestionType(Enum):
    """问题类型"""
    CHOICE = "choice"      # 选择题
    FILL_BLANK = "fill"    # 填空题
    CONFIRM = "confirm"    # 确认/取消
    MULTI_CHOICE = "multi" # 多选


class AnnouncementScope(Enum):
    """公告作用域"""
    SESSION = "session"    # 当前会话
    GLOBAL = "global"      # 全局
    PHASE = "phase"        # 当前阶段
    TASK = "task"          # 当前任务


@dataclass
class Question:
    """交互式问题"""
    id: str
    type: str  # choice/fill/confirm/multi
    title: str
    description: str = ""
    options: List[Dict] = field(default_factory=list)  # 选项列表 [{"value": "A", "label": "选项A"}]
    default_value: str = ""
    required: bool = True
    timeout: int = 0  # 超时时间(秒), 0表示不超时
    created_at: str = ""
    answered: bool = False
    answer: str = ""
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> "Question":
        return cls(**data)


@dataclass
class Announcement:
    """公告/指令"""
    id: str
    content: str
    scope: str  # session/global/phase/task
    priority: str = "normal"  # high/normal/low
    active: bool = True
    created_at: str = ""
    expires_at: str = ""  # 过期时间
    metadata: Dict = field(default_factory=dict)  # 额外数据
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> "Announcement":
        return cls(**data)


@dataclass
class DiscussionContext:
    """讨论上下文（全局）"""
    id: str
    session_id: str
    topic: str = ""  # 讨论主题
    messages: List[Dict] = field(default_factory=list)  # 对话历史 [{"role": "user/system", "content": "...", "timestamp": "..."}]
    locked_suggestion: str = ""  # 锁定的建议
    locked_action: str = ""  # 锁定的行动指令
    status: str = "active"  # active/locked/closed
    created_at: str = ""
    updated_at: str = ""
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> "DiscussionContext":
        return cls(**data)


@dataclass
class SessionContext:
    """会话上下文"""
    session_id: str
    announcements: List[str] = field(default_factory=list)  # 公告 ID 列表
    questions: List[str] = field(default_factory=list)  # 问题 ID 列表
    answers: Dict[str, str] = field(default_factory=dict)  # 问题答案
    variables: Dict[str, Any] = field(default_factory=dict)  # 会话变量
    discussion_id: str = ""  # 当前讨论 ID
    created_at: str = ""
    updated_at: str = ""
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> "SessionContext":
        return cls(**data)


class InteractionManager:
    """
    交互管理器
    
    职责：
    1. 管理交互式问题（选择/填空/确认）
    2. 管理会话级公告/指令
    3. 与 Brain 集成，在决策时插入提问
    4. 等待用户回答，阻塞执行流程
    """
    
    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)
        
        # 存储文件
        self.questions_file = self.data_dir / "questions.json"
        self.announcements_file = self.data_dir / "announcements.json"
        self.sessions_file = self.data_dir / "sessions.json"
        self.discussions_file = self.data_dir / "discussions.json"
        
        # 内存数据
        self.questions: Dict[str, Question] = {}
        self.announcements: Dict[str, Announcement] = {}
        self.sessions: Dict[str, SessionContext] = {}
        self.discussions: Dict[str, DiscussionContext] = {}
        
        # 回调函数（用于通知前端）
        self.on_question_created: Optional[Callable[[Question], None]] = None
        self.on_announcement_created: Optional[Callable[[Announcement], None]] = None
        self.on_discussion_updated: Optional[Callable[[DiscussionContext], None]] = None
        
        self._load()
    
    def _load(self):
        """加载数据"""
        if self.questions_file.exists():
            with open(self.questions_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.questions = {k: Question.from_dict(v) for k, v in data.items()}
        
        if self.announcements_file.exists():
            with open(self.announcements_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.announcements = {k: Announcement.from_dict(v) for k, v in data.items()}
        
        if self.sessions_file.exists():
            with open(self.sessions_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.sessions = {k: SessionContext.from_dict(v) for k, v in data.items()}
        
        if self.discussions_file.exists():
            with open(self.discussions_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.discussions = {k: DiscussionContext.from_dict(v) for k, v in data.items()}
    
    def _save(self):
        """保存数据"""
        with open(self.questions_file, "w", encoding="utf-8") as f:
            json.dump({k: v.to_dict() for k, v in self.questions.items()}, f, indent=2, ensure_ascii=False)
        
        with open(self.announcements_file, "w", encoding="utf-8") as f:
            json.dump({k: v.to_dict() for k, v in self.announcements.items()}, f, indent=2, ensure_ascii=False)
        
        with open(self.sessions_file, "w", encoding="utf-8") as f:
            json.dump({k: v.to_dict() for k, v in self.sessions.items()}, f, indent=2, ensure_ascii=False)
        
        with open(self.discussions_file, "w", encoding="utf-8") as f:
            json.dump({k: v.to_dict() for k, v in self.discussions.items()}, f, indent=2, ensure_ascii=False)
    
    # ===================== 会话管理 =====================
    
    def get_or_create_session(self, session_id: str) -> SessionContext:
        """获取或创建会话"""
        if session_id not in self.sessions:
            self.sessions[session_id] = SessionContext(
                session_id=session_id,
                created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                updated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            self._save()
        return self.sessions[session_id]
    
    def set_session_variable(self, session_id: str, key: str, value: Any):
        """设置会话变量"""
        session = self.get_or_create_session(session_id)
        session.variables[key] = value
        session.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save()
    
    def get_session_variable(self, session_id: str, key: str, default: Any = None) -> Any:
        """获取会话变量"""
        session = self.get_or_create_session(session_id)
        return session.variables.get(key, default)
    
    # ===================== 问题管理 =====================
    
    def create_choice_question(self, session_id: str, title: str, options: List[Dict],
                              description: str = "", required: bool = True) -> Question:
        """
        创建选择题
        
        Args:
            session_id: 会话ID
            title: 问题标题
            options: 选项列表 [{"value": "A", "label": "选项A", "description": "描述"}]
            description: 问题描述
            required: 是否必填
            
        Returns:
            Question 对象
        """
        qid = f"q_{session_id}_{int(time.time())}"
        question = Question(
            id=qid,
            type="choice",
            title=title,
            description=description,
            options=options,
            required=required,
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.questions[qid] = question
        
        # 关联到会话
        session = self.get_or_create_session(session_id)
        session.questions.append(qid)
        session.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save()
        
        # 触发回调
        if self.on_question_created:
            self.on_question_created(question)
        
        return question
    
    def create_fill_question(self, session_id: str, title: str, 
                            description: str = "", default_value: str = "",
                            required: bool = True) -> Question:
        """
        创建填空题
        
        Args:
            session_id: 会话ID
            title: 问题标题
            description: 问题描述
            default_value: 默认值
            required: 是否必填
            
        Returns:
            Question 对象
        """
        qid = f"q_{session_id}_{int(time.time())}"
        question = Question(
            id=qid,
            type="fill",
            title=title,
            description=description,
            default_value=default_value,
            required=required,
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.questions[qid] = question
        
        session = self.get_or_create_session(session_id)
        session.questions.append(qid)
        session.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save()
        
        if self.on_question_created:
            self.on_question_created(question)
        
        return question
    
    def create_confirm_question(self, session_id: str, title: str,
                               description: str = "") -> Question:
        """
        创建确认题（是/否）
        
        Args:
            session_id: 会话ID
            title: 问题标题
            description: 问题描述
            
        Returns:
            Question 对象
        """
        qid = f"q_{session_id}_{int(time.time())}"
        question = Question(
            id=qid,
            type="confirm",
            title=title,
            description=description,
            options=[
                {"value": "yes", "label": "确认"},
                {"value": "no", "label": "取消"},
            ],
            required=True,
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.questions[qid] = question
        
        session = self.get_or_create_session(session_id)
        session.questions.append(qid)
        session.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save()
        
        if self.on_question_created:
            self.on_question_created(question)
        
        return question
    
    def answer_question(self, question_id: str, answer: str) -> bool:
        """
        回答问题
        
        Args:
            question_id: 问题ID
            answer: 答案
            
        Returns:
            是否成功
        """
        if question_id not in self.questions:
            return False
        
        question = self.questions[question_id]
        question.answer = answer
        question.answered = True
        
        # 更新会话答案
        for session_id, session in self.sessions.items():
            if question_id in session.questions:
                session.answers[question_id] = answer
                session.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
                break
        
        self._save()
        return True
    
    def get_pending_question(self, session_id: str) -> Optional[Question]:
        """获取会话中待回答的问题"""
        session = self.get_or_create_session(session_id)
        for qid in reversed(session.questions):  # 最新的优先
            if qid in self.questions and not self.questions[qid].answered:
                return self.questions[qid]
        return None
    
    def get_question(self, question_id: str) -> Optional[Question]:
        """获取问题"""
        return self.questions.get(question_id)
    
    def get_session_answers(self, session_id: str) -> Dict[str, str]:
        """获取会话的所有答案"""
        session = self.get_or_create_session(session_id)
        return session.answers.copy()
    
    # ===================== 公告管理 =====================
    
    def create_announcement(self, content: str, scope: str = "session",
                           priority: str = "normal", session_id: str = "",
                           expires_in: int = 0) -> Announcement:
        """
        创建公告/指令
        
        Args:
            content: 公告内容
            scope: 作用域 (session/global/phase/task)
            priority: 优先级 (high/normal/low)
            session_id: 会话ID（scope=session时必填）
            expires_in: 过期时间（秒），0表示不过期
            
        Returns:
            Announcement 对象
        """
        aid = f"ann_{scope}_{int(time.time())}"
        
        expires_at = ""
        if expires_in > 0:
            expires_at = time.strftime("%Y-%m-%d %H:%M:%S", 
                                       time.localtime(time.time() + expires_in))
        
        announcement = Announcement(
            id=aid,
            content=content,
            scope=scope,
            priority=priority,
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            expires_at=expires_at,
            metadata={"session_id": session_id} if session_id else {},
        )
        self.announcements[aid] = announcement
        
        # 关联到会话
        if scope == "session" and session_id:
            session = self.get_or_create_session(session_id)
            session.announcements.append(aid)
            session.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        
        self._save()
        
        if self.on_announcement_created:
            self.on_announcement_created(announcement)
        
        return announcement
    
    def get_active_announcements(self, session_id: str = "", scope: str = "") -> List[Announcement]:
        """
        获取活跃公告
        
        Args:
            session_id: 会话ID
            scope: 作用域过滤
            
        Returns:
            公告列表
        """
        results = []
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        
        for ann in self.announcements.values():
            if not ann.active:
                continue
            if ann.expires_at and ann.expires_at < now:
                continue
            if scope and ann.scope != scope:
                continue
            if session_id and ann.scope == "session":
                if ann.metadata.get("session_id") != session_id:
                    continue
            results.append(ann)
        
        # 按优先级排序
        priority_order = {"high": 0, "normal": 1, "low": 2}
        results.sort(key=lambda x: priority_order.get(x.priority, 1))
        
        return results
    
    def dismiss_announcement(self, announcement_id: str):
        """关闭公告"""
        if announcement_id in self.announcements:
            self.announcements[announcement_id].active = False
            self._save()
    
    def clear_session_announcements(self, session_id: str):
        """清除会话的所有公告"""
        session = self.get_or_create_session(session_id)
        for aid in session.announcements:
            if aid in self.announcements:
                self.announcements[aid].active = False
        session.announcements = []
        self._save()
    
    # ===================== Brain 集成 =====================
    
    def build_context_for_brain(self, session_id: str) -> Dict:
        """为 Brain 构建上下文：只给最相关的信息"""
        context = {"has_pending_question": False, "pending_question": None, "announcements": [], "session_variables": {}}
        pending = self.get_pending_question(session_id)
        if pending:
            context["has_pending_question"] = True
            context["pending_question"] = {"title": pending.title, "type": pending.type, "id": pending.id, "answer": pending.answer}
        announcements = self.get_active_announcements(session_id)
        context["announcements"] = [{"content": ann.content, "priority": ann.priority} for ann in announcements[:3]]
        session = self.get_or_create_session(session_id)
        context["session_variables"] = session.variables
        return context
    
    def should_block_execution(self, session_id: str) -> bool:
        """检查是否阻塞执行 — 只有 required=True 且非反馈类问题才阻塞"""
        pending = self.get_pending_question(session_id)
        if pending is None:
            return False
        if not pending.required:
            return False
        if "评价" in pending.title or "反馈" in pending.title or "请评价" in pending.title:
            return False
        return True
    
    def format_for_prompt(self, session_id: str) -> str:
        """格式化为简短的 Prompt 文本"""
        announcements = self.get_active_announcements(session_id)
        if announcements:
            parts = ["【系统指令】"]
            for ann in announcements[:2]:
                parts.append(f"- {ann.content[:100]}")
            return "\n".join(parts)
        return ""
    
    # ===================== 讨论管理 =====================
    
    def start_discussion(self, session_id: str, topic: str = "") -> DiscussionContext:
        """发起讨论"""
        disc_id = f"disc_{session_id}_{int(time.time())}"
        discussion = DiscussionContext(
            id=disc_id,
            session_id=session_id,
            topic=topic,
            messages=[],
            status="active",
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            updated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.discussions[disc_id] = discussion
        
        # 关联到会话
        session = self.get_or_create_session(session_id)
        session.discussion_id = disc_id
        session.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save()
        
        if self.on_discussion_updated:
            self.on_discussion_updated(discussion)
        
        return discussion
    
    def add_discussion_message(self, disc_id: str, role: str, content: str) -> Optional[DiscussionContext]:
        """添加讨论消息"""
        if disc_id not in self.discussions:
            return None
        
        discussion = self.discussions[disc_id]
        discussion.messages.append({
            "role": role,
            "content": content,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        discussion.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save()
        
        if self.on_discussion_updated:
            self.on_discussion_updated(discussion)
        
        return discussion
    
    def lock_discussion(self, disc_id: str, suggestion: str, action: str) -> Optional[DiscussionContext]:
        """锁定讨论（明确建议和行动）"""
        if disc_id not in self.discussions:
            return None
        
        discussion = self.discussions[disc_id]
        discussion.locked_suggestion = suggestion
        discussion.locked_action = action
        discussion.status = "locked"
        discussion.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save()
        
        # 同时创建公告注入到上下文
        if suggestion:
            self.create_announcement(
                content=f"[讨论锁定] {suggestion}",
                scope="session",
                priority="high",
                session_id=discussion.session_id,
                metadata={"discussion_id": disc_id, "action": action},
            )
        
        if self.on_discussion_updated:
            self.on_discussion_updated(discussion)
        
        return discussion
    
    def close_discussion(self, disc_id: str) -> Optional[DiscussionContext]:
        """关闭讨论"""
        if disc_id not in self.discussions:
            return None
        
        discussion = self.discussions[disc_id]
        discussion.status = "closed"
        discussion.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save()
        
        # 清除会话关联
        session = self.get_or_create_session(discussion.session_id)
        if session.discussion_id == disc_id:
            session.discussion_id = ""
        self._save()
        
        if self.on_discussion_updated:
            self.on_discussion_updated(discussion)
        
        return discussion
    
    def get_discussion(self, disc_id: str) -> Optional[DiscussionContext]:
        """获取讨论"""
        return self.discussions.get(disc_id)
    
    def get_session_discussion(self, session_id: str) -> Optional[DiscussionContext]:
        """获取会话的讨论"""
        session = self.get_or_create_session(session_id)
        if session.discussion_id and session.discussion_id in self.discussions:
            return self.discussions[session.discussion_id]
        return None
    
    def get_discussion_context_for_brain(self, session_id: str) -> Dict:
        """为 Brain 提供讨论上下文"""
        discussion = self.get_session_discussion(session_id)
        if not discussion:
            return {"has_discussion": False}
        
        return {
            "has_discussion": True,
            "topic": discussion.topic,
            "messages": discussion.messages[-10:],  # 最近 10 条
            "locked_suggestion": discussion.locked_suggestion,
            "locked_action": discussion.locked_action,
            "status": discussion.status,
        }


# 便捷函数
def create_interaction_manager(data_dir: str = "data") -> InteractionManager:
    """创建 InteractionManager 实例的工厂函数"""
    return InteractionManager(data_dir)
