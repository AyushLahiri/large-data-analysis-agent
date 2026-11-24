"""Read bounded public HTTP responses, pinning the validated address on each hop."""
import hashlib
import os
import tempfile
import http.client
import ipaddress
import json
import socket
import subprocess
import sys
from urllib.parse import quote, urljoin, urlsplit

LIMIT = 12 * 1024 * 1024

def download(url, archive=None):
    for _ in range(6):
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            raise ValueError('Expected a public HTTP(S) URL')
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        if port not in (80, 443):
            raise ValueError('Unsupported port')
        addresses = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
        for entry in addresses:
            address = ipaddress.ip_address(entry[4][0])
            if not address.is_global or address.is_multicast or getattr(address, 'ipv4_mapped', None):
                raise ValueError('Only public addresses are allowed')
        connection_type = http.client.HTTPSConnection if parts.scheme == 'https' else http.client.HTTPConnection
        conn = connection_type(parts.hostname, port, timeout=12)
        conn._create_connection = lambda *args, **kwargs: socket.create_connection(addresses[0][4][:2], timeout=12)
        try:
            path = quote(parts.path or '/', safe="/%:@!$&'()*+,;=-._~")
            if parts.query:
                path += '?' + quote(parts.query, safe="/%:@!$&'()*+,;=?-._~")
            conn.request('GET', path, headers={'User-Agent': 'v5-research/1.0', 'Accept-Encoding': 'identity'})
            res = conn.getresponse()
            if res.status in (301, 302, 303, 307, 308) and res.getheader('Location'):
                url = urljoin(url, res.getheader('Location'))
                continue
            if res.status != 200:
                raise ValueError(f'Publisher returned HTTP {res.status}')
            raw = res.read(LIMIT + 1)
            if len(raw) > LIMIT:
                raise ValueError('Document exceeds download limit')
            mime = res.headers.get_content_type()
            if raw.startswith(b'%PDF-') or mime == 'application/pdf':
                out = subprocess.run(['pdftotext', '-layout', '-', '-'], input=raw, capture_output=True, check=True, timeout=12)
                digest = hashlib.sha256(raw).hexdigest()
                if archive:
                    with tempfile.NamedTemporaryFile(dir=archive, delete=False) as tmp:
                        tmp.write(raw)
                    os.replace(tmp.name, os.path.join(archive, digest + '.pdf'))
                pages = out.stdout.decode('utf-8', 'replace').split('\f')
                if not any(page.strip() for page in pages):
                    raise ValueError('PDF has no extractable text')
                content = '\n'.join(f'[PDF page {i}]\n{page}' for i, page in enumerate(pages, 1) if page.strip())
                return dict(url=url, kind='text', digest=digest, pdf=True, content=content[:1500000])
            if not (mime.startswith('text/') or mime in ('application/xhtml+xml', 'application/json', 'application/xml')):
                raise ValueError('Unsupported content type: ' + mime)
            return dict(url=url, digest=hashlib.sha256(raw).hexdigest(), pdf=False, kind='html' if 'html' in mime else 'text',
                        content=raw.decode(res.headers.get_content_charset() or 'utf-8', 'replace')[:1500000])
        finally:
            conn.close()
    raise ValueError('Too many redirects')

if __name__ == '__main__':
    try:
        print(json.dumps(download(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None), ensure_ascii=False))
    except Exception as exc:
        sys.exit(str(exc))

