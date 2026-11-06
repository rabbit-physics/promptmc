"""Pre-confirmation precheck gate: LLM-guided iterative inspection with deterministic tool execution
and recorded evidence.

The kernel first builds physics models, generates solver inputs, runs readback comparisons, evaluates
built-in rules, and checks OpenMC geometry; prechecks start afterward. Each step records an evidence
entry (subject = paramlist hash, kind = check, context = <run_id>/<step>), transitioning from running
to verdict. Falls back to fixed sequential checks when the LLM is unavailable or fails mid-run.
"""
import ast
import math
import json
import operator
import re

from ..domain import composition as C
from ..domain import model as M
from ..domain import nuclides as N
from ..domain import params as P
from ..util import diff_flat, now
from . import intake, rules as R
from .llm import LlmBadReply, LlmUnavailable
from .store import J, jl

MAX_STEPS = 10
RANK = {'info': 0, 'pass': 1, 'warn': 2, 'fail': 3}

TOOLS = {
    'params': ('Parameter list: id, label, value, unit, nature (given, derived, assumed, edited), and source', {}),
    'model': ('Physics model fields for one case (geometry, regions, materials, thermal_scattering, source) '
              'plus expanded atom fractions materials.<mat>.atom.<nuclide>', {'case': 'case ID'}),
    'compare': ('Differing physics fields between two cases; omits args to diff every adjacent pair',
                {'a': 'case ID, optional', 'b': 'case ID, optional'}),
    'assert': ('Evaluate arithmetic/comparison assertions on physics fields. Use paths from model; string fields '
               'only support == and !=. Prefix paths with case ID (e.g. S0.regions.moderator.temperature_K) to '
               'reference another case; supports abs, min, max',
               {'case': 'case ID', 'checks': '[{"label": "what this assertion verifies", "expr": "geometry.ring.clad.outer_radius_cm < geometry.pitch_cm / 2"}]'}),
    'readback': ('Field-by-field readback comparison results for both solver inputs',
                 {'case': 'case ID, optional', 'solver': 'openmc or ref_mc, optional'}),
    'rules': ('Built-in rule evaluation results on model and binding', {'case': 'case ID, optional'}),
    'geometry': ('OpenMC geometry debug results (overlaps and undefined cells)', {'case': 'case ID, optional'}),
    'bindings': ('Solver binding approximations requiring user approval', {'solver': 'openmc or ref_mc, optional'}),
    'input': ('Lines matching grep in generated solver input (or first 40 lines if omitted)',
              {'case': 'case ID', 'solver': 'openmc or ref_mc', 'grep': 'search string, optional'}),
    'user_rules': ('Evaluation results for user-defined rules across cases', {}),
    'estimate': ('Remaining run count, estimated wall time from history, and fraction of wall-time budget', {}),
    'tolerance': ('Synthetic reproduction acceptance tolerance trial at estimated sigma', {}),
}

SYSTEM = """You are performing pre-confirmation verification checks for a Monte Carlo criticality study. \
The kernel has already built the physics models from the parameter list, generated OpenMC and Reference MC \
inputs, and run readback, built-in rules, and geometry overlap checks. You decide what to inspect and in \
what order: invoke one tool per turn, judge the result, and choose the next step.

Tools (name: purpose; args):
{tools}

Output exactly one JSON object per turn:
{{"judge": {{"status": "pass|warn|fail|info", "finding": "one-sentence verdict on the previous step with key numbers"}} or null (on first step),
 "next": {{"title": "short step title <= 16 words", "tool": "tool_name", "args": {{...}}}} or null,
 "summary": "when next is null, concise pre-confirmation summary (<= 100 words, or 'All checks passed'); empty string otherwise"}}

Requirements:
- At most {max_steps} steps total. Cover readback, geometry, rules, bindings, and estimate across all cases (omit case to check all).
- Write at least two problem-specific assert checks beyond built-ins (e.g. ring nesting, single-variable deltas between adjacent cases, density/concentration sanity). Use compare without args first to inspect adjacent cases.
- Use pass (expected), warn (non-blocking notice), fail (must resolve before confirm), or info (informational).
- Do not repeat checks; cite concrete numbers rather than generic prose, and avoid internal variable names (wall_s, core_s, runs, sigma_est)."""


