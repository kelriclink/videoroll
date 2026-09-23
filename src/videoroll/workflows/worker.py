from __future__ import annotations

from videoroll.workflows.client import create_hatchet_client
from videoroll.workflows.probe import register_workflow_probe
from videoroll.workflows.settings import WorkflowRuntimeSettings
from videoroll.workflows.video_pipeline import register_video_pipeline


def main() -> None:
    settings = WorkflowRuntimeSettings.from_env()
    hatchet = create_hatchet_client()
    probe = register_workflow_probe(hatchet)
    video_pipeline = register_video_pipeline(hatchet, settings=settings)
    worker = hatchet.worker(
        settings.worker_name,
        slots=settings.worker_slots,
        workflows=[probe, video_pipeline],
        labels={"service": "videoroll", "pool": "workflow-core"},
    )
    worker.start()


if __name__ == "__main__":
    main()
