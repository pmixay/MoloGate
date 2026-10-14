"""Insert provider accounts that point at the fake provider (run with the server's env)."""

import asyncio
import json
import sys

from open_webui.models.provider_accounts import ProviderAccounts

BASE = 'http://127.0.0.1:8199/v1'


async def main(owners):
    def models(*ids):
        names = {
            'gpt-6': 'GPT-6',
            'gpt-luna-6': 'GPT Luna',
            'claude-haiku-5-5': 'Claude Haiku 5.5',
            'claude-opus-5-5': 'Claude Opus 5.5',
        }
        return [{'id': model_id, 'name': names[model_id]} for model_id in ids]

    created = {}
    for name, owner, provider, key, ids in (
        ('A', owners['ann'], 'openai_api', 'key-a', ('gpt-6', 'gpt-luna-6')),
        ('B', owners['bob'], 'openai_api', 'key-b', ('gpt-6', 'gpt-luna-6')),
        ('C', None, 'anthropic_api', 'key-c', ('claude-opus-5-5', 'claude-haiku-5-5')),
    ):
        account = await ProviderAccounts.insert(
            owner_user_id=owner,
            provider=provider,
            plan='api',
            external_account_id='fingerprint-' + key,
            secret={'api_key': key, 'base_url': BASE},
            sharing='shared',
            capacity_weight=1,
            budget={'amount': 20, 'currency': 'USD'},
            status='ok',
            models=models(*ids),
        )
        created[name] = account.id
    print(json.dumps(created))


asyncio.run(main(json.loads(sys.argv[1])))
