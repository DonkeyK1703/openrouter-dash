#!/usr/bin/env python3
"""Break-even throughput calculator: OpenRouter poller + static server (stdlib only).

    python3 app.py [port]      # default 8787, serves http://127.0.0.1:8787
    python3 app.py --once      # CI: run due jobs, rebuild, exit

Raw API responses go to data/, the page's data to public/dashboard.json.
"""
import functools, gzip, http.server, json, os, re, sys, threading, time, urllib.error, urllib.parse, urllib.request
from collections import defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA, PUBLIC = os.path.join(ROOT, 'data'), os.path.join(ROOT, 'public')
BASE = 'https://openrouter.ai/api/v1/'

# Schedule (seconds):
# - catalog (/models): prices and :batch entries change any time; 1 call.
# - rankings: rebuilt daily (~00:00-03:00 UTC); checked every 3h, app rankings refetched only when it moves.
# - endpoints: per-provider prices/uptime; one call per open-weight model. Not in the Data API's
#   30/min + 500/day bucket (verified 2026-10-29), so every 30 min is fine.
CATALOG_EVERY = 3600
RANKINGS_EVERY = 3 * 3600
ENDPOINTS_EVERY = 1800
TOP_N = 5
CALL_GAP = 2.1  # Data API: 30 req/min per key
EP_GAP = 0.3

# Endpoint tag prefixes that count as the lab itself (first-party), when they differ from the model author.
LAB_TAGS = {'qwen': {'alibaba'}, 'mistralai': {'mistral'}, 'google': {'google-vertex', 'google-ai-studio'},
            'moonshotai': {'moonshotai', 'moonshot'}, 'z-ai': {'z-ai', 'zai'}}


def env_key():
    if os.environ.get('OPENROUTER_API_KEY'):
        return os.environ['OPENROUTER_API_KEY']
    env = os.path.join(ROOT, '.env')
    for line in open(env) if os.path.exists(env) else []:
        if line.startswith('OPENROUTER_API_KEY='):
            return line.split('=', 1)[1].strip()
    sys.exit('OPENROUTER_API_KEY missing (env or .env)')


KEY = env_key()
TRACES = os.path.join(PUBLIC, 'traces.jsonl')
TRACE_KEEP_S = 2 * 86400
RUN = {'id': None, 'job': None}  # set by run_due(); stamped on every trace line


def trace(path, status, t0, size=0, attempt=0):
    with open(TRACES, 'a') as f:
        f.write(json.dumps({'ts': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'run': RUN['id'], 'job': RUN['job'],
                            'path': path, 'status': status, 'ms': round((time.time() - t0) * 1000), 'bytes': size, 'attempt': attempt}) + '\n')


def trim_traces():
    cutoff = datetime.fromtimestamp(time.time() - TRACE_KEEP_S, timezone.utc).isoformat()
    def recent(l):
        try:
            return json.loads(l)['ts'] >= cutoff[:19]
        except (ValueError, KeyError):  # torn line from a crash mid-write
            return False
    try:
        keep = [l for l in open(TRACES) if recent(l)]
    except OSError:
        return
    with open(TRACES + '.tmp', 'w') as f:
        f.writelines(keep)
    os.replace(TRACES + '.tmp', TRACES)


def log(*a):
    print(datetime.now().strftime('%Y-%m-%d %H:%M:%S'), *a, flush=True)


def get(path, gap=CALL_GAP, **params):
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = BASE + path + ('?' + q if q else '')
    for attempt in range(3):
        time.sleep(gap)
        req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + KEY, 'User-Agent': 'openrouter-dash'})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
            trace(path, 200, t0, len(body), attempt)
            return json.loads(body)
        except urllib.error.HTTPError as e:
            trace(path, e.code, t0, 0, attempt)
            if e.code == 429 and attempt < 2:
                log('429, backing off', path)
                time.sleep(60)
                continue
            log('HTTP', e.code, url)
            return None
        except Exception as e:  # network blip: skip this call, next cycle retries
            trace(path, 'error: ' + type(e).__name__, t0, 0, attempt)
            log('ERR', url, e)
            return None


