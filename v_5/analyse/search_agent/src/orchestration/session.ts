import { AsyncLocalStorage } from 'node:async_hooks';
import { randomUUID } from 'node:crypto';
import { z } from 'zod';

const id = z.string().regex(/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/);
export const scopeSchema = z.object({session_id:id, project_id:id.max(80), chat_id:id});
export type Scope = z.infer<typeof scopeSchema>;
export const SOL_LIMIT = 500_000;
export type Turn = {question:string; answer:string; sources:string[]};
export type ChatAgent = {
  scope:Scope; key:string; agentId:string; generation:number; used:number;
  lifetime:AbortController; turns:Turn[]; decisions:string[]; handoff:string[]; runIds:Set<string>; busy:boolean;
};
const agents=new Map<string,ChatAgent>();
const current=new AsyncLocalStorage<ChatAgent>();
const keyOf=(s:Scope)=>JSON.stringify([s.session_id,s.project_id,s.chat_id]);
export function lookupAgent(scope:Scope, create=false) {
  const key=keyOf(scope);
  let agent=agents.get(key);
  if(!agent && create){
    agent={scope,key,agentId:randomUUID(),generation:1,used:0,lifetime:new AbortController(),turns:[],decisions:[],handoff:[],runIds:new Set(),busy:false};
    agents.set(key,agent);
  }
  return agent;
}
export function sessionAgents(scope:Scope) {
  return [...agents.values()].filter(a=>a.scope.session_id===scope.session_id);
}
export function forgetAgent(agent:ChatAgent) { agents.delete(agent.key); }
export const currentAgent=()=>current.getStore();
export const withAgent=<T>(agent:ChatAgent,fn:()=>T)=>current.run(agent,fn);
export function rollAgent(agent:ChatAgent) {
  // Pending assignments, verified evidence, deadlines and Luna reservations live
  // on the search run and are supplied independently to the successor.
  agent.handoff=[...agent.handoff,...agent.decisions].slice(-8);
  agent.decisions=[];
  agent.agentId=randomUUID();
  agent.generation++;
  agent.used=0;
}
export function agentState(agent:ChatAgent) {
  return {...agent.scope,agent_id:agent.agentId,generation:agent.generation,
    sol_tokens:agent.used,sol_limit:SOL_LIMIT};
}

