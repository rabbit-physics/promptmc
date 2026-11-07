"""Coordinator admin API client shared by the product backend and CLI tool.

The admin token lives outside the repository (default ~/pmx-coord/admin.token, overridable via
P1_NODES_TOKEN_FILE or constructor arg) and is re-read on every request.
"""
import hashlib
import http.client
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

DEFAULT_URL = 'http://10.0.0.10:8991'
DEFAULT_TOKEN_FILE = '~/pmx-coord/admin.token'


class NodesError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


class Client:
    def __init__(self, url=None, token_file=None, timeout=10):
        self.url = (url or os.environ.get('P1_NODES_URL') or DEFAULT_URL).rstrip('/')
        self.token_file = Path(token_file or os.environ.get('P1_NODES_TOKEN_FILE') or DEFAULT_TOKEN_FILE).expanduser()
        self.timeout = timeout

    def _token(self):
        try:
            t = self.token_file.read_text().strip()
        except OSError:
            raise NodesError(503, f'Cannot read admin token file {self.token_file}')
        if not t:
            raise NodesError(503, f'Admin token file {self.token_file} is empty')
        return t

    def call(self, method, path, body=None, timeout=None):
        data = json.dumps(body).encode() if body is not None else (b'{}' if method == 'POST' else None)
        r = urllib.request.Request(self.url + path, data=data, method=method,
                                   headers={'Authorization': 'Bearer ' + self._token(), 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(r, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get('error') or f'Coordinator returned {e.code}'
            except Exception:
                msg = f'Coordinator returned {e.code}'
            raise NodesError(e.code, msg)
        except (urllib.error.URLError, OSError) as e:
            raise NodesError(503, f'Cannot connect to coordinator {self.url}: {getattr(e, "reason", e)}')
        except (http.client.HTTPException, ValueError) as e:
            # Incomplete response (e.g., coordinator restarting mid-reply) or malformed JSON
            raise NodesError(503, f'Incomplete coordinator response: {type(e).__name__}')

    def nodes(self):
        return self.call('GET', '/admin/nodes')

    def set_enabled(self, name, enabled):
        return self.call('POST', f'/admin/nodes/{quote(name, safe="")}/enable', {'enabled': bool(enabled)})

    def stop(self, name):
        return self.call('POST', f'/admin/nodes/{quote(name, safe="")}/stop', {})

    def rescan(self, name):
        return self.call('POST', f'/admin/nodes/{quote(name, safe="")}/rescan', {})

    def jobs(self):
        return self.call('GET', '/admin/jobs')

    def job(self, jid, events=False):
        return self.call('GET', f'/admin/jobs/{quote(jid, safe="")}' + ('?events=1' if events else ''))

    def create_job(self, body):
        # Creating Reference MC jobs probes 1 history to discover opened data files, which can take tens of seconds
        return self.call('POST', '/admin/jobs', body, timeout=900)

    def plan(self, jobs, timeout=3):
        # Interactive UI estimation: fall back quickly to local estimates if the coordinator stalls
        return self.call('POST', '/admin/plan', {'jobs': jobs}, timeout=timeout)

    def cancel(self, jid):
        return self.call('POST', f'/admin/jobs/{quote(jid, safe="")}/cancel', {})

    def merge(self, jid):
        return self.call('GET', f'/admin/jobs/{quote(jid, safe="")}/merge', timeout=600)

    def hold(self, name, seconds, why=''):
        return self.call('POST', f'/admin/nodes/{quote(name, safe="")}/hold', {'seconds': seconds, 'why': why})

    def purge(self, jid):
        return self.call('POST', f'/admin/jobs/{quote(jid, safe="")}/purge', {})

    def withdraw(self, jid):
        return self.call('POST', f'/admin/jobs/{quote(jid, safe="")}/withdraw', {})

    def activate(self, jid):
        return self.call('POST', f'/admin/jobs/{quote(jid, safe="")}/activate', {})

    def _raw(self, method, path, data=None, timeout=300):
        r = urllib.request.Request(self.url + path, data=data, method=method,
                                   headers={'Authorization': 'Bearer ' + self._token(), 'Content-Type': 'application/octet-stream'})
        try:
            return urllib.request.urlopen(r, timeout=timeout)
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get('error') or f'Coordinator returned {e.code}'
            except Exception:
                msg = f'Coordinator returned {e.code}'
            raise NodesError(e.code, msg)
        except (urllib.error.URLError, OSError) as e:
            raise NodesError(503, f'Cannot connect to coordinator {self.url}: {getattr(e, "reason", e)}')
        except http.client.HTTPException as e:
            raise NodesError(503, f'Incomplete coordinator response: {type(e).__name__}')

    def put_blob(self, path):
        """Upload a file and return its sha256."""
        data = Path(path).read_bytes()
        h = hashlib.sha256(data).hexdigest()
        with self._raw('PUT', f'/admin/blob/{h}', data):
            pass
        return h

    def get_blob(self, h, path):
        """Download blob by sha256 to path; verifies digest and leaves no partial file on error."""
        path = Path(path)
        tmp = path.with_name(path.name + '.part')
        d = hashlib.sha256()
        resp = self._raw('GET', f'/admin/blob/{h}')
        try:
            with resp, open(tmp, 'wb') as g:
                for chunk in iter(lambda: resp.read(1 << 20), b''):
                    d.update(chunk)
                    g.write(chunk)
        except (OSError, http.client.HTTPException) as e:
            tmp.unlink(missing_ok=True)
            raise NodesError(503, f'Interrupted download for blob {h[:12]}: {type(e).__name__}')
        if d.hexdigest() != h:
            tmp.unlink()
            raise NodesError(502, f'Checksum mismatch for blob {h[:12]}')
        tmp.replace(path)
        return path
