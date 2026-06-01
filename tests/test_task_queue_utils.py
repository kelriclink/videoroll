from __future__ import annotations

import unittest

from videoroll.utils.task_queue import available_task_queue_capacity, task_queue_slot_reserved_for


class TaskQueueUtilsTests(unittest.TestCase):
    def test_available_capacity_never_goes_negative(self) -> None:
        self.assertEqual(available_task_queue_capacity(3, 0), 3)
        self.assertEqual(available_task_queue_capacity(3, 2), 1)
        self.assertEqual(available_task_queue_capacity(3, 7), 0)

    def test_slot_reserved_only_for_first_n_locked_tasks(self) -> None:
        locked = ["task-a", "task-b", "task-c", "task-d"]

        self.assertTrue(task_queue_slot_reserved_for("task-a", locked, 2))
        self.assertTrue(task_queue_slot_reserved_for("task-b", locked, 2))
        self.assertFalse(task_queue_slot_reserved_for("task-c", locked, 2))
        self.assertFalse(task_queue_slot_reserved_for("task-d", locked, 2))


if __name__ == "__main__":
    unittest.main()
