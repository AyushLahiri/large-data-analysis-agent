import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { join } from 'node:path';
import { JSDOM, VirtualConsole } from 'jsdom';
import { ReadResponse } from '../types';

const execute = promisify(execFile);
export async function localRead(url: string, includeLinks = false, archive?: string, signal?: AbortSignal): Promise<{response: ReadResponse; digest: string; pdf: boolean}> {
  const { stdout } = await execute('python3', [join(__dirname, 'public-page.py'), url, ...(archive ? [archive] : [])],
    { signal, timeout: 35000, maxBuffer: 10 * 1024 * 1024 });
  const page = JSON.parse(stdout);
  let content: string = page.content;
  let title = page.url;
  const links: [string, string][] = [];
  if (page.kind === 'html') {
    const dom = new JSDOM(content, { url: page.url, virtualConsole: new VirtualConsole() });
    const doc = dom.window.document;
    title = doc.title || title;
    doc.querySelectorAll('script,style,noscript,nav,footer,svg,template').forEach(el => el.remove());
    const main = doc.querySelector('main,article') || doc.body;
    if (includeLinks) main.querySelectorAll('a[href]').forEach(el => {
      const href = (el as HTMLAnchorElement).href;
      if (/^https?:\/\//i.test(href) && links.length < 200) links.push([el.textContent?.trim() || href, href]);
    });
    main.querySelectorAll('p,div,br,li,tr,h1,h2,h3,h4').forEach(el => el.append('\n'));
    content = (main.textContent || '').replace(/[ \t]+/g, ' ').replace(/\n(?:\s*\n)+/g, '\n').trim();
    dom.window.close();
  }
  if (!content.trim()) throw new Error('Page contains no readable text');
  return { digest: page.digest, pdf: page.pdf, response: { code: 200, status: 200, data: {
    url: page.url, title, description: '', content, links, images: {}, usage: { tokens: 0 }
  } } };
}
