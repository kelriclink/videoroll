from __future__ import annotations

from typing import Any, Callable

from videoroll.workflows.probe import PROBE_TASK_NAME, WorkflowProbeInput, register_workflow_probe


class _FakeHatchet:
    def __init__(self) -> None:
        self.options: dict[str, Any] = {}

    def task(self, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        self.options = dict(kwargs)

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            return fn

        return decorator


def test_workflow_probe_is_zero_side_effect_and_typed() -> None:
    hatchet = _FakeHatchet()
    task = register_workflow_probe(hatchet)

    output = task(WorkflowProbeInput(message="videoroll"), object())

    assert hatchet.options["name"] == PROBE_TASK_NAME
    assert hatchet.options["input_validator"] is WorkflowProbeInput
    assert output.model_dump() == {"message": "videoroll", "worker": "hatchet"}
