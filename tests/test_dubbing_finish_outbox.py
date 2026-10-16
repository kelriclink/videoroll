from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from videoroll.workflows import dubbing_job


def test_dubbing_terminal_notification_is_persisted_for_retry() -> None:
    db = MagicMock()
    factory = MagicMock()
    factory.return_value.__enter__.return_value = db
    with (
        patch("videoroll.config.get_subtitle_settings", return_value=SimpleNamespace(database_url="test")),
        patch("videoroll.db.session.get_sessionmaker", return_value=factory),
        patch("videoroll.apps.outbox.service.create_outbox_event") as enqueue,
    ):
        dubbing_job.enqueue_dubbing_finished("job-1", "succeeded")

    enqueue.assert_called_once_with(
        db, event_type="workflow.dubbing.finished", aggregate_type="dubbing_job",
        aggregate_id="job-1", task_name="subtitle_service.push_dubbing_workflow_event",
        args={"args": ["job-1", "succeeded", None], "queue": "subtitle-control"},
        operation_key="workflow-dubbing-finished:job-1:succeeded",
    )
    db.commit.assert_called_once()
