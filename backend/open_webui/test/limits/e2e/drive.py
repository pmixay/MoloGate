"""Drive Mologate's enforcement end to end over HTTP, against the fake provider.

Members: Ann (admin, owns key A), Bob (owns key B), Cat and Dan (no account). A team Anthropic key C
offers a worker on another provider. Every key has a $20 monthly budget.
"""

import concurrent.futures
import json
import os
import sqlite3
import subprocess
import sys
import time

import requests

MOLOGATE = 'http://localhost:8080'
FAKE = 'http://127.0.0.1:8199'
HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get('E2E_DIR', '/tmp/mologate-e2e')
DB = os.path.join(WORK, 'data', 'webui.db')
GPT = 'account:openai_api:gpt-6'
CACHE_WAIT = 16  # the pool's numbers are kept for 15 seconds
results = []


def check(name, ok, detail=''):
    results.append((name, bool(ok)))
    print(('PASS ' if ok else 'FAIL ') + name + (f'  [{detail}]' if detail else ''), flush=True)


def fake(path, body=None):
    if body is None:
        return requests.get(FAKE + path).json()
    return requests.post(FAKE + path, json=body).json()


def control(**keys):
    fake('/control', keys)


def calls_since(reset=True):
    data = fake('/calls')
    if reset:
        fake('/calls/reset', {})
    return data


def chat(token, model=GPT, stream=False):
    response = requests.post(
        MOLOGATE + '/api/chat/completions',
        headers={'Authorization': 'Bearer ' + token},
        json={'model': model, 'messages': [{'role': 'user', 'content': 'Hi'}], 'stream': stream},
        timeout=300,
    )
    if stream:
        return response.status_code, response.text, response.headers
    try:
        body = response.json()
    except ValueError:
        body = response.text
    return response.status_code, body, response.headers


def standing(token, model=GPT):
    return requests.get(
        MOLOGATE + '/api/v1/models/model/limits', params={'id': model}, headers={'Authorization': 'Bearer ' + token}
    ).json()


def ledger(user_id, since):
    time.sleep(1)  # rows are written in the background
    with sqlite3.connect(DB) as db:
        return db.execute(
            'SELECT ts, account_id, model_id, tier, status, decision FROM usage_event '
            'WHERE user_id = ? AND ts >= ? ORDER BY created_at',
            (user_id, since),
        ).fetchall()


def signin(email):
    return requests.post(MOLOGATE + '/api/v1/auths/signin', json={'email': email, 'password': 'E2e-test-1234'}).json()


def setup():
    admin = requests.post(
        MOLOGATE + '/api/v1/auths/signup',
        json={'name': 'Ann', 'email': 'ann@example.com', 'password': 'E2e-test-1234'},
    ).json()
    users = {'ann': admin}
    for name in ('Bob', 'Cat', 'Dan'):
        requests.post(
            MOLOGATE + '/api/v1/auths/add',
            headers={'Authorization': 'Bearer ' + admin['token']},
            json={'name': name, 'email': f'{name.lower()}@example.com', 'password': 'E2e-test-1234', 'role': 'user'},
        ).raise_for_status()
        users[name.lower()] = signin(f'{name.lower()}@example.com')
    seeded = subprocess.run(
        [sys.executable, os.path.join(HERE, 'seed_accounts.py'), json.dumps({k: v['id'] for k, v in users.items()})],
        capture_output=True,
        text=True,
        check=True,
    )
    accounts = json.loads(seeded.stdout.strip().splitlines()[-1])
    state = {'users': {k: {'id': v['id'], 'token': v['token']} for k, v in users.items()}, 'accounts': accounts}
    with open(os.path.join(WORK, 'state.json'), 'w') as handle:
        json.dump(state, handle)
    return state


