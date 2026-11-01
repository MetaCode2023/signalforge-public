-- Allow the forecast_trial report type (Crucible).
--
-- SQLite cannot alter a CHECK constraint, so the table is rebuilt: copy every
-- row into a table whose CHECK also allows 'forecast_trial', swap it in, and
-- recreate the three indexes. Columns, defaults and indexes are otherwise
-- identical to the table this replaces.
--
-- Run once, BEFORE deploying the API code that accepts forecast_trial:
--   npx wrangler d1 execute signalforge-research --remote --file cloudflare/signalforge-research-api/migrations/0002_allow_forecast_trial.sql
-- Then check: SELECT COUNT(*) FROM reports;  (should equal the count before)
-- D1 Time Travel can restore the database to any minute in the last 30 days.

CREATE TABLE reports_new (
  event_id TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  run_id TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  body_sha256 TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  is_test INTEGER NOT NULL DEFAULT 0 CHECK (is_test IN (0,1)),
  CHECK (event_type IN ('heartbeat','paper_session','backtest','risk_event','data_health',
                        'experiment','validation_failure','forecast_trial'))
);
INSERT INTO reports_new (event_id, schema_version, event_type, run_id, occurred_at, received_at,
                         body_sha256, payload_json, is_test)
SELECT event_id, schema_version, event_type, run_id, occurred_at, received_at,
       body_sha256, payload_json, is_test
FROM reports;
DROP TABLE reports;
ALTER TABLE reports_new RENAME TO reports;
CREATE INDEX idx_reports_received ON reports(is_test, received_at DESC);
CREATE INDEX idx_reports_run_time ON reports(run_id, occurred_at DESC);
CREATE INDEX idx_reports_type_time ON reports(event_type, is_test, occurred_at DESC);
