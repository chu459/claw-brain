"""
Planner - 规划与任务管理系统
实现多轮目标分解、Phase规划、Todos任务列表、State状态跟踪
"""

import json
import time
import os
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict, field
from enum import Enum, auto


class TaskStatus(Enum):
    """任务状态"""
    PENDING = "pending"      # 待办
    IN_PROGRESS = "in_progress"  # 进行中
    COMPLETED = "completed"  # 已完成
    BLOCKED = "blocked"      # 被阻塞
    CANCELLED = "cancelled"  # 已取消


class PhaseStatus(Enum):
    """阶段状态"""
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    SKIPPED = "skipped"


@dataclass
class Todo:
    """单个任务"""
    id: str
    title: str
    description: str = ""
    status: str = "pending"
    priority: str = "medium"  # high, medium, low
    assignee: str = ""  # 分配给哪个agent
    parent_phase: str = ""  # 所属阶段
    created_at: str = ""
    completed_at: str = ""
    result: str = ""  # 执行结果
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> "Todo":
        return cls(**data)


@dataclass
class Phase:
    """规划阶段"""
    id: str
    name: str
    description: str = ""
    status: str = "pending"
    start_round: int = 0  # 开始轮次
    end_round: int = 0    # 结束轮次
    todos: List[str] = field(default_factory=list)  # 任务ID列表
    completed_todos: int = 0
    total_todos: int = 0
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> "Phase":
        return cls(**data)


@dataclass
class Plan:
    """完整规划"""
    id: str
    goal: str  # 终极目标
    description: str = ""
    created_at: str = ""
    current_phase: str = ""  # 当前阶段ID
    current_round: int = 0
    total_rounds: int = 0
    phases: List[str] = field(default_factory=list)  # 阶段ID列表
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> "Plan":
        return cls(**data)


@dataclass
class State:
    """系统状态"""
    current_plan: str = ""  # 当前规划ID
    current_phase: str = ""  # 当前阶段
    current_todo: str = ""   # 当前任务
    loop_count: int = 0
    last_action: str = ""
    last_result: str = ""
    summary: str = ""  # 当前状态摘要
    updated_at: str = ""
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> "State":
        return cls(**data)


