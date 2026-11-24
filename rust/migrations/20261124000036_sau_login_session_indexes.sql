-- At most one native browser login session may reserve the shared display.
CREATE UNIQUE INDEX IF NOT EXISTS ux_operation_inbox_sau_login_active
 ON operation_inbox ((1))
 WHERE runtime_engine='rust-sau-login' AND status IN ('pending','processing');
CREATE INDEX IF NOT EXISTS ix_operation_inbox_sau_login_processing
 ON operation_inbox(lease_until)
 WHERE runtime_engine='rust-sau-login' AND status='processing';
CREATE INDEX IF NOT EXISTS ix_operation_inbox_sau_login_latest
 ON operation_inbox ((request_json->>'platform'),created_at DESC,id DESC)
 WHERE runtime_engine='rust-sau-login';