def tool_doc():
    return '\n'.join(f"- {k}: {d}; args {json.dumps(a, ensure_ascii=False) if a else 'none'}" for k, (d, a) in TOOLS.items())


class Checks:
    def __init__(self, kernel, gid, rid, version):
        self.k, self.st = kernel, kernel.store
        self.gid, self.rid, self.version = gid, rid, version
        self.g = kernel.goal(gid)
        self.plist_hash = self.g['spec']['paramlist']
        self.plist = self.st.get_blob(self.plist_hash)
        self.cases = [c['case_id'] for c in self.st.q('SELECT case_id FROM cases WHERE goal=? ORDER BY ord', (gid,))]
        self.labels = {c['case_id']: c['label'] for c in self.st.q('SELECT case_id, label FROM cases WHERE goal=?', (gid,))}
        self.pre = kernel.goal_precheck(gid)
        self.steps = []
        self.summary_call = None

    # ------------------------------------------------------------ Step Recording
    def _ctx(self, n):
        return f'{self.rid}/{n:02d}'

    def _put(self, n, step, status, finding):
        with self.st.tx():
            self.st.put_evidence(self.plist_hash, 'check', self._ctx(n), status, finding,
                                 {'run': self.rid, 'n': n, 'title': step['title'], 'tool': step['tool'],
                                  'args': step.get('args') or {}, 'result': step.get('result'),
                                  'call': step.get('call')},
                                 goal=self.gid, case_id=(step.get('args') or {}).get('case'))

    def begin(self, title, tool, args, call=None):
        n = len(self.steps) + 1
        step = {'n': n, 'title': title[:40], 'tool': tool, 'args': args or {}, 'call': call}
        self.steps.append(step)
        self._put(n, step, 'running', '')
        try:
            step['result'] = self.call(tool, args or {})
        except Exception as e:
            step['result'] = {'status': 'fail', 'summary': f'Tool error: {type(e).__name__}: {e}', 'data': None}
        return step

    def judge(self, step, status, finding):
        tool_status = step['result']['status']
        if status not in RANK:
            status = 'info'
        if tool_status != 'info' and RANK[tool_status] > RANK[status]:
            status = tool_status
        step['status'], step['finding'] = status, (finding or step['result']['summary'])[:300]
        self._put(step['n'], step, status, step['finding'])

    def stale(self):
        g = self.k.goal(self.gid)
        return g['version'] != self.version or g['status'] != 'draft'

    # ------------------------------------------------------------ Tools
    def _case_list(self, case):
        if case:
            if case not in self.cases:
                raise ValueError(f"Unknown case {case}; available cases: {', '.join(self.cases)}")
            return [case]
        return self.cases

    def _model(self, case):
        s = self.k._stage(self.gid, case, 'model')
        if not s:
            raise ValueError(f'{case} has no physics model')
        return self.st.get_blob(s['node'])

    def call(self, tool, args):
        fn = getattr(self, 't_' + tool, None)
        if fn is None:
            raise ValueError(f'Unknown tool {tool}')
        return fn(**{k: v for k, v in args.items() if k in TOOLS[tool][1]})

    def t_params(self):
        rows = [{'id': p['id'], 'label': p['label'], 'value': p['value'], 'unit': p['unit'], 'nature': p['nature'],
                 'source': p['source'], 'alternatives': len(p['alternatives'])} for p in self.plist['params']]
        n = {}
        for r in rows:
            n[r['nature']] = n.get(r['nature'], 0) + 1
        names = {'given': 'given', 'derived': 'derived', 'assumed': 'assumed', 'edited': 'edited', 'user': 'user',
                 'candidate': 'candidate', 'conflict': 'conflict', 'ambiguous': 'ambiguous', 'missing': 'missing'}
        return {'status': 'info', 'summary': f"{len(rows)} parameters: " + ', '.join(f'{names.get(k, k)} {v}' for k, v in n.items()),
                'data': rows}

    def _flat(self, case):
        return flat_with_atoms(self._model(case))

    def t_model(self, case=None):
        case = case or self.cases[0]
        flat = self._flat(case)
        return {'status': 'info', 'summary': f'{case} has {len(flat)} physics fields', 'data': flat}

    def t_compare(self, a=None, b=None):
        if a and b:
            d = M.diff_models(self._model(a), self._model(b))
            return {'status': 'info', 'summary': f'{a} and {b} differ in {len(d)} fields', 'data': d}
        chain = {}
        for x, y in zip(self.cases, self.cases[1:]):
            chain[f'{x}->{y}'] = M.diff_models(self._model(x), self._model(y))
        return {'status': 'info', 'summary': '; '.join(f'{k}: {len(v)} differing fields' for k, v in chain.items()),
                'data': chain}

    def t_assert(self, case=None, checks=()):
        case = case or self.cases[0]
        flats = {}

        def flat(c):
            if c not in flats:
                flats[c] = self._flat(c)
            return flats[c]

        if isinstance(checks, str):
            checks = json.loads(checks)
        if isinstance(checks, dict):
            checks = [checks]
        out = []
        for c in list(checks)[:12]:
            expr = str(c.get('expr', ''))
            try:
                v = _safe_eval(expr, lambda path: _lookup(path, case, self.cases, flat))
                out.append({'label': c.get('label', ''), 'expr': expr, 'ok': bool(v)})
            except Exception as e:
                out.append({'label': c.get('label', ''), 'expr': expr, 'ok': None, 'error': str(e)})
        bad = [o for o in out if o['ok'] is False]
        err = [o for o in out if o['ok'] is None]
        status = 'fail' if bad else 'warn' if err else 'pass'
        summary = (f"{len(out) - len(bad) - len(err)} of {len(out)} assertions passed"
                   + (f"; failed: {'; '.join(o['label'] or o['expr'] for o in bad)}" if bad else '')
                   + (f"; unevaluated: {'; '.join(o['expr'] + ' (' + o['error'] + ')' for o in err)}" if err else ''))
        return {'status': status, 'summary': summary, 'data': out}

    def t_readback(self, case=None, solver=None):
        rows = []
        for c in self._case_list(case):
            for s in ([solver] if solver else ['openmc', 'ref_mc']):
                gs = self.k._stage(self.gid, c, f'gen.{s}')
                rb = self.k._stage(self.gid, c, f'readback.{s}')
                ev = self.st.head(gs['node'], 'readback', rb['input_key']) if gs and rb else None
                rows.append({'case': c, 'solver': s, 'status': ev['status'] if ev else None,
                             'summary': ev['summary'] if ev else 'No readback result'})
        bad = [r for r in rows if r['status'] != 'pass']
        return {'status': 'fail' if bad else 'pass',
                'summary': (f"{len(rows)} input readbacks match physics models" if not bad else
                            '; '.join(f"{r['case']} {r['solver']}: {r['summary']}" for r in bad)),
                'data': rows}

    def t_rules(self, case=None):
        rows = []
        for c in self._case_list(case):
            s = self.k._stage(self.gid, c, 'model')
            ev = self.st.head(s['node'], 'rules', R.RULESET) if s else None
            rows.append({'case': c, 'status': ev['status'] if ev else None, 'summary': ev['summary'] if ev else '',
                         'detail': self.st.get_blob(ev['detail']) if ev and ev['detail'] else None})
        worst = max((r['status'] for r in rows if r['status'] in RANK), key=lambda s: RANK[s], default='pass')
        if any(r['status'] in ('fail', 'error') for r in rows):
            worst = 'fail'
        return {'status': worst, 'summary': '; '.join(f"{r['case']}: {r['summary']}" for r in rows if r['summary'])
                or f'{len(rows)} cases passed rule checks', 'data': rows}

    def t_geometry(self, case=None):
        rows = []
        for c in self._case_list(case):
            ev = self.st.head(self.k._geom_key(self._model(c)), 'geomcheck', 'openmc-0.15.3')
            rows.append({'case': c, 'status': ev['status'] if ev else None, 'summary': ev['summary'] if ev else 'Not run'})
        bad = [r for r in rows if r['status'] != 'pass']
        return {'status': 'fail' if bad else 'pass',
                'summary': (f'{len(rows)} cases passed geometry debug' if not bad else
                            '; '.join(f"{r['case']}: {r['summary']}" for r in bad)), 'data': rows}

    def t_bindings(self, solver=None):
        items = [f for f in self.pre['foreseeable'] if not solver or f['solver'] == solver]
        uniq = {}
        for f in items:
            uniq.setdefault(f['signature'], {'solver': f['solver'], 'what': f['item']['what'],
                                             'choice': f['item']['choice'], 'detail': f['item'].get('detail', ''),
                                             'cases': []})['cases'].append(f['case'])
        rows = list(uniq.values())
        return {'status': 'warn' if rows else 'pass',
                'summary': ('; '.join(f"{r['solver']} {r['what']} = {r['choice']} ({', '.join(r['cases'])})" for r in rows)
                            if rows else 'No binding approximations requiring approval'), 'data': rows}

    def t_input(self, case=None, solver='openmc', grep=''):
        case = case or self.cases[0]
        gs = self.k._stage(self.gid, case, f'gen.{solver}')
        if not gs:
            raise ValueError(f'{case} has no {solver} input')
        lines = self.st.get_blob(gs['node'], as_json=False).splitlines()
        hits = [(i + 1, ln) for i, ln in enumerate(lines) if grep.lower() in ln.lower()] if grep else \
            [(i + 1, ln) for i, ln in enumerate(lines[:40])]
        return {'status': 'info', 'summary': f'{case} {solver} input has {len(lines)} lines, matched {len(hits)}',
                'data': [f'{i:4d}  {ln}' for i, ln in hits[:40]]}

    def t_estimate(self):
        e, b, spent = self.pre['estimate'], self.pre['budget'], self.pre.get('spent', 0)
        frac = (spent + e['wall_s']) / b['wall_s'] if e['runs'] else 0
        from .core import fmt_dur
        summary = (f"{e['runs']} runs, estimated {fmt_dur(e['wall_s'])}" + (f" (+ {fmt_dur(spent)} spent)" if spent >= 1 else '')
                   + f", {frac:.0%} of {fmt_dur(b['wall_s'])} wall-time budget ({e['basis']})"
                   if e['runs'] else 'All cases reuse cached results; no runs required')
        return {'status': 'fail' if frac > 1 else 'warn' if frac > 0.8 else 'pass', 'summary': summary,
                'data': {'estimate': e, 'budget': b}}

    def t_user_rules(self):
        res = self.k.rule_results(self.gid, self.g['spec'].get('user_rules', []))
        if not res:
            return {'status': 'info', 'summary': 'No user-defined rules', 'data': []}
        bad = [f"{r['id']} '{r['label']}' failed on {', '.join(c for c, v in r['results'].items() if v is False)}"
               for r in res if any(v is False for v in r['results'].values())]
        errors = [f"User rule {r['id']} '{r['label']}' error on {c}: {reason}"
                  for r in res for c, reason in r.get('errors', {}).items()]
        findings = bad + errors
        return {'status': 'fail' if findings else 'pass',
                'summary': '; '.join(findings) if findings else f'All {len(res)} user rules passed on applicable cases',
                'data': res}

    def t_tolerance(self):
        t = self.pre['acceptance_trial']
        if not t['tolerance_pcm']:
            return {'status': 'info', 'summary': t['note'], 'data': t}
        return {'status': 'info', 'summary': f"Trial acceptance tolerance ~{t['tolerance_pcm']:.0f} pcm at estimated sigma", 'data': t}

    # ------------------------------------------------------------ Execution Modes
    def run_llm(self):
        llm = self.k.llm
        system = SYSTEM.format(tools=tool_doc(), max_steps=MAX_STEPS)
        last = None
        while True:
            if self.stale():
                return None
            prompt = self._prompt()
            try:
                r, call = llm.ask_json('precheck', system, prompt, goal=self.gid, max_tokens=2000, timeout=120)
            except LlmBadReply:
                return self.run_fixed(note='Unparseable LLM reply; remaining checks executed sequentially')
            except LlmUnavailable as e:
                return self.run_fixed(note=f'{e}; remaining checks executed sequentially')
            with self.st.tx():
                self.k._budget_warn(self.gid)
            if last:
                j = r.get('judge') or {}
                self.judge(last, j.get('status', 'info'), j.get('finding', ''))
            nxt = r.get('next')
            if not nxt or len(self.steps) >= MAX_STEPS:
                summary = (r.get('summary') or '').strip()
                self.summary_call = call
                if self._cover() or not summary or nxt:
                    summary = self._final_summary(llm) or self._fixed_summary()
                return summary
            tool = nxt.get('tool')
            if tool not in TOOLS:
                tool, nxt = 'params', {'title': nxt.get('title') or 'Parameter list', 'args': {}}
            last = self.begin(nxt.get('title') or tool, tool, nxt.get('args') or {}, call)

    def _prompt(self):
        g = self.g
        tpl = intake.context_of(g['spec'])
        lines = [f"Goal: {g['title']}. {g['spec'].get('objective', '')}",
                 ('Approval policy: auto (binding and run approvals auto-approved after confirm; write "note" instead of "needs approval")'
                  if g['mode'] == 'goal' else 'Approval policy: ask (user approves bindings and runs individually after confirm)'),
                 'Cases: ' + '; '.join(f'{c} ({self.labels[c]})' for c in self.cases),
                 'Case design: ' + tpl.get('design', 'none'),
                 f"Parameter list has {len(self.plist['params'])} entries; user edits vs. source: "
                 + (', '.join(f"{m['label']} {m['old']}->{m['new']}" for m in P.modifications(self.plist)) or 'none'),
                 'User rules (check via user_rules tool): ' + ('; '.join(f"{r['id']} {r['label']}: {r['expr']}"
                                                                         for r in g['spec'].get('user_rules', [])) or 'none')]
        if not self.steps:
            lines.append('No checks executed yet.')
        for s in self.steps:
            res = json.dumps(s['result'].get('data'), ensure_ascii=False, default=str)
            if len(res) > 6000:
                res = res[:6000] + '... (truncated)'
            lines.append(f"Step {s['n']} '{s['title']}' tool={s['tool']} args={json.dumps(s['args'], ensure_ascii=False)}\n"
                         f"  tool_status={s['result']['status']}: {s['result']['summary']}\n  data={res}"
                         + (f"\n  your_verdict={s['status']}: {s['finding']}" if s.get('status') else '\n  (awaiting your verdict)'))
        lines.append(f'Used {len(self.steps)} / {MAX_STEPS} steps. Output next JSON.')
        return '\n'.join(lines)

    def _cover(self):
        """Backfill any required checks skipped by the LLM; returns count of added steps."""
        done = {s['tool'] for s in self.steps if not (s['args'] or {}).get('case') and not (s['args'] or {}).get('solver')}
        n = 0
        need = list(self.FIXED[:5])
        if self.g['spec'].get('user_rules'):
            need.append(('User rules', 'user_rules', {}))
        for title, tool, args in need:
            if tool not in done and not self.stale():
                s = self.begin(title, tool, args)
                self.judge(s, s['result']['status'], s['result']['summary'])
                n += 1
        return n

    def _final_summary(self, llm):
        prompt = self._prompt().rsplit('\n', 1)[0] + '\nAll checks complete. Output only {"summary": "..."}.'
        try:
            r, call = llm.ask_json('precheck', SYSTEM.format(tools=tool_doc(), max_steps=MAX_STEPS), prompt,
                                   goal=self.gid, max_tokens=600, timeout=60)
            self.summary_call = call
            return (r.get('summary') or '').strip()
        except LlmUnavailable:
            return ''

    FIXED = [('Input readbacks', 'readback', {}), ('Geometry debug', 'geometry', {}), ('Rule checks', 'rules', {}),
             ('Binding choices', 'bindings', {}), ('Runtime estimate', 'estimate', {}), ('Acceptance tolerance', 'tolerance', {})]

    def fixed_plan(self):
        plan = list(self.FIXED)
        if self.g['spec'].get('user_rules'):
            plan.insert(3, ('User rules', 'user_rules', {}))
        return plan

    def run_fixed(self, note=''):
        for s in self.steps:
            if not s.get('status'):
                self.judge(s, s['result']['status'], s['result']['summary'])
        done = {s['tool'] for s in self.steps if not s['args']}
        for title, tool, args in self.fixed_plan():
            if tool in done:
                continue
            if self.stale():
                return None
            s = self.begin(title, tool, args)
            self.judge(s, s['result']['status'], s['result']['summary'])
        return self._fixed_summary(note)

    def _fixed_summary(self, note=''):
        return (note + '. ' if note else '') + self.pre['summary']

    def worst(self):
        return max((s.get('status', 'info') for s in self.steps), key=lambda x: RANK.get(x, 0), default='pass')