def enforce_run():
    state = setup()
    users, accounts = state['users'], state['accounts']
    key_of = {accounts['A']: 'key-a', accounts['B']: 'key-b', accounts['C']: 'key-c'}
    for user in users.values():
        requests.get(MOLOGATE + '/api/models', headers={'Authorization': 'Bearer ' + user['token']})
    control(**{key: {'mode': 'ok', 'cost': 1.0, 'delay': 0} for key in ('key-a', 'key-b', 'key-c')})
    calls_since()

    # 1. The owner is served by their own account.
    start = int(time.time())
    status, body, _ = chat(users['ann']['token'])
    calls = calls_since()['calls']
    rows = ledger(users['ann']['id'], start)
    check(
        '1 owner served by own account (tier reserve)',
        status == 200 and [c['key'] for c in calls] == ['key-a'] and rows and rows[-1][3] == 'reserve',
        f'status={status} calls={[c["key"] for c in calls]} tiers={[r[3] for r in rows]}',
    )
    check('1 decision recorded as enforce', rows and json.loads(rows[-1][5])['mode'] == 'enforce')
    time.sleep(CACHE_WAIT)

    # 2. Cat within her share: the pool, the model she asked for. Each request costs $5.50:
    # 27.5 units of a share of 125 / 4 = 31.25.
    control(**{key: {'mode': 'ok', 'cost': 5.5, 'delay': 0} for key in ('key-a', 'key-b')})
    start = int(time.time())
    status, body, _ = chat(users['cat']['token'])
    calls = calls_since()['calls']
    rows = ledger(users['cat']['id'], start)
    check(
        '2 member within share served from the pool with the model asked for',
        status == 200 and calls and calls[-1]['model'] == 'gpt-6' and rows[-1][3] == 'pool',
        f'status={status} calls={calls} tiers={[r[3] for r in rows]}',
    )
    check('2 no notice within the share', isinstance(body, dict) and 'notice' not in (body.get('mologate') or {}))
    time.sleep(CACHE_WAIT)

    # 3. The context indicator's standing: 88% of the share, a warning, minutes left.
    limits = standing(users['cat']['token'])
    allowance = limits.get('allowance') or {}
    window = (allowance.get('windows') or {}).get('budget') or {}
    check(
        '3 standing shows tier pool, 88% of share, low, minutes left',
        allowance.get('tier') == 'pool'
        and window.get('level') == 'low'
        and round(window.get('percent', 0)) == 88
        and window.get('minutes_left') is not None
        and window.get('resets_at'),
        json.dumps(window),
    )
    check('3 account windows still listed beside it', 'windows' in limits and 'outlook' in limits)

    # 4. One more within the share, then past it: the worker model, with a notice.
    chat(users['cat']['token'])
    calls_since()
    time.sleep(CACHE_WAIT)
    start = int(time.time())
    status, body, _ = chat(users['cat']['token'])
    calls = calls_since()['calls']
    rows = ledger(users['cat']['id'], start)
    notice = (body.get('mologate') or {}).get('notice', '') if isinstance(body, dict) else ''
    check(
        '4 past the share the reply uses the worker model',
        status == 200
        and calls
        and calls[-1]['model'] == 'gpt-luna-6'
        and rows[-1][3] == 'worker'
        and rows[-1][2] == 'gpt-luna-6',
        f'status={status} calls={[c["model"] for c in calls]} rows={[(r[2], r[3]) for r in rows]}',
    )
    check(
        '4 the answer carries the notice with a reset time',
        notice.startswith('Your share is used up, so this reply uses GPT Luna. Resets in '),
        notice,
    )
    content = body['choices'][0]['message']['content'] if isinstance(body, dict) and body.get('choices') else ''
    check("4 the reply itself is the worker model's", content == 'Hello from gpt-luna-6', content)
    allowance = standing(users['cat']['token']).get('allowance') or {}
    check(
        '4 standing shows the worker tier and the model by name',
        allowance.get('tier') == 'worker'
        and allowance.get('model_name') == 'GPT Luna'
        and allowance['windows']['budget']['level'] == 'empty',
        json.dumps({k: allowance.get(k) for k in ('tier', 'model_name')}),
    )

    # 5. Streaming past the share: the worker model, the notice in a header.
    status, text, headers = chat(users['cat']['token'], stream=True)
    calls = calls_since()['calls']
    check(
        '5 a streamed reply past the share uses the worker',
        status == 200 and calls and calls[-1]['model'] == 'gpt-luna-6' and 'Hello from gpt-luna-6' in text,
        f'status={status} calls={[c["model"] for c in calls]}',
    )
    check('5 stream carries the notice header', 'X-Mologate-Notice' in headers, str(dict(headers))[:300])

    # 6. Failover within the tier: whichever account Dan is given first answers 429.
    control(**{key: {'mode': 'ok', 'cost': 0.1, 'delay': 0} for key in ('key-a', 'key-b')})
    failovers = 0
    for limited in ('key-a', 'key-b'):
        control(**{limited: {'mode': '429', 'cost': 0.1}})
        start = int(time.time())
        status, body, _ = chat(users['dan']['token'])
        keys = [c['key'] for c in calls_since()['calls']]
        rows = ledger(users['dan']['id'], start)
        control(**{limited: {'mode': 'ok', 'cost': 0.1}})
        tiers = [(r[4], r[3]) for r in rows]
        failed_over = keys == [limited, ({'key-a', 'key-b'} - {limited}).pop()]
        failovers += failed_over
        check(
            f'6 Dan served despite {limited} answering 429, within the pool tier',
            status == 200
            and keys.count(limited) <= 1
            and keys[-1] != limited
            and all(tier == 'pool' for _, tier in tiers)
            and (not failed_over or tiers == [('limited', 'pool'), ('ok', 'pool')]),
            f'keys={keys} rows={tiers}',
        )
    check('6 at least one request actually failed over', failovers >= 1, f'failovers={failovers}')

    # 7. An owner whose own account is limited goes to the pool.
    control(**{'key-b': {'mode': '429', 'cost': 0.1}})
    start = int(time.time())
    status, body, _ = chat(users['bob']['token'])
    keys = [c['key'] for c in calls_since()['calls']]
    rows = ledger(users['bob']['id'], start)
    control(**{'key-b': {'mode': 'ok', 'cost': 0.1}})
    check(
        '7 owner with limited own account goes to the pool',
        status == 200
        and keys == ['key-b', 'key-a']
        and [(r[4], r[3]) for r in rows] == [('limited', 'reserve'), ('ok', 'pool')],
        f'keys={keys} rows={[(r[4], r[3]) for r in rows]}',
    )

    # 8. The per-account cap (1 at once in this run): three requests at once from Dan.
    control(**{key: {'mode': 'ok', 'cost': 0.01, 'delay': 4} for key in ('key-a', 'key-b')})
    calls_since()
    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(3) as pool:
        statuses = [future.result()[0] for future in [pool.submit(chat, users['dan']['token']) for _ in range(3)]]
    elapsed = time.time() - started
    data = calls_since()
    check(
        '8 per-account cap: never two at once on one key, the third waits, all served',
        statuses == [200, 200, 200] and all(v <= 1 for v in data['peak'].values()) and elapsed >= 7.5,
        f'statuses={statuses} peak={data["peak"]} elapsed={elapsed:.1f}s keys={[c["key"] for c in data["calls"]]}',
    )
    check('8 both accounts used in parallel', len({c['key'] for c in data['calls']}) == 2)
    control(**{key: {'mode': 'ok', 'cost': 10.0, 'delay': 0} for key in ('key-a', 'key-b')})

    # 9. Spend the pool until it has no room: Cat (on the worker) gets a limit error.
    refused = None
    for _ in range(6):
        time.sleep(CACHE_WAIT)
        status, body, _ = chat(users['cat']['token'])
        calls_since()
        if status != 200:
            refused = (status, body)
            break
    detail = refused[1].get('detail', '') if refused and isinstance(refused[1], dict) else str(refused)
    check('9 empty pool: 429', refused is not None and refused[0] == 429, str(refused)[:300])
    check('9 says when it frees up', 'frees up in' in detail, detail)
    check('9 offers a worker on another provider', 'Claude Haiku 5.5 (Anthropic)' in detail, detail)
    status, body, _ = chat(users['cat']['token'], model='account:anthropic_api:claude-haiku-5-5')
    check('9 that worker does serve Cat', status == 200, str(body)[:200])
    calls_since()
    control(**{'key-a': {'mode': 'ok', 'cost': 0.1}})
    start = int(time.time())
    status, body, _ = chat(users['ann']['token'])
    rows = ledger(users['ann']['id'], start)
    keys = [c['key'] for c in calls_since()['calls']]
    check(
        '9 the owner still gets their locked reserve',
        status == 200 and keys == ['key-a'] and rows[-1][3] == 'reserve',
        f'status={status} keys={keys} tiers={[r[3] for r in rows]}',
    )


