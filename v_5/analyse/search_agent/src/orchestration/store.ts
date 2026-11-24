import { createHash, randomUUID } from 'node:crypto';
import { existsSync, mkdirSync, mkdtempSync, rmSync, readFileSync, readdirSync, renameSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { z } from 'zod';
import { Ledger, Usage } from './model';

export const projectKey = z.string().regex(/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$/);
export const asset = z.object({name: z.string().max(300), url: z.string().url().optional(),
  text: z.string().min(1).max(80000).optional(), pdf_base64:z.string().min(8).max(8*1024*1024).optional()}).refine(a => a.url || a.text || a.pdf_base64);
export const intake = z.object({question: z.string().trim().min(1).max(30000),
  project: projectKey.optional(), analyst_context: z.boolean().optional(), assets: z.array(asset).max(30).default([])});
export type Source = {id: string; url: string; title: string; digest: string; retrieved: string; pdf: boolean};
export type Reference = Source & {reason: string; file?: string; summary?: string; publisher?: string; publication_year?: number | null};
export type Curation = {status:'awaiting_delivery'|'curating'|'complete'; delivered_at?:string; processed:string[]; saved:string[]};
export type Finding = {kind?:'repository'|'web'; referenceIds?:string[]; state?: 'active'|'returned'|'interrupted'; task: string; notes: string; evidence: {source: string; quote: string; date: string}[]};
export type Run = Omit<z.infer<typeof intake>, "assets"> & {assets:(z.infer<typeof asset> & {source?:string})[]} & {id: string; date: string; deadline: number; analystRequests: number; ledger: Ledger; waves: number;
  status: 'running'|'needs_input'|'complete'; dialogue: string[]; findings: Finding[];
  sources: Record<string, Source>; questions: string[]; recalledHistory?:{question:string;answer:string}[]; curation?:Curation; result?: object; discoveryTokens: number; usage?: Usage};
const scratch = mkdtempSync(join(tmpdir(),'analysis-search-'));
const runs = new Map<string,Run>();
const root = process.env.RESEARCH_DATA_DIR || '/data/research';
export const identified = (value: string) => createHash('sha256').update(value).digest('hex');
export function folder(...parts: string[]) {
  const path = join(parts[0]==='runs' ? scratch : root, ...parts);
  mkdirSync(path, {recursive:true});
  return path;
}
export function atomic(path: string, value: string) {
  const tmp = path + '.' + randomUUID();
  writeFileSync(tmp, value);
  renameSync(tmp, path);
}
export const persist = (run: Run) => { runs.set(run.id,structuredClone(run)); };
export function loadRun(id: string) { const saved=runs.get(id);return saved && structuredClone(saved); }
export function discardRun(id:string) {
  if (!z.string().uuid().safeParse(id).success) throw Error('Invalid run ID');
  runs.delete(id);
  rmSync(join(scratch,'runs',id),{recursive:true,force:true});
}
export function library(project?: string): Reference[] {
  if (!project) return [];
  const dir = join(root,'projects',projectKey.parse(project));
  if (!existsSync(dir)) return [];
  return readdirSync(dir).filter(n=>n.endsWith('.json')).map(n=>{
    const ref:Reference=JSON.parse(readFileSync(join(dir,n),'utf8'));
    if(ref.url.startsWith('/v1/search/')) ref.url='/v1/projects/'+project+'/references/'+ref.id+'/document';
    return ref;
  });
}
export function projectDir(project: string) { return folder('projects', projectKey.parse(project)); }