def flat_with_atoms(m):
    """Flatten physics model fields plus expanded material atom fractions."""
    flat = M.physics_flat(m)
    for mid, mat in m['materials'].items():
        basis, fr = C.expand(mat)
        for n, v in N.to_atom_fractions(basis, fr).items():
            flat[f'materials.{mid}.atom.{n}'] = v
    return flat


def chain_diffs(kernel, gid, limit=14):
    """Differing fields between adjacent cases, up to limit per pair (atom fractions and scalars first)."""
    cases = [c['case_id'] for c in kernel.store.q('SELECT case_id FROM cases WHERE goal=? ORDER BY ord', (gid,))]
    flats = {}
    for c in cases:
        s = kernel._stage(gid, c, 'model')
        if s:
            flats[c] = flat_with_atoms(kernel.store.get_blob(s['node']))
    out = {}
    for a, b in zip(cases, cases[1:]):
        if a in flats and b in flats:
            d = [x for x in diff_flat(flats[a], flats[b])]
            d.sort(key=lambda x: (not x['path'].split('.')[2:3] == ['atom'] if x['path'].startswith('materials.') else 1))
            out[f'{a}->{b}'] = [f"{x['path']}: {_short(x['a'])} -> {_short(x['b'])}" for x in d[:limit]] + (
                [f'... and {len(d) - limit} more fields'] if len(d) > limit else [])
    return out


