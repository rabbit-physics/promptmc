"""Shared utilities: canonical JSON, content hashing, dict flattening, and numeric comparison."""
import hashlib
import json
import math
import time


def canonical(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def sha256(data) -> str:
    if isinstance(data, str):
        data = data.encode('utf-8')
    elif not isinstance(data, (bytes, bytearray)):
        data = canonical(data)
    return hashlib.sha256(data).hexdigest()


def now() -> float:
    return round(time.time(), 3)


def flatten(obj, prefix=''):
    """Flatten a nested dict into {path: leaf_value}; lists are indexed by position."""
    out = {}
    if isinstance(obj, dict):
        for k in sorted(obj):
            p = f'{prefix}.{k}' if prefix else str(k)
            out.update(flatten(obj[k], p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, f'{prefix}[{i}]'))
    else:
        out[prefix] = obj
    return out


def same_value(a, b, rel=1e-6, abs_=1e-12):
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if math.isnan(a) or math.isnan(b):
            return False
        return math.isclose(a, b, rel_tol=rel, abs_tol=abs_)
    return a == b


def diff_flat(a: dict, b: dict, rel=1e-6):
    """Compare two flat dicts, returning [{path, a, b, status}] where status is diff, only_a, or only_b."""
    rows = []
    for p in sorted(set(a) | set(b)):
        if p not in b:
            rows.append({'path': p, 'a': a[p], 'b': None, 'status': 'only_a'})
        elif p not in a:
            rows.append({'path': p, 'a': None, 'b': b[p], 'status': 'only_b'})
        elif not same_value(a[p], b[p], rel=rel):
            rows.append({'path': p, 'a': a[p], 'b': b[p], 'status': 'diff'})
    return rows
