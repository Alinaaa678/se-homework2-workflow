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
        title = (title or "").strip()
        if not title:
            print("[错误] 任务标题不能为空。")
            return None

        priority = (priority or "").strip().lower()
        if priority not in VALID_PRIORITIES:
            print(f"[错误] 非法优先级 '{priority}'，可选值：high / medium / low。")
            return None

        task = Task(id=self._next_id, title=title, priority=priority)
        self._tasks.append(task)
        self._next_id += 1
        print(f"[成功] 已添加任务 #{task.id}：{task.title}（优先级：{task.priority}）")
        return task

    def complete(self, task_id: int) -> bool:
        try:
            task_id = int(task_id)
        except (TypeError, ValueError):
            print(f"[错误] 非法编号 '{task_id}'，请输入整数编号。")
            return False

        for task in self._tasks:
            if task.id == task_id:
                if task.done:
                    print(f"[提示] 任务 #{task_id} 已经是完成状态。")
                    return False
                task.done = True
                print(f"[成功] 已完成任务 #{task.id}：{task.title}")
                return True

        print(f"[错误] 找不到编号为 {task_id} 的任务。")
        return False

    def list_tasks(self) -> List[Task]:
        sorted_tasks = sorted(
            self._tasks,
            key=lambda t: (t.done, PRIORITY_ORDER.get(t.priority, 99), t.id),
        )
        if not sorted_tasks:
            print("[提示] 当前没有任务。")
            return sorted_tasks

        print("---- 任务列表 ----")
        for task in sorted_tasks:
            status = "✔" if task.done else " "
            print(f"[{status}] #{task.id} ({task.priority}) {task.title}")
        print("------------------")
        return sorted_tasks


if __name__ == "__main__":
    todo = TodoManager()

    # 正常添加
    todo.add("写周报", "high")
    todo.add("买牛奶")  # 默认 medium
    todo.add("整理桌面", "low")
    todo.add("准备会议材料", "high")

    # 非法输入演示
    todo.add("", "high")          # 空标题
    todo.add("   ", "low")        # 空白标题
    todo.add("测试任务", "urgent")  # 非法优先级

    # 完成任务
    todo.complete(2)
    todo.complete(2)      # 重复完成
    todo.complete(999)    # 不存在的编号
    todo.complete("abc")  # 非整数编号

    # 列出任务
    todo.list_tasks()