def observe_run():
    with open(os.path.join(WORK, 'state.json')) as handle:
        state = json.load(handle)
    users = state['users']
    control(**{key: {'mode': 'ok', 'cost': 0.1, 'delay': 0} for key in ('key-a', 'key-b', 'key-c')})
    calls_since()
    for user in users.values():
        requests.get(MOLOGATE + '/api/models', headers={'Authorization': 'Bearer ' + user['token']})
    start = int(time.time())
    status, body, _ = chat(users['cat']['token'])
    calls = calls_since()['calls']
    rows = ledger(users['cat']['id'], start)
    decision = json.loads(rows[-1][5]) if rows and rows[-1][5] else {}
    check(
        'O1 observe: Cat past her share still gets the model she asked for',
        status == 200 and [c['model'] for c in calls] == ['gpt-6'],
        f'status={status} calls={calls}',
    )
    check('O1 no notice in observe', isinstance(body, dict) and not (body.get('mologate') or {}).get('notice'))
    check(
        'O1 decision recorded beside it, tier column still whose account',
        decision.get('mode') == 'observe' and decision.get('tier') in ('worker', 'empty') and rows[-1][3] == 'pool',
        f'decision={decision.get("tier")}/{decision.get("mode")} tier={rows[-1][3] if rows else None}',
    )


