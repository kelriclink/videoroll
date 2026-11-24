-- Preserve original event/job/operation tables; only index the Rust-owned SAU queue.
CREATE INDEX IF NOT EXISTS ix_outbox_native_sau_pending
 ON outbox_events(available_at,created_at,id)
 WHERE task_name='social_publisher.process_job' AND status='native_pending';
CREATE INDEX IF NOT EXISTS ix_outbox_native_sau_running
 ON outbox_events(lease_until)
 WHERE task_name='social_publisher.process_job' AND status='native_running';
CREATE UNIQUE INDEX IF NOT EXISTS ux_outbox_native_sau_operation
 ON outbox_events(operation_key)
 WHERE task_name='social_publisher.process_job' AND status IN ('native_pending','native_running','native_completed');
