# Cloudflare Workers: Crucible on the private dashboard

Source for the two Workers behind the owner-only SignalForge dashboard, with the
Crucible tab added. Each folder holds a single `index.js` that replaces the
deployed bundle as-is; bindings, secrets, routes and Access policies are
untouched by deploying it.

| Folder | Worker | Change |
|---|---|---|
| `signalforge-research-api/` | `signalforge-research-api` | accepts the `forecast_trial` report (strict schema, no price fields) |
| `signalforge-research-api/migrations/0002_allow_forecast_trial.sql` | D1 `signalforge-research` | lets the `reports` table store that type |
| `signalforge-dashboard/` | `signalforge-dashboard` | `/crucible` tab and a tab strip on `/`; operator page otherwise unchanged |

What reaches Cloudflare is scores, a forward cone as percent moves, and simulated
equity. No price level is in the report schema, so licensed bars still never
leave the trading computer (README, Data and licensing). The Crucible page runs
no script under the existing CSP; its motion is CSS and stops under
`prefers-reduced-motion`.

## Deploy, in this order

All from the repo root on the trading computer, logged in to Wrangler as for
`mirror_cli`. PowerShell:

```powershell
$env:CLOUDFLARE_ACCOUNT_ID = "YOUR_CLOUDFLARE_ACCOUNT_ID"   # company account

# 1. Database: note the count, migrate, check the count again (must match)
npx wrangler d1 execute signalforge-research --remote --command "SELECT COUNT(*) FROM reports"
npx wrangler d1 execute signalforge-research --remote --file cloudflare/signalforge-research-api/migrations/0002_allow_forecast_trial.sql
npx wrangler d1 execute signalforge-research --remote --command "SELECT COUNT(*) FROM reports"
```

2. **API Worker.** Cloudflare dashboard → Workers & Pages →
   `signalforge-research-api` → Edit code. Replace the contents of `index.js`
   with `cloudflare/signalforge-research-api/index.js` (delete `index.js.map`
   if the editor shows it) → Deploy.
3. **Dashboard Worker.** Same for `signalforge-dashboard` with
   `cloudflare/signalforge-dashboard/index.js`.
4. **First report:**

   ```powershell
   python -m signalforge.forecast_cli trial --symbols SPY,QQQ,AAPL,MSFT,JPM,XOM,KO,WMT `
       --window validation --source tiingo --backtest --report
   ```

   It prints `Crucible report <id>: accepted`. `--report-dry-run` instead
   prints the JSON and sends nothing.
5. Open the dashboard and tap **Crucible** (path `/crucible`).

If the order slips, nothing breaks: the old API refuses the report with
`event_type`, an API deployed before the migration answers
`storage_rejects_type_run_migration_0002`, and the Crucible tab says which
Worker still needs deploying.

## Undo

- Workers: the Worker's Deployments tab → roll back to the previous version.
- Database: the migration only widens what the table accepts. D1 Time Travel
  restores to any minute in the last 30 days if ever needed.

## Tests

`tests/test_crucible_report.py` builds a report in Python and, when Node is
installed, runs it through this API code's validator and renders it with this
dashboard code, so the two copies of the contract cannot drift unnoticed. It
also rehearses the migration on a copy of the schema.
