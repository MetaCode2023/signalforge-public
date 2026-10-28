export const EVENT_TYPES = new Set(['heartbeat','paper_session','backtest','risk_event','data_health','experiment']);
const iso = x => typeof x === 'string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$/.test(x) && Number.isFinite(Date.parse(x));
const finite = x => typeof x === 'number' && Number.isFinite(x);
const slug = x => typeof x === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$/.test(x);
const plain = x => x !== null && typeof x === 'object' && !Array.isArray(x);
const forbiddenKey = key => /(?:token|secret|password|credential|api[_-]?key|account[_-]?(?:number|id)|ssn|broker[_-]?order|live[_-]?order|access[_-]?key|private[_-]?key)/i.test(key);
function safePayload(value, depth = 0) {
  if (depth > 3) return false;
  if (value === null || typeof value === 'boolean') return true;
  if (typeof value === 'string') return value.length <= 2048;
  if (typeof value === 'number') return Number.isFinite(value);
  if (Array.isArray(value)) return value.length <= 100 && value.every(v => safePayload(v,depth+1));
  if (!plain(value) || Object.keys(value).length > 40) return false;
  return Object.entries(value).every(([key,child]) => key.length <= 80 && !forbiddenKey(key) && safePayload(child,depth+1));
}

export function validateEnvelope(input) {
  if (!plain(input)) return 'JSON object required';
  if (Object.keys(input).some(k => !['schema_version','paper_only','event_id','event_type','run_id','occurred_at','payload'].includes(k))) return 'Unknown envelope field';
  if (input.schema_version !== 1) return 'Unsupported schema_version';
  if (input.paper_only !== true) return 'paper_only must be true';
  if (typeof input.event_id !== 'string' || !/^[0-9a-f]{8}-[0-9a-f-]{27,}$/i.test(input.event_id) || input.event_id.length > 64) return 'Invalid event_id';
  if (!EVENT_TYPES.has(input.event_type)) return 'Unknown event_type';
  if (!slug(input.run_id)) return 'Invalid run_id';
  if (!iso(input.occurred_at) || Date.parse(input.occurred_at) > Date.now() + 300000) return 'Invalid occurred_at';
  if (!plain(input.payload) || Object.keys(input.payload).length > 40) return 'Invalid payload';
  const p = input.payload;
  if (!safePayload(p)) return 'Forbidden or invalid payload field';
  if (input.event_type === 'paper_session') {
    if (!finite(p.equity) || p.equity < 0 || !finite(p.cash) || p.cash < 0 || p.cash > p.equity + .01 || !slug(p.strategy_id)) return 'Invalid paper portfolio';
  }
  if (input.event_type === 'backtest') {
    if (!slug(p.strategy_id) || !slug(p.dataset_version) || !slug(p.commit_sha)) return 'Missing backtest provenance';
    if (!finite(p.return_pct) || !finite(p.max_drawdown_pct) || p.max_drawdown_pct < 0 || p.max_drawdown_pct > 100 || !Number.isInteger(p.trades) || p.trades < 0) return 'Invalid backtest metrics';
    if (!iso(p.period_start) || !iso(p.period_end) || p.period_start >= p.period_end || !['candidate','rejected','verified'].includes(p.status)) return 'Invalid backtest period/status';
  }
  if (input.event_type === 'risk_event' && (!slug(p.rule) || !['allow','block','halt','warning'].includes(p.decision))) return 'Invalid risk event';
  if (input.event_type === 'data_health' && (!slug(p.dataset_version) || !Number.isInteger(p.rows) || p.rows < 0)) return 'Invalid data health';
  if (input.event_type === 'experiment' && (!slug(p.strategy_id) || !['proposed','tested','rejected','awaiting_approval'].includes(p.status))) return 'Invalid experiment';
  if (input.event_type === 'heartbeat' && !['running','idle','stopped','error'].includes(p.status)) return 'Invalid heartbeat';
  return null;
}
