from __future__ import annotations

import os

from videoroll.workflows.client import create_hatchet_client
from videoroll.workflows.video_pipeline import register_subtitle_job_task, register_video_pipeline


def _slots() -> int:
    try:
        value = int(os.getenv("HATCHET_SUBTITLE_WORKER_SLOTS") or "1")
    except (TypeError, ValueError) as exc:
        raise ValueError("HATCHET_SUBTITLE_WORKER_SLOTS must be an integer") from exc
    if value < 1 or value > 32:
        raise ValueError("HATCHET_SUBTITLE_WORKER_SLOTS must be in the range 1..32")
    return value


def main() -> None:
    hatchet = create_hatchet_client()
    video_pipeline = register_video_pipeline(hatchet)
    subtitle_job = register_subtitle_job_task(hatchet)
    worker = hatchet.worker(
        str(os.getenv("HATCHET_SUBTITLE_WORKER_NAME") or "videoroll-subtitle-01"),
        slots=_slots(),
        workflows=[video_pipeline, subtitle_job],
        labels={"service": "videoroll", "pool": "subtitle"},
    )
    worker.start()


if __name__ == "__main__":
    main()
