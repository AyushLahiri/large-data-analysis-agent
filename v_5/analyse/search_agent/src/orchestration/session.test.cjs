// Pipe into node in the built container. Mock only OpenAI responses.
const assert=require('node:assert/strict');
const {mkdtempSync,existsSync}=require('node:fs');
process.env.RESEARCH_DATA_DIR=mkdtempSync('/tmp/session-test-');
const realFetch=global.fetch;
const base='/app/dist/orchestration';
const {lookupAgent,withAgent,SOL_LIMIT}=require(base+'/session');
const model=require(base+'/model');
const scope={session_id:'test',project_id:'project',chat_id:'chat'};
(async()=>{
  const agent=lookupAgent(scope,true);
  agent.used=SOL_LIMIT-100;
  agent.decisions=['Look up the archived guideline'];
  agent.turns=[{question:'Which version?',answer:'2020',sources:['https://example.org/guide']}];
  const first=agent.agentId;
  let captured,counts=0;
  global.fetch=async(url,options)=>{
    const body=JSON.parse(options.body);
    if(url.endsWith('input_tokens')){counts++;return Response.json({input_tokens:120});}
    captured=JSON.parse(body.input.split('\n').slice(1).join('\n'));
    assert.ok(agent.used<=SOL_LIMIT);
    return Response.json({status:'completed',usage:{total_tokens:217},
      output:[{content:[{type:'output_text',text:'{"action":"finish"}'}]}]});
  };
  await withAgent(agent,()=>model.decision(model.SOL,'test',{currentTask:'find section 3'}));
  assert.equal(counts,2);assert.notEqual(agent.agentId,first);
  assert.equal(agent.generation,2);assert.equal(agent.used,217);
  assert.equal(captured.input.currentTask,'find section 3');
  assert.deepEqual(captured.chatContext.handoff,['Look up the archived guideline']);
  assert.equal(captured.chatContext.recentTurns[0].answer,'2020');
  global.fetch=realFetch;

  // Exercise the actual HTTP boundary and search state machine.
  let plannerCall=0;
  model.decision=async(role,prompt,input)=>{
    if(role===model.LUNA)return {action:'finish',notes:'No evidence in fixture',evidence:[]};
    plannerCall++;
    if(plannerCall<=3 && !input.correction)return {action:'ask',questions:['Provide context']};
    if(!input.priorTasks.length)return {action:'dispatch',tasks:['Check fixture']};
    return {action:'finish',sections:[],gaps:['Fixture gap']};
  };
  const app=require('/app/dist/app').default;
  const server=await new Promise(resolve=>{const s=app.listen(0,'127.0.0.1',()=>resolve(s));});
  const port=server.address().port;
  const headers={Authorization:'Bearer '+process.env.V5_ANALYSIS_SEARCH_TOKEN,
    'Content-Type':'application/json','X-Project-Id':'project','X-Chat-Id':'alpha','X-Session-Id':'sessionA'};
  const req=(method,path,body,extra)=>realFetch('http://127.0.0.1:'+port+path,{
    method,headers:{...headers,...extra},body:body===undefined?undefined:JSON.stringify(body)});
  try{
    const a=await (await req('POST','/v1/search',{question:'test'})).json();
    assert.equal(a.recipient,'analysis_agent');
    const path='/v1/search/'+a.search_id;
    const second=await (await req('POST',path,{answer:'more'})).json();
    assert.equal(second.status,'needs_input');
    const finished=await (await req('POST',path,{answer:'enough'})).json();
    assert.equal(finished.status,'complete');
    const next=await (await req('POST','/v1/search',{question:'another query'})).json();
    assert.equal(next.orchestrator.agent_id,a.orchestrator.agent_id);
    for(const change of [{'X-Project-Id':'other'},{'X-Chat-Id':'beta'},{'X-Session-Id':'sessionB'}]){
      assert.equal((await req('GET',path,undefined,change)).status,404);
      const fresh=await (await req('POST','/v1/search',{question:'separate'},change)).json();
      assert.notEqual(fresh.orchestrator.agent_id,a.orchestrator.agent_id);
    }
    assert.equal((await req('GET',path,undefined,{Authorization:'Bearer wrong'})).status,401);
    assert.equal((await req('POST','/v1/chat/completions',{})).status,404);
    assert.equal((await req('DELETE','/v1/session')).status,204);
    assert.equal((await req('GET',path)).status,404);
    assert.equal(lookupAgent({session_id:'sessionA',project_id:'project',chat_id:'alpha'}),undefined);
    assert.ok(lookupAgent({session_id:'sessionB',project_id:'project',chat_id:'alpha'}));
    assert.equal(existsSync(process.env.RESEARCH_DATA_DIR+'/runs'),false);
    let started;
    const entered=new Promise(r=>{started=r;});
    model.decision=async(role,prompt,input,a,signal)=>{
      started();
      return new Promise((_,reject)=>signal.addEventListener('abort',()=>reject(signal.reason),{once:true}));
    };
    const inFlight=req('POST','/v1/search',{question:'Pending at teardown'},{'X-Session-Id':'sessionB'});
    await entered;
    assert.equal((await req('DELETE','/v1/session',undefined,{'X-Session-Id':'sessionB'})).status,204);
    await inFlight;
    assert.equal(lookupAgent({session_id:'sessionB',project_id:'project',chat_id:'alpha'}),undefined);
  }finally{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
  console.log('PASS: authenticated analysis-only access, two follow-ups, scope isolation, in-session reuse, session cleanup and 500k Sol rollover with handoff.');
})().catch(e=>{console.error(e);process.exitCode=1});

