-- Bounded receipt reconciliation for the original execution ledger.
CREATE INDEX IF NOT EXISTS ix_render_native_terminal_receipts
 ON render_executions(updated_at,id)
 WHERE runtime_engine='rust-v2' AND state IN ('completed','failed');
