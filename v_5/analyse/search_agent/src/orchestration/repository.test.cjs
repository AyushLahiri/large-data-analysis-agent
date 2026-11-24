// Pipe into node in the built service container. Uses a temporary project and no provider calls.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
process.env.RESEARCH_DATA_DIR=fs.mkdtempSync('/tmp/repo-worker-test-');
const dist=process.env.RESEARCH_DIST || '/app/dist/orchestration';
const model=require(path.join(dist,'model'));
const workers=require(path.join(dist,'worker'));
const {createRun,conduct}=require(path.join(dist,'search'));
const {folder,projectDir,library,loadRun}=require(path.join(dist,'store'));
const {acknowledgeDelivery}=require(path.join(dist,'curation'));
(async()=>{
  const project='fixture';
  const entry=i=>({id:'source'+i,url:'https://example.org/'+i,title:'Guide '+i,digest:'digest'+i,pdf:false,
    retrieved:'2026-11-23',reason:'fixture'});
  for(let i=0;i<3;i++) fs.writeFileSync(path.join(projectDir(project),'source'+i+'.json'),JSON.stringify(entry(i)));
  const repo=Array.from({length:3},(_,i)=>({task:'inspect topic '+i, referenceIds:['source'+i]}));
  const web=['find revisions','find independent validation','find adoption data'];
  const plans=[{action:'dispatch',tasks:web,repositoryTasks:repo.concat(repo[0])},
    {action:'dispatch',tasks:web,repositoryTasks:repo},{action:'finish',sections:[{text:'Verified',sources:['source3']}],gaps:[]}];
  const kinds=[];
  let peaks=0,active=0,edits=0;
  const goodSummary='This official guide describes a documented method and the scope of its original release. '+
    'It contains definitions, worked examples, and instructions for interpreting the results. '+
    'It is useful for comparing later changes against a historical baseline and for checking the language used in the source. '+
    'The example agency issued the guide in 2020. Researchers can consult the original when they need the precise wording, examples, definitions, and exceptions that applied to this edition.';
  model.decision=async(role,prompt,input)=>{
    assert.equal(role,model.SOL);
    if(plans.length) return plans.shift();
    assert.ok(loadRun(run.id).curation.delivered_at);
    return {keep:true,reason:'Historical baseline',title:'Official guide',
      summary:++edits===1?'too short':goodSummary,publisher:'Example agency',publication_year:2020,
      publication_evidence:'Published 2020'};
  };
  workers.worker=async(run,f,signal,refs,assigned)=>{
    assert.equal(assigned.length,3);kinds.push(f.kind);peaks=Math.max(peaks,++active);
    await new Promise(r=>setTimeout(r,5));active--;
    if(f.task===web[0]){
      run.sources.source3=entry(3);
      fs.writeFileSync(path.join(folder('runs',run.id,'documents'),'source3.txt'),'Published 2020. Verified.');
      f.evidence.push({source:'source3',quote:'Verified.',date:'2020'});
    }
    return f;
  };
  const run=createRun({question:'Compare the historical guide with new evidence.',assets:[],project});
  const done=await conduct(run);
  assert.equal(done.curation.status,'awaiting_delivery');
  assert.equal(kinds.filter(k=>k==='repository').length,3);
  assert.equal(kinds.filter(k=>k==='web').length,3);
  assert.equal(peaks,6);
  assert.equal(library(project).length,3);
  await acknowledgeDelivery(run);
  const saved=library(project).find(s=>s.id==='source3');
  assert.equal(saved.summary,goodSummary);
  assert.equal(edits,2);
  assert.equal(saved.publication_year,2020);
  await acknowledgeDelivery(loadRun(run.id));
  assert.equal(edits,2);
  assert.equal(library(project).length,4);
  console.log('PASS: fourth repository worker rejected, three repository + three web workers overlap, no early write, post-receipt summary validation and idempotent retry.');
})().catch(e=>{console.error(e);process.exitCode=1;});
