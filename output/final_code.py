from dataclasses import dataclass, field
from typing import List, Optional

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
VALID_PRIORITIES = set(PRIORITY_ORDER.keys())


@dataclass
class Task:
    id: int
    title: str
    priority: str = "medium"
    done: bool = False


class TodoManager:
    def __init__(self) -> None:
        self._tasks: List[Task] = []
        self._next_id: int = 1

    def add(self, title: str, priority: str = "medium") -> Optional[Task]:
        if not isinstance(title, str) or not title.strip():
            print("错误：任务标题不能为空。")
            return None
        if not isinstance(priority, str) or priority.lower() not in VALID_PRIORITIES:
            print(f"错误：非法优先级 '{priority}'，请使用 high / medium / low。")
            return None
        task = Task(id=self._next_id, title=title.strip(), priority=priority.lower())
        self._tasks.append(task)
        self._next_id += 1
        print(f"已添加任务 #{task.id}: {task.title} [{task.priority}]")
        return task

    def complete(self, task_id: int) -> bool:
        if not isinstance(task_id, int):
            print(f"错误：任务编号必须是整数，收到 '{task_id}'。")
            return False
        task = self._find(task_id)
        if task is None:
            print(f"错误：找不到编号为 {task_id} 的任务。")
            return False
        if task.done:
            print(f"提示：任务 #{task_id} 已经是完成状态。")
            return False
        task.done = True
        print(f"已完成任务 #{task.id}: {task.title}")
        return True

    def _find(self, task_id: int) -> Optional[Task]:
        for t in self._tasks:
            if t.id == task_id:
                return t
        return None

    def list_tasks(self) -> List[Task]:
        ordered = sorted(
            self._tasks,
            key=lambda t: (t.done, PRIORITY_ORDER.get(t.priority, 99), t.id),
        )
        if not ordered:
            print("（暂无任务）")
            return ordered
        print("任务列表：")
        for t in ordered:
            status = "[x]" if t.done else "[ ]"
            print(f"  {status} #{t.id} ({t.priority}) {t.title}")
        return ordered


if __name__ == "__main__":
    todo = TodoManager()

    print("=== 添加任务 ===")
    todo.add("写周报", "high")
    todo.add("买牛奶")
    todo.add("整理书架", "low")
    todo.add("   ")            # 空标题
    todo.add("非法任务", "urgent")  # 非法优先级

    print("\n=== 完成任务 ===")
    todo.complete(2)
    todo.complete(2)      # 重复完成
    todo.complete(999)    # 不存在的编号
    todo.complete("abc")  # 非法类型

    print("\n=== 列出任务 ===")
    todo.list_tasks()
