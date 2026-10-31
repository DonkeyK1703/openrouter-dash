import json, os, tempfile
import app

assert app.rk_key({'id': 'openai/gpt-6-luna:batch', 'canonical_slug': 'openai/gpt-6-luna-20260922'}) == 'openai/gpt-6-luna-20260922:batch'
assert app.rk_key({'id': 'openai/gpt-6-luna', 'canonical_slug': 'openai/gpt-6-luna-20260922'}) == 'openai/gpt-6-luna-20260922'
assert app.base_of('deepseek/deepseek-v4.1-flash-20261017') == app.base_of('deepseek/deepseek-v4.1-flash-20260910:batch') == 'deepseek/deepseek-v4.1-flash'
assert app.base_of('deepseek/deepseek-r1-0528') == 'deepseek/deepseek-r1-0528'  # short tags are distinct models
assert app.per_m('0.0000025') == 2.5 and app.per_m(None) is None

# listed prices are already post-discount; slim() must not discount again
e = app.slim({'provider_name': 'P', 'tag': 'p', 'pricing': {'prompt': '0.000000098', 'completion': '0.000000294', 'discount': 0.51}})
assert (e['in'], e['out'], e['discount']) == (0.098, 0.294, 0.51)

app.TRACES = os.path.join(tempfile.mkdtemp(), 't.jsonl')
open(app.TRACES, 'w').write(json.dumps({'ts': '2000-01-01T00:00:00+00:00'}) + '\n')
app.trace('x', 200, app.time.time())
app.trim_traces()
assert [json.loads(l)['path'] for l in open(app.TRACES)] == ['x']
print('ok')
open(app.TRACES, 'a').write('{"ts": "2099-01-01T0')  # torn line must not break the build
app.trim_traces()
assert [json.loads(l)['path'] for l in open(app.TRACES)] == ['x']
print('torn-line ok')
