"""Two-level diff sync and verification of the local warehouse against D1.

Level 1 compares one GROUP BY per table on each side, grouped by
(key, source, year). Level 2 looks only inside the groups that differ. The
whole plan is computed before any write; the write is a series of upsert and
delete files; afterwards level 1 is re-run and must match.

Micro-sums are plain SQLite sum() of the scaled integer of each value column.
They are exact, do not trust row_hash, and are bounded by group size (about
366 rows): a sum could only overflow int64 for prices above 2.5e10, and
SQLite raises on integer overflow rather than wrapping, so the failure is loud.
The mod operator is not usable because the cmd.exe guard bans the percent sign.
"""

from __future__ import annotations

import math
from collections import defaultdict
import random
import time
from collections import namedtuple
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from signalforge.mirror import codec
from signalforge.mirror.codec import SPECS, TableSpec

DEFAULT_OUT_DIR = Path("data") / "mirror"
FETCH_ROWS = 50_000
QUERY_BUDGET = 6_000
SAMPLE_LATEST = 10
SUMMARY_PAGE_ROWS = 250_000
LEVEL2_ROW_CAP = 40_000

Stat = namedtuple("Stat", "n hsum lo hi micro")


class MirrorError(RuntimeError):
    """A guard refused the run; nothing was written."""


# ---- version state -------------------------------------------------------

def _remote_state(d1) -> dict | None:
    rows = d1.query("SELECT name FROM sqlite_master WHERE type='table' AND name='mirror_state'")
    if not rows:
        return None
    return {r["key"]: r["value"] for r in d1.query("SELECT key, value FROM mirror_state")}


def _expected_versions():
    return (("schema_version", codec.SCHEMA_VERSION), ("hash_version", codec.HASH_VERSION))


def check_versions(d1) -> None:
    state = _remote_state(d1)
    if state is None:
        raise MirrorError("the D1 mirror is not initialised; run: python -m signalforge.mirror_cli init")
    for key, want in _expected_versions():
        if state.get(key) != want:
            raise MirrorError(f"{key} on D1 is {state.get(key)!r} but this code needs {want!r}")


def run_init(d1, out_dir=DEFAULT_OUT_DIR, say=print) -> None:
    state = _remote_state(d1)
    for key, want in _expected_versions():
        if state is not None and key in state and state[key] != want:
            raise MirrorError(f"{key} on D1 is {state[key]!r} but this code needs {want!r}; refusing to init")
    _apply_text(d1, Path(out_dir), "init.sql", "\n".join(codec.schema_statements()) + "\n")
    say("init: schema and versions present on D1")


