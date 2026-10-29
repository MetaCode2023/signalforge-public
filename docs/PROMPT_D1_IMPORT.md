# Prompt — finish the D1 market-data import

Paste everything below the line into Claude Code on the Windows machine
(the warehouse only exists there).

---

Finish the SIGNALFORGE Cloudflare D1 market-data mirror.

Repository: `C:\Users\you\repos\signalforge`
Target: D1 database `signalforge-market-data` in the **company account**
(`YOUR_CLOUDFLARE_ACCOUNT_ID`). No other account, no other database.

Read `docs/D1_MARKET_MIRROR.md` and `signalforge/mirror/` first. Use the
existing `mirror_cli` commands; do not write a new import path.

## Rules

- **Private forever.** Tiingo's terms forbid redistribution. No Worker,
  binding, route or public URL may read this database.
- **No remote deletes.** Run every `sync`/`load` with `--max-deletes 0`. If a
  sync wants to delete rows, stop and report how many and why.
- Never run `init` against a database that already has rows.
- Never print, log or commit environment values, tokens or keys.
- Keep using Wrangler's OAuth login as the doc describes. Do not create,
  widen or store an API token.
- Paper only. Do not touch anything under `signalforge/paper/` or the broker.
- If a permission, login or plan limit blocks you, stop and report the exact
  error and the command that produced it.

## Steps

**0. Check the plan and limits.** Find the account's Workers plan. Estimate
the bytes and rows `sync --dry-run` will write. If the plan is Free, or the
estimate exceeds the plan's per-database size or daily write limit, stop and
report the numbers. Do not start a trickle import.

**1. Check what already exists.** Confirm the database exists in the
company account. If it does, record its current row counts per table
(`bars`, `series`, `corporate_actions`) before changing anything. If it does
not exist, run `init` once.

**2. Check the local warehouse.** Confirm the collector is not running (the
mirror exits 2 if the warehouse is locked). Record local counts per table,
distinct symbols, distinct series, and min/max date per table. Expected
roughly: 4,087,602 bars, 894,347 series rows, 47,903 corporate actions, 493
symbols. Report any large difference before continuing.

**3. Dry run.** `python -m signalforge.mirror_cli sync --dry-run --max-deletes 0`.
Report files, rows and bytes it plans to send, and any planned deletes.

**4. Pilot.** `sync --max-files 1 --max-deletes 0`, then `verify --sample 200`.
Continue only if verify exits 0.

**5. Bulk import.** `load --max-deletes 0`. It is resumable; if it fails or
times out, rerun the same command. Do not restart from scratch. Stop after
three consecutive failures on the same file and report it.

**6. Compare local and remote** with cheap aggregates, not row-by-row hashes:
- row counts per table;
- per symbol in `bars`: row count, min date, max date, sum of close
  (rounded to 4 dp);
- per series ID in `series`: row count, min date, max date;
- symbols or series present on one side only.
Any mismatch is a failure; list the first 20.

**7. Daily sync.** Append the mirror step to the end of
`scripts/signalforge-daily.cmd`, after the collector finishes, as
`sync --max-deletes 0` then `verify --sample 50`. Log exit codes to the
existing log location. Do not add a separate scheduled task that could run
while the collector is writing.

Then tell me plainly: the Wrangler OAuth login will eventually expire, and
when it does the daily step will fail. Report how I'll see that failure
(log line, exit code) and what I'd run to re-login. Do not fix it by adding a
token.

**8. Report.** Write `reports/D1_MIRROR_VERIFICATION.md` and commit it:
- the exact remote row count per table and in total;
- local vs remote for every check in step 6, with pass/fail;
- the commands run, in order, with exit codes;
- any rows refused, deletes avoided, or blockers hit;
- no secrets or environment values.

## Also flag (do not do)

The docs say a Cloudflare API token and the Tiingo key were pasted into
chat sessions. Remind me to rotate both, and say whether either is still
present in the environment (yes/no only, never the value).
