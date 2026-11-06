"""LLM client: drives precheck planning, tool-using conversation (read queries run live; edits emit
review cards), discrepancy attribution, report narratives, and document extraction.

API provider groups (name, enabled, provider, base_url, key, models, params) are stored in
<root>/llm.json, falling back to PMX_HOME configuration if absent. Each goal records its selected
group, model, and reasoning effort under spec.llm. Streaming responses emit output/textDelta
notifications with reasoning chunks and a final done marker; token usage is logged to llm_calls.
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlsplit

from ..util import now, sha256

PURPOSES = ('intake', 'precheck', 'parse_edit', 'answer', 'attribution', 'report', 'extract', 'check', 'assist')
PROVIDERS = ('openai', 'anthropic')
EFFORTS = ('off', 'low', 'medium', 'high', 'max')
# Reasoning tokens count against max_tokens: expand token budgets and timeouts when thinking is enabled
THINK_TOKENS = {'low': 8000, 'medium': 16000, 'high': 24000, 'max': 32000}
THINK_SECONDS = {'low': 180, 'medium': 300, 'high': 600, 'max': 900}
ANTHROPIC_BUDGET = {'low': 2048, 'medium': 6144, 'high': 16384, 'max': 32000}
RESERVED = ('model', 'messages', 'system', 'stream')
DELTA_EVERY = 0.8


class LlmUnavailable(Exception):
    pass


class LlmBadReply(LlmUnavailable):
    """API call succeeded, but no valid JSON object could be parsed from the reply."""


class Conn:
    def __init__(self, provider, base_url, key):
        self.provider, self.base_url, self.key = provider, base_url, key


def _env():
    """Load fallback configuration from PMX_HOME: (provider, base_url, model, key) or None."""
    home = os.environ.get('PMX_HOME')
    if not home or not os.path.isdir(home):
        return None
    if home not in sys.path:
        sys.path.insert(0, home)
    try:
        from pmx.config import load_config, private_environment, ENV_KEYS  # noqa: WPS433
        cfg = load_config()
        if not cfg.api_key_present or not cfg.base_url:
            return None
        key = private_environment(cfg).get(ENV_KEYS['api_key'], '')
    except Exception:
        return None
    return cfg.provider, _norm_url(cfg.base_url), cfg.model, key


def _norm_url(u):
    return str(u or '').strip().rstrip('/')


def _merge(payload, params):
    """Merge custom request parameters; null values remove keys from the payload."""
    for k, v in (params or {}).items():
        if v is None:
            payload.pop(k, None)
        else:
            payload[k] = v
    return payload


def user_content(provider, text, images=None):
    """Build user message content; images is a list of (bytes, mime_type) pairs."""
    if not images:
        return text
    enc = [(base64.b64encode(data).decode('ascii'), mt) for data, mt in images]
    if provider == 'openai':
        return [{'type': 'text', 'text': text}] + [
            {'type': 'image_url', 'image_url': {'url': f'data:{mt};base64,{b}'}} for b, mt in enc]
    return [{'type': 'image', 'source': {'type': 'base64', 'media_type': mt, 'data': b}} for b, mt in enc] + [
        {'type': 'text', 'text': text}]


def tool_specs(provider, tools):
    """Convert [{'name', 'description', 'parameters'}] to provider-specific tool schemas."""
    if provider == 'openai':
        return [{'type': 'function', 'function': {'name': t['name'], 'description': t['description'],
                                                  'parameters': t['parameters']}} for t in tools]
    return [{'name': t['name'], 'description': t['description'], 'input_schema': t['parameters']} for t in tools]


def build_request(conn, model, effort, system, user, max_tokens, params=None, json_mode=True, images=None,
                  messages=None, tools=None, tool_choice=None):
    """Return (url, headers, payload, extra_timeout_s). When messages is supplied, user and images are ignored."""
    think = effort != 'off'
    if messages is None:
        messages = [{'role': 'user', 'content': user_content(conn.provider, user, images)}]
    if conn.provider == 'openai':
        payload = {'model': model, 'messages': [{'role': 'system', 'content': system}] + messages,
                   'max_tokens': max_tokens + (THINK_TOKENS[effort] if think else 0), 'stream': False,
                   'reasoning_effort': effort if think else 'none'}
        if json_mode:
            payload['response_format'] = {'type': 'json_object'}
        if tools:
            payload['tools'] = tool_specs('openai', tools)
            if tool_choice:
                payload['tool_choice'] = tool_choice
        url = conn.base_url + '/chat/completions'
        headers = {'Authorization': f'Bearer {conn.key}', 'Content-Type': 'application/json'}
    else:
        payload = {'model': model, 'system': system, 'messages': messages,
                   'max_tokens': max_tokens + (ANTHROPIC_BUDGET[effort] if think else 0),
                   'thinking': {'type': 'enabled', 'budget_tokens': ANTHROPIC_BUDGET[effort]} if think
                   else {'type': 'disabled'}}
        if tools:
            payload['tools'] = tool_specs('anthropic', tools)
            if tool_choice:
                payload['tool_choice'] = {'type': tool_choice}
        url = conn.base_url + '/v1/messages'
        headers = {'x-api-key': conn.key, 'anthropic-version': '2023-06-01', 'Content-Type': 'application/json'}
    return url, headers, _merge(payload, params), (THINK_SECONDS[effort] if think else 0)


def _scrub(msg, key):
    """Redact API keys and masked key fragments echoed in provider error messages."""
    msg = re.sub(r'\*{2,}\S*|\bsk-\S+', '...', str(msg))
    for n in range(len(key or ''), 3, -1):
        msg = msg.replace(key[:n], '...').replace(key[-n:], '...')
    return msg


def _http_error(e, key):
    try:
        body = json.loads(e.read() or b'{}')
        err = body.get('error')
        msg = (err.get('message') if isinstance(err, dict) else err) or body.get('message') or ''
    except Exception:
        msg = ''
    msg = _scrub(msg, key)
    return f'http {e.code}' + (f': {msg[:200]}' if msg else '')


def _reasoning_of(provider, data):
    if provider == 'openai':
        return ((data.get('choices') or [{}])[0].get('message') or {}).get('reasoning_content') or ''
    return ''.join(b.get('thinking', '') for b in data.get('content', []) if b.get('type') == 'thinking')


def _read_stream(provider, resp, deadline, on_reasoning):
    """Read an SSE stream and return (text, reasoning, usage, finish, think_end_ts)."""
    r = _read_stream_full(provider, resp, deadline, on_reasoning)
    return r['text'], r['reasoning'], r['usage'], r['finish'], r['think_end']


def _read_stream_full(provider, resp, deadline, on_reasoning):
    """Read an SSE stream and return {text, reasoning, usage, finish, think_end, tool_calls, blocks}.
    blocks preserves raw Anthropic content blocks (including thinking signatures) for multi-turn tool calls."""
    text, reasoning, usage, finish, think_end = [], [], {}, None, None
    calls, blocks = {}, {}
    for raw in resp:
        if time.time() > deadline:
            raise TimeoutError('LLM response timed out')
        line = raw.decode('utf-8', 'replace').strip()
        if not line.startswith('data:'):
            continue
        body = line[5:].strip()
        if body == '[DONE]':
            break
        try:
            ev = json.loads(body)
        except json.JSONDecodeError:
            continue
        r = t = ''
        if provider == 'openai':
            for ch in ev.get('choices') or []:
                d = ch.get('delta') or {}
                r += d.get('reasoning_content') or ''
                t += d.get('content') or ''
                finish = ch.get('finish_reason') or finish
                for tc in d.get('tool_calls') or []:
                    c = calls.setdefault(tc.get('index', len(calls)), {'id': '', 'name': '', 'arguments': ''})
                    c['id'] = tc.get('id') or c['id']
                    fn = tc.get('function') or {}
                    c['name'] += fn.get('name') or ''
                    c['arguments'] += fn.get('arguments') or ''
            u = ev.get('usage')
            if u:
                usage = {'prompt': u.get('prompt_tokens', 0), 'completion': u.get('completion_tokens', 0)}
        else:
            kind = ev.get('type')
            if kind == 'message_start':
                usage['prompt'] = ((ev.get('message') or {}).get('usage') or {}).get('input_tokens', 0)
            elif kind == 'content_block_start':
                b = dict(ev.get('content_block') or {})
                if b.get('type') == 'tool_use':
                    b['input'], b['_json'] = {}, ''
                blocks[ev.get('index', len(blocks))] = b
            elif kind == 'content_block_delta':
                d = ev.get('delta') or {}
                r = d.get('thinking') or ''
                t = d.get('text') or ''
                b = blocks.get(ev.get('index'))
                if b is not None:
                    if d.get('type') == 'input_json_delta':
                        b['_json'] += d.get('partial_json') or ''
                    elif d.get('type') == 'signature_delta':
                        b['signature'] = b.get('signature', '') + (d.get('signature') or '')
                    elif r and b.get('type') == 'thinking':
                        b['thinking'] = b.get('thinking', '') + r
                    elif t and b.get('type') == 'text':
                        b['text'] = b.get('text', '') + t
            elif kind == 'message_delta':
                usage['completion'] = (ev.get('usage') or {}).get('output_tokens', 0)
                finish = (ev.get('delta') or {}).get('stop_reason') or finish
            elif kind == 'error':
                raise LlmUnavailable(str((ev.get('error') or {}).get('message') or ev)[:200])
        if r:
            reasoning.append(r)
            on_reasoning(r)
        if t:
            if reasoning and think_end is None:
                think_end = time.time()
            text.append(t)
    if provider == 'openai':
        tool_calls = [calls[k] for k in sorted(calls)]
        out_blocks = []
    else:
        out_blocks = [blocks[k] for k in sorted(blocks)]
        tool_calls = []
        for b in out_blocks:
            if b.get('type') == 'tool_use':
                tool_calls.append({'id': b.get('id', ''), 'name': b.get('name', ''), 'arguments': b.pop('_json', '') or '{}'})
                try:
                    b['input'] = json.loads(tool_calls[-1]['arguments'])
                except json.JSONDecodeError:
                    b['input'] = {}
    return {'text': ''.join(text), 'reasoning': ''.join(reasoning), 'usage': usage, 'finish': finish,
            'think_end': think_end, 'tool_calls': tool_calls, 'blocks': out_blocks}


def _calls_of(provider, data):
    """Extract tool calls and Anthropic content blocks from a non-streaming response."""
    if provider == 'openai':
        msg = (data.get('choices') or [{}])[0].get('message') or {}
        return [{'id': c.get('id', ''), 'name': (c.get('function') or {}).get('name', ''),
                 'arguments': (c.get('function') or {}).get('arguments') or '{}'} for c in msg.get('tool_calls') or []], []
    blocks = list(data.get('content') or [])
    return [{'id': b.get('id', ''), 'name': b.get('name', ''), 'arguments': json.dumps(b.get('input') or {}, ensure_ascii=False)}
            for b in blocks if b.get('type') == 'tool_use'], blocks


def _text_of(provider, data):
    if provider == 'openai':
        choice = (data.get('choices') or [{}])[0]
        u = data.get('usage') or {}
        text = (choice.get('message') or {}).get('content') or ''
        return text, {'prompt': u.get('prompt_tokens', 0), 'completion': u.get('completion_tokens', 0)}, \
            (None if text.strip() else f"empty ({choice.get('finish_reason')})")
    u = data.get('usage') or {}
    text = ''.join(b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text')
    return text, {'prompt': u.get('input_tokens', 0), 'completion': u.get('output_tokens', 0)}, \
        (None if text.strip() else f"empty ({data.get('stop_reason')})")


class Llm:
    def __init__(self, store):
        self.store = store
        cols = [r['name'] for r in store.q('PRAGMA table_info(llm_calls)')]
        for c, t in (('effort', 'TEXT'), ('think_s', 'REAL'), ('reasoning_chars', 'INTEGER')):
            if c not in cols:
                store.db.execute(f'ALTER TABLE llm_calls ADD COLUMN {c} {t}')
        self.path = os.path.join(store.root, 'llm.json')
        self.env = _env()
        self.reload()

    # ------------------------------------------------------------ Provider Groups
    def _file(self):
        try:
            with open(self.path, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def _load_groups(self):
        s, env = self._file(), self.env
        if s is None:
            if not env:
                return []
            return [{'id': 'env', 'name': urlsplit(env[1]).hostname or env[1], 'enabled': True, 'provider': env[0],
                     'base_url': env[1], 'models': [env[2]], 'catalog': [], 'params': {}, 'env': True}]
        if 'groups' in s:
            return s['groups']
        # Legacy single-connection format
        g = {'id': 'env', 'name': urlsplit(s.get('base_url') or '').hostname or 'API', 'enabled': True,
             'provider': s.get('provider'), 'base_url': s.get('base_url'), 'models': s.get('models') or [],
             'catalog': s.get('catalog') or [], 'params': s.get('params') or {}}
        if s.get('key'):
            g['key'] = s['key']
        else:
            g['env'] = True
        return [g]

    def _key_of(self, g):
        if g.get('key'):
            return g['key']
        env = self.env
        return env[3] if g.get('env') and env and (g.get('provider'), g.get('base_url')) == (env[0], env[1]) else ''

    def _usable(self):
        return [g for g in self.groups if g.get('enabled', True) and g.get('models') and self._key_of(g)]

    def _group(self, gid):
        return next((g for g in self.groups if g['id'] == gid), None)

    def _conn(self, g):
        return Conn(g['provider'], g['base_url'], self._key_of(g))

    def reload(self):
        self.groups = self._load_groups()
        usable = self._usable()
        self.cfg = self._conn(usable[0]) if usable else None
        self.error = None if usable else 'No enabled LLM API groups configured'

    @property
    def available(self):
        return self.cfg is not None

    def _find(self, gid, model):
        """Find an enabled group carrying model; if gid is None, pick the first matching group."""
        for g in self._usable():
            if (gid is None or g['id'] == gid) and model in g['models']:
                return g
        return None

    def default_choice(self):
        usable = self._usable()
        return {'group': usable[0]['id'], 'model': usable[0]['models'][0], 'effort': 'off'} if usable \
            else {'group': '', 'model': '', 'effort': 'off'}

    def choice(self, goal=None, choice=None):
        """Resolve effective group, model, and effort from argument, goal spec, or default."""
        c = choice
        if c is None and goal:
            row = self.store.q1('SELECT spec FROM goals WHERE id=?', (goal,))
            c = (json.loads(row['spec']).get('llm') if row else None)
        c = c or {}
        d = self.default_choice()
        g = self._find(c.get('group'), c.get('model'))
        return {'group': g['id'] if g else d['group'], 'model': c['model'] if g else d['model'],
                'effort': c.get('effort') if c.get('effort') in EFFORTS else d['effort']}

    def check_choice(self, c):
        g = self._find(c.get('group'), c.get('model'))
        if not g:
            raise ValueError(f"Model {c.get('model')} not found in enabled groups")
        effort = c.get('effort', 'off')
        if effort not in EFFORTS:
            raise ValueError(f"Effort must be one of {', '.join(EFFORTS)}; got {effort}")
        return {'group': g['id'], 'model': c['model'], 'effort': effort}

    def public(self):
        """Return group settings without raw keys."""
        groups = []
        for g in self.groups:
            o = {k: v for k, v in g.items() if k not in ('key', 'env')}
            o.update(key_set=bool(self._key_of(g)), enabled=g.get('enabled', True))
            groups.append(o)
        return {'groups': groups, 'providers': list(PROVIDERS), 'efforts': list(EFFORTS)}

    def describe(self):
        out = {'available': self.available, 'efforts': list(EFFORTS), 'default': self.default_choice(),
               'groups': [{'id': g['id'], 'name': g['name'], 'provider': g['provider'], 'models': list(g['models'])}
                          for g in self._usable()]}
        if not self.available:
            out['reason'] = self.error
        return out

    def _validate(self, data):
        provider = data.get('provider')
        if provider not in PROVIDERS:
            raise ValueError('Provider must be openai or anthropic')
        base = _norm_url(data.get('base_url'))
        if urlsplit(base).scheme not in ('http', 'https') or not urlsplit(base).hostname:
            raise ValueError('API URL must begin with http:// or https://')
        return provider, base

    def _saved_key(self, data, provider, base):
        """Reuse existing key only when the group exists and neither provider nor base_url changed."""
        old = self._group(data.get('id'))
        return self._key_of(old) if old and (provider, base) == (old.get('provider'), old.get('base_url')) else ''

    def _conn_for(self, data):
        provider, base = self._validate(data)
        key = str(data.get('key') or '').strip() or self._saved_key(data, provider, base)
        if not key:
            raise ValueError('API key is required')
        return Conn(provider, base, key)

    def _write(self, groups):
        tmp = self.path + '.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'groups': groups}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
        self.reload()

    def save_group(self, data):
        """Create or update a group and return its id."""
        gid = data.get('id')
        old = self._group(gid)
        if gid and not old:
            raise ValueError('Group no longer exists')
        name = str(data.get('name') or '').strip()
        if not name:
            raise ValueError('Group name is required')
        if any(g['name'] == name and g['id'] != gid for g in self.groups):
            raise ValueError(f'Group named {name} already exists')
        provider, base = self._validate(data)
        models = list(dict.fromkeys(str(m).strip() for m in data.get('models') or [] if str(m).strip()))
        if not models:
            raise ValueError('Select at least one model')
        params = data.get('params') or {}
        if not isinstance(params, dict):
            raise ValueError('Custom params must be a JSON object')
        bad = [k for k in params if k in RESERVED]
        if bad:
            raise ValueError(f"Custom params cannot override {', '.join(bad)}")
        g = {'id': gid or 'api-' + uuid.uuid4().hex[:8], 'name': name, 'enabled': bool(data.get('enabled', True)),
             'provider': provider, 'base_url': base, 'models': models,
             'catalog': list(dict.fromkeys([str(m) for m in data.get('catalog') or []] + models)), 'params': params}
        key = str(data.get('key') or '').strip()
        if key:
            g['key'] = key
        elif not self._saved_key(data, provider, base):
            raise ValueError('API key is required')
        elif old.get('key'):
            g['key'] = old['key']
        else:
            g['env'] = True
        self._write([g if x['id'] == gid else x for x in self.groups] if old else self.groups + [g])
        return g['id']

    def delete_group(self, gid):
        if not self._group(gid):
            raise ValueError('Group no longer exists')
        self._write([g for g in self.groups if g['id'] != gid])

    def reveal_key(self, gid):
        """Return raw key only on explicit reveal request in the settings dialog."""
        g = self._group(gid)
        if not g:
            raise ValueError('Group no longer exists')
        return self._key_of(g)

    def list_models(self, data):
        c = self._conn_for(data)
        if c.provider == 'openai':
            url, headers = c.base_url + '/models', {'Authorization': f'Bearer {c.key}'}
        else:
            url, headers = c.base_url + '/v1/models', {'x-api-key': c.key, 'anthropic-version': '2023-06-01'}
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise LlmUnavailable(_http_error(e, c.key))
        except Exception as e:
            raise LlmUnavailable(_scrub(f'{type(e).__name__}: {e}', c.key))
        return sorted({m['id'] for m in data.get('data') or [] if isinstance(m, dict) and m.get('id')})

    def test(self, data):
        c = self._conn_for(data)
        model = str(data.get('model') or '').strip()
        if not model:
            raise ValueError('Select a model first')
        url, headers, payload, _ = build_request(c, model, 'off', 'Reply only with ok.', 'ok', 16,
                                                 data.get('params') if isinstance(data.get('params'), dict) else None,
                                                 json_mode=False)
        t0 = time.time()
        try:
            r = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method='POST')
            with urllib.request.urlopen(r, timeout=60) as resp:
                json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise LlmUnavailable(_http_error(e, c.key))
        except Exception as e:
            raise LlmUnavailable(_scrub(f'{type(e).__name__}: {e}', c.key))
        return {'ok': True, 'seconds': round(time.time() - t0, 2), 'model': model}

    # ------------------------------------------------------------ Invocation
    def _delta(self, goal, call_id, purpose, params):
        with self.store.tx():
            self.store.emit('output/textDelta', dict(params, call=call_id, purpose=purpose), goal=goal)

    def _prepare(self, goal, choice):
        if not self.cfg:
            raise LlmUnavailable(self.error or 'No enabled LLM API available')
        ch = self.choice(goal, choice)
        group = self._group(ch['group'])
        if not group:
            raise LlmUnavailable(self.error or 'No enabled LLM API group')
        return ch, group, self._conn(group)

    def _call(self, purpose, ch, group, cfg, goal, timeout, record, **req):
        """Send one API request and record it in llm_calls; non-ok status is returned without raising."""
        call_id = 'llm-' + uuid.uuid4().hex[:10]
        url, headers, payload, extra = build_request(cfg, ch['model'], ch['effort'], req.pop('system'), req.pop('user', ''),
                                                     req.pop('max_tokens'), group.get('params'), **req)
        payload = dict(payload, stream=True)
        if cfg.provider == 'openai':
            payload['stream_options'] = {'include_usage': True}
        req_h = self.store.put_blob(dict(record, group=ch['group'], model=ch['model'], effort=ch['effort']))
        t0 = time.time()
        status, out = 'ok', {'text': '', 'reasoning': '', 'usage': {}, 'think_end': None, 'tool_calls': [], 'blocks': []}
        buf, sent = [], [t0]

        def on_reasoning(r):
            buf.append(r)
            if goal and time.time() - sent[0] >= DELTA_EVERY:
                self._delta(goal, call_id, purpose, {'reasoning': ''.join(buf)})
                buf.clear()
                sent[0] = time.time()
        try:
            r = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method='POST')
            with urllib.request.urlopen(r, timeout=timeout + extra) as resp:
                if 'text/event-stream' in (resp.headers.get('Content-Type') or ''):
                    out = _read_stream_full(cfg.provider, resp, t0 + timeout + extra, on_reasoning)
                    if not out['text'].strip() and not out['tool_calls']:
                        status = f"empty ({out['finish']})"
                else:
                    data = json.loads(resp.read())
                    text, usage, empty = _text_of(cfg.provider, data)
                    tool_calls, blocks = _calls_of(cfg.provider, data)
                    out = {'text': text, 'usage': usage, 'reasoning': _reasoning_of(cfg.provider, data),
                           'think_end': None, 'tool_calls': tool_calls, 'blocks': blocks}
                    if empty and not tool_calls:
                        status = empty
        except urllib.error.HTTPError as e:
            status = _http_error(e, cfg.key)
        except LlmUnavailable as e:
            status = _scrub(e, cfg.key)
        except Exception as e:
            status = f'{type(e).__name__}'
        if status != 'ok':
            out.update(text='', tool_calls=[], blocks=[])
        dt = time.time() - t0
        reasoning = out['reasoning']
        think_s = round(((out['think_end'] or time.time()) - t0) if reasoning else 0, 1)
        if goal:
            self._delta(goal, call_id, purpose, {'reasoning': ''.join(buf), 'done': True, 'think_s': think_s})
        resp_h = self.store.put_blob({'text': out['text'], 'status': status, 'reasoning': reasoning, 'think_s': think_s,
                                     **({'tool_calls': out['tool_calls']} if out['tool_calls'] else {})})
        usage = out['usage']
        with self.store.tx():
            self.store.x('INSERT INTO llm_calls(id, goal, purpose, model, prompt_tokens, completion_tokens, seconds, '
                         'status, request, response, created, effort, think_s, reasoning_chars) '
                         'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (call_id, goal, purpose, ch['model'], usage.get('prompt', 0), usage.get('completion', 0),
                          round(dt, 2), status, req_h, resp_h, now(), ch['effort'], think_s, len(reasoning)))
        return dict(out, status=status, call=call_id)

    def ask_json(self, purpose, system, user, goal=None, max_tokens=2000, timeout=90, choice=None, images=None):
        assert purpose in PURPOSES
        ch, group, cfg = self._prepare(goal, choice)
        # Store image hashes only; source pages remain in document storage
        record = {'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
                  'images': [sha256(d) for d, _ in images or []]}
        out = self._call(purpose, ch, group, cfg, goal, timeout, record, system=system, user=user,
                         max_tokens=max_tokens, images=images)
        if out['status'] != 'ok':
            raise LlmUnavailable(f"LLM call failed: {out['status']}")
        call_id, text = out['call'], out['text']
        t = text.strip()
        if t.startswith('```'):
            t = t.strip('`').split('\n', 1)[1] if '\n' in t else t
            t = t.rsplit('```', 1)[0]
        # Extract the first JSON object even if wrapped in prose or containing raw newlines
        s = t.find('{')
        if s >= 0:
            try:
                return json.JSONDecoder(strict=False).raw_decode(t[s:])[0], call_id
            except json.JSONDecodeError:
                pass
        raise LlmBadReply('Incomplete JSON in LLM reply')

    def ask_tools(self, purpose, system, user, tools, run_tool, goal=None, max_tokens=4000, timeout=120, choice=None,
                  images=None, rounds=4):
        """Multi-turn tool loop: calls run_tool(name, args) until the model replies with plain text or
        reaches rounds (last round disables tool choice). Passes None when tool arguments fail JSON parsing."""
        assert purpose in PURPOSES
        ch, group, cfg = self._prepare(goal, choice)
        messages = [{'role': 'user', 'content': user_content(cfg.provider, user, images)}]
        record_user = {'role': 'user', 'content': user}
        calls, log = [], [record_user]
        for i in range(rounds):
            last = i == rounds - 1
            record = {'messages': [{'role': 'system', 'content': system}] + log, 'tools': [t['name'] for t in tools],
                      'images': [sha256(d) for d, _ in images or []]}
            out = self._call(purpose, ch, group, cfg, goal, timeout, record, system=system, max_tokens=max_tokens,
                             json_mode=False, messages=messages, tools=tools, tool_choice='none' if last else None)
            calls.append(out['call'])
            if out['status'] != 'ok':
                raise LlmUnavailable(f"LLM call failed: {out['status']}")
            if not out['tool_calls'] or last:
                if not out['text'].strip():
                    raise LlmUnavailable('LLM call failed: empty response')
                return out['text'].strip(), calls
            results = []
            for c in out['tool_calls']:
                try:
                    args = json.loads(c['arguments'] or '{}')
                except json.JSONDecodeError:
                    args = None
                results.append((c, str(run_tool(c['name'], args if isinstance(args, dict) else None))))
            if cfg.provider == 'openai':
                a = {'role': 'assistant', 'content': out['text'] or None,
                     'tool_calls': [{'id': c['id'], 'type': 'function',
                                     'function': {'name': c['name'], 'arguments': c['arguments'] or '{}'}}
                                    for c in out['tool_calls']]}
                # DeepSeek thinking mode requires reasoning_content to be echoed back in the same turn
                if out['reasoning']:
                    a['reasoning_content'] = out['reasoning']
                messages.append(a)
                messages += [{'role': 'tool', 'tool_call_id': c['id'], 'content': res} for c, res in results]
            else:
                messages.append({'role': 'assistant', 'content': out['blocks']})
                messages.append({'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': c['id'], 'content': res}
                                                             for c, res in results]})
            log.append({'role': 'assistant', 'content': out['text'],
                        'tool_calls': [{'name': c['name'], 'arguments': c['arguments']} for c in out['tool_calls']]})
            log += [{'role': 'tool', 'name': c['name'], 'content': res} for c, res in results]
        raise LlmUnavailable('LLM call failed: empty response')
