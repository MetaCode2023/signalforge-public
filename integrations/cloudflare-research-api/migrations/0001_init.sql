CREATE TABLE IF NOT EXISTS reports (
  event_id TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  run_id TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  body_sha256 TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  CHECK (event_type IN ('heartbeat','paper_session','backtest','risk_event','data_health','experiment'))
);
CREATE INDEX IF NOT EXISTS idx_reports_type_time ON reports(event_type, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_run_time ON reports(run_id, occurred_at DESC);
CREATE TABLE IF NOT EXISTS ingest_nonces (
  nonce TEXT PRIMARY KEY,
  received_unix INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ingest_nonces_time ON ingest_nonces(received_unix);
