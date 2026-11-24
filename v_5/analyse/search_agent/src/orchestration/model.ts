import { AsyncLocalStorage } from 'node:async_hooks';
import { OPENAI_API_KEY, OPENAI_BASE_URL } from '../config';

import { currentAgent, rollAgent, SOL_LIMIT } from './session';

export const SOL = 'gpt-6.2-sol';
export const LUNA = 'gpt-6-luna';
export type Ledger = { used: number; reserved: number; replenishment: string | null };
// Billed tokens per model for one search (input includes cached input; output includes reasoning).
export type Usage = Record<string, { input: number; cached: number; output: number; calls: number }>;
export const usageScope = new AsyncLocalStorage<Usage>();
function tally(model: string, usage: any) {
  const store = usageScope.getStore();
  if (!store || !usage) return;
  const entry = store[model] ||= { input: 0, cached: 0, output: 0, calls: 0 };
  entry.input += usage.input_tokens || 0;
  entry.cached += usage.input_tokens_details?.cached_tokens || 0;
  entry.output += usage.output_tokens || 0;
  entry.calls += 1;
}
export const limit = (l: Ledger) => 250000 + (l.replenishment ? 100000 : 0);

export function reserve(l: Ledger, input: number, desired: number) {
  const output = Math.min(desired, limit(l) - l.used - l.reserved - input);
  if (output < 512) throw new Error('Insufficient unreserved Luna tokens');
  l.reserved += input + output;
  return output;
}

async function post(route: string, body: object, signal?: AbortSignal) {
  const res = await fetch((OPENAI_BASE_URL || 'https://api.openai.com/v1').replace(/\/$/, '') + route, {
    method: 'POST', headers: {Authorization: 'Bearer ' + OPENAI_API_KEY, 'Content-Type': 'application/json'},
    body: JSON.stringify(body), signal: signal ? AbortSignal.any([signal,AbortSignal.timeout(180000)]) : AbortSignal.timeout(180000)
  });
  if (!res.ok) throw new Error('OpenAI ' + route + ' returned HTTP ' + res.status);
  return res.json();
}

// Luna calls use explicit history and no hosted tools. Count the complete input, then
// atomically reserve input + maximum output before sending (including reasoning tokens).
export async function decision(model: typeof SOL | typeof LUNA, instructions: string, input: unknown,
  accounting?: { ledger: Ledger; save: () => void }, signal?: AbortSignal) {
  const lifetime=currentAgent()?.lifetime.signal;
  if(lifetime) signal=signal ? AbortSignal.any([lifetime,signal]) : lifetime;
  signal?.throwIfAborted();
  const request = { model, instructions, input: 'Return JSON.\n' + JSON.stringify(input),
    text: { format: {type: 'json_object'} }, reasoning: {effort: model === SOL ? 'high' : 'xhigh'} };
  let output = model === SOL ? 16000 : 10000;
  let held = 0;
  if (model === LUNA) {
    if (!accounting) throw new Error('Missing Luna ledger');
    const count = (await post('/responses/input_tokens', request, signal)).input_tokens;
    if (!Number.isSafeInteger(count) || count < 1) throw new Error('Unavailable input token count');
    signal?.throwIfAborted();
    output = reserve(accounting.ledger, count, output);
    held = count + output;
    accounting.save();
  }
  const agent = model===SOL ? currentAgent() : undefined;
  if(agent){
    const setContext=()=>{
      request.input='Return JSON.\n'+JSON.stringify({input,chatContext:{generation:agent.generation,
        recentTurns:agent.turns,handoff:agent.handoff,recentDecisions:agent.decisions}});
    };
    const countInput=async()=>{
      setContext();
      const n=(await post('/responses/input_tokens',request,signal)).input_tokens;
      if(!Number.isSafeInteger(n) || n<1) throw Error('Unavailable Sol input token count');
      return n as number;
    };
    let count=await countInput();
    if(agent.used>0 && agent.used+count+output>SOL_LIMIT){
      rollAgent(agent);
      count=await countInput();
    }
    output=Math.min(output,SOL_LIMIT-agent.used-count);
    if(output<512) throw Error('Sol request exceeds a fresh orchestrator generation');
    signal?.throwIfAborted();
    held=count+output;
    agent.used+=held;
  }
  const response = await post('/responses', {...request, max_output_tokens: output, store: false}, signal);
  tally(model, response.usage);
  if(agent){
    const total=response.usage?.total_tokens;
    if(!Number.isSafeInteger(total) || total<0) throw Error('Unknown Sol usage; reservation retained');
    agent.used+=total-held;
  }
  if (accounting && held) {
    const total = response.usage?.total_tokens;
    if (!Number.isSafeInteger(total) || total < 0) throw new Error('Unknown worker usage; reservation retained');
    accounting.ledger.reserved -= held;
    accounting.ledger.used += total;
    accounting.save();
  }
  if (response.status !== 'completed') throw new Error('Incomplete ' + model + ' decision');
  const text = response.output.flatMap((o: any) => o.content || [])
    .filter((c: any) => c.type === 'output_text').map((c: any) => c.text).join('');
  const result=JSON.parse(text);
  if(agent){
    const note=JSON.stringify(result);
    agent.decisions.push(note.slice(0,1400));
    if(agent.decisions.length>8) agent.decisions.shift();
  }
  return result;
}
