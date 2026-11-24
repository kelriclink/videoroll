-- Due native batch events remain in the original outbox.
CREATE INDEX IF NOT EXISTS ix_outbox_native_publish_batch_due
 ON outbox_events(available_at,created_at,id)
 WHERE task_name IN ('rust.publish.batch_finished','rust.publish.cleanup') AND status='native_pending';
-- Adopt only untouched legacy events of already Rust-owned tasks. No
-- dispatched/leased/retried event is assumed safe to take over.
UPDATE outbox_events e SET status='native_pending',
 task_name=CASE e.task_name WHEN 'subtitle_service.push_publish_workflow_event' THEN 'rust.publish.batch_finished' ELSE 'rust.publish.cleanup' END,
 updated_at=now()
 FROM publish_batches b JOIN tasks t ON t.id=b.task_id
 WHERE e.aggregate_type='publish_batch' AND e.aggregate_id=b.id::text
 AND t.runtime_owner='rust' AND e.status='pending' AND e.attempt_count=0
 AND e.lease_owner IS NULL AND e.lease_until IS NULL AND e.delivered_at IS NULL
 AND e.task_name IN ('subtitle_service.push_publish_workflow_event','subtitle_service.cleanup_task')
 AND NOT EXISTS(SELECT 1 FROM operation_inbox i WHERE i.operation_key=e.operation_key);
