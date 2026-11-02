// signalforge-research-api: signed, schema-checked ingest of PAPER-ONLY research reports.
//
// Single-file source of the deployed Worker (it was bundled from src/validation.js,
// src/auth.js and src/index.js), with one addition: the forecast_trial report type
// that feeds the dashboard's Crucible tab. Everything else is unchanged from the
// deployed version. forecast_trial carries scores and a forward cone in fractional
// returns only: no price levels ever reach this Worker or D1.
//
// Bindings this code expects (already configured on the deployed Worker):
//   DB                          D1 database signalforge-research
//   SIGNALFORGE_INGEST_SECRET   HMAC secret shared with the trading computer
//   SIGNALFORGE_READ_TOKEN      bearer token for the dashboard's reads
// Deploy only AFTER migrations/0002_allow_forecast_trial.sql has run.

// ---------------------------------------------------------------- validation
const EVENT_TYPES = new Set(["heartbeat", "paper_session", "backtest", "risk_event", "data_health", "experiment", "validation_failure", "forecast_trial"]);
const REJECTION_REASONS = ["overfit", "insufficient_trades", "drawdown_limit", "underperforms_baseline", "data_quality", "risk_rule_violation", "lookahead_bias", "cost_sensitivity", "unstable_parameters", "manual_review"];
const ISO = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,3})?Z$/;
const SLUG = /^[a-z0-9][a-z0-9._-]{0,63}$/;
const SYMBOL = /^[A-Z]{1,5}(?:\.[A-Z])?$/;
const plain = (x) => x !== null && typeof x === "object" && !Array.isArray(x);
const T = {
  iso: { check: (x) => typeof x === "string" && ISO.test(x) && Number.isFinite(Date.parse(x)) },
  slug: { check: (x) => typeof x === "string" && SLUG.test(x) },
  sha: { check: (x) => typeof x === "string" && /^[0-9a-f]{7,40}$/.test(x) },
  num: { check: (x) => typeof x === "number" && Number.isFinite(x) && Math.abs(x) < 1e12 },
  nonneg: { check: (x) => typeof x === "number" && Number.isFinite(x) && x >= 0 && x < 1e12 },
  pct: { check: (x) => typeof x === "number" && Number.isFinite(x) && x >= -1e3 && x <= 1e5 },
  dd: { check: (x) => typeof x === "number" && Number.isFinite(x) && x >= 0 && x <= 100 },
  int: { check: (x) => Number.isSafeInteger(x) && x >= 0 },
  bool: { check: (x) => typeof x === "boolean" },
  enumOf: (...v) => ({ check: (x) => v.includes(x) }),
  slugList: (max) => ({ check: (x) => Array.isArray(x) && x.length <= max && x.every((s) => typeof s === "string" && SLUG.test(s)) && new Set(x).size === x.length })
};
const R = (t) => ({ ...t, req: true });
const POSITION = { symbol: R({ check: (x) => typeof x === "string" && SYMBOL.test(x) }), qty: R(T.int), avg_cost: R(T.nonneg), market_value: R(T.nonneg) };
const positions = { check: (x) => Array.isArray(x) && x.length <= 100 && x.every((p) => plain(p) && strict(p, POSITION) === null) };

