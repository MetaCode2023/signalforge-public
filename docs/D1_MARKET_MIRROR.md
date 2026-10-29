# D1 market-data mirror

A private, read-mostly copy of three warehouse tables (`bars`, `series`,
`corporate_actions`) in the Cloudflare D1 database `signalforge-market-data`.
The local DuckDB warehouse stays the source of truth; D1 is a mirror.

## Privacy rule

The bars come from Tiingo and the Tiingo terms forbid redistribution. This
database stays private. No Worker, binding, or public route may ever read it.
The tool refuses any D1 database name other than `signalforge-market-data`.

## Auth

Wrangler's OAuth login is used. The leaked `CLOUDFLARE_API_TOKEN` (and
`CLOUDFLARE_API_KEY`, `CLOUDFLARE_EMAIL`) are removed from the subprocess
environment, and `CLOUDFLARE_ACCOUNT_ID` is set to the company account. The
tool never mutates its own environment and never prints environment values.

## How to run

All commands run from the repo root; the warehouse is opened read-only, so the
collector must not be writing at that moment (otherwise exit 2).

```bash
python -m signalforge.mirror_cli init                      # once, idempotent
python -m signalforge.mirror_cli sync --dry-run            # plan only: files, rows, bytes
python -m signalforge.mirror_cli sync --max-files 1        # pilot: first file only
python -m signalforge.mirror_cli load                      # full load (alias of sync)
python -m signalforge.mirror_cli verify                    # counts, per-group sums, 200-row sample
python -m signalforge.mirror_cli sync                      # daily, after the collector finishes
```

Options for `sync` and `load`: `--db PATH`, `--dry-run`, `--max-files N`,
`--max-deletes N` (default 100), `--rows-per-file N` (default 50,000).
`verify` takes `--sample N` (default 200 rows per table: random rows plus the
table's latest dates).

Exit codes: 0 ok, 1 failed postcondition / verify mismatch / wrangler failure
(including a timeout: 180 s per query, 900 s per file import), 2 refusal
(guard, version mismatch, missing or locked warehouse), 3 incomplete (stopped by
`--max-files`; rerun `sync` to continue). `--max-files` must be at least 1.

## How sync works

Each mirrored row carries `row_hash`, the first 4 bytes of a sha256 over a
canonical row text (hash version `sfm1`). Sync compares one GROUP BY per table
on each side, grouped by (symbol or series_id, source, year): count,
sum(row_hash), min and max date, and exact per-column value sums computed from
the stored text (not from `row_hash`). Level 1 is paged by key range (about 250,000 rows per page, cut from the
sorted local keys) so each page uses the primary key; one extra `NOT BETWEEN`
query filters the whole table but aggregates only keys that exist only on D1. Only differing groups
are examined row by row (level 2), and many groups are fetched per query
(grouped by source and year, packed into `IN (...)` under the 7,000 character
command limit and about 40,000 estimated rows per response), so a daily sync
makes a handful of wrangler calls, not one per symbol. Sums travel as text
because D1 JSON numbers are exact only to 2^53. Rows are sent as upserts, remote-only rows are deleted, and the SQL
files in `data/mirror/` are deleted after a successful import. There is no
local state: rerunning `sync` is the recovery path after any failure.

## Guards (sync refuses, writes nothing)

- any local table has 0 rows;
- a local table has fewer than 50% of the rows on D1;
- planned deletes exceed `--max-deletes`;
- `schema_version` or `hash_version` on D1 differs from the code.

After applying, level 1 is re-run and any mismatch exits 1. That check sees
count, row-hash sums, min and max date, and the value sums of the micro-summed
columns only (open, high, low, close, adj_close and volume on bars; value on
series; ratio and cash_amount on corporate_actions). An edit on D1 to any other
column (quality_status, source_reference, retrieved_at, adjustment_method,
volume_known) that leaves `row_hash` alone is NOT seen by the post-check; it is
caught only when the row hash itself differs, or by `verify`'s sample.
Every clean sync, including one with nothing to write, stamps `last_sync_at`
in `mirror_state`.

## Cost

About 1.2 to 1.6 GB on D1. The account is already above the 5 GB included D1
storage, so expect roughly $1 to $1.20 per month in storage. The full load
writes about 5 million rows once; a daily sync writes a few thousand.

## Recovery

Rerun `sync`. An interrupted import leaves some rows applied; upserts are
idempotent and the diff is recomputed from scratch. Stale files in
`data/mirror/` are cleared at the start of every run.

## Known limits

- A value changed on D1 while its `row_hash` stays intact is found by `verify`
  and by the post-sync check (for the micro-summed columns); sync resends the
  whole group only when the group sums disagree and no row hash differs.
- The micro-sums are plain SQLite `sum()` of scaled integers. They are bounded
  by group size (about 366 rows); SQLite raises on int64 overflow, so an
  absurd price would fail loudly rather than pass.
