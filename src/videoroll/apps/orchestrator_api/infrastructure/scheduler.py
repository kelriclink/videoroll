from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone

from videoroll.apps.orchestrator_api.services import asset_service, maintenance_service, youtube_service
from videoroll.apps.orchestrator_api.storage_retention_store import get_storage_retention_settings
from videoroll.apps.subtitle_service.worker_concurrency import RecoverySummary, recover_expired_leases
from videoroll.apps.youtube_ingest.source_service import (
    DEFAULT_SOURCE_SCAN_LOCK_TTL_SECONDS,
    get_due_youtube_source_ids,
    scan_youtube_source_by_id,
)
from videoroll.config import OrchestratorSettings
from videoroll.db.session import get_sessionmaker
from videoroll.storage.s3 import S3Store


logger = logging.getLogger(__name__)


class OrchestratorScheduler:
    def __init__(self, settings: OrchestratorSettings) -> None:
        self.settings = settings
        self._cleanup_stop = threading.Event()
        self._home_scan_stop = threading.Event()
        self._source_scan_stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._cleanup_interval_seconds = int(os.getenv("STORAGE_CLEANUP_INTERVAL_SECONDS", "3600") or "3600")
        self._publishing_timeout_hours = max(1, int(os.getenv("PUBLISHING_TASK_TIMEOUT_HOURS", "48") or "48"))
        self._failed_resource_retention_hours = max(
            1,
            int(os.getenv("FAILED_TASK_RESOURCE_RETENTION_HOURS", "48") or "48"),
        )
        self._lease_recovery_interval_seconds = int(os.getenv("WORKER_LEASE_RECOVERY_INTERVAL_SECONDS", "30") or "30")
        self._home_scan_tick_seconds = int(os.getenv("YOUTUBE_HOME_SCAN_TICK_SECONDS", "30") or "30")
        self._source_scan_tick_seconds = int(os.getenv("YOUTUBE_SOURCE_SCAN_TICK_SECONDS", "30") or "30")
        self._shutdown_timeout_seconds = max(
            1.0,
            float(os.getenv("ORCHESTRATOR_SHUTDOWN_TIMEOUT_SECONDS", "30") or "30"),
        )
        self._source_scan_lock_ttl_seconds = int(
            os.getenv("YOUTUBE_SOURCE_SCAN_LOCK_TTL_SECONDS", str(DEFAULT_SOURCE_SCAN_LOCK_TTL_SECONDS))
            or str(DEFAULT_SOURCE_SCAN_LOCK_TTL_SECONDS)
        )
        self._source_scan_worker_id = f"{os.getenv('HOSTNAME') or 'orchestrator'}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    @property
    def running_thread_count(self) -> int:
        return sum(1 for thread in self._threads if thread.is_alive())

    def start(self) -> None:
        if self.running_thread_count:
            return
        self._cleanup_stop.clear()
        self._home_scan_stop.clear()
        self._source_scan_stop.clear()
        self._threads = [
            self._start_thread("videoroll-storage-cleanup", self._cleanup_loop),
            self._start_thread("videoroll-youtube-home-scan", self._home_scan_loop),
            self._start_thread("videoroll-youtube-source-scan", self._source_scan_loop),
            self._start_thread("videoroll-workdir-startup-cleanup", self._workdir_startup_cleanup),
            self._start_thread("videoroll-worker-lease-recovery", self._worker_lease_recovery_loop),
        ]

    def stop(self) -> None:
        self._cleanup_stop.set()
        self._home_scan_stop.set()
        self._source_scan_stop.set()
        deadline = time.monotonic() + self._shutdown_timeout_seconds
        remaining: list[threading.Thread] = []
        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                remaining.append(thread)
        self._threads = remaining
        if remaining:
            names = ", ".join(thread.name for thread in remaining)
            logger.error("orchestrator scheduler shutdown timed out; threads still running: %s", names)

    @staticmethod
    def _start_thread(name: str, target) -> threading.Thread:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        return thread

    def _cleanup_storage_once(self) -> dict[str, int]:
        session_local = get_sessionmaker(self.settings.database_url)
        db = session_local()
        try:
            timed_out_tasks = maintenance_service.expire_stale_publishing_tasks(
                db,
                timeout_hours=self._publishing_timeout_hours,
            )
            config = get_storage_retention_settings(db)
            store = S3Store(self.settings)
            deleted_objects = asset_service.retry_pending_s3_deletes(db, store)
            ttl_days = int(config.get("asset_ttl_days") or 0)
            retention = maintenance_service.cleanup_terminal_task_resources(
                self.settings,
                db,
                published_older_than_days=ttl_days if ttl_days > 0 else None,
                failed_older_than_hours=self._failed_resource_retention_hours,
                owner_prefix="retention",
            )
            if retention is None:
                return {
                    "timed_out_tasks": timed_out_tasks,
                    "deleted_objects": deleted_objects,
                    "deleted_assets": 0,
                    "deleted_subtitles": 0,
                }
            return {
                "timed_out_tasks": timed_out_tasks,
                "deleted_objects": deleted_objects + retention.deleted_objects,
                "deleted_assets": retention.deleted_assets,
                "deleted_subtitles": retention.deleted_subtitles,
            }
        finally:
            db.close()

    def _cleanup_loop(self) -> None:
        while not self._cleanup_stop.is_set():
            try:
                self._cleanup_storage_once()
            except Exception:
                logger.exception("storage cleanup loop failed")
            self._cleanup_stop.wait(timeout=max(30, self._cleanup_interval_seconds))

    def _recover_worker_leases_once(self) -> RecoverySummary:
        """Repair only jobs whose database owner lease actually expired."""
        session_local = get_sessionmaker(self.settings.database_url)
        db = session_local()
        try:
            result = recover_expired_leases(db, now=datetime.now(timezone.utc), limit=100)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _worker_lease_recovery_loop(self) -> None:
        while not self._cleanup_stop.is_set():
            try:
                result = self._recover_worker_leases_once()
                if result.total_recovered:
                    logger.warning(
                        "recovered expired worker leases: subtitle=%s render=%s",
                        result.subtitle_requeued,
                        result.render_requeued,
                    )
            except Exception:
                logger.exception("worker lease recovery loop failed")
            self._cleanup_stop.wait(timeout=max(5, self._lease_recovery_interval_seconds))

    def _home_scan_loop(self) -> None:
        while not self._home_scan_stop.is_set():
            try:
                youtube_service.run_home_scan(self.settings)
            except Exception:
                logger.exception("youtube home scan loop failed")
            self._home_scan_stop.wait(timeout=max(15, self._home_scan_tick_seconds))

    def _run_due_source_scans(self) -> int:
        session_local = get_sessionmaker(self.settings.database_url)
        db = session_local()
        try:
            source_ids = get_due_youtube_source_ids(db)
        finally:
            db.close()

        started = 0
        for source_id in source_ids:
            if self._source_scan_stop.is_set():
                break
            db = session_local()
            try:
                result = scan_youtube_source_by_id(
                    db,
                    source_id,
                    user_agent=self.settings.youtube_user_agent,
                    default_proxy=self.settings.youtube_proxy,
                    force=False,
                    raise_if_locked=False,
                    lock_owner_prefix=f"scheduled_youtube_source_scan:{self._source_scan_worker_id}",
                    lock_ttl_seconds=self._source_scan_lock_ttl_seconds,
                )
                if result is not None:
                    started += 1
            except Exception:
                logger.exception("scheduled youtube source scan failed", extra={"source_id": str(source_id)})
            finally:
                db.close()
        return started

    def _source_scan_loop(self) -> None:
        while not self._source_scan_stop.is_set():
            try:
                self._run_due_source_scans()
            except Exception:
                logger.exception("youtube source scan loop failed")
            self._source_scan_stop.wait(timeout=max(15, self._source_scan_tick_seconds))

    def _workdir_startup_cleanup(self) -> None:
        try:
            result = maintenance_service.run_startup_workdir_cleanup(self.settings)
            if result is None:
                logger.info("workdir startup cleanup skipped: another cleanup is running")
                return
            logger.info(
                "workdir startup cleanup finished: scanned_dirs=%s reclaimable_dirs=%s deleted_dirs=%s reclaimed_bytes=%s errors=%s",
                result.scanned_dirs,
                result.reclaimable_dirs,
                result.deleted_dirs,
                result.deleted_bytes,
                len(result.errors),
            )
        except Exception:
            logger.exception("workdir startup cleanup failed")
