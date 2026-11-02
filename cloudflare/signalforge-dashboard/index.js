// signalforge-dashboard: owner-only PAPER-ONLY dashboard behind Cloudflare Access.
//
// Single-file source of the deployed Worker (bundled from src/access.js and
// src/index.js), plus the Crucible tab at /crucible. The operator page at / is
// unchanged apart from the tab strip. Crucible renders the latest forecast_trial
// report: scores and a forward cone in percent moves. It never shows a price,
// because none is ever sent to the API.
//
// Bindings this code expects (already configured on the deployed Worker):
//   API                     service binding to signalforge-research-api
//   SIGNALFORGE_READ_TOKEN  bearer token for the API's read routes
//   ACCESS_TEAM_DOMAIN, ACCESS_AUD, OWNER_EMAIL   Cloudflare Access checks
//   STALE_MINUTES, SHEET_URL                       as before
// The page runs no script; its CSP allows inline styles only, and all motion is CSS.

// ---------------------------------------------------------------- access
const b64u = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(s.length / 4) * 4, "=")), (c) => c.charCodeAt(0));
let cache = { at: 0, keys: null, team: null };
export async function verifyAccess(request, env, fetcher = fetch) {
  const team = env.ACCESS_TEAM_DOMAIN, aud = env.ACCESS_AUD, owner = (env.OWNER_EMAIL || "").toLowerCase();
  if (!team || !aud || !owner) return { ok: false, reason: "access_not_configured" };
  const jwt = request.headers.get("cf-access-jwt-assertion");
  if (!jwt) return { ok: false, reason: "no_access_token" };
  const parts = jwt.split(".");
  if (parts.length !== 3) return { ok: false, reason: "bad_token" };
  let header, claims;
  try {
    header = JSON.parse(new TextDecoder().decode(b64u(parts[0])));
    claims = JSON.parse(new TextDecoder().decode(b64u(parts[1])));
  } catch {
    return { ok: false, reason: "bad_token" };
  }
  if (header.alg !== "RS256") return { ok: false, reason: "bad_alg" };
  if (!cache.keys || cache.team !== team || Date.now() - cache.at > 3600000) {
    const r = await fetcher(`https://${team}/cdn-cgi/access/certs`);
    if (!r.ok) return { ok: false, reason: "certs_unavailable" };
    cache = { at: Date.now(), keys: (await r.json()).keys || [], team };
  }
  const jwk = cache.keys.find((k) => k.kid === header.kid);
  if (!jwk) return { ok: false, reason: "unknown_kid" };
  const key = await crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
  const valid = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, b64u(parts[2]), new TextEncoder().encode(parts[0] + "." + parts[1]));
  if (!valid) return { ok: false, reason: "bad_signature" };
  const now = Date.now() / 1000;
  if (!(claims.exp > now)) return { ok: false, reason: "expired" };
  if (claims.iss !== `https://${team}`) return { ok: false, reason: "bad_issuer" };
  const auds = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  if (!auds.includes(aud)) return { ok: false, reason: "bad_audience" };
  if (String(claims.email || "").toLowerCase() !== owner) return { ok: false, reason: "not_owner" };
  return { ok: true, email: claims.email };
}
export const _resetCache = () => { cache = { at: 0, keys: null, team: null }; };

