from __future__ import annotations

import uuid

from videoroll.apps.bilibili_publisher.storage_keys import unique_publish_result_key


def test_publish_result_keys_are_unique_per_write() -> None:
    task_id = uuid.uuid4()

    first = unique_publish_result_key(task_id)
    second = unique_publish_result_key(task_id)

    assert first.startswith(f"meta/{task_id}/publish_result_")
    assert first.endswith(".json")
    assert first != second
