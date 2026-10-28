# SIGNALFORGE Research API — staging handoff

An independently tested Cloudflare Worker + D1 service for receiving **paper-trading research reports** from the local SIGNALFORGE computer. It is **not deployed** and no Cloudflare resources have been created from this workspace.

## Security model

- **Write route**: `POST /v1/ingest`, 64 KiB maximum UTF-8 JSON body, `paper_only: true`, required HMAC-SHA256 signature over `v1:<timestamp>:<nonce>:<exact-json-body>`, five-minute timestamp window, D1 nonce uniqueness, event-ID uniqueness, validated event type and payload.
- **Read routes**: `GET /v1/summary`, `GET /v1/events?limit=20`, both require a separate 32+ character bearer token. **Never place this token in browser JavaScript or a public environment variable.** Use a server-side private proxy or Cloudflare Access-protected read layer for the owner-only SIGNALFORGE Site.
- **Public health**: `GET /health` has only a minimal service/mode response.
- **Excluded**: live brokerage order endpoints, Schwab credentials, order submission, strategy promotion, payments, margin, leverage, public CORS.
- **Retention**: hourly/daily data is not deleted automatically; only spent nonce records older than 7 days are removed daily. Decide report retention separately.

## Current status

`npm test` runs dependency-free Node tests. Cloudflare integration, network deployment, end-to-end D1 behavior, private site proxy, and scheduled local agent reporting remain unverified.

## Deployment procedure — requires the owner's authenticated Cloudflare account

1. Verify the intended Cloudflare account and free-tier limits. Do not assume an account ID.
2. Install Wrangler in an operator-controlled environment and authenticate through the supported login flow. Do not copy private tokens into this repository.
3. Create a D1 database named `signalforge-research`, then copy `wrangler.toml.example` to `wrangler.toml` and set the returned database ID. Keep `workers_dev = false` until a protected custom domain/route has been configured.
4. Apply `migrations/0001_init.sql` against the intended D1 environment (`--remote` for the deployed database), after reviewing the target.
5. Generate independent random 32+ byte secrets in a password manager. Set `SIGNALFORGE_INGEST_SECRET` and `SIGNALFORGE_READ_TOKEN` through `wrangler secret put`. Do not log or echo their values.
6. Configure a restricted domain/route and Cloudflare Access for owner-only access. Confirm the write route can be reached by the approved terminal runner without bypassing Access; if a service token is necessary, scope it narrowly.
7. Deploy with Wrangler. Verify `GET /health` and the behavior of missing/invalid auth before sending any real paper research event.
8. Send one signed paper heartbeat with `scripts/send-report.mjs`. Confirm it exists once, rejects replay, and appears only through an authenticated read.
9. Integrate `GET /v1/summary` into the **server side** of the private SIGNALFORGE Site; do not expose the read token to browsers. Use least privilege and audit logs.
10. Only after this validation, schedule the local terminal runner to send verified experiment and paper-session reports.

## Event contract (v1)

```json
{
  "schema_version": 1,
  "paper_only": true,
  "event_id": "2d1b21c8-6a34-4a49-bc91-c2f32cb85e5a",
  "event_type": "heartbeat",
  "run_id": "terminal-20261027",
  "occurred_at": "2026-10-27T20:00:00Z",
  "payload": {"status": "idle"}
}
```

Use a fresh UUID for every event and an actual current timestamp. That example is an illustrative schema, **not evidence of a running terminal agent**.

Event types: `heartbeat`, `paper_session`, `backtest`, `risk_event`, `data_health`, `experiment`. See `src/validation.js` for required event-specific fields.

The worker stores the data as received after schema validation, but **does not independently verify trading economics**. The dashboard must distinguish *ingested* from *independently reviewed* results.

## Trading-computer client (paper only)

The included `scripts/send-report.mjs` submits one JSON research report. It performs local envelope validation, signs the exact request body and supports Cloudflare Access service credentials. It does not fetch market data, run strategies, execute trades or schedule itself.

Configure these values **on the trading computer**, using its secret manager. Never commit an `.env` file:

- `SIGNALFORGE_INGEST_SECRET` — report-signing secret.
- `SIGNALFORGE_ACCESS_CLIENT_ID` — Cloudflare Access service-token Client ID.
- `SIGNALFORGE_ACCESS_CLIENT_SECRET` — corresponding Access Client Secret.

The latter two are passed in `CF-Access-Client-Id` and `CF-Access-Client-Secret` headers; the signing secret is **never** sent over the network.

Run `node scripts/send-report.mjs https://<approved-private-host>/v1/ingest report.json` once the secrets have been loaded into the environment. A 202 response confirms the API accepted the report, not that the dashboard rendered it. Verify the event ID via an independently authenticated dashboard read.

**Important:** This client was built against the initial schema. The deployed API has reportedly been hardened independently to stricter field allowlists. Reconcile the current `HANDOFF.md` and deployed schema before sending anything beyond a minimal heartbeat. No actual deployment or remote ingestion was performed by this workspace.

## Known blockers and follow-up

- Cloudflare account ID and token are not configured in this workspace.
- Production D1 has not been created or migrated.
- Domain, routing, and Access policy are not configured.
- The private SIGNALFORGE Site currently shows a historical static snapshot and a pending terminal-report placeholder; it has no live API connection yet.
- Verify private-site server proxy authentication, replay behavior on actual D1, and report-data retention before deployment.
