from __future__ import annotations

import uuid


def unique_publish_result_key(task_id: uuid.UUID) -> str:
    return f"meta/{task_id}/publish_result_{uuid.uuid4().hex[:12]}.json"