// forecast_trial building blocks. Fractions are 0..1; returns are relative to the
// forecast origin's close (-1..10), so no price level can be sent.
const isFrac = (x) => typeof x === "number" && Number.isFinite(x) && x >= 0 && x <= 1;
const isRet = (x) => typeof x === "number" && Number.isFinite(x) && x >= -1 && x <= 10;
const isNonneg = (x) => T.nonneg.check(x);
const FRAC = { check: isFrac };
const listOf = (min, max, pred) => ({ check: (x) => Array.isArray(x) && x.length >= min && x.length <= max && x.every(pred) });
const objOf = (schema) => (x) => plain(x) && strict(x, schema) === null;
const BUCKET = { label: R({ check: (x) => typeof x === "string" && /^\d{1,3}-\d{1,3}$/.test(x) }), n: R(T.int), hit_rate: FRAC, lo: FRAC, hi: FRAC };
const FORECASTER = {
  id: R(T.slug), n: R(T.int), n_independent: R(T.int),
  coverage: R(FRAC), coverage_lo: R(FRAC), coverage_hi: R(FRAC),
  mean_crps: R({ check: (x) => isNonneg(x) && x < 10 }),
  hit_rate: R(FRAC), hit_lo: R(FRAC), hit_hi: R(FRAC), base_rate_up: R(FRAC),
  trending_share: R(FRAC), trending_n: R(T.int), trending_hit: FRAC, choppy_n: T.int, choppy_hit: FRAC,
  crps_skill: { check: (x) => typeof x === "number" && Number.isFinite(x) && Math.abs(x) <= 10 },
  pit: R(listOf(10, 10, isFrac)), agreement: R(listOf(0, 6, objOf(BUCKET)))
};
const CONE = {
  symbol: R({ check: (x) => typeof x === "string" && SYMBOL.test(x) }), as_of: R(T.iso), forecaster: R(T.slug),
  horizon: R(T.int), p10: R(listOf(1, 252, isRet)), p50: R(listOf(1, 252, isRet)), p90: R(listOf(1, 252, isRet)),
  paths: R(listOf(0, 60, (p) => Array.isArray(p) && p.length >= 1 && p.length <= 252 && p.every(isRet))),
  paths_up: R(T.int), n_paths: R(T.int), regime: R(T.enumOf("TRENDING", "CHOPPY")), direction: R(T.enumOf("UP", "DOWN"))
};
const BACKTEST = {
  rule_id: R(T.slug), period_start: R(T.iso), period_end: R(T.iso),
  hold_final: R(T.nonneg), rule_final: R(T.nonneg), hold_sharpe: T.num, rule_sharpe: T.num,
  hold_equity: R(listOf(2, 200, isNonneg)), rule_equity: R(listOf(2, 200, isNonneg))
};

const SCHEMAS = {
  heartbeat: { status: R(T.enumOf("running", "idle", "stopped", "error")), engine_version: T.slug, uptime_s: T.int, active_strategies: T.slugList(50), risk_checks_ok: T.bool },
  paper_session: { strategy_id: R(T.slug), as_of: R(T.iso), equity: R(T.nonneg), cash: R(T.nonneg), realized_pnl: R(T.num), unrealized_pnl: R(T.num), max_drawdown_pct: T.dd, trades: T.int, positions },
  backtest: { strategy_id: R(T.slug), dataset_version: R(T.slug), commit_sha: R(T.sha), return_pct: R(T.pct), max_drawdown_pct: R(T.dd), sharpe: T.num, trades: R(T.int), period_start: R(T.iso), period_end: R(T.iso), status: R(T.enumOf("candidate", "rejected", "verified")), baseline_strategy_id: T.slug, rejection_reason: T.enumOf(...REJECTION_REASONS) },
  experiment: { strategy_id: R(T.slug), status: R(T.enumOf("proposed", "tested", "rejected", "awaiting_approval")), hypothesis_code: T.slug, parent_strategy_id: T.slug, metric: T.enumOf("return_pct", "sharpe", "max_drawdown_pct", "win_rate"), metric_delta: T.num, rejection_reason: T.enumOf(...REJECTION_REASONS) },
  risk_event: { rule: R(T.slug), decision: R(T.enumOf("allow", "block", "halt", "warning")), strategy_id: T.slug, value: T.num, limit: T.num },
  data_health: { dataset_version: R(T.slug), rows: R(T.int), last_observation_at: R(T.iso), stale: R(T.bool) },
  validation_failure: { check: R(T.slug), severity: R(T.enumOf("info", "warning", "error")), strategy_id: T.slug, count: R(T.int) },
  forecast_trial: {
    trial_id: R(T.slug), commit_sha: R(T.sha), window_start: R(T.iso), window_end: R(T.iso),
    symbols: R({ check: (x) => Array.isArray(x) && x.length >= 1 && x.length <= 50 && x.every((s) => typeof s === "string" && SYMBOL.test(s)) && new Set(x).size === x.length }),
    horizon: R({ check: (x) => Number.isSafeInteger(x) && x >= 1 && x <= 252 }),
    n_paths: R({ check: (x) => Number.isSafeInteger(x) && x >= 2 && x <= 10000 }),
    trend_agreement: R({ check: (x) => typeof x === "number" && x > 0.5 && x <= 1 }),
    forecasters: R(listOf(1, 5, objOf(FORECASTER))),
    cone: { check: objOf(CONE) },
    backtest: { check: objOf(BACKTEST) }
  }
};

