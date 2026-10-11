#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from videoroll.db.models import (
    Account,
    Asset,
    AppSetting,
    DesktopAccessGrant,
    OperationInbox,
    OutboxEvent,
    PipelineRun,
    PlayoutAssetLink,
    PublishBatch,
    PublishJob,
    PublishState,
    RemoteAPIRequest,
    RenderExecution,
    RenderJob,
    RenderJobStatus,
    RenderWorker,
    RenderWorkerEnrollment,
    SubtitleJob,
    SubtitleJobStatus,
    Subtitle,
    Task,
    TaskStatus,
)
from videoroll.db.session import get_configured_database_url, get_sessionmaker
from videoroll.utils.db_only import strip_legacy_encrypted_values
from videoroll.utils.fernet import is_legacy_encrypted_token


_REASON = "DB-only restore: previous runtime/local state was discarded"


def recover_database_only(*, execute: bool, preserve_files: bool = False) -> dict[str, int]:
    database_url = get_configured_database_url()
    SessionLocal = get_sessionmaker(database_url)
    now = datetime.now(timezone.utc)
    counts: dict[str, int] = {}

    with SessionLocal() as db:
        settings_changed = 0
        legacy_secret_fields = 0
        for row in db.query(AppSetting).all():
            original = dict(row.value_json or {})
            cleaned, removed = strip_legacy_encrypted_values(original)
            if row.key == "youtube.settings" and removed:
                cleaned["cookies_enabled"] = False
                cleaned.pop("cookies_updated_at", None)
            if removed:
                row.value_json = cleaned
                db.add(row)
                settings_changed += 1
                legacy_secret_fields += removed
        counts["settings_rows_reset"] = settings_changed
        counts["legacy_secret_fields_removed"] = legacy_secret_fields

        accounts_reset = 0
        for account in db.query(Account).all():
            if is_legacy_encrypted_token(account.secrets_encrypted):
                account.secrets_encrypted = ""
                account.is_active = False
                account.check_state = "unchecked"
                account.last_checked_at = None
                account.last_check_message = "Credentials were reset by DB-only restore; sign in again."
                db.add(account)
                accounts_reset += 1
        counts["social_accounts_reset"] = accounts_reset

        # DB-only disaster recovery deletes indexes for missing files. An
        # in-place cutover may retain files and should preserve their indexes.
        if preserve_files:
            counts["playout_asset_links_removed"] = 0
            counts["subtitles_removed"] = 0
            counts["assets_removed"] = 0
        else:
            counts["playout_asset_links_removed"] = db.query(PlayoutAssetLink).delete(synchronize_session=False)
            counts["subtitles_removed"] = db.query(Subtitle).delete(synchronize_session=False)
            counts["assets_removed"] = db.query(Asset).delete(synchronize_session=False)

        counts["subtitle_jobs_abandoned"] = (
            db.query(SubtitleJob)
            .filter(SubtitleJob.status.in_([SubtitleJobStatus.queued, SubtitleJobStatus.running]))
            .update(
                {
                    SubtitleJob.status: SubtitleJobStatus.failed,
                    SubtitleJob.error_message: _REASON,
                    SubtitleJob.lease_owner: None,
                    SubtitleJob.lease_until: None,
                    SubtitleJob.heartbeat_at: None,
                },
                synchronize_session=False,
            )
        )

        counts["render_jobs_abandoned"] = (
            db.query(RenderJob)
            .filter(RenderJob.status.in_([RenderJobStatus.queued, RenderJobStatus.running]))
            .update(
                {
                    RenderJob.status: RenderJobStatus.canceled,
                    RenderJob.error_message: _REASON,
                    RenderJob.lease_owner: None,
                    RenderJob.lease_until: None,
                    RenderJob.heartbeat_at: None,
                    RenderJob.finished_at: now,
                },
                synchronize_session=False,
            )
        )

        counts["publish_jobs_abandoned"] = (
            db.query(PublishJob)
            .filter(PublishJob.state.in_([PublishState.draft, PublishState.submitting]))
            .update(
                {
                    PublishJob.state: PublishState.failed,
                    PublishJob.upload_active: False,
                    PublishJob.lease_owner: None,
                    PublishJob.lease_until: None,
                    PublishJob.heartbeat_at: None,
                    PublishJob.finished_at: now,
                },
                synchronize_session=False,
            )
        )

        counts["publish_batches_abandoned"] = (
            db.query(PublishBatch)
            .filter(PublishBatch.state == "active")
            .update(
                {
                    PublishBatch.state: "failed",
                    PublishBatch.finished_at: now,
                },
                synchronize_session=False,
            )
        )

        counts["pipeline_runs_abandoned"] = (
            db.query(PipelineRun)
            .filter(PipelineRun.state.in_(["submitting", "submitted", "cancel_requested"]))
            .update(
                {
                    PipelineRun.state: "canceled",
                    PipelineRun.error_message: _REASON,
                    PipelineRun.finished_at: now,
                },
                synchronize_session=False,
            )
        )

        counts["render_executions_abandoned"] = (
            db.query(RenderExecution)
            .filter(RenderExecution.state.in_(["claimed", "running", "uploading"]))
            .update(
                {
                    RenderExecution.state: "lost",
                    RenderExecution.error_message: _REASON,
                    RenderExecution.lease_until: None,
                    RenderExecution.heartbeat_at: None,
                    RenderExecution.finished_at: now,
                },
                synchronize_session=False,
            )
        )

        active_task_statuses = [
            status
            for status in TaskStatus
            if status not in {TaskStatus.published, TaskStatus.failed, TaskStatus.canceled}
        ]
        counts["tasks_marked_failed"] = (
            db.query(Task)
            .filter(Task.status.in_(active_task_statuses))
            .update(
                {
                    Task.status: TaskStatus.failed,
                    Task.error_code: "DB_ONLY_RESTORE",
                    Task.error_message: _REASON,
                    Task.stopped_status: None,
                },
                synchronize_session=False,
            )
        )

        counts["render_workers_reset"] = db.query(RenderWorker).update(
            {
                RenderWorker.status: "offline",
                RenderWorker.active_jobs: 0,
                RenderWorker.credential_hash: None,
                RenderWorker.credential_revoked_at: now,
            },
            synchronize_session=False,
        )
        counts["active_enrollments_revoked"] = (
            db.query(RenderWorkerEnrollment)
            .filter(RenderWorkerEnrollment.status == "active")
            .update({RenderWorkerEnrollment.status: "revoked"}, synchronize_session=False)
        )

        counts["outbox_events_discarded"] = (
            db.query(OutboxEvent)
            .filter(OutboxEvent.status.in_(["pending", "failed", "dispatching"]))
            .update(
                {
                    OutboxEvent.status: "discarded",
                    OutboxEvent.lease_owner: None,
                    OutboxEvent.lease_until: None,
                    OutboxEvent.heartbeat_at: now,
                    OutboxEvent.last_error: _REASON,
                },
                synchronize_session=False,
            )
        )
        counts["inbox_operations_failed"] = (
            db.query(OperationInbox)
            .filter(OperationInbox.status.in_(["pending", "processing"]))
            .update(
                {
                    OperationInbox.status: "failed",
                    OperationInbox.lease_owner: None,
                    OperationInbox.lease_until: None,
                    OperationInbox.heartbeat_at: now,
                    OperationInbox.last_error: _REASON,
                    OperationInbox.completed_at: now,
                },
                synchronize_session=False,
            )
        )
        counts["remote_requests_failed"] = (
            db.query(RemoteAPIRequest)
            .filter(RemoteAPIRequest.status.in_(["pending", "processing"]))
            .update(
                {
                    RemoteAPIRequest.status: "failed",
                    RemoteAPIRequest.lease_owner: None,
                    RemoteAPIRequest.lease_until: None,
                    RemoteAPIRequest.heartbeat_at: now,
                    RemoteAPIRequest.last_error: _REASON,
                    RemoteAPIRequest.completed_at: now,
                },
                synchronize_session=False,
            )
        )
        counts["desktop_grants_revoked"] = (
            db.query(DesktopAccessGrant)
            .filter(DesktopAccessGrant.status == "active")
            .update(
                {
                    DesktopAccessGrant.status: "revoked",
                    DesktopAccessGrant.revoked_at: now,
                    DesktopAccessGrant.last_error: _REASON,
                },
                synchronize_session=False,
            )
        )

        if execute:
            db.commit()
        else:
            db.rollback()

    return counts


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare a VideoRoll PostgreSQL database for a restore where no old local/Redis/Hatchet state exists"
    )
    parser.add_argument("--execute", action="store_true", help="commit changes; default is dry-run")
    parser.add_argument("--preserve-files", action="store_true", help="retain file index rows for in-place migration")
    args = parser.parse_args()

    counts = recover_database_only(execute=args.execute, preserve_files=args.preserve_files)
    mode = "EXECUTED" if args.execute else "DRY-RUN"
    print(f"DB-only recovery {mode}")
    for key in sorted(counts):
        print(f"{key}={counts[key]}")
    if not args.execute:
        print("No database changes were committed. Re-run with --execute to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
