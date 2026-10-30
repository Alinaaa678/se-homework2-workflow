import unittest
from candidate import TodoManager


class HiddenTodoTests(unittest.TestCase):
    def test_full_contract(self):
        manager = TodoManager()
        high = manager.add("  高优先级  ", "high")
        medium = manager.add("默认")
        low = manager.add("低", "low")
        self.assertEqual((high.id, medium.id, low.id), (1, 2, 3))
        self.assertEqual(high.title, "高优先级")
        self.assertEqual(medium.priority, "medium")
        self.assertIsNone(manager.add("   "))
        self.assertIsNone(manager.add("错误", "urgent"))
        self.assertTrue(manager.complete(medium.id))
        self.assertFalse(manager.complete(999))
        ordered = manager.list_tasks()
        self.assertEqual([task.id for task in ordered], [high.id, low.id, medium.id])


if __name__ == "__main__":
    unittest.main()
