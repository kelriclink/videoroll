from __future__ import annotations

from typing import Any

from pydantic import BaseModel


PROBE_TASK_NAME = "videoroll-workflow-probe-v1"


class WorkflowProbeInput(BaseModel):
    message: str = "ok"


class WorkflowProbeOutput(BaseModel):
    message: str
    worker: str = "hatchet"


def register_workflow_probe(hatchet: Any) -> Any:
    """Register a zero-side-effect task used to validate Hatchet connectivity."""

    @hatchet.task(name=PROBE_TASK_NAME, input_validator=WorkflowProbeInput)
    def workflow_probe(input: WorkflowProbeInput, _ctx: Any) -> WorkflowProbeOutput:
        return WorkflowProbeOutput(message=input.message)

    return workflow_probe
