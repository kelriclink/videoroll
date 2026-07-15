from __future__ import annotations

import unittest
from unittest.mock import Mock, patch


class OrchestratorLifecycleTests(unittest.TestCase):
    def test_scheduler_is_inert_until_started(self) -> None:
        from videoroll.apps.orchestrator_api.infrastructure.scheduler import OrchestratorScheduler

        scheduler = OrchestratorScheduler(Mock())

        self.assertEqual(scheduler.running_thread_count, 0)

    def test_lifespan_module_exposes_context_manager(self) -> None:
        from videoroll.apps.orchestrator_api.infrastructure.lifecycle import orchestrator_lifespan

        self.assertTrue(callable(orchestrator_lifespan))

    def test_scheduler_stop_waits_for_threads_to_finish(self) -> None:
        from videoroll.apps.orchestrator_api.infrastructure.scheduler import OrchestratorScheduler

        scheduler = OrchestratorScheduler(Mock())
        thread = Mock()
        thread.name = "blocked-thread"
        thread.is_alive.side_effect = [True, True]
        scheduler._threads = [thread]

        scheduler.stop()

        timeout = thread.join.call_args.kwargs["timeout"]
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 30)

    def test_pending_deletes_retry_even_when_retention_is_disabled(self) -> None:
        from videoroll.apps.orchestrator_api.infrastructure import scheduler as scheduler_module

        settings = Mock(database_url="postgresql://example")
        scheduler = scheduler_module.OrchestratorScheduler(settings)
        db = Mock()
        session_local = Mock(return_value=db)
        store = Mock()

        with (
            patch.object(scheduler_module, "get_sessionmaker", return_value=session_local),
            patch.object(scheduler_module, "get_storage_retention_settings", return_value={"asset_ttl_days": 0}),
            patch.object(scheduler_module, "S3Store", return_value=store),
            patch.object(scheduler_module.asset_service, "retry_pending_s3_deletes", return_value=2) as retry,
            patch.object(scheduler_module.maintenance_service, "expire_stale_publishing_tasks", return_value=1) as expire,
            patch.object(scheduler_module.maintenance_service, "cleanup_terminal_task_resources") as cleanup,
        ):
            cleanup.return_value = Mock(deleted_objects=3, deleted_assets=4, deleted_subtitles=5)
            result = scheduler._cleanup_storage_once()

        expire.assert_called_once_with(db, timeout_hours=48)
        retry.assert_called_once_with(db, store)
        cleanup.assert_called_once_with(
            settings,
            db,
            published_older_than_days=None,
            failed_older_than_hours=48,
            owner_prefix="retention",
        )
        self.assertEqual(result["timed_out_tasks"], 1)
        self.assertEqual(result["deleted_objects"], 5)


if __name__ == "__main__":
    unittest.main()