def _apply_text(d1, out_dir: Path, name: str, text: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    path.write_text(text, encoding="utf-8", newline="\n")
    try:
        d1.execute_file(path)
    finally:
        path.unlink(missing_ok=True)


# ---- level 1 -------------------------------------------------------------

def _micro_terms(spec: TableSpec) -> list[str]:
    terms = []
    for col in spec.micro:
        if spec.kind(col) == "price":
            terms.append(f"coalesce(CAST(replace({col},'.','') AS INTEGER),0)")
        else:
            terms.append(f"coalesce({col},0)")
    return terms


def _micro_value(kind: str, value) -> int:
    if value is None:
        return 0
    return int(value.replace(".", "")) if kind == "price" else int(value)


def remote_summary_sql(spec: TableSpec, where: str = "") -> str:
    # Sums come back as TEXT: D1 JSON numbers are doubles, exact only to 2**53.
    sums = ", ".join(f"CAST(sum({t}) AS TEXT) AS m{i}" for i, t in enumerate(_micro_terms(spec)))
    d, k = spec.date_col, spec.key_col
    return (
        f"SELECT {k} AS k, source AS s, substr({d},1,4) AS y, count(*) AS n, "
        f"CAST(sum(row_hash) AS TEXT) AS h, min({d}) AS lo, max({d}) AS hi, {sums} "
        f"FROM {spec.name}{where} GROUP BY {k}, source, substr({d},1,4)"
    )


def page_ranges(local: dict, target_rows: int):
    """Inclusive (first_key, last_key) ranges of local keys, about target_rows each.

    None means one page (no WHERE at all)."""
    per_key: dict = defaultdict(int)
    for (k, _s, _y), stat in local.items():
        per_key[k] += stat.n
    pages, start, acc = [], None, 0
    keys = sorted(per_key)
    for k in keys:
        start = k if start is None else start
        acc += per_key[k]
        if acc >= target_rows:
            pages.append((start, k))
            start, acc = None, 0
    if start is not None:
        pages.append((start, keys[-1]))
    return pages if len(pages) > 1 else None


def _page_filters(spec: TableSpec, pages) -> list[str]:
    """One filter per page, plus one complement filter so remote-only keys outside
    every page (before the first, between, after the last) are still seen. The
    cmd.exe guard bans < and >, so the complement is NOT BETWEEN."""
    if not pages:
        return [""]
    k, lit = spec.key_col, codec.sql_literal
    filters = [f" WHERE {k} BETWEEN {lit(a)} AND {lit(b)}" for a, b in pages]
    rest = " AND ".join(f"{k} NOT BETWEEN {lit(a)} AND {lit(b)}" for a, b in pages)
    return filters + [" WHERE " + rest]


def remote_summary(d1, spec: TableSpec, pages=None) -> dict:
    out = {}
    for where in _page_filters(spec, pages):
        for r in d1.query(remote_summary_sql(spec, where)):
            micro = tuple(int(r[f"m{i}"]) for i in range(len(spec.micro)))
            out[(r["k"], r["s"], r["y"])] = Stat(int(r["n"]), int(r["h"]), r["lo"], r["hi"], micro)
    return out


def _stream(wh, spec: TableSpec, where: str = "", params=()):
    cols = ", ".join(spec.names)
    cur = wh.con.execute(
        f"SELECT {cols} FROM {spec.name}{where} ORDER BY {', '.join(spec.pk)}", list(params)
    )
    while True:
        rows = cur.fetchmany(FETCH_ROWS)
        if not rows:
            return
        for raw in rows:
            yield codec.prepare_row(spec, raw)


def _group_of(spec: TableSpec, prepared: tuple) -> tuple:
    names = spec.names
    return (
        prepared[names.index(spec.key_col)],
        prepared[names.index("source")],
        prepared[names.index(spec.date_col)][:4],
    )


def local_summary(wh, spec: TableSpec) -> dict:
    d_i = spec.names.index(spec.date_col)
    micro = [(spec.names.index(c), spec.kind(c)) for c in spec.micro]
    acc: dict = {}
    for p in _stream(wh, spec):
        g = _group_of(spec, p)
        d = p[d_i]
        s = acc.get(g)
        if s is None:
            s = acc[g] = [0, 0, d, d, [0] * len(micro)]
        s[0] += 1
        s[1] += p[-1]
        s[2] = min(s[2], d)
        s[3] = max(s[3], d)
        for j, (i, kind) in enumerate(micro):
            s[4][j] += _micro_value(kind, p[i])
    return {g: Stat(s[0], s[1], s[2], s[3], tuple(s[4])) for g, s in acc.items()}


def compare_stats(table: str, local: dict, remote: dict) -> list[str]:
    out = []
    for g in sorted(set(local) | set(remote)):
        a, b = local.get(g), remote.get(g)
        if b is None:
            out.append(f"[missing group] {table} {g}: on warehouse ({a.n} rows) but not on D1")
        elif a is None:
            out.append(f"[extra group] {table} {g}: on D1 ({b.n} rows) but not in the warehouse")
        else:
            if a.n != b.n:
                out.append(f"[count] {table} {g}: warehouse {a.n} D1 {b.n}")
            if a.hsum != b.hsum:
                out.append(f"[hash] {table} {g}: sum(row_hash) warehouse {a.hsum} D1 {b.hsum}")
            if (a.lo, a.hi) != (b.lo, b.hi):
                out.append(f"[minmax] {table} {g}: warehouse {a.lo}..{a.hi} D1 {b.lo}..{b.hi}")
            if a.micro != b.micro:
                out.append(f"[value] {table} {g}: value sums warehouse {a.micro} D1 {b.micro}")
    return out


# ---- level 2 and the plan ------------------------------------------------

@dataclass
class TablePlan:
    spec: TableSpec
    whole: list = field(default_factory=list)
    partial: dict = field(default_factory=dict)
    deletes: list = field(default_factory=list)
    upserts: int = 0


@dataclass
class Plan:
    tables: list
    local: dict
    pages: dict = field(default_factory=dict)
    remote: dict = field(default_factory=dict)

    @property
    def upserts(self) -> int:
        return sum(t.upserts for t in self.tables)

    @property
    def deletes(self) -> int:
        return sum(len(t.deletes) for t in self.tables)


def _group_where(spec: TableSpec) -> str:
    return f" WHERE {spec.key_col} = ? AND source = ? AND year({spec.date_col}) = ?"


def local_group_rows(wh, spec: TableSpec, group: tuple):
    k, s, y = group
    return _stream(wh, spec, _group_where(spec), (k, s, int(y)))


def _remote_hashes(d1, spec: TableSpec, estimates: dict) -> dict:
    """(pk -> row_hash) per group, many groups per query.

    Groups are batched by (source, year); keys are packed into IN (...) up to the
    command budget and an estimated-row cap so each response stays modest."""
    out = {g: {} for g in estimates}
    by_year: dict = defaultdict(list)
    for (k, s, y), est in sorted(estimates.items()):
        by_year[(s, y)].append((k, est))
    key_i, date_i = spec.pk.index(spec.key_col), spec.pk.index(spec.date_col)
    src_i = spec.pk.index("source")

    def flush(head, lits):
        for r in d1.query(head + ",".join(lits) + ")"):
            pk = tuple(r[c] for c in spec.pk)
            out[(pk[key_i], pk[src_i], pk[date_i][:4])][pk] = r["row_hash"]

    for (s, y), items in sorted(by_year.items()):
        head = (
            # Same year predicate as level 1, so every row a group counts is fetched.
            f"SELECT {', '.join(spec.pk)}, row_hash FROM {spec.name} WHERE "
            f"substr({spec.date_col},1,4) = {codec.sql_literal(y)} AND source = {codec.sql_literal(s)} "
            f"AND {spec.key_col} IN ("
        )
        lits, size, rows = [], len(head) + 1, 0
        for k, est in items:
            lit = codec.sql_literal(k)
            if lits and (size + len(lit) + 1 > QUERY_BUDGET or rows + est > LEVEL2_ROW_CAP):
                flush(head, lits)
                lits, size, rows = [], len(head) + 1, 0
            lits.append(lit)
            size += len(lit) + 1
            rows += est
        if lits:
            flush(head, lits)
    return out


def _apply_level2(wh, tp: TablePlan, group: tuple, in_local: bool, remote: dict) -> None:
    spec = tp.spec
    local = {codec.pk_of(spec, p): p[-1] for p in local_group_rows(wh, spec, group)} if in_local else {}
    changed = {pk for pk, h in local.items() if remote.get(pk) != h}
    gone = sorted(pk for pk in remote if pk not in local)
    tp.deletes.extend(gone)
    if in_local and not changed and not gone:
        tp.whole.append(group)  # sums disagree but every row hash agrees: resend the group
        tp.upserts += len(local)
    elif changed:
        tp.partial[group] = changed
        tp.upserts += len(changed)


def _plan_table(wh, d1, spec: TableSpec, local: dict, remote: dict) -> TablePlan:
    tp = TablePlan(spec)
    differing = []
    for g in sorted(local):
        if g not in remote:
            tp.whole.append(g)
            tp.upserts += local[g].n
        elif local[g] != remote[g]:
            differing.append((g, True))
    differing += [(g, False) for g in sorted(set(remote) - set(local))]
    estimates = {g: max(local[g].n if g in local else 0, remote[g].n if g in remote else 0)
                 for g, _ in differing}
    hashes = _remote_hashes(d1, spec, estimates) if differing else {}
    for g, in_local in differing:
        _apply_level2(wh, tp, g, in_local, hashes[g])
    tp.whole.sort()
    return tp


def build_plan(wh, d1, max_deletes: int, page_rows: int = SUMMARY_PAGE_ROWS) -> Plan:
    for spec in SPECS:
        if wh.count(spec.name) == 0:
            raise MirrorError(f"local table {spec.name} has 0 rows; refusing to sync")
    tables, locals_, pages, remotes = [], {}, {}, {}
    for spec in SPECS:
        local = locals_[spec.name] = local_summary(wh, spec)
        pages[spec.name] = page_ranges(local, page_rows)
        remote = remotes[spec.name] = remote_summary(d1, spec, pages[spec.name])
        n_local = sum(s.n for s in local.values())
        n_remote = sum(s.n for s in remote.values())
        if n_local * 2 < n_remote:
            raise MirrorError(
                f"local {spec.name} has {n_local} rows, under 50% of the {n_remote} on D1; refusing"
            )
        tables.append(_plan_table(wh, d1, spec, local, remote))
    plan = Plan(tables, locals_, pages, remotes)
    if plan.deletes > max_deletes:
        raise MirrorError(f"{plan.deletes} planned deletes exceeds --max-deletes {max_deletes}; nothing written")
    return plan


def iter_items(wh, plan: Plan):
    for tp in plan.tables:
        spec = tp.spec
        for g in sorted(set(tp.whole) | set(tp.partial)):
            only = tp.partial.get(g)
            for p in local_group_rows(wh, spec, g):
                if only is None or codec.pk_of(spec, p) in only:
                    yield ("u", spec, p)
    for tp in plan.tables:
        for pk in sorted(tp.deletes):
            yield ("d", tp.spec, pk)


# ---- sync ----------------------------------------------------------------

@dataclass
class SyncResult:
    files_total: int = 0
    files_done: int = 0
    rows: int = 0
    bytes: int = 0
    upserts: int = 0
    deletes: int = 0
    complete: bool = False
    mismatches: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches


def _clear_stale(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.glob("*.sql"):
        f.unlink()


def postcheck(d1, plan: Plan) -> list[str]:
    out = []
    for spec in SPECS:
        out += compare_stats(
            spec.name, plan.local[spec.name], remote_summary(d1, spec, plan.pages[spec.name])
        )
    return out


def _stamp(d1, out_dir: Path, plan: Plan, now) -> None:
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    state = [
        codec.state_upsert("last_sync_at", stamp, replace=True),
        codec.state_upsert("last_sync_upserts", str(plan.upserts), replace=True),
        codec.state_upsert("last_sync_deletes", str(plan.deletes), replace=True),
    ]
    _apply_text(d1, out_dir, "state.sql", chr(10).join(state) + chr(10))


def run_sync(
    wh, d1, *, dry_run=False, max_files=None, max_deletes=100, rows_per_file=codec.ROWS_PER_FILE,
    out_dir=DEFAULT_OUT_DIR, say=print, clock=time.monotonic, now=None,
    summary_page_rows=SUMMARY_PAGE_ROWS,
) -> SyncResult:
    out_dir = Path(out_dir)
    check_versions(d1)
    plan = build_plan(wh, d1, max_deletes, summary_page_rows)
    total = plan.upserts + plan.deletes
    result = SyncResult(
        files_total=math.ceil(total / rows_per_file), upserts=plan.upserts, deletes=plan.deletes
    )
    if dry_run:
        for text, n in codec.render_files(iter_items(wh, plan), rows_per_file):
            result.rows += n
            result.bytes += len(text.encode("utf-8"))
        say(
            f"dry run: {result.files_total} files, {result.rows} rows ({plan.upserts} upserts, "
            f"{plan.deletes} deletes), {result.bytes} bytes; nothing written"
        )
        return result
    if total == 0:
        say("up to date: nothing to write")
        result.complete = True
        result.mismatches = [
            m for spec in SPECS
            for m in compare_stats(spec.name, plan.local[spec.name], plan.remote[spec.name])
        ]
        if result.ok:
            _stamp(d1, out_dir, plan, now)
        return result
    _clear_stale(out_dir)
    start = clock()
    for i, (text, n) in enumerate(codec.render_files(iter_items(wh, plan), rows_per_file), 1):
        size = len(text.encode("utf-8"))
        _apply_text(d1, out_dir, f"mirror_{i:05d}.sql", text)
        result.files_done, result.rows, result.bytes = i, result.rows + n, result.bytes + size
        say(f"file {i}/{result.files_total} rows={n} bytes={size} elapsed={clock() - start:.1f}s")
        if max_files is not None and i >= max_files and i < result.files_total:
            say(f"stopped after {i} files (--max-files); rerun sync to continue")
            return result
    result.complete = True
    result.mismatches = postcheck(d1, plan)
    if result.ok:
        _stamp(d1, out_dir, plan, now)
    return result


# ---- verify --------------------------------------------------------------

@dataclass
class VerifyReport:
    mismatches: list = field(default_factory=list)
    local_total: int = 0
    remote_total: int = 0
    sampled: int = 0

    @property
    def ok(self) -> bool:
        return not self.mismatches


def _sample_local(wh, spec: TableSpec, n: int, seed: int) -> dict:
    cols = ", ".join(spec.names)
    queries = [
        f"SELECT {cols} FROM {spec.name} USING SAMPLE reservoir({n} ROWS) REPEATABLE ({seed})",
        f"SELECT {cols} FROM {spec.name} ORDER BY {spec.date_col} DESC, {', '.join(spec.pk)} "
        f"LIMIT {min(n, SAMPLE_LATEST)}",
    ]
    out = {}
    for q in queries:
        for raw in wh.con.execute(q).fetchall():
            p = codec.prepare_row(spec, raw)
            out[codec.pk_of(spec, p)] = p
    return out


def _remote_rows(d1, spec: TableSpec, pks) -> dict:
    cols = ", ".join(spec.names + ("row_hash",))
    head = f"SELECT {cols} FROM {spec.name} WHERE ({', '.join(spec.pk)}) IN (VALUES "
    found, chunk, size = {}, [], len(head)

    def flush():
        for r in d1.query(head + ",".join(chunk) + ")"):
            found[tuple(r[c] for c in spec.pk)] = r

    for pk in pks:
        tup = "(" + ",".join(codec.sql_literal(v) for v in pk) + ")"
        if chunk and size + len(tup) + 1 > QUERY_BUDGET:
            flush()
            chunk, size = [], len(head)
        chunk.append(tup)
        size += len(tup) + 1
    if chunk:
        flush()
    return found


def _check_sample(d1, spec: TableSpec, sample: dict) -> list[str]:
    out = []
    remote = _remote_rows(d1, spec, sorted(sample))
    for pk in sorted(sample):
        local, r = sample[pk], remote.get(pk)
        if r is None:
            out.append(f"[sample] {spec.name} {pk}: missing on D1")
            continue
        canon = tuple(r[c] for c in spec.names)
        if canon != local[:-1]:
            cols = [c for c, a, b in zip(spec.names, canon, local) if a != b]
            out.append(f"[sample] {spec.name} {pk}: values differ in {cols}")
        elif r["row_hash"] != codec.row_hash(spec, canon):
            out.append(f"[sample] {spec.name} {pk}: stored row_hash does not match its own values")
    return out


def run_verify(
    wh, d1, *, sample=200, seed=None, say=print, summary_page_rows=SUMMARY_PAGE_ROWS
) -> VerifyReport:
    check_versions(d1)
    report = VerifyReport()
    seed = random.SystemRandom().randrange(2**31) if seed is None else seed
    for spec in SPECS:
        local = local_summary(wh, spec)
        remote = remote_summary(d1, spec, page_ranges(local, summary_page_rows))
        n_l, n_r = sum(s.n for s in local.values()), sum(s.n for s in remote.values())
        say(f"{spec.name}: warehouse {n_l} D1 {n_r}")
        report.local_total += n_l
        report.remote_total += n_r
        if n_l != n_r:
            report.mismatches.append(f"[total] {spec.name}: warehouse {n_l} D1 {n_r}")
        report.mismatches += compare_stats(spec.name, local, remote)
        if sample > 0:
            rows = _sample_local(wh, spec, sample, seed)
            report.sampled += len(rows)
            report.mismatches += _check_sample(d1, spec, rows)
    say(f"total: warehouse {report.local_total} D1 {report.remote_total}; sampled {report.sampled} rows")
    return report
