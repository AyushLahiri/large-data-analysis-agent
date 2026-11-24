import { execFile } from 'node:child_process';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { promisify } from 'node:util';
import { localRead } from '../tools/local-read';
import { Reference, Run, Source, folder, identified, projectDir } from './store';

const extract = promisify(execFile);
export function sourceURL(url: string) {
  try {
    const u=new URL(url);
    u.hash='';
    return u.href;
  } catch { return url; }
}

// Use the stored copy by default. A worker can explicitly reopen its publisher
// link to check freshness. New bytes get a separate identity from the snapshot.
export async function openReference(run: Run, ref: Reference, live: boolean, signal: AbortSignal) {
  const dir=projectDir(run.project!);
  const text=join(dir,ref.id+'.txt');
  const binary=ref.file && join(dir,ref.file);
  let content: string;
  let source: Source = ref;
  let links: [string,string][] = [];
  if (!live && existsSync(text)) content=readFileSync(text,'utf8');
  else if (!live && binary && existsSync(binary)) {
    const pdf=await extract('pdftotext',['-layout',binary,'-'],{signal,timeout:20000,maxBuffer:12*1024*1024});
    content=pdf.stdout.split('\f').map((p,i)=>p.trim()?'[PDF page '+(i+1)+']\n'+p:'').join('\n');
  } else {
    const read=await localRead(ref.url,true,folder('runs',run.id,'documents'),signal);
    const page=read.response.data!;
    content=page.content; links=page.links;
    source={id:identified(page.url+read.digest).slice(0,24),title:page.title,url:page.url,
      digest:read.digest,pdf:read.pdf,retrieved:new Date().toISOString()};
  }
  signal.throwIfAborted();
  if(!content.trim()) throw Error('Saved document has no readable text');
  run.sources[source.id]=source;
  writeFileSync(join(folder('runs',run.id,'documents'),source.id+'.txt'),content);
  return {id:source.id,content,links};
}

