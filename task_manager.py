"""
claw-brain 任务管理模块
======================
管理任务创建、查询、状态更新
"""

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any


class TaskManager:
    """任务管理器"""

    TASKS_FILE = "tasks.json"

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or Path(__file__).parent
        self.tasks_file = self.base_dir / self.TASKS_FILE
        self._ensure_file()

    def _ensure_file(self):
        """确保任务文件存在"""
        if not self.tasks_file.exists():
            self._save_tasks([])

    def _load_tasks(self) -> List[Dict[str, Any]]:
        """加载任务列表"""
        try:
            with open(self.tasks_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[TaskManager] 加载任务失败: {e}")
            return []

    def _save_tasks(self, tasks: List[Dict[str, Any]]):
        """保存任务列表"""
        try:
            with open(self.tasks_file, "w", encoding="utf-8") as f:
                json.dump(tasks, ensure_ascii=False, indent=2, fp=f)
        except Exception as e:
            print(f"[TaskManager] 保存任务失败: {e}")

    def create_task(
        self,
        name: str,
        goal: str,
        description: Optional[str] = None,
        mode: Optional[str] = "money"
    ) -> Dict[str, Any]:
        """创建新任务"""
        task_id = f"task_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
        task = {
            "id": task_id,
            "name": name,
            "description": description or name,
            "goal": goal,
            "mode": mode,
            "status": "pending",  # pending, running, completed, failed
            "created_at": datetime.now().isoformat(),
            "started_at": None,
            "completed_at": None,
            "result": None,
            "error": None,
            "outputs": [],
            "rounds": 0,
        }

        tasks = self._load_tasks()
        tasks.insert(0, task)  # 新任务插到前面
        self._save_tasks(tasks)

        return task

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务详情"""
        tasks = self._load_tasks()
        for task in tasks:
            if task["id"] == task_id:
                return task
        return None

    def list_tasks(
        self,
        status: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """获取任务列表"""
        tasks = self._load_tasks()
        if status:
            tasks = [t for t in tasks if t["status"] == status]
        return tasks[:limit]

    def update_task(
        self,
        task_id: str,
        **updates
    ) -> Optional[Dict[str, Any]]:
        """更新任务"""
        tasks = self._load_tasks()
        for task in tasks:
            if task["id"] == task_id:
                # 允许更新的字段
                allowed = ["status", "started_at", "completed_at", "result", "error", "outputs", "rounds"]
                for key, value in updates.items():
                    if key in allowed:
                        task[key] = value
                self._save_tasks(tasks)
                return task
        return None

    def start_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """标记任务为运行中"""
        return self.update_task(
            task_id,
            status="running",
            started_at=datetime.now().isoformat()
        )

    def complete_task(
        self,
        task_id: str,
        result: Optional[str] = None,
        outputs: Optional[List[str]] = None
    ) -> Optional[Dict[str, Any]]:
        """标记任务为已完成"""
        return self.update_task(
            task_id,
            status="completed",
            completed_at=datetime.now().isoformat(),
            result=result,
            outputs=outputs or []
        )

    def fail_task(
        self,
        task_id: str,
        error: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """标记任务为失败"""
        return self.update_task(
            task_id,
            status="failed",
            completed_at=datetime.now().isoformat(),
            error=error
        )

    def delete_task(self, task_id: str) -> bool:
        """删除任务"""
        tasks = self._load_tasks()
        original_count = len(tasks)
        tasks = [t for t in tasks if t["id"] != task_id]
        if len(tasks) < original_count:
            self._save_tasks(tasks)
            return True
        return False

    def get_stats(self) -> Dict[str, int]:
        """获取任务统计"""
        tasks = self._load_tasks()
        stats = {
            "total": len(tasks),
            "pending": 0,
            "running": 0,
            "completed": 0,
            "failed": 0,
        }
        for task in tasks:
            status = task.get("status", "pending")
            if status in stats:
                stats[status] += 1
        return stats


# 全局实例
_task_manager: Optional[TaskManager] = None


def get_task_manager() -> TaskManager:
    """获取全局任务管理器实例"""
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskManager()
    return _task_manager
