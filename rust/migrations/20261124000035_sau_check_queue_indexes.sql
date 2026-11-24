-- Original operation_inbox remains the only durable account check ledger.
-- Small partial indexes keep the standalone worker from repeatedly scanning
-- the entire historical inbox.
CREATE UNIQUE INDEX IF NOT EXISTS ux_operation_inbox_sau_check_pending_account
 ON operation_inbox ((request_json->>'account_id'))
 WHERE runtime_engine='rust-sau-check' AND status='pending';
CREATE INDEX IF NOT EXISTS ix_operation_inbox_sau_check_pending
 ON operation_inbox(created_at,id)
 WHERE runtime_engine='rust-sau-check' AND status='pending';
CREATE INDEX IF NOT EXISTS ix_operation_inbox_sau_check_processing
 ON operation_inbox(lease_until)
 WHERE runtime_engine='rust-sau-check' AND status='processing';
