"""Web backend server: POST /rpc (JSON-RPC 2.0), GET /events (SSE), GET /api/* (read-only resources),
/api/llm/* (LLM settings), /api/nodes* (proxied to coordinator admin API), and static assets.

Usage: python -m p1.server --root var --port 8979 [--nodes-url http://10.0.0.10:8991]
                          [--nodes-token-file ~/pmx-coord/admin.token] [--no-dispatch]
"""
import argparse
import json
import math
import mimetypes
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .kernel import documents as D
from .kernel import protocol
from .kernel.core import Kernel
from .kernel.llm import LlmUnavailable
from .kernel.protocol import ProtocolError
from .kernel.store import jl
from .nodes.client import Client as NodesClient
from .nodes.client import NodesError

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'web')
KERNEL = None
NODES = None


def _finite(x):
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _finite(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_finite(v) for v in x]
    return x


def dumps(x):
    """Replace Infinity/NaN with null so browser JSON.parse never fails."""
    try:
        return json.dumps(x, ensure_ascii=False, default=str, allow_nan=False)
    except ValueError:
        return json.dumps(_finite(x), ensure_ascii=False, default=str)


class Handler(BaseHTTPRequestHandler):
    server_version = 'promptmc-p1'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        if os.environ.get('P1_HTTP_LOG'):
            sys.stderr.write('%s - %s\n' % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype='application/json; charset=utf-8', headers=None):
        if isinstance(body, (dict, list)):
            body = dumps(body).encode('utf-8')
        elif isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store' if ctype.startswith('application/json') else 'no-cache')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get('Content-Length') or 0)
        if u.path == '/api/docs/upload':
            return self._upload(u, n)
        try:
            req = json.loads(self.rfile.read(n) or b'{}')
        except json.JSONDecodeError:
            return self._send(400, {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Invalid JSON'}})
        if u.path == '/rpc':
            rid = req.get('id')
            try:
                res = KERNEL.rpc(req.get('method', ''), req.get('params') or {})
                return self._send(200, {'jsonrpc': '2.0', 'id': rid, 'result': res})
            except ProtocolError as e:
                return self._send(200, {'jsonrpc': '2.0', 'id': rid,
                                        'error': {'code': e.code, 'message': e.message, 'data': e.data}})
            except Exception as e:
                traceback.print_exc()
                return self._send(200, {'jsonrpc': '2.0', 'id': rid,
                                        'error': {'code': -32000, 'message': f'{type(e).__name__}: {e}'}})
        if u.path.startswith('/api/llm/'):
            return self._llm_post(u.path, req)
        if u.path.startswith('/api/nodes/'):
            return self._nodes_post(u.path, req)
        if u.path == '/api/preview':
            try:
                g = KERNEL.goal(req['goal'])
                return self._send(200, KERNEL.preview(g, {k: float(v) for k, v in req['changes'].items()}))
            except Exception as e:
                return self._send(400, {'error': f'{type(e).__name__}: {e}'})
        if u.path == '/api/talks/archive':
            try:
                return self._send(200, KERNEL.talk_archive(req['id'], bool(req.get('archived', True))))
            except ProtocolError as e:
                return self._send(404, {'error': e.message})
            except (KeyError, TypeError, AttributeError):
                return self._send(400, {'error': 'Missing talk id'})
        if u.path == '/api/rules/try':
            try:
                KERNEL._goal_or_404(req['goal'])
                return self._send(200, KERNEL.rule_try(req['goal'], req.get('expr', ''), req.get('cases')))
            except Exception as e:
                return self._send(400, {'error': f'{type(e).__name__}: {e}'})
        return self._send(404, {'error': 'not found'})

    def _upload(self, u, n):
        """Upload raw file bytes with filename passed via ?name= query parameter."""
        if n > D.MAX_BYTES:
            self.close_connection = True
            return self._send(413, {'error': f'File exceeds {D.MAX_BYTES // 1048576} MB limit'})
        data = self.rfile.read(n)
        name = parse_qs(u.query).get('name', ['file'])[0]
        try:
            return self._send(200, KERNEL.docs.ingest(name, data))
        except D.DocError as e:
            return self._send(400, {'error': str(e)})

    def _doc_get(self, p, q):
        """/api/docs/<hash>: metadata; /page/<n>.png: rendered page image; /text/<n>: page text."""
        parts = p.split('/')[3:]
        h = parts[0]
        try:
            m = KERNEL.docs.meta(h)
            if not m:
                return self._send(404, {'error': 'Document not found'})
            if len(parts) == 1:
                return self._send(200, m)
            if len(parts) == 3 and parts[1] == 'page':
                img = KERNEL.docs.png(h, int(parts[2].split('.')[0]), int(q.get('dpi', 110)))
                if img is None:
                    return self._send(404, {'error': 'Document has no page images'})
                return self._send(200, img[0], img[1], {'Cache-Control': 'max-age=31536000, immutable'})
            if len(parts) == 3 and parts[1] == 'text':
                return self._send(200, {'page': int(parts[2]), 'text': KERNEL.docs.page(h, int(parts[2]))})
        except D.DocError as e:
            return self._send(400, {'error': str(e)})
        return self._send(404, {'error': 'not found'})

    def _llm_post(self, path, req):
        llm = KERNEL.llm
        if not (self.headers.get('Content-Type') or '').startswith('application/json'):
            return self._send(415, {'error': 'Expected application/json'})
        try:
            if path == '/api/llm/groups':
                gid = llm.save_group(req)
                return self._send(200, dict(llm.public(), saved=gid))
            if path == '/api/llm/groups/delete':
                llm.delete_group(req.get('id'))
                return self._send(200, llm.public())
            if path == '/api/llm/key':
                return self._send(200, {'key': llm.reveal_key(req.get('id'))})
            if path == '/api/llm/models':
                return self._send(200, {'models': llm.list_models(req)})
            if path == '/api/llm/test':
                return self._send(200, llm.test(req))
        except (ValueError, LlmUnavailable) as e:
            return self._send(400, {'error': str(e)})
        return self._send(404, {'error': 'not found'})

    def _nodes_call(self, fn):
        """Forward coordinator 400/404/409/503 responses directly; map other failures to 502."""
        try:
            return self._send(200, fn(NODES or NodesClient()))
        except NodesError as e:
            code = e.code if e.code in (400, 404, 409, 503) else 502
            return self._send(code, {'error': e.msg})

    def _nodes_post(self, path, req):
        if not (self.headers.get('Content-Type') or '').startswith('application/json'):
            return self._send(415, {'error': 'Expected application/json'})
        name = req.get('node')
        if path in ('/api/nodes/enable', '/api/nodes/stop', '/api/nodes/rescan') and not (isinstance(name, str) and name):
            return self._send(400, {'error': 'Missing node name'})
        if path == '/api/nodes/enable':
            if not isinstance(req.get('enabled'), bool):
                return self._send(400, {'error': 'enabled must be boolean'})
            return self._nodes_call(lambda c: c.set_enabled(name, req['enabled']))
        if path == '/api/nodes/stop':
            return self._nodes_call(lambda c: c.stop(name))
        if path == '/api/nodes/rescan':
            return self._nodes_call(lambda c: c.rescan(name))
        if path == '/api/nodes/jobs':
            return self._nodes_call(lambda c: c.create_job(req))
        if path == '/api/nodes/cancel':
            if not isinstance(req.get('id'), str):
                return self._send(400, {'error': 'Missing job id'})
            return self._nodes_call(lambda c: c.cancel(req['id']))
        return self._send(404, {'error': 'not found'})

    def _nodes_get(self, p, q):
        if p == '/api/nodes':
            return self._nodes_call(lambda c: c.nodes())
        if p == '/api/nodes/jobs':
            return self._nodes_call(lambda c: c.jobs())
        if p == '/api/nodes/job':
            return self._nodes_call(lambda c: c.job(q['id'], q.get('events') == '1'))
        if p == '/api/nodes/merge':
            return self._nodes_call(lambda c: c.merge(q['id']))
        return self._send(404, {'error': 'not found'})

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        try:
            if p == '/events':
                return self._sse(int(q.get('after', self.headers.get('Last-Event-ID') or 0)))
            if p == '/api/goals':
                return self._send(200, KERNEL.goal_list())
            if p == '/api/state':
                snap = KERNEL.snapshot(q['goal'])
                return self._send(200 if snap else 404, snap or {'error': 'no goal'})
            if p == '/api/protocol':
                return self._send(200, {'requests': protocol.REQUESTS, 'notifications': protocol.NOTIFICATIONS,
                                        'count': len(protocol.METHODS)})
            if p.startswith('/api/node/'):
                h = p.rsplit('/', 1)[1]
                n = KERNEL.store.node(h)
                if not n:
                    return self._send(404, {'error': 'no node'})
                if n['kind'] == 'solver_input':
                    n['text'] = KERNEL.store.get_blob(h, as_json=False)
                    if n['meta'].get('line_map'):
                        n['line_map'] = KERNEL.store.get_blob(n['meta']['line_map'])
                else:
                    n['content'] = KERNEL.store.get_blob(h)
                    if n['kind'] == 'model':
                        from .domain import model as M
                        n['fields'] = M.physics_flat(n['content'])
                return self._send(200, n)
            if p.startswith('/api/blob/'):
                h = p.rsplit('/', 1)[1]
                row = KERNEL.store.q1('SELECT kind FROM blobs WHERE hash=?', (h,))
                if not row:
                    return self._send(404, {'error': 'no blob'})
                with open(KERNEL.store.blob_path(h), 'rb') as f:
                    data = f.read()
                ctype = {'png': 'image/png', 'text': 'text/plain; charset=utf-8'}.get(row['kind'],
                                                                                    'application/json; charset=utf-8')
                return self._send(200, data, ctype, {'Cache-Control': 'max-age=31536000, immutable'})
            if p.startswith('/api/evidence/'):
                e = KERNEL.store.evidence(int(p.rsplit('/', 1)[1]))
                return self._send(200 if e else 404, e or {'error': 'no evidence'})
            if p == '/api/evidence':
                return self._send(200, KERNEL.evidence_for_goal(q['goal']))
            if p == '/api/history':
                rows = KERNEL.store.q("SELECT hash, kind, case_id, inputs, meta, created FROM nodes WHERE goal=? "
                                      "AND (case_id=? OR ? = '') ORDER BY created", (q['goal'], q.get('case', ''),
                                                                                    q.get('case', '')))
                for r in rows:
                    r['inputs'], r['meta'] = jl(r['inputs']), jl(r['meta'])
                return self._send(200, rows)
            if p.startswith('/api/docs/'):
                return self._doc_get(p, q)
            if p == '/api/corrections':
                return self._send(200, KERNEL.corrections(q.get('goal')))
            if p == '/api/talks':
                return self._send(200, KERNEL.talk_list())
            if p == '/api/talk':
                t = KERNEL.talk_get(q['id'])
                return self._send(200 if t else 404, t or {'error': 'no talk'})
            if p == '/api/rule_edits':
                return self._send(200, KERNEL.rule_edits(q.get('goal')))
            if p == '/api/rules/fields':
                KERNEL._goal_or_404(q['goal'])
                return self._send(200, KERNEL.rule_fields(q['goal']))
            if p == '/api/llm/settings':
                return self._send(200, KERNEL.llm.public())
            if p == '/api/llm':
                rows = KERNEL.store.q('SELECT * FROM llm_calls WHERE goal=? ORDER BY created', (q['goal'],))
                return self._send(200, rows)
            if p == '/api/events.jsonl':
                rows = KERNEL.store.events_after(int(q.get('after', 0)), limit=100000)
                body = '\n'.join(dumps(r) for r in rows) + '\n'
                return self._send(200, body, 'application/x-ndjson; charset=utf-8')
            if p == '/api/nodes' or p.startswith('/api/nodes/'):
                return self._nodes_get(p, q)
            if p == '/api/health':
                return self._send(200, {'ok': True, 'runner': KERNEL.runner.available(), 'llm': KERNEL.llm.describe(),
                                        'last_seq': KERNEL.store.last_seq()})
            return self._static(p)
        except KeyError as e:
            return self._send(400, {'error': f'Missing parameter {e}'})

    def _static(self, p):
        if p in ('/', ''):
            p = '/index.html'
        path = os.path.normpath(os.path.join(WEB, p.lstrip('/')))
        if not path.startswith(WEB) or not os.path.isfile(path):
            return self._send(404, 'not found', 'text/plain; charset=utf-8')
        ctype = mimetypes.guess_type(path)[0] or 'application/octet-stream'
        if ctype.startswith('text/') or ctype in ('application/javascript',):
            ctype += '; charset=utf-8'
        with open(path, 'rb') as f:
            return self._send(200, f.read(), ctype)

    def _sse(self, after):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        seq = after
        try:
            self.wfile.write(b': open\n\n')
            self.wfile.flush()
            while True:
                evs = KERNEL.store.wait_events(seq, timeout=15.0)
                if not evs:
                    self.wfile.write(b': keepalive\n\n')
                    self.wfile.flush()
                    continue
                for e in evs:
                    data = dumps({'method': e['method'], 'params': e['params'], 'seq': e['seq'], 'goal': e['goal']})
                    self.wfile.write(f"id: {e['seq']}\nevent: notification\ndata: {data}\n\n".encode('utf-8'))
                    seq = e['seq']
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def main(argv=None):
    global KERNEL, NODES
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default=os.environ.get('P1_ROOT', 'var'))
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8979)
    ap.add_argument('--nodes-url', help='Coordinator URL (defaults to P1_NODES_URL, then http://10.0.0.10:8991)')
    ap.add_argument('--nodes-token-file', help='Admin token file (defaults to P1_NODES_TOKEN_FILE, then ~/pmx-coord/admin.token)')
    a = ap.parse_args(argv)
    NODES = NodesClient(a.nodes_url, a.nodes_token_file)
    KERNEL = Kernel(a.root)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f'promptmc-p1 on http://{a.host}:{a.port}  root={KERNEL.root}', flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