function strict(obj, schema) {
  for (const k of Object.keys(obj)) if (!Object.hasOwn(schema, k)) return `Unknown field: ${k.slice(0, 40).replace(/[^\w.-]/g, "?")}`;
  for (const [k, s] of Object.entries(schema)) {
    if (!Object.hasOwn(obj, k)) {
      if (s.req) return `Missing field: ${k}`;
      continue;
    }
    if (!s.check(obj[k])) return `Invalid field: ${k}`;
  }
  return null;
}

const SECRET_PATTERNS = [/\bsk-[A-Za-z0-9_-]{12,}/, /\bgh[pousr]_[A-Za-z0-9]{20,}/, /\bgithub_pat_[A-Za-z0-9_]{20,}/, /\bA(?:KIA|SIA)[0-9A-Z]{16}\b/, /-----BEGIN [A-Z ]*PRIVATE KEY-----/, /\bBearer\s+\S{12,}/i, /\bxox[abprs]-[A-Za-z0-9-]{10,}/, /\bAIza[0-9A-Za-z_-]{30,}/, /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\./];
function entropy(t) {
  const f = {};
  for (const c of t) f[c] = (f[c] || 0) + 1;
  return Object.values(f).reduce((a, n) => a - n / t.length * Math.log2(n / t.length), 0);
}
function looksSecret(str) {
  if (typeof str !== "string") return false;
  if (SECRET_PATTERNS.some((r) => r.test(str))) return true;
  for (const t of str.split(/[\s"',;:=()\[\]{}<>]+/)) {
    if (t.length < 32) continue;
    if (/^[0-9a-f]+$/i.test(t)) {
      if (t.length >= 48) return true;
      continue;
    }
    if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(t)) continue;
    if (/[A-Z]/.test(t) && /[a-z]/.test(t) && /[0-9]/.test(t) && entropy(t) >= 4) return true;
  }
  return false;
}
function anySecret(v, d = 0) {
  if (d > 6) return true;
  if (typeof v === "string") return looksSecret(v);
  if (Array.isArray(v)) return v.some((x) => anySecret(x, d + 1));
  if (v && typeof v === "object") return Object.entries(v).some(([k, x]) => looksSecret(k) || anySecret(x, d + 1));
  return false;
}
const ENVELOPE = { schema_version: R(T.enumOf(1)), paper_only: R({ check: (x) => x === true }), event_id: R({ check: (x) => typeof x === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(x) }), event_type: R({ check: (x) => EVENT_TYPES.has(x) }), run_id: R(T.slug), occurred_at: R(T.iso), payload: R({ check: plain }) };
function isTestRun(runId) {
  return typeof runId === "string" && /^(e2e-test|test|smoke)-/.test(runId);
}

function forecastTrialIssue(p) {
  if (!(p.window_start < p.window_end)) return "Invalid trial window";
  if (p.cone) {
    const c = p.cone, h = c.horizon;
    if (![c.p10, c.p50, c.p90].every((s) => s.length === h) || !c.paths.every((s) => s.length === h)) return "Invalid cone: series length must equal horizon";
    if (c.paths_up > c.n_paths) return "Invalid cone: paths_up exceeds n_paths";
  }
  if (p.backtest && !(p.backtest.period_start < p.backtest.period_end)) return "Invalid backtest period";
  for (const f of p.forecasters) if (f.coverage_lo > f.coverage_hi || f.hit_lo > f.hit_hi) return "Invalid interval";
  return null;
}

export function validateEnvelope(input) {
  if (!plain(input)) return "JSON object required";
  if (anySecret(input)) return "Secret-like value rejected";
  if (input.paper_only !== true) return "paper_only must be true";
  const e = strict(input, ENVELOPE);
  if (e) return e;
  if (Date.parse(input.occurred_at) > Date.now() + 3e5) return "Invalid field: occurred_at";
  const p = input.payload, t = input.event_type;
  const pe = strict(p, SCHEMAS[t]);
  if (pe) return pe;
  if (t === "paper_session" && p.cash > p.equity + 0.01) return "Invalid paper portfolio: cash exceeds equity";
  if (t === "backtest" && !(p.period_start < p.period_end)) return "Invalid backtest period";
  if (t === "backtest" && p.status === "rejected" && !p.rejection_reason) return "Missing field: rejection_reason";
  if (t === "experiment" && p.status === "rejected" && !p.rejection_reason) return "Missing field: rejection_reason";
  if (t === "experiment" && p.metric === void 0 !== (p.metric_delta === void 0)) return "metric and metric_delta must be sent together";
  if (t === "forecast_trial") return forecastTrialIssue(p);
  return null;
}

// ---------------------------------------------------------------- auth
const encoder = new TextEncoder();
const canonical = (timestamp, nonce, body) => `v1:${timestamp}:${nonce}:${body}`;
function fromHex(value) {
  if (typeof value !== "string" || !/^[0-9a-f]{64}$/i.test(value)) return null;
  return new Uint8Array(value.match(/../g).map((part) => parseInt(part, 16)));
}
async function verifyIngest(request, raw, secret, now = Date.now()) {
  if (typeof secret !== "string" || secret.length < 32) return { ok: false, reason: "server_not_configured" };
  const timestamp = request.headers.get("x-signalforge-timestamp");
  const nonce = request.headers.get("x-signalforge-nonce");
  const signature = request.headers.get("x-signalforge-signature");
  if (!/^\d{10,13}$/.test(timestamp || "") || !/^[a-f0-9-]{36}$/i.test(nonce || "") || !signature?.startsWith("v1=")) return { ok: false, reason: "auth_missing" };
  const ms = Number(timestamp) * (timestamp.length === 10 ? 1e3 : 1);
  if (!Number.isSafeInteger(ms) || Math.abs(now - ms) > 3e5) return { ok: false, reason: "timestamp_out_of_window" };
  const bytes = fromHex(signature.slice(3));
  if (!bytes) return { ok: false, reason: "bad_signature" };
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
  const ok = await crypto.subtle.verify("HMAC", key, bytes, encoder.encode(canonical(timestamp, nonce, raw)));
  return { ok, reason: ok ? "ok" : "bad_signature", nonce };
}
async function digestHex(raw) {
  const bytes = await crypto.subtle.digest("SHA-256", encoder.encode(raw));
  return [...new Uint8Array(bytes)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
function verifyReadToken(header, secret) {
  if (!secret || secret.length < 32 || !header?.startsWith("Bearer ")) return false;
  const candidate = encoder.encode(header.slice(7));
  const expected = encoder.encode(secret);
  if (candidate.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < expected.length; i++) diff |= candidate[i] ^ expected[i];
  return diff === 0;
}

// ---------------------------------------------------------------- routes
const MAX_BYTES = 65536;
const TYPES = [...EVENT_TYPES];
function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", "x-content-type-options": "nosniff" } });
}
const failure = (error, status) => json({ ok: false, error }, status);
async function readBounded(request) {
  const length = Number(request.headers.get("content-length"));
  if (Number.isFinite(length) && length > MAX_BYTES) return null;
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_BYTES) {
        await reader.cancel();
        return null;
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.length;
  }
  return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
}
async function ingest(request, env) {
  if (!env.DB || !env.SIGNALFORGE_INGEST_SECRET) return failure("service_unconfigured", 503);
  if (!request.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return failure("json_required", 415);
  let raw;
  try {
    raw = await readBounded(request);
  } catch {
    return failure("invalid_utf8", 400);
  }
  if (raw === null) return failure("body_too_large", 413);
  const auth = await verifyIngest(request, raw, env.SIGNALFORGE_INGEST_SECRET);
  if (!auth.ok) return failure("unauthorized", 401);
  let envelope;
  try {
    envelope = JSON.parse(raw);
  } catch {
    return failure("malformed_json", 400);
  }
  const issue = validateEnvelope(envelope);
  if (issue) return failure(issue, 422);
  const now = Math.floor(Date.now() / 1e3);
  const stmts = [
    env.DB.prepare("INSERT INTO ingest_nonces(nonce, received_unix) VALUES (?,?)").bind(auth.nonce, now),
    env.DB.prepare("INSERT INTO reports(event_id,schema_version,event_type,run_id,occurred_at,body_sha256,payload_json,is_test) VALUES (?,?,?,?,?,?,?,?)").bind(envelope.event_id, 1, envelope.event_type, envelope.run_id, envelope.occurred_at, await digestHex(raw), JSON.stringify(envelope.payload), isTestRun(envelope.run_id) ? 1 : 0)
  ];
  try {
    await env.DB.batch(stmts);
  } catch (err) {
    const msg = String(err?.message ?? err);
    if (/UNIQUE/i.test(msg) && /ingest_nonces/i.test(msg)) return failure("replayed_request", 409);
    if (/UNIQUE/i.test(msg) && /reports/i.test(msg)) return failure("duplicate_event", 409);
    if (/CHECK/i.test(msg) && /event_type/i.test(msg)) return failure("storage_rejects_type_run_migration_0002", 503);
    return failure("storage_error", 503);
  }
  return json({ ok: true, event_id: envelope.event_id, mode: "PAPER_ONLY", is_test: isTestRun(envelope.run_id) }, 202);
}
async function getSummary(env) {
  const results = {};
  for (const type of TYPES) {
    const row = await env.DB.prepare("SELECT event_id,run_id,occurred_at,received_at,payload_json FROM reports WHERE event_type=? AND is_test=0 ORDER BY occurred_at DESC LIMIT 1").bind(type).first();
    if (row) {
      results[type] = { event_id: row.event_id, run_id: row.run_id, occurred_at: row.occurred_at, received_at: row.received_at, payload: JSON.parse(row.payload_json) };
    }
  }
  return json({ ok: true, mode: "PAPER_ONLY", source: "ingested_reports_excluding_tests", latest: results });
}
async function getEvents(url, env) {
  const limit = Math.min(500, Math.max(1, Number.parseInt(url.searchParams.get("limit") ?? "20", 10) || 20));
  const includeTest = url.searchParams.get("include_test") === "1";
  const type = url.searchParams.get("type");
  if (type !== null && !EVENT_TYPES.has(type)) return failure("bad_type", 400);
  const where = [includeTest ? null : "is_test=0", type ? "event_type=?" : null].filter(Boolean);
  const sql = `SELECT event_id,event_type,run_id,occurred_at,received_at,payload_json,is_test FROM reports ${where.length ? "WHERE " + where.join(" AND ") : ""} ORDER BY received_at DESC,event_id DESC LIMIT ?`;
  const st = env.DB.prepare(sql);
  const result = await (type ? st.bind(type, limit) : st.bind(limit)).all();
  return json({ ok: true, mode: "PAPER_ONLY", include_test: includeTest, events: (result.results ?? []).map(({ payload_json, ...row }) => ({ ...row, is_test: !!row.is_test, payload: JSON.parse(payload_json) })) });
}

export default {
  async fetch(request, env) {
    try {
      const url = new URL(request.url);
      if (url.pathname === "/health" && request.method === "GET") return json({ ok: true, service: "signalforge-research-api", mode: "PAPER_ONLY" });
      if (url.pathname === "/v1/ingest" && request.method === "POST") return ingest(request, env);
      if (url.pathname === "/v1/summary" && request.method === "GET" || url.pathname === "/v1/events" && request.method === "GET") {
        if (!verifyReadToken(request.headers.get("authorization"), env.SIGNALFORGE_READ_TOKEN)) return failure("unauthorized", 401);
        if (!env.DB) return failure("service_unconfigured", 503);
        return url.pathname === "/v1/summary" ? getSummary(env) : getEvents(url, env);
      }
      return failure("not_found", 404);
    } catch {
      return failure("internal_error", 500);
    }
  },
  async scheduled(_event, env) {
    if (env.DB) await env.DB.prepare("DELETE FROM ingest_nonces WHERE received_unix < ?").bind(Math.floor(Date.now() / 1e3) - 7 * 86400).run();
  }
};
