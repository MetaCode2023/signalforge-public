import test from 'node:test';
import assert from 'node:assert/strict';
import {createHmac,randomUUID} from 'node:crypto';
import worker from '../src/index.js';
import {canonical,verifyIngest,verifyReadToken} from '../src/auth.js';
import {validateEnvelope} from '../src/validation.js';

const secret='S'.repeat(48),readSecret='R'.repeat(48);
const stamp=()=>String(Math.floor(Date.now()/1000));
const payload=(type='heartbeat')=>({schema_version:1,paper_only:true,event_id:randomUUID(),event_type:type,run_id:'test-run-001',occurred_at:new Date().toISOString(),payload:{status:'idle'}});
function request(body,overrides={}){
  const raw=JSON.stringify(body),ts=stamp(),nonce=randomUUID();
  const sig=createHmac('sha256',secret).update(canonical(ts,nonce,raw)).digest('hex');
  return new Request('https://api.example/v1/ingest',{method:'POST',body:raw,headers:{'content-type':'application/json','x-signalforge-timestamp':ts,'x-signalforge-nonce':nonce,'x-signalforge-signature':`v1=${sig}`,...overrides}});
}
function mockDb(){
  const nonces=new Set(),ids=new Set(),calls=[];
  return {
    nonces,ids,calls,
    prepare(sql){
      return {bind(...args){
        return {async run(){
          calls.push({sql,args});
          if(sql.includes('ingest_nonces')){if(nonces.has(args[0]))throw Error('duplicate nonce');nonces.add(args[0]);}
          if(sql.includes('INSERT INTO reports')){if(ids.has(args[0]))throw Error('duplicate event');ids.add(args[0]);}
          return {success:true};
        }};
      }};
    }
  };
}
test('paper-only heartbeat validates',()=>assert.equal(validateEnvelope(payload()),null));
test('rejects live-mode and forbidden fields',()=>{const p=payload();p.paper_only=false;assert.match(validateEnvelope(p),/paper_only/);p.paper_only=true;p.payload.broker_token='bad';assert.match(validateEnvelope(p),/Forbidden/);});
test('rejects nested credentials and non-finite metrics',()=>{const p=payload();p.payload.detail={account:{api_key:'dont-store'}};assert.match(validateEnvelope(p),/Forbidden/);p.payload={status:'running',metrics:{net_profit:Infinity}};assert.match(validateEnvelope(p),/Forbidden/);});
test('rejects malformed backtest provenance and bad drawdown',()=>{const p=payload('backtest');assert.match(validateEnvelope(p),/provenance/);p.payload={strategy_id:'trend',dataset_version:'sample',commit_sha:'abc123',return_pct:1,max_drawdown_pct:-1,trades:2,period_start:'2026-01-01T00:00:00Z',period_end:'2026-06-01T00:00:00Z',status:'candidate'};assert.match(validateEnvelope(p),/metrics/);});
test('read token comparison rejects missing and short inputs',()=>{assert.equal(verifyReadToken('Bearer '+readSecret,readSecret),true);assert.equal(verifyReadToken('Bearer '+'Q'.repeat(48),readSecret),false);assert.equal(verifyReadToken(null,readSecret),false);});
test('valid HMAC signature verifies and altered bytes fail',async()=>{const req=request(payload());const raw=await req.clone().text();assert.equal((await verifyIngest(req,raw,secret)).ok,true);assert.equal((await verifyIngest(req,raw+' ',secret)).ok,false);});
test('rejects unsigned ingestion without touching storage',async()=>{const db=mockDb();const r=await worker.fetch(new Request('https://api.example/v1/ingest',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload())}),{DB:db,SIGNALFORGE_INGEST_SECRET:secret});assert.equal(r.status,401);assert.equal(db.calls.length,0);});
test('rejects oversized upload before database writes',async()=>{const db=mockDb();const req=new Request('https://api.example/v1/ingest',{method:'POST',headers:{'content-type':'application/json'},body:' '.repeat(66000)});const r=await worker.fetch(req,{DB:db,SIGNALFORGE_INGEST_SECRET:secret});assert.equal(r.status,413);assert.equal(db.calls.length,0);});
test('accepts signed paper heartbeat and writes nonce plus report',async()=>{const db=mockDb();const r=await worker.fetch(request(payload()),{DB:db,SIGNALFORGE_INGEST_SECRET:secret});assert.equal(r.status,202);assert.equal(db.calls.length,2);assert.equal((await r.json()).mode,'PAPER_ONLY');});
test('rejects replay of a previously accepted signed request',async()=>{const db=mockDb();const req=request(payload());const env={DB:db,SIGNALFORGE_INGEST_SECRET:secret};assert.equal((await worker.fetch(req.clone(),env)).status,202);assert.equal((await worker.fetch(req.clone(),env)).status,409);assert.equal(db.calls.filter(c=>c.sql.includes('INSERT INTO reports')).length,1);});
test('signed request still cannot ingest live-mode envelope',async()=>{const db=mockDb();const report=payload();report.paper_only=false;const r=await worker.fetch(request(report),{DB:db,SIGNALFORGE_INGEST_SECRET:secret});assert.equal(r.status,422);assert.equal(db.calls.length,0);});
test('blocks stale timestamp',async()=>{const r=request(payload(),{'x-signalforge-timestamp':'1000000000'});assert.equal((await verifyIngest(r,await r.clone().text(),secret)).ok,false);});
test('does not expose reports without read auth',async()=>{const db=mockDb();const r=await worker.fetch(new Request('https://api.example/v1/summary'),{DB:db,SIGNALFORGE_READ_TOKEN:readSecret});assert.equal(r.status,401);assert.equal(db.calls.length,0);});
test('no live trade route exists',async()=>{const db=mockDb();for(const path of ['/v1/orders','/v1/trade','/schwab/order']){const r=await worker.fetch(new Request('https://api.example'+path,{method:'POST'}),{DB:db});assert.equal(r.status,404);}assert.equal(db.calls.length,0);});
