// Pipe into node in the built service container. No provider calls.
const assert = require('node:assert/strict');
const { mkdtempSync } = require('node:fs');
const path = require('node:path');
process.env.RESEARCH_DATA_DIR = mkdtempSync('/tmp/deadline-test-');
const dist = process.env.RESEARCH_DIST || '/app/dist/orchestration';
const model = require(path.join(dist,'model'));
const { loadRun } = require(path.join(dist,'store'));
const { createRun, conduct, SYNTHESIS_MS } = require(path.join(dist,'search'));

const hangUntilAborted = signal => new Promise((_,reject)=>{
  signal.throwIfAborted();
  signal.addEventListener('abort',()=>reject(signal.reason),{once:true});
});
(async()=>{
  const run=createRun({question:'Find the retention period.',assets:[
    {name:'Policy',text:'The retention period is six months. Exceptions require written approval.'}
  ]});
  assert.ok(Math.abs(run.deadline-Date.now()-120000)<100);
  run.deadline=Date.now()+SYNTHESIS_MS+300;
  let plans=0,steps=0,seenBeforeFinish=false;
  model.decision=async(role,prompt,input,accounting,signal)=>{
    if(role===model.SOL){
      assert.equal(input.analystContextEnabled,false);
      if(++plans===1) return {action:'ask',questions:['A user-facing question should be rejected.']};
      if(plans===2) return {action:'dispatch',tasks:['Check the policy']};
      assert.ok(prompt.includes('research window has ended'));
      const passage=input.findings[0].evidence[0];
      assert.match(passage.quote,/six months/);
      return {action:'finish',sections:[{text:'The period is six months.',sources:[passage.source]}],gaps:[],flagged:[]};
    }
    if(++steps===1) return {action:'read',url:input.assets[0].url,find:'six months'};
    const saved=loadRun(run.id);
    seenBeforeFinish=saved.findings[0].state==='active' && saved.findings[0].evidence.length===1;
    if(steps===2) return {action:'checkpoint',evidence:[
      {source:saved.findings[0].evidence[0].source,quote:'fabricated quote',date:'unknown'}]};
    assert.equal(loadRun(run.id).findings[0].evidence.length,1);
    return hangUntilAborted(signal);
  };
  const result=await conduct(run);
  assert.ok(seenBeforeFinish);
  assert.equal(result.status,'complete');
  assert.equal(run.findings[0].state,'interrupted');
  assert.match(result.answer,/six months/);
  // Resume an in-progress search near its persisted deadline; a stalled synthesizer
  // must return a source-linked extract. Neither resume nor a refill restarts the clock.
  run.status='running'; delete run.result; run.ledger.replenishment='test';
  run.deadline=Date.now()+100;
  model.decision=async(role,p,i,a,signal)=>hangUntilAborted(signal);
  const started=Date.now();
  const partial=await conduct(run);
  assert.ok(Date.now()-started<1000);
  assert.ok(partial.partial && partial.answer.includes('six months') && partial.sections[0].sources[0].url);
  const empty=createRun({question:'No evidence',assets:[]});
  empty.deadline=Date.now()-1;
  assert.match((await conduct(empty)).answer,/No source passage/);
  console.log('PASS: pre-return durable evidence, fabricated quote rejected, default has no analyst requests, timed research cancellation, final synthesis and deadline fallback.');
})().catch(error=>{console.error(error);process.exitCode=1;});
