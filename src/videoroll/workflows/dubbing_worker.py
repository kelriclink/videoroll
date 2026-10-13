"""Dedicated worker process: install qwen-tts only in this runtime."""
from __future__ import annotations

import os

from videoroll.workflows.client import create_hatchet_client
from videoroll.workflows.dubbing_job import register_dubbing_job_task


def main() -> None:
    hatchet = create_hatchet_client()
    task = register_dubbing_job_task(hatchet)
    slots = int(os.getenv("HATCHET_DUBBING_WORKER_SLOTS") or "1")
    if slots != 1:
        raise ValueError("dubbing worker requires exactly one slot per loaded GPU model")
    hatchet.worker(
        str(os.getenv("HATCHET_DUBBING_WORKER_NAME") or "videoroll-dubbing-01"),
        slots=slots, workflows=[task], labels={"service": "videoroll", "pool": "dubbing"},
    ).start()


if __name__ == "__main__":
    main()