def path_of(name):
    return os.path.join(DATA, name + '.json')


def save(name, obj, dir=DATA):
    p = os.path.join(dir, name + '.json')
    with open(p + '.tmp', 'w') as f:
        json.dump(obj, f)
    os.replace(p + '.tmp', p)


def load(name, default=None, dir=DATA):
    try:
        with open(os.path.join(dir, name + '.json')) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def age(name):
    try:
        return time.time() - os.path.getmtime(path_of(name))
    except OSError:
        return float('inf')


def fetched_at(name):
    try:
        return datetime.fromtimestamp(os.path.getmtime(path_of(name)), timezone.utc).isoformat()
    except OSError:
        return None


def rk_key(m):
    """rankings permaslug = canonical_slug + variant suffix (:free, :batch)"""
    return m['canonical_slug'] + (':' + m['id'].split(':', 1)[1] if ':' in m['id'] else '')


def base_of(permaslug):
    """merge rule: drop :variant and trailing 8-digit date -> one model across snapshots"""
    return re.sub(r'-\d{8}$', '', permaslug.split(':')[0])


def per_m(v):
    try:
        return round(float(v) * 1e6, 6)
    except (TypeError, ValueError):
        return None


# ---------- fetch jobs ----------

def fetch_catalog():
    r = get('models')
    if r:
        save('models', r['data'])
    log('catalog ok')


def fetch_rankings():
    r = get('datasets/rankings-daily')
    if not r:
        return
    old = load('rankings')
    save('rankings', r)
    if old and old['meta'].get('end_date') == r['meta'].get('end_date') and load('apps'):
        log('rankings unchanged', r['meta'].get('end_date'))
        return
    apps = {}
    for sort in ('popular', 'trending'):
        rows, meta = [], None
        for offset in (0, 100):
            a = get('datasets/app-rankings', sort=sort, limit=100, offset=offset)
            if a:
                rows += a['data']
                meta = a['meta']
        apps[sort] = {'data': rows, 'meta': meta}
    save('apps', apps)
    log('rankings + apps refreshed', r['meta'].get('end_date'))


def fetch_endpoints():
    targets = defaultdict(list)  # variants share one endpoints page
    for m in load('models') or []:
        if m.get('hugging_face_id'):
            targets[(m.get('links') or {}).get('details') or f"/api/v1/models/{m['id']}/endpoints"].append(m['id'])
    eps, ok, ts = load('endpoints', {}), 0, datetime.now(timezone.utc).isoformat()
    os.makedirs(os.path.join(DATA, 'snapshots'), exist_ok=True)
    # one gzip member per poll, appended; gzip.open reads them back as one stream
    with gzip.open(os.path.join(DATA, 'snapshots', ts[:10] + '.jsonl.gz'), 'at') as snap:
        for l, ids in targets.items():
            r = get(l.removeprefix('/api/v1/'), gap=EP_GAP)
            if not r:
                continue
            ok += 1
            snap.write(json.dumps({'ts': ts, 'link': l, 'ids': ids, 'endpoints': r['data']['endpoints']}) + '\n')
            for i in ids:
                eps[i] = {'fetched': ts, 'endpoints': r['data']['endpoints']}
    save('endpoints', eps)
    log(f'endpoints ok {ok}/{len(targets)}')


# ---------- build ----------

def tok7():
    rk = load('rankings') or {'data': []}
    last7 = set(sorted({r['date'] for r in rk['data']})[-7:])
    out = defaultdict(int)
    for r in rk['data']:
        if r['date'] in last7:
            out[r['model_permaslug']] += int(r['total_tokens'])
    return out


def slim(e):
    # Listed prices are already effective (post-promotion): e.g. DeepInfra $0.098 = $0.20 x (1 - 0.51).
    # discount is kept so the UI can flag the promo and show the list price.
    p = e['pricing']
    return {'provider': e['provider_name'], 'tag': e['tag'], 'tier': e.get('service_tier'), 'status': e.get('status'),
            'uptime': e.get('uptime_last_1d'), 'quant': e.get('quantization'), 'discount': float(p.get('discount') or 0),
            'in': per_m(p.get('prompt')), 'cache': per_m(p.get('input_cache_read')), 'out': per_m(p.get('completion'))}