def _short(v):
    return f'{v:.6g}' if isinstance(v, float) else v


def _lookup(path, case, cases, flat):
    head, _, rest = path.partition('.')
    if head in cases and rest:
        case, path = head, rest
    f = flat(case)
    if path not in f:
        raise KeyError(f'{case} has no field {path}')
    return f[path]


def _finite(x):
    """Reject non-finite floats (Inf/NaN) and complex roots from fractional negative powers."""
    if isinstance(x, complex):
        raise ValueError('Result is not a real number')
    if isinstance(x, float) and not math.isfinite(x):
        raise OverflowError
    return x


def _arith(op):
    def f(a, b):
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            raise TypeError('String fields can only be compared with == or !=')
        return _finite(op(_finite(a), _finite(b)))
    return f


# Evaluate powers in float so huge integer exponents overflow immediately instead of hanging
_BIN = {ast.Add: _arith(operator.add), ast.Sub: _arith(operator.sub), ast.Mult: _arith(operator.mul),
        ast.Div: _arith(operator.truediv), ast.Pow: _arith(lambda a, b: float(a) ** float(b))}


def _eq(a, b):
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    return abs(_finite(a) - _finite(b)) <= 1e-9 * max(1.0, abs(a), abs(b))


def _num(op):
    def f(a, b):
        if isinstance(a, str) or isinstance(b, str):
            raise TypeError('String fields can only be compared with == or !=')
        return op(a, b)
    return f