// ---------------------------------------------------------------- shared
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const HDR = { "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "x-frame-options": "DENY", "x-content-type-options": "nosniff", "referrer-policy": "no-referrer", "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" };
const TABS_CSS = ".tabs{display:flex;gap:4px;border-bottom:1px solid #222;margin:6px 0 4px}.tabs a{color:#889;text-decoration:none;padding:10px 14px;border-bottom:3px solid transparent;margin-bottom:-1px;font-weight:600}.tabs a[aria-current=page]{color:#e6eaf0;border-bottom-color:#ff8a3d}";
const tabs = (here) => `<nav class="tabs" aria-label="Pages">${[["/", "Operator"], ["/crucible", "Crucible"]].map(([href, label]) => `<a href="${href}"${href === here ? ' aria-current="page"' : ""}>${esc(label)}</a>`).join("")}</nav>`;
const page = (body, status = 200) => new Response(`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>SIGNALFORGE</title><style>body{font:14px system-ui;background:#0b0f14;color:#dde;margin:0;padding:20px;max-width:1100px;margin:auto}h1{font-size:20px}h2{font-size:15px;border-bottom:1px solid #333;padding-bottom:4px;margin-top:28px}.b{padding:10px;border-radius:6px;margin:8px 0}.warn{background:#4a3200;color:#ffd27a}.err{background:#4a0010;color:#ff9aa8}.ok{background:#073b1c;color:#9ff0b8}.empty{color:#889;font-style:italic}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #222;padding:4px 6px;text-align:left;font-size:12px}code{color:#9cf}.tag{display:inline-block;background:#223;padding:2px 6px;border-radius:4px;font-size:11px}${TABS_CSS}</style></head><body>${body}</body></html>`, { status, headers: HDR });
const kv = (o) => o && typeof o === "object" ? `<table>${Object.entries(o).map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(typeof v === "object" ? JSON.stringify(v) : v)}</td></tr>`).join("")}</table>` : "";
const ageMin = (t) => t ? (Date.now() - Date.parse(t)) / 60000 : Infinity;

async function api(env, path) {
  if (!env.API || !env.SIGNALFORGE_READ_TOKEN) return { error: "dashboard_unconfigured" };
  try {
    const r = await env.API.fetch(new Request("https://signalforge-api.internal" + path, { headers: { authorization: "Bearer " + env.SIGNALFORGE_READ_TOKEN } }));
    if (!r.ok) {
      let code = null;
      try { code = (await r.json()).error; } catch {}
      return { error: `api_http_${r.status}`, code };
    }
    return { data: await r.json() };
  } catch {
    return { error: "api_unreachable" };
  }
}

const tbl = (heads, rows) => rows.length ? `<table><tr>${heads.map((h) => `<th>${esc(h)}</th>`).join("")}</tr>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</table>` : "";
const money = (n) => typeof n === "number" ? (n < 0 ? "-" : "") + "$" + Math.abs(n).toLocaleString("en-US", { maximumFractionDigits: 2 }) : "—";
const num = (n, d = 2) => typeof n === "number" ? n.toFixed(d) : "—";
const recv = (e) => e.received_at.endsWith("Z") ? e.received_at : e.received_at + "Z";

// ---------------------------------------------------------------- operator page (unchanged)
export function model(events) {
  const ev = events.filter((e) => !e.is_test).sort((a, b) => recv(a) < recv(b) ? 1 : -1);
  const by = (t) => ev.filter((e) => e.event_type === t);
  const hb = by("heartbeat")[0] || null;
  const sessions = {};
  for (const e of by("paper_session").slice().reverse()) { (sessions[e.payload.strategy_id] ??= []).push(e); }
  const perf = Object.entries(sessions).map(([id, list]) => { const last = list.at(-1).payload, first = list[0].payload; return { id, as_of: last.as_of, equity: last.equity, cash: last.cash, pnl: last.realized_pnl + last.unrealized_pnl, realized: last.realized_pnl, unrealized: last.unrealized_pnl, dd: last.max_drawdown_pct, positions: (last.positions || []).length, change: list.length > 1 ? last.equity - first.equity : null, n: list.length }; });
  const testing = new Map();
  for (const e of by("experiment")) if (["proposed", "tested", "awaiting_approval"].includes(e.payload.status) && !testing.has(e.payload.strategy_id)) testing.set(e.payload.strategy_id, { id: e.payload.strategy_id, source: "experiment", status: e.payload.status, at: e.received_at });
  for (const e of by("backtest")) if (e.payload.status === "candidate" && !testing.has(e.payload.strategy_id)) testing.set(e.payload.strategy_id, { id: e.payload.strategy_id, source: "backtest", status: "candidate", at: e.received_at });
  if (hb?.payload.active_strategies) for (const id of hb.payload.active_strategies) if (!testing.has(id)) testing.set(id, { id, source: "heartbeat", status: "active", at: hb.received_at });
  const improving = [...by("experiment").filter((e) => typeof e.payload.metric_delta === "number").map((e) => ({ id: e.payload.strategy_id, parent: e.payload.parent_strategy_id, metric: e.payload.metric, delta: e.payload.metric_delta, better: e.payload.metric === "max_drawdown_pct" ? e.payload.metric_delta < 0 : e.payload.metric_delta > 0, status: e.payload.status })),
    ...perf.filter((p) => p.change !== null).map((p) => ({ id: p.id, parent: null, metric: "paper_equity", delta: p.change, better: p.change > 0, status: `${p.n} sessions` }))];
  const rejected = [...by("backtest"), ...by("experiment")].filter((e) => e.payload.status === "rejected").map((e) => ({ id: e.payload.strategy_id, kind: e.event_type, reason: e.payload.rejection_reason, at: e.received_at, ret: e.payload.return_pct, dd: e.payload.max_drawdown_pct }));
  const rules = {};
  for (const e of by("risk_event")) rules[e.payload.rule] ??= e;
  const vfail = by("validation_failure").slice(0, 20);
  const data = by("data_health")[0] || null;
  return { count: ev.length, hb, perf, testing: [...testing.values()], improving, rejected, rules: Object.values(rules), vfail, data };
}

export async function render(env, now = Date.now()) {
  const staleMin = Number(env.STALE_MINUTES) || 30;
  const res = await api(env, "/v1/events?limit=500");
  const failed = res.error;
  let out = `<h1>SIGNALFORGE <span class="tag">PAPER ONLY</span> <span class="tag">owner only</span></h1>${tabs("/")}
