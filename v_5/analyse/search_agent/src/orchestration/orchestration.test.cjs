// Run against the built service: node < this file, with RESEARCH_DATA_DIR set to a temporary directory.
const assert = require('node:assert/strict');
const path = require('node:path');
const { mkdtempSync } = require('node:fs');
process.env.RESEARCH_DATA_DIR = mkdtempSync('/tmp/research-test-');
const dist = process.env.RESEARCH_DIST || '/app/dist/orchestration';
const model = require(path.join(dist,'model'));
const workers = require(path.join(dist,'worker'));
const { createRun, conduct } = require(path.join(dist,'search'));
const prompts=[];
const plan = [
  {action:'ask',questions:['What period?', 'Supply the applicable document.']},
  {action:'ask',questions:['Which edition?']},
  {action:'ask',questions:['Invalid third message']},
  {action:'dispatch',tasks:['invalid1','invalid2','invalid3','invalid4']},
  ...[3,21,20].map((n,i)=>({action:'dispatch',tasks:Array.from({length:n},(_,j)=>i+'-'+j)})),
  {action:'replenish',reason:'remaining evidence gap'},
  {action:'replenish',reason:'invalid second request'},
  {action:'dispatch',tasks:['last']},
  {action:'finish',sections:[],gaps:['Synthetic fixture'],flagged:[]}
];
model.decision=async (role,prompt,input)=>{
  assert.equal(role,model.SOL);
  prompts.push(input.correction);
  assert.ok(plan.length);
  return plan.shift();
};
let active=0,peak=0;
const starts=[];
workers.worker=async(run,finding)=>{
  starts.push(run.waves-1); peak=Math.max(peak,++active);
  await new Promise(r=>setTimeout(r,5)); --active;
  return {task:finding.task,notes:'Test',evidence:[]};
};
(async()=>{
  const ledger={used:230000,reserved:0,replenishment:null};
  assert.equal(model.reserve(ledger,1000,15000),15000);
  assert.equal(model.reserve(ledger,1000,9000),3000);
  assert.throws(()=>model.reserve(ledger,1,512));
  const run=createRun({question:'Test',assets:[],analyst_context:true});
  assert.equal((await conduct(run)).status,'needs_input');
  run.dialogue.push('Reply: 2024');run.status='running';
  assert.equal((await conduct(run)).recipient,'analysis_agent');
  const result=await conduct(run);
  assert.equal(result.workers,24);
  assert.deepEqual(starts,[...Array(3).fill(0),...Array(20).fill(1),2]);
  assert.equal(peak,20);
  assert.equal(result.luna.limit,350000);
  assert.ok(prompts.some(v=>v.includes('First wave')));
  assert.ok(prompts.some(v=>v.includes('already used')));
  assert.equal(result.status,'complete');
  console.log('PASS: initial barrier, parallel limit, repeat waves, clarification, one replenishment and shared reservations');
})().catch(e=>{console.error(e);process.exitCode=1});

