const encoder = new TextEncoder();
export const canonical = (timestamp, nonce, body) => `v1:${timestamp}:${nonce}:${body}`;

function fromHex(value) {
  if (typeof value !== 'string' || !/^[0-9a-f]{64}$/i.test(value)) return null;
  return new Uint8Array(value.match(/../g).map(part => parseInt(part, 16)));
}

export async function verifyIngest(request, raw, secret, now = Date.now()) {
  if (typeof secret !== 'string' || secret.length < 32) return {ok:false,reason:'server_not_configured'};
  const timestamp = request.headers.get('x-signalforge-timestamp');
  const nonce = request.headers.get('x-signalforge-nonce');
  const signature = request.headers.get('x-signalforge-signature');
  if (!/^\d{10,13}$/.test(timestamp || '') || !/^[a-f0-9-]{36}$/i.test(nonce || '') || !signature?.startsWith('v1=')) return {ok:false,reason:'auth_missing'};
  const ms = Number(timestamp) * (timestamp.length === 10 ? 1000 : 1);
  if (!Number.isSafeInteger(ms) || Math.abs(now-ms)>300000) return {ok:false,reason:'timestamp_out_of_window'};
  const bytes = fromHex(signature.slice(3));
  if (!bytes) return {ok:false,reason:'bad_signature'};
  const key = await crypto.subtle.importKey('raw',encoder.encode(secret),{name:'HMAC',hash:'SHA-256'},false,['verify']);
  const ok = await crypto.subtle.verify('HMAC',key,bytes,encoder.encode(canonical(timestamp,nonce,raw)));
  return {ok,reason:ok?'ok':'bad_signature',nonce};
}

export async function digestHex(raw) {
  const bytes = await crypto.subtle.digest('SHA-256',encoder.encode(raw));
  return [...new Uint8Array(bytes)].map(b=>b.toString(16).padStart(2,'0')).join('');
}

export function verifyReadToken(header, secret) {
  if (!secret || secret.length < 32 || !header?.startsWith('Bearer ')) return false;
  const candidate = encoder.encode(header.slice(7));
  const expected = encoder.encode(secret);
  if (candidate.length !== expected.length) return false;
  let diff=0;
  for(let i=0;i<expected.length;i++) diff |= candidate[i]^expected[i];
  return diff===0;
}
