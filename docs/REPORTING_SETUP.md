# Private reporting setup

Status recorded 2026-10-27 (America/Phoenix). PAPER TRADING ONLY.

## Credentials and deployment

- 1Password account: MCG LLC; vault: Employee; existing item: `signalforce`.
- The existing Cloudflare management credential was preserved.
- These three additional fields are concealed and were saved with verified readbacks:
  - `SIGNALFORGE_ACCESS_CLIENT_ID`
  - `SIGNALFORGE_ACCESS_CLIENT_SECRET`
  - `SIGNALFORGE_INGEST_SECRET`
- Local copies are in this repository's `.env`. The file is gitignored, untracked,
  and has inheritance disabled with one explicit access rule for the current user.
  Do not print its contents or include it in a commit, attachment, or diagnostic log.
- Existing Access token `signalforge-trading-ingest` was rotated by Austin. Its
  application remains `SIGNALFORGE API ingest` at
  `signalforge-api.example.com/v1/ingest`.
- Austin deployed the replacement `SIGNALFORGE_INGEST_SECRET` on
  `signalforge-research-api`. Cloudflare shows version `d7127999`, Ready, 100% traffic,
  with commit message `Updated secret: SIGNALFORGE_INGEST_SECRET`.
- A temporary loopback-only form saved the local file and then shut down. It did
  not send a reporting request or start an engine or scheduler.

No live orders, brokerage submissions, margin, risk-limit changes, resource
deletions, or changes to unrelated production were performed.

## Verification still required

The current automation session cannot connect outbound to the API. A command-line
HTTPS connectivity check failed before receiving any HTTP response (curl displays
`000`; that is not an HTTP response code). No signed ingest was sent and no 202
acceptance has been established.

The Cloudflare code editor also remained at Loading. Retrieve the deployed
allowlisted report schema before constructing the test payload; do not guess the
report type, envelope, or fields. Do not weaken Access, HMAC, CORS, or risk controls
to work around these tooling limits.

Once outbound access and the schema are available:

1. Load the three variables privately from `.env` into the sender process.
2. Serialize the allowlisted test payload once, with a run ID of
   `e2e-test-<local YYYY-MM-DD>`. Do NOT send an `is_test` field: the envelope is
   strict and rejects it. The server marks a record as test data from the run ID
   prefix (`e2e-test-`, `test-` or `smoke-`).
3. Generate the current Unix timestamp and a fresh nonce for every request. Sign
   the exact UTF-8 bytes of `v1:<timestamp>:<nonce>:<raw body>` with HMAC-SHA256,
   using `SIGNALFORGE_INGEST_SECRET` as the key.
4. POST only to `https://signalforge-api.example.com/v1/ingest`, with headers
   `CF-Access-Client-Id`, `CF-Access-Client-Secret`, `x-signalforge-timestamp`,
   `x-signalforge-nonce`, and `x-signalforge-signature: v1=<hex digest>`.
   Do not follow redirects carrying credentials. Log only safe status metadata.
5. Confirm HTTP 202 and verify the stored record remains test-only and excluded
   from metrics. Keep test data, as requested.

## Integration plan (not yet implemented or enabled)

After the successful test, add reporting outside the socket-free paper execution
path. Keep `signalforge/paper/**`, the broker, strategies, and risk engine unchanged.

- Export only persisted, real paper-run results into the deployed report schema.
  Exclude free-text notes, approval reasons, raw ledger events, and credentials.
- Use a separate report sender, fixed to the reporting API, with both Access and
  HMAC authentication. Credential failures must fail closed.
- Use stable report identities and verify API duplicate semantics before adding
  retries. Give every transmission attempt a fresh timestamp and nonce.
- Test signature bytes, field rejection, test isolation, duplicate behavior, and
  the existing structural no-network/no-live-order safeguards.
- Review current strategy approval and existing risk limits before any engine
  run. No strategy approval or override was granted by this reporting task.
- Show Austin the concrete continuous-operation plan before enabling any loop,
  scheduled task, service, or daemon. None is currently enabled by this work.

## Command-line sender (implemented)

Code: `signalforge/reports/events.py` (builders and strict local validator),
`signalforge/reports/sender.py` (signed POST), `signalforge/report_cli.py`.
It sits outside the socket-free paper path and is fixed to
`https://signalforge-api.example.com/v1/ingest` (no redirects followed).

- `python -m signalforge.report_cli smoke` sends one heartbeat with run ID
  `e2e-test-<YYYY-MM-DD>` and prints the outcome. Needs the three `.env` variables.
- `python -m signalforge.report_cli push [--db PATH] [--dry-run]` builds data_health,
  backtest, paper_session and heartbeat events (run ID `research-<YYYYMMDD>`) from the
  warehouse opened read-only, validates all of them locally (any invalid event: exit 2,
  nothing sent), then sends in that order. Exit 0 only if every event is 202 or
  `duplicate_event`. `--dry-run` prints the events and needs no credentials or network.
- Event IDs are deterministic (uuid5 of the natural key), so resending is safe: the
  server answers `duplicate_event`, which counts as delivered.
- Only train and validation experiments are exported; the test (hold-out) window never is.
  Dataset fingerprints are shortened to 12 hex characters.
- paper_session events are skipped with a warning because realized/unrealized PnL and
  positions are not persisted in `paper_sessions`.
- Backtest events: one per experiment (event_id = uuid5 of experiment_id), only for the newest dataset
  fingerprint per dataset kind (series id, or `ohlcv<N>-<source>`) and window. strategy_id is
  `<kind>.<strategy-params>`. Rows without a valid recorded commit sha are skipped, never given HEAD.
- `.env` is read as UTF-8 (BOM tolerated); only the three variables are kept; inline comments
  (`KEY=value # note`) are NOT supported.
- A missing `.env` variable fails closed and names only the variable. Credentials are
  never printed, logged or placed in exception text.
