import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync, copyFileSync } from 'node:fs';
import { join } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { Run, folder, identified } from './store';

const execute = promisify(execFile);
export async function prepareAssets(run: Run, signal?: AbortSignal) {
  const dir=folder('runs',run.id,'assets');
  for(const item of run.assets) {
    signal?.throwIfAborted();
    if(item.source || (!item.text && !item.pdf_base64)) continue;
    let text=item.text || '';
    const pdf=!!item.pdf_base64;
    const bytes=pdf?Buffer.from(item.pdf_base64!, 'base64'):Buffer.from(text);
    const digest=createHash('sha256').update(bytes).digest('hex');
    const id=identified(item.name+digest).slice(0,24);
    if(pdf){
      if(!bytes.subarray(0,5).equals(Buffer.from('%PDF-'))) throw Error('Expected PDF attachment');
      writeFileSync(join(dir,digest+'.pdf'),bytes);
      const result=await execute('pdftotext',['-layout',join(dir,digest+'.pdf'),'-'],{signal,timeout:20000,maxBuffer:12*1024*1024});
      text=result.stdout.split('\f').map((p,i)=>p.trim()?'[PDF page '+(i+1)+']\n'+p:'').join('\n').trim();
      if(!text) throw Error('PDF attachment requires OCR or a supplied text version');
      copyFileSync(join(dir,digest+'.pdf'),join(folder('runs',run.id,'documents'),digest+'.pdf'));
    }
    writeFileSync(join(dir,id+'.txt'),text);
    writeFileSync(join(folder('runs',run.id,'documents'),id+'.txt'),text);
    run.sources[id]={id,url:item.url || '/v1/search/'+run.id+'/assets/'+id,title:item.name,digest,pdf,retrieved:new Date().toISOString()};
    item.source=id;delete item.pdf_base64;delete item.text;
  }
}
export function attachedContent(run:Run,id:string) {
  return readFileSync(join(folder('runs',run.id,'assets'),id+'.txt'),'utf8');
}

