from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from videoroll.apps.subtitle_service.main import put_task_queue_settings_view
from videoroll.apps.subtitle_service.schemas import TaskQueueSettingsUpdate
from videoroll.apps.subtitle_service.worker_concurrency import (
    main,
    normalize_subtitle_worker_concurrency,
    resolve_subtitle_worker_concurrency,
    sync_subtitle_worker_concurrency,
    sync_subtitle_worker_concurrency_for_task_queue_settings,
    subtitle_worker_concurrency_for_task_queue_settings,
)


class _FakeSession:
    def close(self) -> None:
        return None


class _FakeInspect:
    def __init__(self, *, active_queues: dict[str, object] | None = None, stats: dict[str, object] | None = None) -> None:
        self._active_queues = active_queues or {}
        self._stats = stats or {}

    def active_queues(self) -> dict[str, object]:
        return dict(self._active_queues)

    def stats(self) -> dict[str, object]:
        return dict(self._stats)


class _FakeControl:
    def __init__(self, *, active_queues: dict[str, object] | None = None, stats: dict[str, object] | None = None) -> None:
        self._active_queues = active_queues or {}
        self._stats = stats or {}
        self.inspect_calls: list[dict[str, object]] = []
        self.grow_calls: list[dict[str, object]] = []
        self.shrink_calls: list[dict[str, object]] = []
        self.send_task_calls: list[dict[str, object]] = []

    def inspect(self, destination: list[str] | None = None, timeout: float = 1.0) -> _FakeInspect:
        self.inspect_calls.append({"destination": destination, "timeout": timeout})
        if destination:
            active_queues = {k: v for k, v in self._active_queues.items() if k in destination}
            stats = {k: v for k, v in self._stats.items() if k in destination}
        else:
            active_queues = dict(self._active_queues)
            stats = dict(self._stats)
        return _FakeInspect(active_queues=active_queues, stats=stats)

    def pool_grow(self, n: int, destination: list[str] | None = None, reply: bool = False, timeout: float = 1.0) -> list[dict[str, object]]:
        self.grow_calls.append({"n": n, "destination": destination, "reply": reply, "timeout": timeout})
        host = (destination or ["unknown"])[0]
        return [{host: {"ok": "pool will grow"}}]

    def pool_shrink(self, n: int, destination: list[str] | None = None, reply: bool = False, timeout: float = 1.0) -> list[dict[str, object]]:
        self.shrink_calls.append({"n": n, "destination": destination, "reply": reply, "timeout": timeout})
        host = (destination or ["unknown"])[0]
        return [{host: {"ok": "pool will shrink"}}]

    def send_task(self, name: str, *, args: list[object], queue: str) -> None:
        self.send_task_calls.append({"name": name, "args": list(args), "queue": queue})


class _FakeCeleryApp:
    def __init__(self, *, active_queues: dict[str, object] | None = None, stats: dict[str, object] | None = None) -> None:
        self.control = _FakeControl(active_queues=active_queues, stats=stats)

    def send_task(self, name: str, *, args: list[object], queue: str) -> None:
        self.control.send_task(name, args=args, queue=queue)


