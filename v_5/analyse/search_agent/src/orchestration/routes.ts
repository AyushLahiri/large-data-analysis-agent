import { Router, Request, Response } from 'express';
import { join } from 'node:path';
import { z } from 'zod';
import { acknowledgeDelivery } from './curation';
import { asset, discardRun, folder, intake, library, loadRun, projectDir, Run } from './store';
import { conduct, createRun, expire, summary, SYNTHESIS_MS } from './search';
import { agentState, ChatAgent, forgetAgent, lookupAgent, Scope, sessionAgents, withAgent } from './session';

export const researchRoutes=Router();
const pending=new WeakMap<ChatAgent,{idle:Promise<void>; release:()=>void}>();
const closingSessions=new Set<string>();
function lock(agent:ChatAgent) {
  if(agent.busy) throw Error('Overlapping calls to a chat agent');
  agent.busy=true;
  let release!:()=>void;
  const idle=new Promise<void>(resolve=>{release=resolve;});
  pending.set(agent,{idle,release});
}
function unlock(agent:ChatAgent) {
  agent.busy=false;
  pending.get(agent)?.release();
  pending.delete(agent);
}
const timers=new Map<string,NodeJS.Timeout>();
const reply=z.object({answer:z.string().trim().max(30000).optional(),assets:z.array(asset).max(30).default([])});
function scope(res:Response):Scope {return res.locals.scope;}
function ownedRun(req:Request,res:Response) {
  const agent=lookupAgent(scope(res));
  return agent?.runIds.has(req.params.id) ? loadRun(req.params.id) : undefined;
}
function remember(agent:ChatAgent,run:Run) {
  const result=run.result as {answer?:string;sections?:{sources:{url:string}[]}[]}|undefined;
  agent.turns.push({question:run.question.slice(0,1500),answer:(result?.answer || '').slice(0,2800),
    sources:[...new Set(result?.sections?.flatMap(s=>s.sources.map(x=>x.url)) || [])].slice(0,10)});
  if(agent.turns.length>12) agent.turns.shift();
}
function cancelTimer(id:string) { clearTimeout(timers.get(id));timers.delete(id); }
function wrapUp(agent:ChatAgent,id:string,deadline:number) {
  cancelTimer(id);
  const timer=setTimeout(async()=>{
    timers.delete(id);
    const run=loadRun(id);
    if(run?.status!=='needs_input' || !agent.runIds.has(id)) return;
    if(agent.busy){
      // Finalize locally at the deadline without issuing overlapping Sol calls.
      if(Date.now()>=deadline){expire(run);remember(agent,run);}
      else wrapUp(agent,id,deadline);
      return;
    }
    lock(agent);
    try {await withAgent(agent,()=>conduct(run));}
    catch {expire(run);}
    finally {remember(agent,run);unlock(agent);}
  },Math.max(agent.busy ? 100 : 0,deadline-SYNTHESIS_MS-Date.now()));
  timer.unref();timers.set(id,timer);
}
researchRoutes.get('/v1/projects/:project/references',(req,res)=>{
  if(req.params.project!==scope(res).project_id){res.sendStatus(404);return;}
  res.json(library(req.params.project).map(s=>({...s,document_url:
    '/v1/projects/'+req.params.project+'/references/'+s.id+'/document'})));
});
researchRoutes.get('/v1/projects/:project/references/:source/document',(req,res)=>{
  if(req.params.project!==scope(res).project_id){res.sendStatus(404);return;}
  const entry=library(req.params.project).find(s=>s.id===req.params.source);
  if(!entry){res.sendStatus(404);return;}
  res.download(join(projectDir(req.params.project),entry.file || entry.id+'.txt'),entry.id+(entry.file?'.pdf':'.txt'));
});
researchRoutes.get('/v1/search/:id/assets/:source',(req,res)=>{
  const run=ownedRun(req,res);
  const src=run?.sources[req.params.source];
  if(!run || !src || !run.assets.some(a=>a.source===src.id)){res.sendStatus(404);return;}
  res.download(join(folder('runs',run.id,'assets'),src.pdf?src.digest+'.pdf':src.id+'.txt'));
});
researchRoutes.get('/v1/search/:id',(req,res)=>{
  const run=ownedRun(req,res);
  if(!run){res.sendStatus(404);return;}
  res.json({...summary(run),orchestrator:agentState(lookupAgent(scope(res))!)});
});
async function runSearch(req:Request,res:Response,mode:'start'|'reply'|'delivered') {
  let locked:ChatAgent|undefined;
  let id:string|undefined;
  try {
    if(closingSessions.has(scope(res).session_id)){res.sendStatus(410);return;}
    const start=mode==='start' ? intake.parse({...req.body,project:scope(res).project_id,analyst_context:true}) : undefined;
    if(req.body?.project && req.body.project!==scope(res).project_id){res.sendStatus(400);return;}
    const agent=lookupAgent(scope(res),!!start);
    if(!agent){res.sendStatus(404);return;}
    if(agent.busy){res.status(409).json({error:'This chat search agent is busy'});return;}
    if(start && [...agent.runIds].some(key=>{const r=loadRun(key);return r && r.status!=='complete';})){
      res.status(409).json({error:'Resume the pending search for this chat first'});return;
    }
    const run=start ? createRun(start) : ownedRun(req,res);
    if(!run){res.sendStatus(404);return;}
    lock(agent);locked=agent;
    id=run.id;agent.runIds.add(id);res.setHeader('X-Search-Id',id);
    if(mode==='delivered'){
      if(run.status!=='complete' || req.body?.recipient!=='analysis_agent'){
        res.status(409).json({error:'Expected delivery of a completed answer to analysis'});return;
      }
      await withAgent(agent,()=>acknowledgeDelivery(run));
    } else if(run.status!=='complete'){
      if(mode==='reply' && Date.now()<run.deadline-SYNTHESIS_MS){
        const input=reply.parse(req.body || {});
        if(run.status==='needs_input' && !input.answer && !input.assets.length){
          res.status(400).json({error:'Supply the requested analysis context or assets'});return;
        }
        if(input.answer) run.dialogue.push('Reply: '+input.answer);
        run.assets.push(...input.assets);run.questions=[];
      }
      cancelTimer(id);
      run.status='running';
      await withAgent(agent,()=>conduct(run));
      if((run.status as Run['status'])==='needs_input') wrapUp(agent,id,run.deadline);
      else remember(agent,run);
    }
    res.json({...summary(run),orchestrator:agentState(agent)});
  } catch(err) {
    res.status(err instanceof z.ZodError?400:502).json({error:(err as Error).message,search_id:id});
  } finally {if(locked) unlock(locked);}
}
researchRoutes.post('/v1/search',(req,res)=>{void runSearch(req,res,'start');});
researchRoutes.post('/v1/search/:id',(req,res)=>{void runSearch(req,res,'reply');});
researchRoutes.post('/v1/search/:id/delivered',(req,res)=>{void runSearch(req,res,'delivered');});
researchRoutes.delete('/v1/session',async (_req,res)=>{
  const sessionId=scope(res).session_id;
  if(closingSessions.has(sessionId)){res.status(409).json({error:'Session is closing'});return;}
  closingSessions.add(sessionId);
  try {
    const agents=sessionAgents(scope(res));
    for(const agent of agents){
      for(const id of agent.runIds) cancelTimer(id);
      agent.lifetime.abort();
    }
    await Promise.all(agents.map(a=>pending.get(a)?.idle));
    for(const agent of agents){
      for(const id of agent.runIds){cancelTimer(id);discardRun(id);}
      forgetAgent(agent);
    }
    res.sendStatus(204);
  }finally{closingSessions.delete(sessionId);}
});