def is_lab(m, e):
    author = m['id'].split('/')[0]
    return e['tag'].split('/')[0] in LAB_TAGS.get(author, {author})


def build():
    models = load('models') or []
    eps = load('endpoints', {})
    tokens = tok7()
    by_key = {rk_key(m): m for m in models}
    by_id = {m['id']: m for m in models}
    # Step 1: open-weight, real-time permaslugs (no :free/:batch), ranked by trailing-7d tokens
    cands = sorted((k for k in tokens if k in by_key and ':' not in k and by_key[k].get('hugging_face_id')), key=lambda k: -tokens[k])
    rows = []
    for i, k in enumerate(cands[:TOP_N], 1):
        m = by_key[k]
        own = (eps.get(m['id']) or {}).get('endpoints') or []
        # anchor: lab's own standard endpoint on this slug, else the lab's price on a sibling snapshot
        anchor = None
        for src in [m] + [by_key[s] for s in by_key if s != k and ':' not in s and base_of(s) == base_of(k)]:
            labs = [e for e in (eps.get(src['id']) or {}).get('endpoints') or [] if is_lab(m, e)]
            labs.sort(key=lambda e: e.get('service_tier') is not None)
            if labs:
                anchor = slim(labs[0]) | {'from': None if src is m else src['id']}
                break
        b = by_id.get(m['id'] + ':batch')
        bp = (b or {}).get('pricing') or {}
        rows.append({
            'rank': i, 'permaslug': k, 'id': m['id'], 'name': m['name'], 'tok7': tokens[k],
            'prices_fetched': (eps.get(m['id']) or {}).get('fetched'),
            'endpoints': [slim(e) for e in own], 'anchor': anchor,
            'batch': {'id': b['id'], 'in': per_m(bp.get('prompt')), 'cache': per_m(bp.get('input_cache_read')),
                      'out': per_m(bp.get('completion'))} if b else None,
        })
    save('dashboard', {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'rankings_end': ((load('rankings') or {}).get('meta') or {}).get('end_date'),
        'sources': {n: {'fetched': fetched_at(n)} for n in ('models', 'rankings', 'apps', 'endpoints')},
        'models': rows,
        'apps': load('apps') or {},
    }, dir=PUBLIC)
    trim_traces()
    log('built', [r['permaslug'] for r in rows])


JOBS = [('models', CATALOG_EVERY, fetch_catalog), ('rankings', RANKINGS_EVERY, fetch_rankings), ('endpoints', ENDPOINTS_EVERY, fetch_endpoints)]


def run_due():
    RUN['id'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    for name, every, fn in JOBS:
        if age(name) > every - 300:  # 5 min slack: scheduled CI runs drift
            RUN['job'] = name
            try:
                fn()
                build()
            except Exception as e:
                trace('job:' + name, 'error: ' + repr(e)[:200], time.time())
                log('job failed', name, repr(e))


def restore_mtimes():
    """CI has no disk between runs: raw files come back from the deployed site, so put
    their fetch times back from dashboard.json for age() to see."""
    for name, v in ((load('dashboard', dir=PUBLIC) or {}).get('sources') or {}).items():
        if (v or {}).get('fetched') and os.path.exists(path_of(name)):
            t = datetime.fromisoformat(v['fetched']).timestamp()
            os.utime(path_of(name), (t, t))


def poll_forever():
    if load('models'):
        build()
    while True:
        run_due()
        time.sleep(60)


if __name__ == '__main__':
    if '--once' in sys.argv:
        restore_mtimes()
        run_due()
        sys.exit()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    threading.Thread(target=poll_forever, daemon=True).start()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=PUBLIC)
    http.server.SimpleHTTPRequestHandler.log_message = lambda *a: None
    log(f'serving http://127.0.0.1:{port}')
    http.server.ThreadingHTTPServer(('127.0.0.1', port), handler).serve_forever()