def my_credits(token):
    return requests.get(
        MOLOGATE + '/api/v1/provider-accounts/credits', headers={'Authorization': 'Bearer ' + token}
    ).json()


def credits_run():
    """Phase 5: credits earned through a key, granted for a subscription, spent past the share and
    forfeited on disconnect. Both owners stay idle, so the pool is 150 units and each share 37.5."""
    state = setup()
    users, accounts = state['users'], state['accounts']
    owner_of = {'key-a': 'ann', 'key-b': 'bob'}
    for user in users.values():
        requests.get(MOLOGATE + '/api/models', headers={'Authorization': 'Bearer ' + user['token']})
    control(**{key: {'mode': 'ok', 'cost': 4.0, 'delay': 0} for key in ('key-a', 'key-b', 'key-c')})
    calls_since()

    view = my_credits(users['dan']['token'])
    check(
        'C1 a Z.ai Max plan earns 200 credits when connected',
        view['balance'] == 200
        and [(g['provider'], g['plan'], g['amount']) for g in view['grants']] == [('zai', 'max', 200)],
        json.dumps(view)[:300],
    )

    # Cat spends $4 through a member's key: its owner earns 2 credits.
    status, _, _ = chat(users['cat']['token'])
    key = calls_since()['calls'][-1]['key']
    time.sleep(1)
    earned = my_credits(users[owner_of[key]]['token'])
    check(
        'C2 a key earns its owner 0.5 credits per dollar spent through it',
        status == 200 and earned['balance'] == 2 and earned['grants'][0]['plan'] == 'api',
        f'key={key} balance={earned["balance"]}',
    )
    check('C2 the member who spent earns nothing', my_credits(users['cat']['token'])['balance'] == 0)

    # Dan: $8 (40 units) puts him past his 37.5; his 200 credits then keep GPT-6 for him.
    control(**{key: {'mode': 'ok', 'cost': 8.0, 'delay': 0} for key in ('key-a', 'key-b')})
    chat(users['dan']['token'])
    calls_since()
    time.sleep(CACHE_WAIT)
    start = int(time.time())
    status, body, _ = chat(users['dan']['token'])
    calls = calls_since()['calls']
    time.sleep(1)
    with sqlite3.connect(DB) as db:
        row = db.execute(
            'SELECT tier, model_id, credits_charged FROM usage_event WHERE user_id = ? AND ts >= ? AND status = ? '
            'ORDER BY created_at DESC',
            (users['dan']['id'], start, 'ok'),
        ).fetchone()
    balance = my_credits(users['dan']['token'])['balance']
    check(
        'C3 past the share, credits pay for the big model (tier credit)',
        status == 200 and calls[-1]['model'] == 'gpt-6' and row and row[0] == 'credit',
        f'calls={[c["model"] for c in calls]} row={row}',
    )
    check(
        'C3 charged 40 units / (0.4% of 150) = 66.67 credits',
        row and abs(row[2] - 40 / 0.6) < 0.01 and abs(balance - (200 - 40 / 0.6)) < 0.01,
        f'charged={row and row[2]} balance={balance}',
    )
    allowance = standing(users['dan']['token']).get('allowance') or {}
    check(
        'C3 the context indicator shows the balance',
        abs(allowance.get('credits', 0) - balance) < 0.01,
        str(allowance.get('credits')),
    )

    # Disconnecting the plan forfeits what is left of its credits; past the share is worker again.
    deleted = requests.delete(
        MOLOGATE + f'/api/v1/provider-accounts/{accounts["Z"]}',
        headers={'Authorization': 'Bearer ' + users['dan']['token']},
    )
    view = my_credits(users['dan']['token'])
    check(
        'C4 disconnecting forfeits the unspent credits',
        deleted.status_code == 200 and view['balance'] == 0 and view['grants'][0]['forfeited_at'],
        f'status={deleted.status_code} balance={view["balance"]}',
    )
    time.sleep(CACHE_WAIT)
    status, _, _ = chat(users['dan']['token'])
    calls = calls_since()['calls']
    check(
        'C4 without credits, past the share is the worker model',
        status == 200 and calls[-1]['model'] == 'gpt-luna-6',
        str(calls),
    )


def keys_api(token, path='', method='GET', body=None):
    response = requests.request(
        method, MOLOGATE + '/api/v1/api-keys' + path, headers={'Authorization': 'Bearer ' + token}, json=body
    )
    return response.status_code, response.json()