class WorkerConcurrencyTests(unittest.TestCase):
    def test_normalize_enforces_minimum_one_worker(self) -> None:
        self.assertEqual(normalize_subtitle_worker_concurrency(0), 1)
        self.assertEqual(normalize_subtitle_worker_concurrency(-3), 1)

    def test_task_queue_setting_maps_directly_to_worker_concurrency(self) -> None:
        self.assertEqual(subtitle_worker_concurrency_for_task_queue_settings({"max_concurrency": 2}), 2)
        self.assertEqual(subtitle_worker_concurrency_for_task_queue_settings({"max_concurrency": 7}), 7)
        self.assertEqual(subtitle_worker_concurrency_for_task_queue_settings({"max_concurrency": 0}), 1)

    def test_sync_grows_only_workers_consuming_subtitle_queue(self) -> None:
        fake_celery = _FakeCeleryApp(
            active_queues={
                "celery@subtitle": [{"name": "subtitle"}],
                "celery@publish": [{"name": "publish"}],
            },
            stats={
                "celery@subtitle": {"pool": {"max-concurrency": 1}},
                "celery@publish": {"pool": {"max-concurrency": 9}},
            },
        )

        result = sync_subtitle_worker_concurrency(fake_celery, 3, timeout=2.0)

        self.assertTrue(result["ok"])
        self.assertEqual(result["target_concurrency"], 3)
        self.assertEqual(
            fake_celery.control.grow_calls,
            [{"n": 2, "destination": ["celery@subtitle"], "reply": True, "timeout": 2.0}],
        )
        self.assertEqual(fake_celery.control.shrink_calls, [])

    def test_sync_shrinks_worker_when_target_is_lower(self) -> None:
        fake_celery = _FakeCeleryApp(
            active_queues={"celery@subtitle": [{"name": "subtitle"}]},
            stats={"celery@subtitle": {"pool": {"max-concurrency": 4}}},
        )

        result = sync_subtitle_worker_concurrency(fake_celery, 2, timeout=2.0)

        self.assertTrue(result["ok"])
        self.assertEqual(fake_celery.control.grow_calls, [])
        self.assertEqual(
            fake_celery.control.shrink_calls,
            [{"n": 2, "destination": ["celery@subtitle"], "reply": True, "timeout": 2.0}],
        )

    def test_sync_for_task_queue_settings_maps_zero_to_one_worker(self) -> None:
        fake_celery = _FakeCeleryApp(
            active_queues={"celery@subtitle": [{"name": "subtitle"}]},
            stats={"celery@subtitle": {"pool": {"max-concurrency": 3}}},
        )

        result = sync_subtitle_worker_concurrency_for_task_queue_settings(fake_celery, {"max_concurrency": 0}, timeout=2.0)

        self.assertTrue(result["ok"])
        self.assertEqual(result["target_concurrency"], 1)
        self.assertEqual(
            fake_celery.control.shrink_calls,
            [{"n": 2, "destination": ["celery@subtitle"], "reply": True, "timeout": 2.0}],
        )

    def test_put_task_queue_settings_view_syncs_runtime_worker_and_kicks_scheduler(self) -> None:
        fake_celery = _FakeCeleryApp()
        runtime_sync = {
            "ok": True,
            "target_concurrency": 4,
            "detail": "synchronized 1 worker(s) to concurrency=4",
            "workers": [
                {
                    "hostname": "celery@subtitle",
                    "current_concurrency": 1,
                    "target_concurrency": 4,
                    "action": "grow",
                    "ok": True,
                    "detail": "pool will grow",
                }
            ],
        }

        with (
            patch("videoroll.apps.subtitle_service.main.update_task_queue_settings", return_value={"max_concurrency": 4}),
            patch("videoroll.apps.subtitle_service.main.sync_subtitle_worker_concurrency_for_task_queue_settings", return_value=runtime_sync),
            patch("videoroll.apps.subtitle_service.main.celery_app", fake_celery),
        ):
            response = put_task_queue_settings_view(TaskQueueSettingsUpdate(max_concurrency=4), db=object())  # type: ignore[arg-type]

        self.assertEqual(response.max_concurrency, 4)
        self.assertEqual(response.runtime_worker_concurrency, 4)
        self.assertTrue(response.runtime_sync_ok)
        self.assertEqual(response.runtime_sync_detail, "synchronized 1 worker(s) to concurrency=4")
        self.assertEqual(len(response.runtime_sync_workers), 1)
        self.assertEqual(
            fake_celery.control.send_task_calls,
            [{"name": "subtitle_service.task_queue_tick", "args": [], "queue": "subtitle"}],
        )

    def test_resolve_reads_task_queue_settings_from_db(self) -> None:
        fake_session = _FakeSession()

        with (
            patch("videoroll.apps.subtitle_service.worker_concurrency.get_sessionmaker", return_value=lambda: fake_session),
            patch("videoroll.apps.subtitle_service.worker_concurrency.get_task_queue_settings", return_value={"max_concurrency": 2}),
        ):
            resolved = resolve_subtitle_worker_concurrency("postgresql://demo")

        self.assertEqual(resolved, 2)

    def test_main_falls_back_when_db_lookup_fails(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch("videoroll.apps.subtitle_service.worker_concurrency.os.getenv", side_effect=lambda key, default=None: {"DATABASE_URL": "postgresql://demo", "CELERY_SUB_CONCURRENCY_FALLBACK": "3"}.get(key, default)),
            patch("videoroll.apps.subtitle_service.worker_concurrency.resolve_subtitle_worker_concurrency", side_effect=RuntimeError("db down")),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = main()

        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue().strip(), "3")
        self.assertIn("failed to resolve subtitle worker concurrency", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