class Planner:
    """
    规划管理器
    
    职责：
    1. 创建和管理 Plan（多轮规划）
    2. 分解 Phase（阶段）
    3. 管理 Todos（任务列表）
    4. 跟踪 State（状态）
    5. 与 Brain 协作，根据120谱生成规划
    """
    
    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)
        
        # 存储文件
        self.plans_file = self.data_dir / "plans.json"
        self.phases_file = self.data_dir / "phases.json"
        self.todos_file = self.data_dir / "todos.json"
        self.state_file = self.data_dir / "state.json"
        
        # 内存数据
        self.plans: Dict[str, Plan] = {}
        self.phases: Dict[str, Phase] = {}
        self.todos: Dict[str, Todo] = {}
        self.state: State = State()
        
        self._load()
    
    def _load(self):
        """加载数据"""
        # 加载 Plans
        if self.plans_file.exists():
            with open(self.plans_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.plans = {k: Plan.from_dict(v) for k, v in data.items()}
        
        # 加载 Phases
        if self.phases_file.exists():
            with open(self.phases_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.phases = {k: Phase.from_dict(v) for k, v in data.items()}
        
        # 加载 Todos
        if self.todos_file.exists():
            with open(self.todos_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.todos = {k: Todo.from_dict(v) for k, v in data.items()}
        
        # 加载 State
        if self.state_file.exists():
            with open(self.state_file, "r", encoding="utf-8") as f:
                self.state = State.from_dict(json.load(f))
    
    def _save(self):
        """保存数据"""
        # 保存 Plans
        with open(self.plans_file, "w", encoding="utf-8") as f:
            json.dump({k: v.to_dict() for k, v in self.plans.items()}, f, indent=2, ensure_ascii=False)
        
        # 保存 Phases
        with open(self.phases_file, "w", encoding="utf-8") as f:
            json.dump({k: v.to_dict() for k, v in self.phases.items()}, f, indent=2, ensure_ascii=False)
        
        # 保存 Todos
        with open(self.todos_file, "w", encoding="utf-8") as f:
            json.dump({k: v.to_dict() for k, v in self.todos.items()}, f, indent=2, ensure_ascii=False)
        
        # 保存 State
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(self.state.to_dict(), f, indent=2, ensure_ascii=False)
    
    # ===================== Plan 管理 =====================
    
    def create_plan(self, goal: str, description: str = "") -> Plan:
        """
        创建新规划
        
        Args:
            goal: 终极目标
            description: 描述
            
        Returns:
            Plan 对象
        """
        plan_id = f"plan_{int(time.time())}"
        plan = Plan(
            id=plan_id,
            goal=goal,
            description=description,
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            current_round=0,
        )
        self.plans[plan_id] = plan
        self.state.current_plan = plan_id
        self._save()
        return plan
    
    def get_current_plan(self) -> Optional[Plan]:
        """获取当前规划"""
        if self.state.current_plan and self.state.current_plan in self.plans:
            return self.plans[self.state.current_plan]
        return None
    
    def get_plan(self, plan_id: str) -> Optional[Plan]:
        """获取指定规划"""
        return self.plans.get(plan_id)
    
    # ===================== Phase 管理 =====================
    
    def create_phase(self, plan_id: str, name: str, description: str = "", 
                     start_round: int = 0, end_round: int = 0) -> Phase:
        """
        创建阶段
        
        Args:
            plan_id: 所属规划ID
            name: 阶段名称
            description: 描述
            start_round: 开始轮次
            end_round: 结束轮次
            
        Returns:
            Phase 对象
        """
        phase_id = f"phase_{plan_id}_{name}_{int(time.time())}"
        phase = Phase(
            id=phase_id,
            name=name,
            description=description,
            start_round=start_round,
            end_round=end_round,
        )
        self.phases[phase_id] = phase
        
        # 添加到规划
        if plan_id in self.plans:
            self.plans[plan_id].phases.append(phase_id)
            self.plans[plan_id].total_rounds = max(self.plans[plan_id].total_rounds, end_round)
        
        self._save()
        return phase
    
    def get_current_phase(self) -> Optional[Phase]:
        """获取当前阶段"""
        if self.state.current_phase and self.state.current_phase in self.phases:
            return self.phases[self.state.current_phase]
        return None
    
    def activate_phase(self, phase_id: str):
        """激活阶段"""
        if phase_id in self.phases:
            # 停用之前的阶段
            if self.state.current_phase and self.state.current_phase in self.phases:
                self.phases[self.state.current_phase].status = "completed"
            
            # 激活新阶段
            self.phases[phase_id].status = "active"
            self.state.current_phase = phase_id
            
            # 更新规划
            plan = self.get_current_plan()
            if plan:
                plan.current_phase = phase_id
            
            self._save()
    
    def complete_phase(self, phase_id: str):
        """完成阶段"""
        if phase_id in self.phases:
            self.phases[phase_id].status = "completed"
            self._save()
    
    # ===================== Todo 管理 =====================
    
    def create_todo(self, title: str, description: str = "", 
                    priority: str = "medium", assignee: str = "",
                    phase_id: str = "") -> Todo:
        """
        创建任务
        
        Args:
            title: 任务标题
            description: 描述
            priority: 优先级 (high/medium/low)
            assignee: 分配给哪个agent
            phase_id: 所属阶段
            
        Returns:
            Todo 对象
        """
        todo_id = f"todo_{int(time.time())}_{hash(title) % 10000}"
        todo = Todo(
            id=todo_id,
            title=title,
            description=description,
            priority=priority,
            assignee=assignee,
            parent_phase=phase_id,
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.todos[todo_id] = todo
        
        # 添加到阶段
        if phase_id and phase_id in self.phases:
            self.phases[phase_id].todos.append(todo_id)
            self.phases[phase_id].total_todos += 1
        
        self._save()
        return todo
    
    def get_todo(self, todo_id: str) -> Optional[Todo]:
        """获取任务"""
        return self.todos.get(todo_id)
    
    def get_current_todo(self) -> Optional[Todo]:
        """获取当前任务"""
        if self.state.current_todo and self.state.current_todo in self.todos:
            return self.todos[self.state.current_todo]
        return None
    
    def start_todo(self, todo_id: str):
        """开始任务"""
        if todo_id in self.todos:
            self.todos[todo_id].status = "in_progress"
            self.state.current_todo = todo_id
            self._save()
    
    def complete_todo(self, todo_id: str, result: str = ""):
        """完成任务"""
        if todo_id in self.todos:
            todo = self.todos[todo_id]
            todo.status = "completed"
            todo.completed_at = time.strftime("%Y-%m-%d %H:%M:%S")
            todo.result = result
            
            # 更新阶段统计
            if todo.parent_phase and todo.parent_phase in self.phases:
                self.phases[todo.parent_phase].completed_todos += 1
            
            self.state.current_todo = ""
            self._save()
    
    def get_pending_todos(self, phase_id: str = "") -> List[Todo]:
        """获取待办任务"""
        todos = []
        for todo in self.todos.values():
            if todo.status == "pending":
                if not phase_id or todo.parent_phase == phase_id:
                    todos.append(todo)
        return sorted(todos, key=lambda x: {"high": 0, "medium": 1, "low": 2}.get(x.priority, 1))
    
    def get_phase_todos(self, phase_id: str) -> List[Todo]:
        """获取阶段的所有任务"""
        if phase_id not in self.phases:
            return []
        return [self.todos[tid] for tid in self.phases[phase_id].todos if tid in self.todos]
    
    # ===================== State 管理 =====================
    
    def update_state(self, **kwargs):
        """更新状态"""
        for key, value in kwargs.items():
            if hasattr(self.state, key):
                setattr(self.state, key, value)
        self.state.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save()
    
    def get_state(self) -> State:
        """获取当前状态"""
        return self.state
    
    def update_loop_count(self, count: int):
        """更新轮次"""
        self.state.loop_count = count
        plan = self.get_current_plan()
        if plan:
            plan.current_round = count
        self._save()
    
    # ===================== 智能规划生成（AI 驱动） =====================
    
    @staticmethod
    def _repair_json(text: str) -> str:
        import re
        repaired = text
        repaired = re.sub(r',\s*([}\]])', r'\1', repaired)
        repaired = re.sub(r'([{,])\s*,\s*', r'\1 ', repaired)
        repaired = re.sub(r'\[\s*,', '[', repaired)
        repaired = repaired.replace('\t', ' ').replace('\r\n', '\n').replace('\r', '\n')
        def unescape_strings(s):
            result = []
            i = 0
            in_string = False
            escape = False
            while i < len(s):
                c = s[i]
                if escape:
                    result.append(c)
                    escape = False
                    i += 1
                    continue
                if c == '\\':
                    result.append(c)
                    escape = True
                    i += 1
                    continue
                if c == '"':
                    in_string = not in_string
                    result.append(c)
                    i += 1
                    continue
                if in_string:
                    if c == '\n':
                        result.append('\\n')
                    else:
                        result.append(c)
                else:
                    result.append(c)
                i += 1
            return ''.join(result)
        repaired = unescape_strings(repaired)
        return repaired
    
    @staticmethod
    def _parse_json_robust(content: str):
        import re, json
        candidates = []
        direct = content.strip()
        if direct.startswith('{'):
            candidates.append(("原始文本", direct))
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', content, re.DOTALL)
        if m:
            candidates.append(("markdown提取", m.group(1)))
        start = content.find('{')
        end = content.rfind('}')
        if start != -1 and end != -1 and end > start:
            candidates.append(("括号截取", content[start:end+1]))
        last_error = None
        for source, candidate in candidates:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
            try:
                repaired = Planner._repair_json(candidate)
                result = json.loads(repaired)
                print(f"[Planner] JSON 修复成功 (来源: {source})")
                return result
            except json.JSONDecodeError as e:
                last_error = e
        if candidates:
            repaired = Planner._repair_json(candidates[0][1])
            return json.loads(repaired)
        raise last_error or ValueError("无法从响应中提取有效 JSON")
    
    def generate_plan_with_brain(self, goal: str, brain_client, model: str, max_rounds: int = 12) -> Plan:
        """
        使用 Brain AI 生成定制化规划
        
        Args:
            goal: 终极目标
            brain_client: OpenAI 客户端实例
            model: 模型名称
            max_rounds: 最大轮次
            
        Returns:
            Plan 对象
        """
        import json
        
        prompt = f"""你是一个战略规划专家。请为以下目标制定一个详细的执行规划。

目标: {goal}

请输出 JSON 格式的规划，包含:
1. 分析目标的核心难点和关键成功因素
2. 制定 3-5 个执行阶段，每个阶段包含:
   - 阶段名称（简洁，如"市场调研"、"产品开发"）
   - 阶段描述（具体要做什么）
   - 阶段包含的 2-4 个具体任务，每个任务包含:
     * 任务标题
     * 任务描述
     * 优先级 (high/medium/low)
     * 执行者 (brain/research-agent/content-agent/dev-agent/bd-agent)

输出格式:
{{
  "analysis": "目标分析...",
  "phases": [
    {{
      "name": "阶段名称",
      "description": "阶段描述",
      "start_round": 1,
      "end_round": 3,
      "todos": [
        {{"title": "任务1", "description": "...", "priority": "high", "assignee": "research-agent"}}
      ]
    }}
  ]
}}

确保规划具体、可执行，与目标强相关。不要套用通用模板。"""

        try:
            print(f"[Planner] 开始调用 Brain API，模型: {model}")
            
            # 修改提示词，明确要求只返回 JSON
            json_prompt = prompt + """

重要：只返回纯 JSON，不要用 markdown 代码块包裹，不要加任何解释文字。
JSON 格式要求：
- 字符串中的双引号必须转义为 \\"
- 字符串中的换行必须转义为 \\n
- 数组/对象的最后一个元素后面不能有逗号
- 所有键必须用双引号包裹"""
            
            response = brain_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "你是一个专业的战略规划专家，擅长将目标拆解为可执行的阶段和任务。你必须只输出有效的 JSON 格式。"},
                    {"role": "user", "content": json_prompt},
                ],
                temperature=0.7,
                timeout=30,
            )
            
            print(f"[Planner] Brain API 响应成功")
            content = response.choices[0].message.content.strip()
            print(f"[Planner] 响应内容长度: {len(content)}")
            print(f"[Planner] 响应预览: {content[:200]}...")
            
            # 尝试提取 JSON
            import re
            plan_data = self._parse_json_robust(content)
            
            print(f"[Planner] JSON 解析成功，阶段数: {len(plan_data.get('phases', []))}")
            
            # 创建规划
            plan = self.create_plan(goal, plan_data.get("analysis", "AI 生成规划"))
            
            # 创建阶段和任务
            for phase_data in plan_data.get("phases", []):
                phase = self.create_phase(
                    plan.id,
                    phase_data["name"],
                    phase_data.get("description", ""),
                    phase_data.get("start_round", 1),
                    phase_data.get("end_round", max_rounds)
                )
                
                # 创建任务
                for todo_data in phase_data.get("todos", []):
                    self.create_todo(
                        title=todo_data["title"],
                        description=todo_data.get("description", ""),
                        priority=todo_data.get("priority", "medium"),
                        assignee=todo_data.get("assignee", "brain"),
                        phase_id=phase.id
                    )
            
            # 激活第一个阶段
            if plan.phases:
                self.activate_phase(plan.phases[0])
            
            self._save()
            print(f"[Planner] AI 生成规划完成: {len(plan_data.get('phases', []))} 个阶段")
            return plan
            
        except Exception as e:
            import traceback
            print(f"[Planner] AI 规划失败: {e}")
            print(f"[Planner] 错误详情: {traceback.format_exc()}")
            # 强制 AI 规划，失败直接抛异常，不回退到模板
            raise RuntimeError(f"AI 规划失败: {e}") from e
    
    # ===================== 摘要和报告 =====================
    
    def get_plan_summary(self, plan_id: str = "") -> str:
        """获取规划摘要"""
        plan = self.get_plan(plan_id) if plan_id else self.get_current_plan()
        if not plan:
            return "(无活跃规划)"
        
        lines = [f"规划: {plan.goal}"]
        lines.append(f"轮次: {plan.current_round}/{plan.total_rounds}")
        
        for phase_id in plan.phases:
            if phase_id in self.phases:
                phase = self.phases[phase_id]
                status_icon = "●" if phase.status == "active" else "○" if phase.status == "pending" else "✓"
                progress = f"{phase.completed_todos}/{phase.total_todos}" if phase.total_todos > 0 else ""
                lines.append(f"  {status_icon} {phase.name} {progress}")
        
        return "\n".join(lines)
    
    def get_current_todos_summary(self) -> str:
        """获取当前任务摘要"""
        phase = self.get_current_phase()
        if not phase:
            return "(无活跃阶段)"
        
        todos = self.get_phase_todos(phase.id)
        if not todos:
            return f"阶段 {phase.name}: 无任务"
        
        lines = [f"阶段: {phase.name} ({phase.completed_todos}/{phase.total_todos})"]
        
        for todo in todos:
            status_icon = {
                "pending": "○",
                "in_progress": "●",
                "completed": "✓",
                "blocked": "✗",
            }.get(todo.status, "?")
            priority_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(todo.priority, "")
            lines.append(f"  {status_icon} {priority_icon} {todo.title}")
        
        return "\n".join(lines)
    
    def get_next_todo(self) -> Optional[Todo]:
        """获取下一个待办任务"""
        phase = self.get_current_phase()
        if not phase:
            return None
        
        todos = self.get_pending_todos(phase.id)
        return todos[0] if todos else None
    
    def advance_to_next_todo(self) -> Optional[Todo]:
        """推进到下一个任务"""
        # 完成当前任务
        current = self.get_current_todo()
        if current:
            self.complete_todo(current.id)
        
        # 获取下一个任务
        next_todo = self.get_next_todo()
        if next_todo:
            self.start_todo(next_todo.id)
            return next_todo
        
        # 当前阶段无任务，尝试推进到下一阶段
        return self._advance_to_next_phase()
    
    def _advance_to_next_phase(self) -> Optional[Todo]:
        """推进到下一阶段"""
        plan = self.get_current_plan()
        if not plan:
            return None
        
        # 找到当前阶段的索引
        if not plan.current_phase or plan.current_phase not in self.phases:
            return None
        
        current_idx = plan.phases.index(plan.current_phase)
        if current_idx + 1 >= len(plan.phases):
            return None  # 已经是最后一个阶段
        
        # 激活下一阶段
        next_phase_id = plan.phases[current_idx + 1]
        self.activate_phase(next_phase_id)
        
        # 获取新阶段的第一个任务
        next_todo = self.get_next_todo()
        if next_todo:
            self.start_todo(next_todo.id)
        
        return next_todo


# 便捷函数
def create_planner(data_dir: str = "data") -> Planner:
    """创建 Planner 实例的工厂函数"""
    return Planner(data_dir)