def keys_run():
    """Phase 6: Cat uses Mologate from an outside client (the official OpenAI SDK) with her own key,
    and that use counts in her share."""
    import openai

    state = setup()
    users = state['users']
    control(**{key: {'mode': 'ok', 'cost': 2.0, 'delay': 0} for key in ('key-a', 'key-b', 'key-c')})
    calls_since()
    cat = users['cat']['token']

    status, made = keys_api(cat, '', 'POST', {'name': 'Laptop', 'limits': {'rpm': 30}})
    secret = made.get('secret', '')
    check('K1 a member makes a named key, shown once', status == 200 and secret.startswith('sk-'), str(made)[:200])
    status, listed = keys_api(cat)
    check(
        'K1 the list never shows it again',
        status == 200 and secret not in json.dumps(listed),
        listed['data'][0]['hint'],
    )

    client = openai.OpenAI(base_url=MOLOGATE + '/api', api_key=secret, max_retries=0)
    ids = [model.id for model in client.models.list().data]
    check('K2 the SDK lists the models', GPT in ids, str(ids)[:200])
    before = (standing(cat).get('allowance') or {}).get('windows', {}).get('budget', {}).get('used', 0)
    start = int(time.time())
    answer = client.chat.completions.create(model=GPT, messages=[{'role': 'user', 'content': 'Hi'}])
    check(
        'K3 the SDK chats through the key',
        answer.choices[0].message.content == 'Hello from gpt-6',
        answer.choices[0].message.content,
    )
    chunks = [
        chunk.choices[0].delta.content or ''
        for chunk in client.chat.completions.create(
            model=GPT, messages=[{'role': 'user', 'content': 'Hi'}], stream=True
        )
        if chunk.choices
    ]
    check('K3 and streams', ''.join(chunks) == 'Hello from gpt-6', ''.join(chunks))
    time.sleep(1)
    with sqlite3.connect(DB) as db:
        rows = db.execute(
            'SELECT api_key_id, source, user_id, est_units FROM usage_event WHERE ts >= ? AND status = ?', (start, 'ok')
        ).fetchall()
    key_id = made['key']['id']
    check(
        "K4 the ledger names the key, as the owner's use",
        len(rows) == 2 and all(r[0] == key_id and r[1] == 'api_key' and r[2] == users['cat']['id'] for r in rows),
        str(rows),
    )
    time.sleep(CACHE_WAIT)
    after = (standing(cat).get('allowance') or {}).get('windows', {}).get('budget', {}).get('used', 0)
    check('K5 that use counts in her share (2 x $2 = 20 units)', round(after - before, 3) == 20, f'{before} -> {after}')
    status, listed = keys_api(cat)
    usage = listed['data'][0]['usage']
    check('K5 her keys page shows it', usage['week']['requests'] == 2 and usage['points_today'], json.dumps(usage))

    for path in ('/api/v1/api-keys', '/openai/chat/completions', '/api/v1/provider-accounts'):
        response = requests.get(MOLOGATE + path, headers={'Authorization': 'Bearer ' + secret})
        check(f'K6 the key cannot reach {path}', response.status_code == 403, str(response.status_code))

    status, limited = keys_api(cat, '', 'POST', {'name': 'Script', 'limits': {'rpm': 2}})
    script = openai.OpenAI(base_url=MOLOGATE + '/api', api_key=limited['secret'], max_retries=0)
    errors = []
    for _ in range(3):
        try:
            script.chat.completions.create(model=GPT, messages=[{'role': 'user', 'content': 'Hi'}])
        except openai.RateLimitError as error:
            errors.append(error.status_code)
    check('K7 a key held to 2 requests a minute gets 429 on the third', errors == [429], str(errors))

    status, _ = keys_api(cat, '', 'POST', {'name': 'Too much', 'limits': {'concurrency': 50}})
    check('K8 a key cannot run more at once than its owner', status == 400, str(status))

    keys_api(cat, f'/{key_id}', 'DELETE')
    try:
        client.chat.completions.create(model=GPT, messages=[{'role': 'user', 'content': 'Hi'}])
        revoked = None
    except openai.AuthenticationError as error:
        revoked = error.status_code
    check('K9 a revoked key stops working at once', revoked == 401, str(revoked))


if __name__ == '__main__':
    {'enforce': enforce_run, 'observe': observe_run, 'credits': credits_run, 'keys': keys_run}[sys.argv[1]]()
    failed = [name for name, ok in results if not ok]
    print(f'\n{len(results) - len(failed)} passed, {len(failed)} failed')
    sys.exit(1 if failed else 0)