<div class="b warn">Paper-trading research only. No brokerage connection, no order routes, no live trading. Everything below is ingested from the trading computer and not independently reviewed. Test runs are excluded.</div>`;
  if (failed) {
    out += `<div class="b err">API failure: ${esc(failed)}. No data can be shown. This is not a success state.</div><h2>System Health</h2><div class="b err">API availability: FAILING (${esc(failed)})</div>`;
    return page(out + `<p style="color:#667;font-size:11px">Rendered ${new Date(now).toISOString()}</p>`, 503);
  }
  const m = model(res.data.events || []);
  const age = m.hb ? (now - Date.parse(recv(m.hb))) / 60000 : Infinity;
  out += `<h2>Engine</h2>` + (!m.hb ? `<div class="b warn">No heartbeat ever received. Engine status unknown.</div>` : age > staleMin ? `<div class="b warn">Stale: last heartbeat ${esc(m.hb.received_at)} (${Math.round(age)} min ago, reported "${esc(m.hb.payload.status)}"). Treat the engine as NOT running.</div>` : m.hb.payload.status === "running" ? `<div class="b ok">Running · last heartbeat ${esc(m.hb.received_at)} · version ${esc(m.hb.payload.engine_version ?? "—")}</div>` : `<div class="b warn">Engine reports "${esc(m.hb.payload.status)}" · ${esc(m.hb.received_at)}</div>`);
  if (!m.count) out += `<p class="empty">No real reports ingested yet. Waiting for the trading computer.</p>`;
  out += `<h2>Strategies being tested</h2>` + (tbl(["strategy", "status", "source", "last update"], m.testing.map((t) => [`<code>${esc(t.id)}</code>`, esc(t.status), esc(t.source), esc(t.at)])) || `<p class="empty">No strategies reported in testing.</p>`);
  out += `<h2>Paper performance &amp; P&amp;L</h2>` + (tbl(["strategy", "as of", "equity", "cash", "total P&L", "realized", "unrealized", "max DD %", "positions", "equity change"], m.perf.map((p) => [`<code>${esc(p.id)}</code>`, esc(p.as_of), money(p.equity), money(p.cash), money(p.pnl), money(p.realized), money(p.unrealized), num(p.dd, 1), p.positions, p.change === null ? "—" : money(p.change)])) || `<p class="empty">No paper sessions reported.</p>`);
  out += `<h2>Which strategies improve</h2>` + (tbl(["strategy", "vs parent", "metric", "change", "direction", "status"], m.improving.map((i) => [`<code>${esc(i.id)}</code>`, esc(i.parent ?? "—"), esc(i.metric), num(i.delta, 3), i.better ? "improved" : "worse", esc(i.status)])) || `<p class="empty">No comparable results yet.</p>`);
  out += `<h2>Rejected strategies (why)</h2>` + (tbl(["strategy", "from", "reason", "return %", "max DD %", "when"], m.rejected.map((r) => [`<code>${esc(r.id)}</code>`, esc(r.kind), esc(r.reason), num(r.ret, 1), num(r.dd, 1), esc(r.at)])) || `<p class="empty">No rejections reported.</p>`);
  const halts = m.rules.filter((r) => ["halt", "block"].includes(r.payload.decision));
  out += `<h2>Risk controls</h2>` + (m.hb && m.hb.payload.risk_checks_ok === false ? `<div class="b err">Engine reports risk checks NOT OK.</div>` : "") + (halts.length ? `<div class="b err">${halts.length} rule(s) currently blocking or halting.</div>` : "") + (tbl(["rule", "latest decision", "strategy", "value", "limit", "when"], m.rules.map((r) => [`<code>${esc(r.payload.rule)}</code>`, esc(r.payload.decision), esc(r.payload.strategy_id ?? "—"), num(r.payload.value), num(r.payload.limit), esc(r.received_at)])) || `<p class="empty">No risk events reported. That does not mean risk checks are running.</p>`) + (m.vfail.length ? `<h3>Validation failures</h3>` + tbl(["check", "severity", "strategy", "count", "when"], m.vfail.map((v) => [esc(v.payload.check), esc(v.payload.severity), esc(v.payload.strategy_id ?? "—"), v.payload.count, esc(v.received_at)])) : "");
  out += `<h2>Market data</h2><p>Historical research data stays in the private Google Sheet (licensing). <a href="${esc(env.SHEET_URL)}" rel="noreferrer" style="color:#9cf">Open the sheet</a>.</p>` + (!m.data ? `<p class="empty">No data freshness reports.</p>` : m.data.payload.stale || (now - Date.parse(m.data.payload.last_observation_at)) / 3600000 > 72 ? `<div class="b warn">Data stale: ${esc(m.data.payload.dataset_version)}, last observation ${esc(m.data.payload.last_observation_at)}</div>` : `<div class="b ok">${esc(m.data.payload.dataset_version)} · ${m.data.payload.rows} rows · last observation ${esc(m.data.payload.last_observation_at)}</div>`);
  out += `<h2>System Health</h2><div class="b ok">API reachable via private service binding</div><p class="empty">Ingestion failures and latency are not yet reported by the API.</p>`;
  out += `<p style="color:#667;font-size:11px">Rendered ${new Date(now).toISOString()} · ${m.count} real events in window</p>`;
  return page(out, 200);
}

// ---------------------------------------------------------------- Crucible
const LABELS = { kronos: "Kronos", block_bootstrap: "Block bootstrap", "block-bootstrap": "Block bootstrap", random_walk: "Random walk", "random-walk": "Random walk" };
const label = (id) => LABELS[id] ?? id;
const fx = (n, d = 1) => Number(n).toFixed(d);
const pctTxt = (x, d = 1) => typeof x === "number" ? (x * 100).toFixed(d) + "%" : "n/a";
const signed = (x, d = 1) => (x >= 0 ? "+" : "−") + Math.abs(x * 100).toFixed(d) + "%";

const CRUCIBLE_CSS = `
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;background:#0b0e13;color:#e6eaf0;font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:16px max(16px,env(safe-area-inset-left,0px)) 40px}
h1{font-size:20px;margin:4px 0}h2{font-size:17px;margin:0 0 10px}h3{font-size:14px;margin:16px 0 6px;color:#cfd6df}
.tag{display:inline-block;background:#223;padding:2px 6px;border-radius:4px;font-size:11px}
${TABS_CSS}
.muted{color:#8c96a5}.b{padding:10px;border-radius:8px;margin:10px 0}.warn{background:#4a3200;color:#ffd27a}.err{background:#4a0010;color:#ff9aa8}
.brand{display:grid;gap:2px;margin:16px 0 8px}
.mark{font-weight:900;letter-spacing:.34em;font-size:30px;line-height:1.1;background:linear-gradient(90deg,#ff8a3d,#f5b041,#ff8a3d);background-size:200% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:heat 5s ease-in-out infinite alternate}
.panel{background:#10151c;border:1px solid #1d232c;border-radius:14px;padding:14px;margin:14px 0}
.figs{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
.figs div{border:1px solid #1d232c;border-radius:10px;padding:12px;background:#0e1319;animation:rise .6s ease-out both;animation-delay:var(--d)}
.figs strong{display:block;font-size:24px;font-variant-numeric:tabular-nums}.figs span{font-size:13px;color:#8c96a5}
.grid2{display:grid;grid-template-columns:minmax(0,1fr) 230px;gap:14px;align-items:start}
@media (max-width:760px){.grid2{grid-template-columns:minmax(0,1fr)}}
svg{display:block;width:100%;height:auto;max-width:760px}
.g{stroke:#1a2029;stroke-width:1}.t{fill:#8c96a5;font-size:12px;font-family:ui-monospace,Menlo,Consolas,monospace}
.zero{stroke:#3a4350;stroke-width:1;stroke-dasharray:3 3}
.p{fill:none;stroke-width:1.2;stroke-opacity:.42;stroke-dasharray:1;stroke-dashoffset:1;animation:draw 1.5s cubic-bezier(.2,.7,.2,1) forwards;animation-delay:var(--d)}
.p.up{stroke:#ff8a3d}.p.dn{stroke:#5aa2ef}
.band{fill:none;stroke:#aab3c0;stroke-width:1.3;stroke-dasharray:5 4;opacity:0;animation:fade .7s 1.9s forwards}
.med{fill:none;stroke:#f3f5f8;stroke-width:2.6;stroke-dasharray:1;stroke-dashoffset:1;animation:draw 1s ease-out 1.5s forwards;filter:drop-shadow(0 0 4px rgba(255,255,255,.35))}
.now{stroke:#f3f5f8;stroke-width:1.2;animation:pulse 2.6s ease-in-out infinite}.nowdot{fill:#f3f5f8;animation:glow 2.6s ease-in-out infinite}
.tagbox{fill:#0b0e13;stroke:#3a4350}.tagbox.mid{stroke:#f3f5f8}.tagtext{fill:#cfd6df;font-size:12px;font-family:ui-monospace,Menlo,Consolas,monospace}
.regime{font-size:21px;font-weight:800;letter-spacing:.06em;display:flex;align-items:center;gap:8px}
.regime i{width:11px;height:11px;border-radius:50%;background:#5aa2ef;box-shadow:0 0 12px #5aa2ef}.regime.trending i{background:#ff8a3d;box-shadow:0 0 12px #ff8a3d}
.cells{display:grid;grid-template-columns:repeat(20,1fr);gap:2px;max-width:220px;margin:8px 0}
.cells b{aspect-ratio:1;background:#5aa2ef;transform:scale(0);animation:pop .25s ease-out forwards;animation-delay:var(--d)}.cells b.up{background:#ff8a3d}
dl{display:grid;grid-template-columns:auto 1fr;gap:4px 10px;margin:0;font-size:14px}dt{color:#8c96a5}dd{margin:0;text-align:right;font-family:ui-monospace,Menlo,Consolas,monospace}
.tw{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:7px 8px;border-bottom:1px solid #1d232c;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left}th{color:#8c96a5;font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.bars .bar{fill:#ff8a3d;transform-box:fill-box;transform-origin:bottom;animation:grow .8s cubic-bezier(.2,.7,.2,1) both;animation-delay:var(--d)}
.bars .ref{stroke:#f3f5f8;stroke-width:1.5;stroke-dasharray:6 4}.bars .coin{stroke:#8c96a5;stroke-width:1;stroke-dasharray:3 3}.bars .wh{stroke:#f3f5f8;stroke-width:1.3}
.bars text{fill:#8c96a5;font-size:12px}.bars .val{fill:#e6eaf0}
.pit{max-width:380px}.pit rect{fill:#5aa2ef}.pit.bs rect{fill:#ff8a3d}
.eq .l{fill:none;stroke-width:2.2;stroke-dasharray:1;stroke-dashoffset:1;animation:draw 1.8s ease-out forwards}.eq .hold{stroke:#f3f5f8}.eq .rule{stroke:#ff8a3d}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:13px;color:#8c96a5;margin-top:6px}.legend i{display:inline-block;width:14px;height:3px;margin-right:6px;vertical-align:middle}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
footer{color:#667;font-size:12px;margin-top:20px}
@keyframes draw{to{stroke-dashoffset:0}}@keyframes fade{to{opacity:1}}@keyframes pop{to{transform:scale(1)}}
@keyframes grow{from{transform:scaleY(0)}to{transform:scaleY(1)}}@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@keyframes pulse{0%,100%{stroke-opacity:.45}50%{stroke-opacity:1}}@keyframes glow{0%,100%{filter:drop-shadow(0 0 2px #fff)}50%{filter:drop-shadow(0 0 9px #fff)}}
@keyframes heat{from{background-position:0% 0}to{background-position:100% 0}}
@media (prefers-reduced-motion:reduce){.p,.med,.eq .l{animation:none;stroke-dashoffset:0}.band{animation:none;opacity:1}.cells b{animation:none;transform:none}.bars .bar,.figs div,.now,.nowdot,.mark{animation:none}}
`;

const crucibleShell = (inner, status = 200) => new Response(`<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="robots" content="noindex"><title>SIGNALFORGE · Crucible</title><style>${CRUCIBLE_CSS}</style></head><body><div class="wrap"><h1>SIGNALFORGE <span class="tag">PAPER ONLY</span> <span class="tag">owner only</span></h1>${tabs("/crucible")}<div class="brand"><span class="mark">CRUCIBLE</span><span class="muted">Every forecaster goes through the fire before anything trades on it.</span></div>${inner}</div></body></html>`, { status, headers: HDR });

function ticks(lo, hi, n = 5) {
  const raw = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-12; v += step) out.push(v);
  return out;
}

export function coneSvg(cone) {
  const W = 640, H = 300, L = 8, Rt = 72, Tp = 14, B = 26, h = cone.horizon;
  const all = cone.paths.flat().concat(cone.p10, cone.p90, [0]);
  let lo = Math.min(...all), hi = Math.max(...all);
  const pad = (hi - lo) * 0.06 || 0.01; lo -= pad; hi += pad;
  const x = (s) => L + (W - L - Rt) * (s / h), y = (v) => Tp + (H - Tp - B) * (1 - (v - lo) / (hi - lo));
  const d = (series) => `M${fx(x(0))},${fx(y(0))}` + series.map((v, i) => `L${fx(x(i + 1))},${fx(y(v))}`).join("");
  const tagVals = [["p90", cone.p90[h - 1]], ["med", cone.p50[h - 1]], ["p10", cone.p10[h - 1]]].sort((a, b) => b[1] - a[1]);
  const tagYs = [];
  for (const [, v] of tagVals) { let ty = y(v); if (tagYs.length && ty - tagYs.at(-1) < 18) ty = tagYs.at(-1) + 18; tagYs.push(ty); }
  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(`${cone.symbol}: ${cone.n_paths} sampled paths over the next ${h} sessions, as percent moves from the ${cone.as_of.slice(0, 10)} close`)}">`;
  for (const v of ticks(lo, hi)) {
    s += `<line class="g" x1="${L}" x2="${fx(W - Rt)}" y1="${fx(y(v))}" y2="${fx(y(v))}"/>`;
    if (tagYs.every((ty) => Math.abs(ty - y(v)) > 14)) s += `<text class="t" x="${fx(W - Rt + 6)}" y="${fx(y(v) + 4)}">${esc(signed(v, 0))}</text>`;
  }
  s += `<line class="zero" x1="${L}" x2="${fx(W - Rt)}" y1="${fx(y(0))}" y2="${fx(y(0))}"/>`;
  const order = cone.paths.map((p, i) => [p[h - 1], i]).sort((a, b) => a[0] - b[0]);
  order.forEach(([end, i], rank) => {
    const delay = Math.abs(rank - order.length / 2) / order.length * 0.9;
    s += `<path class="p ${end > 0 ? "up" : "dn"}" pathLength="1" style="--d:${fx(delay, 3)}s" d="${d(cone.paths[i])}"/>`;
  });
  s += `<path class="band" d="${d(cone.p10)}"/><path class="band" d="${d(cone.p90)}"/><path class="med" pathLength="1" d="${d(cone.p50)}"/>`;
  s += `<line class="now" x1="${fx(x(0))}" x2="${fx(x(0))}" y1="${Tp}" y2="${H - B}"/><circle class="nowdot" cx="${fx(x(0))}" cy="${fx(y(0))}" r="3.5"/>`;
  s += `<text class="t" x="${fx(x(0) + 4)}" y="${H - 8}">NOW</text><text class="t" x="${fx(W - Rt)}" y="${H - 8}" text-anchor="end">+${h}</text>`;
  tagVals.forEach(([k, v], i) => {
    s += `<rect class="tagbox${k === "med" ? " mid" : ""}" x="${fx(W - Rt + 2)}" y="${fx(tagYs[i] - 9)}" width="${Rt - 4}" height="18" rx="3"/><text class="tagtext" x="${fx(W - Rt + 7)}" y="${fx(tagYs[i] + 4.5)}">${esc(signed(v))}</text>`;
  });
  return s + "</svg>";
}

function cellsHtml(up, n) {
  const shown = Math.min(n, 100), upShown = Math.round(up / n * shown);
  return `<div class="cells" aria-hidden="true">${Array.from({ length: shown }, (_, i) => `<b class="${i < upShown ? "up" : ""}" style="--d:${fx(0.9 + i * 0.008, 3)}s"></b>`).join("")}</div>`;
}

function bucketsSvg(f) {
  const W = 520, H = 220, L = 42, R = 10, T = 12, B = 40, y0 = 0.3, y1 = 0.85;
  const y = (v) => T + (H - T - B) * (1 - (v - y0) / (y1 - y0));
  const bw = (W - L - R) / Math.max(1, f.agreement.length);
  let s = `<svg class="bars" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label(f.id))}: hit rate by how many paths agree">`;
  for (const v of [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]) s += `<line class="g" x1="${L}" x2="${W - R}" y1="${fx(y(v))}" y2="${fx(y(v))}"/><text x="${L - 6}" y="${fx(y(v) + 4)}" text-anchor="end">${v * 100}%</text>`;
  f.agreement.forEach((b, i) => {
    const x = L + bw * i + bw * 0.2, w = bw * 0.6, cx = x + w / 2;
    if (b.n >= 30 && typeof b.hit_rate === "number") {
      s += `<rect class="bar" style="--d:${fx(i * 0.12, 2)}s" x="${fx(x)}" y="${fx(y(b.hit_rate))}" width="${fx(w)}" height="${fx(y(y0) - y(b.hit_rate))}"/>`;
      if (typeof b.lo === "number") s += `<line class="wh" x1="${fx(cx)}" x2="${fx(cx)}" y1="${fx(y(Math.max(b.lo, y0)))}" y2="${fx(y(Math.min(b.hi, y1)))}"/>`;
      s += `<text class="val" x="${fx(cx)}" y="${fx(y(Math.min(b.hi ?? b.hit_rate, y1)) - 6)}" text-anchor="middle">${esc(pctTxt(b.hit_rate, 0))}</text>`;
    } else if (b.n) s += `<text x="${fx(cx)}" y="${fx(y(0.4))}" text-anchor="middle">too few</text>`;
    s += `<text x="${fx(cx)}" y="${H - B + 16}" text-anchor="middle">${esc(b.label)}%</text><text x="${fx(cx)}" y="${H - B + 32}" text-anchor="middle">n=${esc(b.n)}</text>`;
  });
  s += `<line class="ref" x1="${L}" x2="${W - R}" y1="${fx(y(f.base_rate_up))}" y2="${fx(y(f.base_rate_up))}"/><line class="coin" x1="${L}" x2="${W - R}" y1="${fx(y(0.5))}" y2="${fx(y(0.5))}"/>`;
  return s + "</svg>";
}

function pitSvg(f, cls) {
  const W = 260, H = 110, T = 8, B = 16, top = 0.2, bw = W / 10;
  const y = (v) => T + (H - T - B) * (1 - Math.min(v, top) / top);
  let s = `<svg class="pit ${cls}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label(f.id))} calibration histogram">`;
  f.pit.forEach((v, i) => { s += `<rect x="${fx(i * bw + 1)}" y="${fx(y(v))}" width="${fx(bw - 2)}" height="${fx(y(0) - y(v))}"/>`; });
  s += `<line class="coin" x1="0" x2="${W}" y1="${fx(y(0.1))}" y2="${fx(y(0.1))}" stroke="#f3f5f8" stroke-dasharray="5 4"/>`;
  return s + `<text class="t" x="0" y="${H - 2}">low</text><text class="t" x="${W}" y="${H - 2}" text-anchor="end">high</text></svg>`;
}

function equitySvg(bt) {
  const W = 560, H = 210, L = 58, R = 10, T = 10, B = 22;
  const vals = bt.hold_equity.concat(bt.rule_equity), lo = Math.min(...vals) * 0.97, hi = Math.max(...vals) * 1.02;
  const n = Math.max(bt.hold_equity.length, bt.rule_equity.length);
  const x = (i, len) => L + (W - L - R) * (i / Math.max(1, len - 1)), y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
  let s = `<svg class="eq" viewBox="0 0 ${W} ${H}" role="img" aria-label="Simulated equity: TRENDING-only rule against buy-and-hold">`;
  for (const v of ticks(lo, hi, 4)) s += `<line class="g" x1="${L}" x2="${W - R}" y1="${fx(y(v))}" y2="${fx(y(v))}"/><text class="t" x="${L - 6}" y="${fx(y(v) + 4)}" text-anchor="end">$${esc(Math.round(v / 1000))}k</text>`;
  s += `<text class="t" x="${L}" y="${H - 4}">${esc(bt.period_start.slice(0, 4))}</text><text class="t" x="${W - R}" y="${H - 4}" text-anchor="end">${esc(bt.period_end.slice(0, 4))}</text>`;
  for (const [key, cls] of [["hold_equity", "hold"], ["rule_equity", "rule"]]) {
    const series = bt[key];
    s += `<polyline class="l ${cls}" pathLength="1" points="${series.map((v, i) => `${fx(x(i, series.length))},${fx(y(v))}`).join(" ")}"/>`;
  }
  return s + "</svg>";
}

export function crucibleBody(event) {
  const p = event.payload, fs = p.forecasters;
  const primary = fs.find((f) => f.id === "kronos") ?? fs.find((f) => f.id.includes("bootstrap")) ?? fs[0];
  const kronosIn = fs.some((f) => f.id === "kronos");
  const figs = [
    [pctTxt(primary.base_rate_up), "of windows ended up. Calling UP every time scores this."],
    [pctTxt(primary.hit_rate), `${label(primary.id)} directional hit rate.`],
    [pctTxt(primary.trending_hit), `hit rate on its TRENDING calls (n=${primary.trending_n}).`],
    [pctTxt(primary.coverage), "of outcomes inside the 10–90 band. Target 80%."]
  ];
  if (p.backtest) figs.push([`$${(p.backtest.rule_final / 1000).toFixed(1)}k`, `TRENDING-only rule vs $${(p.backtest.hold_final / 1000).toFixed(1)}k buy-and-hold, simulated.`]);
  let out = `<div class="panel"><h2>The bar</h2><p>${kronosIn ? "Kronos is in this trial." : "Kronos has not been run yet. These are the forecasters with no edge that set the bar."} <span class="muted">Historical backtest, ${esc(p.window_start.slice(0, 10))} to ${esc(p.window_end.slice(0, 10))}, ${esc(p.symbols.length)} symbols.</span></p><div class="figs">${figs.map(([n, t], i) => `<div style="--d:${fx(i * 0.08, 2)}s"><strong>${esc(n)}</strong><span>${esc(t)}</span></div>`).join("")}</div></div>`;
  if (p.cone) {
    const c = p.cone;
    out += `<div class="panel"><h2>${esc(c.symbol)}, the next ${esc(c.horizon)} sessions</h2><p class="muted">${esc(label(c.forecaster))}, from the ${esc(c.as_of.slice(0, 10))} close, drawn as percent moves. A forward forecast: nothing here is scored, and a cone is not a trade.</p>
<div class="grid2"><div>${coneSvg(c)}</div><div><div class="regime ${c.regime === "TRENDING" ? "trending" : ""}"><i></i>${esc(c.regime)}</div><div class="muted">${esc(c.paths_up)}/${esc(c.n_paths)} paths up · ${esc(c.direction)}</div>${cellsHtml(c.paths_up, c.n_paths)}
<dl><dt>Median +${esc(c.horizon)}</dt><dd>${esc(signed(c.p50[c.horizon - 1]))}</dd><dt>10–90 band</dt><dd>${esc(signed(c.p10[c.horizon - 1]))} to ${esc(signed(c.p90[c.horizon - 1]))}</dd><dt>Spread</dt><dd>${esc(pctTxt(c.p90[c.horizon - 1] - c.p10[c.horizon - 1]))}</dd></dl></div></div>
<div class="legend"><span><i style="background:#ff8a3d"></i>path ends up</span><span><i style="background:#5aa2ef"></i>path ends down</span><span><i style="background:#f3f5f8"></i>median</span><span>dashed: 10th and 90th percentile</span></div></div>`;
  }
  out += `<div class="panel"><h2>Evidence</h2><p class="muted">Confidence means something only if confident calls beat a constant. The dashed line on each chart is what calling UP on every window scores.</p>
<div class="tw"><table><tr><th>Forecaster</th><th>Forecasts</th><th>10–90 coverage</th><th>Mean CRPS</th><th>Hit rate</th><th>Always UP</th><th>Hit when TRENDING</th></tr>${fs.map((f) => `<tr><td>${esc(label(f.id))}</td><td>${esc(f.n)}</td><td>${esc(pctTxt(f.coverage))}</td><td>${esc(f.mean_crps.toFixed(4))}</td><td>${esc(pctTxt(f.hit_rate))}</td><td>${esc(pctTxt(f.base_rate_up))}</td><td>${esc(pctTxt(f.trending_hit))} (n=${esc(f.trending_n)})</td></tr>`).join("")}</table></div>
<div class="cols">${fs.filter((f) => f.agreement.filter((b) => b.n >= 30).length > 1).map((f) => `<div><h3>${esc(label(f.id))}: hit rate by path agreement</h3>${bucketsSvg(f)}</div>`).join("")}</div>
<h3>Calibration: where the outcome landed among the paths (flat at 10% is calibrated)</h3><div class="cols">${fs.map((f) => `<div><div class="muted">${esc(label(f.id))} · ${esc(pctTxt(f.coverage))} in band</div>${pitSvg(f, f.id.includes("bootstrap") || f.id === "kronos" ? "bs" : "")}</div>`).join("")}</div></div>`;
  if (p.backtest) {
    const bt = p.backtest;
    out += `<div class="panel"><h2>The cone rule in the real engine</h2><p class="muted">Long a name only while its cone reads TRENDING UP, re-read every horizon. Same fills, costs, single-name cap and risk engine as buy-and-hold. Simulated.</p>${equitySvg(bt)}
<div class="legend"><span><i style="background:#f3f5f8"></i>Buy and hold ${esc(money(bt.hold_final))}${typeof bt.hold_sharpe === "number" ? ` · Sharpe ${esc(bt.hold_sharpe.toFixed(2))}` : ""}</span><span><i style="background:#ff8a3d"></i>TRENDING-only ${esc(money(bt.rule_final))}${typeof bt.rule_sharpe === "number" ? ` · Sharpe ${esc(bt.rule_sharpe.toFixed(2))}` : ""}</span></div></div>`;
  }
  out += `<footer>Trial ${esc(p.trial_id)} · commit ${esc(p.commit_sha.slice(0, 7))} · ${esc(p.n_paths)} paths, ${esc(p.horizon)}-session horizon, TRENDING at ${esc(Math.round(p.trend_agreement * 100))}% agreement · received ${esc(event.received_at)}. Scores only: prices never leave the trading computer.</footer>`;
  return out;
}

export async function renderCrucible(env) {
  const res = await api(env, "/v1/events?type=forecast_trial&limit=1");
  if (res.error) {
    const why = res.code === "bad_type"
      ? "The reporting API does not know the forecast_trial report yet. Deploy the updated signalforge-research-api."
      : `API failure: ${res.error}. No data can be shown. This is not a success state.`;
    return crucibleShell(`<div class="b err">${esc(why)}</div>`, 503);
  }
  const event = (res.data.events || [])[0];
  if (!event) {
    return crucibleShell(`<div class="b warn">No Crucible report yet. On the trading computer run: <code>python -m signalforge.forecast_cli trial --symbols SPY --window validation --source tiingo --backtest --report</code></div>`);
  }
  return crucibleShell(crucibleBody(event));
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method !== "GET") return new Response("method not allowed", { status: 405 });
    const a = await verifyAccess(request, env);
    if (!a.ok) return new Response(`Forbidden (${a.reason})`, { status: 403, headers: { "cache-control": "no-store", "content-type": "text/plain" } });
    if (url.pathname === "/") return render(env);
    if (url.pathname === "/crucible") return renderCrucible(env);
    return new Response("not found", { status: 404 });
  }
};