_CMP = {ast.Lt: _num(operator.lt), ast.LtE: _num(operator.le), ast.Gt: _num(operator.gt), ast.GtE: _num(operator.ge),
        ast.Eq: _eq, ast.NotEq: lambda a, b: not _eq(a, b)}
_FN = {'abs': abs, 'min': min, 'max': max}


def _dotted(n):
    parts = []
    while isinstance(n, ast.Attribute):
        parts.append(n.attr)
        n = n.value
    if not isinstance(n, ast.Name):
        raise ValueError('Invalid field path syntax')
    parts.append(n.id)
    return '.'.join(reversed(parts))


# Preserve string literals verbatim while translating && and || outside strings to and / or
_C_LOGIC = re.compile(r"""('(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")|&&|\|\|""")


def _safe_eval(expr, lookup):
    """Evaluate restricted expressions: numeric/string literals, field paths, arithmetic/power,
    comparisons (equality only for strings), and/or/not (including &&/||), and abs/min/max."""
    expr = _C_LOGIC.sub(lambda m: m.group(1) or (' and ' if m.group(0) == '&&' else ' or '), expr)
    # Numeric segments like geometry.pin.F.0.outer_radius_cm are invalid Python attributes; rewrite around AST parse
    expr = re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|(?<=[A-Za-z_])\.(\d+)\b", lambda m: f'._{m.group(1)}' if m.group(1) else m.group(0), expr)

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float, str)) and not isinstance(n.value, bool):
            return _finite(n.value)
        if isinstance(n, (ast.Name, ast.Attribute)):
            return lookup(re.sub(r'\._(\d+)\b', r'.\1', _dotted(n)))
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            return _BIN[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return -ev(n.operand)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            return not ev(n.operand)
        if isinstance(n, ast.BoolOp):
            vals = [ev(v) for v in n.values]
            return all(vals) if isinstance(n.op, ast.And) else any(vals)
        if isinstance(n, ast.Compare):
            left = ev(n.left)
            for op, c in zip(n.ops, n.comparators):
                right = ev(c)
                if type(op) not in _CMP or not _CMP[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FN and not n.keywords:
            return _FN[n.func.id](*[ev(a) for a in n.args])
        raise ValueError('Only fields, numbers, arithmetic, power, comparisons, and, or, not, abs, min, max are allowed')
    return ev(ast.parse(expr, mode='eval'))


def eval_reason(e):
    """Human-readable evaluation error reason for rule authors."""
    if isinstance(e, OverflowError) or (isinstance(e, OSError) and e.errno == 34):
        return 'Numeric overflow'
    if isinstance(e, ZeroDivisionError):
        return 'Division by zero'
    if isinstance(e, RecursionError):
        return 'Expression nested too deeply'
    if isinstance(e, (ValueError, KeyError)):
        return str(e)
    return 'Numeric fields only here'


def snapshot(store, gid):
    out = {}
    for r in store.q('SELECT * FROM check_runs WHERE goal=? ORDER BY created', (gid,)):
        steps = []
        for e in store.q("SELECT e.* FROM evidence_head h JOIN evidence e ON e.id = h.evidence "
                         "WHERE h.kind='check' AND h.context LIKE ? ORDER BY h.context", (r['id'] + '/%',)):
            d = store.get_blob(e['detail']) if e['detail'] else {}
            res = d.get('result') or {}
            steps.append({'n': d.get('n'), 'title': d.get('title'), 'tool': d.get('tool'), 'args': d.get('args'),
                          'call': d.get('call'),
                          'status': e['status'], 'finding': e['summary'], 'evidence': e['id'],
                          'detail': {'summary': res.get('summary'), 'data': res.get('data')}})
        out[r['id']] = {'id': r['id'], 'version': r['version'], 'status': r['status'], 'source': r['source'],
                        'summary': r['summary'], 'steps': steps}
    return out
