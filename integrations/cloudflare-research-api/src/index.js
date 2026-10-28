import {validateEnvelope} from './validation.js';
import {verifyIngest,verifyReadToken,digestHex} from './auth.js';

const MAX_BYTES=65536;
const TYPES=['heartbeat','paper_session','backtest','risk_event','data_health','experiment'];
function json(data,status=200){return new Response(JSON.stringify(data),{status,headers:{'content-type':'application/json; charset=utf-8','cache-control':'no-store','x-content-type-options':'nosniff'}})}
const failure=(error,status)=>json({ok:false,error},status);

async function readBounded(request){
  const length=Number(request.headers.get('content-length'));
  if (Number.isFinite(length) && length>MAX_BYTES) return null;
  if (!request.body) return '';
  const reader=request.body.getReader();
  const chunks=[];let total=0;
  try{
    while(true){
      const {done,value}=await reader.read();if(done)break;
      total+=value.byteLength;
      if(total>MAX_BYTES){await reader.cancel();return null;}
      chunks.push(value);
    }
  }finally{reader.releaseLock();}
  const bytes=new Uint8Array(total);let offset=0;
  for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
  return new TextDecoder('utf-8',{fatal:true}).decode(bytes);
}

async function ingest(request,env){
  if(!env.DB || !env.SIGNALFORGE_INGEST_SECRET) return failure('service_unconfigured',503);
  if(!request.headers.get('content-type')?.toLowerCase().startsWith('application/json')) return failure('json_required',415);
  let raw;try{raw=await readBounded(request);}catch{return failure('invalid_utf8',400);}
  if(raw===null) return failure('body_too_large',413);
  const auth=await verifyIngest(request,raw,env.SIGNALFORGE_INGEST_SECRET);
  if(!auth.ok) return failure('unauthorized',401);
  let envelope;try{envelope=JSON.parse(raw);}catch{return failure('malformed_json',400);}
  const issue=validateEnvelope(envelope);
  if(issue) return failure(issue,422);
  const now=Math.floor(Date.now()/1000);
  // Unique nonce blocks signed-request replay, even with a fresh event id.
  try{await env.DB.prepare('INSERT INTO ingest_nonces(nonce, received_unix) VALUES (?,?)').bind(auth.nonce,now).run();}
  catch{return failure('replayed_request',409);}
  try{
    await env.DB.prepare('INSERT INTO reports(event_id,schema_version,event_type,run_id,occurred_at,body_sha256,payload_json) VALUES (?,?,?,?,?,?,?)')
      .bind(envelope.event_id,1,envelope.event_type,envelope.run_id,envelope.occurred_at,await digestHex(raw),JSON.stringify(envelope.payload)).run();
  }catch{return failure('duplicate_event_or_storage_failure',409);}
  return json({ok:true,event_id:envelope.event_id,mode:'PAPER_ONLY'},202);
}

async function getSummary(env){
  const results={};
  for(const type of TYPES){
    const row=await env.DB.prepare('SELECT event_id,run_id,occurred_at,received_at,payload_json FROM reports WHERE event_type=? ORDER BY occurred_at DESC LIMIT 1').bind(type).first();
    if(row){results[type]={event_id:row.event_id,run_id:row.run_id,occurred_at:row.occurred_at,received_at:row.received_at,payload:JSON.parse(row.payload_json)};}
  }
  return json({ok:true,mode:'PAPER_ONLY',source:'verified_ingested_reports',latest:results});
}

async function getEvents(url,env){
  const limit=Math.min(100,Math.max(1,Number.parseInt(url.searchParams.get('limit')??'20',10)||20));
  const result=await env.DB.prepare('SELECT event_id,event_type,run_id,occurred_at,received_at,payload_json FROM reports ORDER BY received_at DESC,event_id DESC LIMIT ?').bind(limit).all();
  return json({ok:true,mode:'PAPER_ONLY',events:(result.results??[]).map(row=>({...row,payload:JSON.parse(row.payload_json),payload_json:undefined}))});
}

export default {
  async fetch(request,env){
    try{
      const url=new URL(request.url);
      if(url.pathname==='/health' && request.method==='GET') return json({ok:true,service:'signalforge-research-api',mode:'PAPER_ONLY'});
      if(url.pathname==='/v1/ingest' && request.method==='POST') return ingest(request,env);
      if(url.pathname==='/v1/summary' && request.method==='GET' || url.pathname==='/v1/events' && request.method==='GET'){
        if(!verifyReadToken(request.headers.get('authorization'),env.SIGNALFORGE_READ_TOKEN)) return failure('unauthorized',401);
        if(!env.DB) return failure('service_unconfigured',503);
        return url.pathname==='/v1/summary'?getSummary(env):getEvents(url,env);
      }
      return failure('not_found',404);
    }catch{return failure('internal_error',500);}
  },
  async scheduled(_event,env){
    if(env.DB) await env.DB.prepare('DELETE FROM ingest_nonces WHERE received_unix < ?').bind(Math.floor(Date.now()/1000)-7*86400).run();
  }
};
