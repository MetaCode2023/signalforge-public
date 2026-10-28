import {createHmac,randomUUID} from 'node:crypto';
import {canonical} from './auth.js';
import {validateEnvelope} from './validation.js';

export function preparePaperReport(endpoint, envelope, config, now = Date.now()) {
  const url = new URL(endpoint);
  if (url.protocol !== 'https:' || url.pathname !== '/v1/ingest' || url.username || url.password || url.search || url.hash) {
    throw Error('An HTTPS /v1/ingest URL without credentials or query parameters is required');
  }
  const issue = validateEnvelope(envelope);
  if (issue) throw Error(`Report rejected locally: ${issue}`);
  const secret = config.ingestSecret;
  if (typeof secret !== 'string' || secret.length < 32) throw Error('SIGNALFORGE_INGEST_SECRET is missing or too short');
  const accessId = config.accessClientId;
  const accessSecret = config.accessClientSecret;
  if (Boolean(accessId) !== Boolean(accessSecret)) throw Error('Both Cloudflare Access service credentials are required together');
  const timestamp = String(Math.floor(now / 1000));
  const nonce = randomUUID();
  const body = JSON.stringify(envelope);
  const signature = createHmac('sha256',secret).update(canonical(timestamp,nonce,body)).digest('hex');
  const headers = {
    'content-type':'application/json',
    'x-signalforge-timestamp':timestamp,
    'x-signalforge-nonce':nonce,
    'x-signalforge-signature':`v1=${signature}`
  };
  if (accessId && accessSecret) {
    headers['CF-Access-Client-Id'] = accessId;
    headers['CF-Access-Client-Secret'] = accessSecret;
  }
  return {endpoint:url.href,body,headers};
}

export async function submitPaperReport(endpoint,envelope,config,send = fetch) {
  const {endpoint:href,body,headers} = preparePaperReport(endpoint,envelope,config);
  const response = await send(href,{method:'POST',headers,body,signal:AbortSignal.timeout(15000)});
  let result;
  try { result=await response.json(); } catch { result={}; }
  // Never log server body, request headers or secrets. Only narrowly scoped metadata.
  const output={ok:response.ok && result.ok===true,status:response.status,event_id:result.event_id??null};
  if (!output.ok) output.error='report_not_accepted';
  return output;
}
