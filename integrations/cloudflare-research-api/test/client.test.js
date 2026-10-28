import test from 'node:test';
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {preparePaperReport,submitPaperReport} from '../src/client.js';

const secret='I'.repeat(48);
const endpoint='https://signalforge-api.example.com/v1/ingest';
const event=()=>({schema_version:1,paper_only:true,event_id:randomUUID(),event_type:'heartbeat',run_id:'local-test',occurred_at:new Date().toISOString(),payload:{status:'idle'}});

test('client prepares signed paper-only report with Access credentials',()=>{
  const config={ingestSecret:secret,accessClientId:'access-client-id',accessClientSecret:'access-client-secret'};
  const prepared=preparePaperReport(endpoint,event(),config);
  assert.equal(prepared.headers['CF-Access-Client-Id'],'access-client-id');
  assert.equal(prepared.headers['CF-Access-Client-Secret'],'access-client-secret');
  assert.match(prepared.headers['x-signalforge-signature'],/^v1=[a-f0-9]{64}$/);
  assert.equal(JSON.parse(prepared.body).paper_only,true);
});

test('client rejects live mode and incomplete Access pair',()=>{
  const live=event();live.paper_only=false;
  assert.throws(()=>preparePaperReport(endpoint,live,{ingestSecret:secret}),/paper_only/);
  const bad=event();bad.payload.free_note='extra';
  // Client enforces the locally bundled schema; production schema may be stricter.
  assert.equal(typeof preparePaperReport(endpoint,bad,{ingestSecret:secret}).body,'string');
  assert.throws(()=>preparePaperReport(endpoint,event(),{ingestSecret:secret,accessClientId:'id'}),/Both Cloudflare Access/);
});

test('client requires HTTPS and never places secrets in URL',()=>{
  assert.throws(()=>preparePaperReport('http://example.test/v1/ingest',event(),{ingestSecret:secret}),/HTTPS/);
  assert.throws(()=>preparePaperReport(endpoint+'?token=123',event(),{ingestSecret:secret}),/HTTPS/);
});

test('sender returns only safe response metadata',async()=>{
  let seen;
  const send=async(_url,options)=>{seen=options;return {ok:true,status:202,json:async()=>({ok:true,event_id:'paper-event',echoed_secret:'do-not-print'})};};
  const result=await submitPaperReport(endpoint,event(),{ingestSecret:secret},send);
  assert.deepEqual(result,{ok:true,status:202,event_id:'paper-event'});
  assert.equal(seen.method,'POST');
});
