import { OPENAI_API_KEY, OPENAI_BASE_URL } from '../config';
import { SERPQuery } from '../types';
import { TokenTracker } from '../utils/token-tracker';

type Citation = { type: string; url?: string; title?: string; start_index?: number; end_index?: number };
type Output = {
  type: string;
  action?: { sources?: Citation[] };
  content?: { text?: string; annotations?: Citation[] }[];
};

export async function openaiSearch(query: SERPQuery, tracker: TokenTracker, signal?: AbortSignal) {
  const response = await fetch((OPENAI_BASE_URL || 'https://api.openai.com/v1').replace(/\/$/, '') + '/responses', {
    method: 'POST',
    headers: { Authorization: 'Bearer ' + OPENAI_API_KEY, 'Content-Type': 'application/json' },
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(60000)]) : AbortSignal.timeout(60000),
    body: JSON.stringify({
      model: process.env.OPENAI_SEARCH_MODEL || 'gpt-4.1-mini',
      tools: [{ type: 'web_search' }], tool_choice: 'required',
      include: ['web_search_call.action.sources'],
      instructions: 'Search for the requested information. Give a short description for each useful source with inline citations.',
      input: JSON.stringify(query), max_output_tokens: 900, store: false
    })
  });
  if (!response.ok) throw new Error('OpenAI web search returned HTTP ' + response.status);
  const data = await response.json();
  const results = new Map<string, { url: string; title: string; description: string }>();
  for (const item of data.output as Output[]) {
    for (const source of item.action?.sources || []) {
      if (source.url) results.set(source.url, { url: source.url, title: source.title || source.url, description: '' });
    }
    for (const part of item.content || []) {
      for (const cite of part.annotations || []) {
        if (!cite.url || cite.type !== 'url_citation') continue;
        const before = (part.text || '').slice(0, cite.start_index);
        const description = before.split('\n').pop()?.slice(-500) || '';
        results.set(cite.url, { url: cite.url, title: cite.title || cite.url, description });
      }
    }
  }
  if (data.usage) tracker.trackUsage('search', { promptTokens: data.usage.input_tokens,
    completionTokens: data.usage.output_tokens, totalTokens: data.usage.total_tokens });
  return [...results.values()];
}
