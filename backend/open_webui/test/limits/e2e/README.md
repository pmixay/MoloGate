# Fair use end to end (phase 4)

Runs the real app against a fake OpenAI-compatible provider and checks every enforcement rule over
HTTP. Use it before switching a site to `MOLOGATE_LIMITS_MODE=enforce`. It never touches a real
provider or database: everything lives in `E2E_DIR` (default `/tmp/mologate-e2e`).

The team: Ann (admin) owns OpenAI key A, Bob owns key B, Cat and Dan have none, and a team
Anthropic key C offers a worker on another provider. Each key has a $20 budget and points at the
fake provider, which can answer 429, take its time and say what a request cost.

```bash
cd backend/open_webui/test/limits/e2e
python fake_provider.py &                       # port 8199
PYTHON=python ./server.sh start enforce fresh   # port 8080, one request per account at once
E2E_DIR=/tmp/mologate-e2e DATA_DIR=/tmp/mologate-e2e/data WEBUI_SECRET_KEY=e2e-secret \
  PYTHONPATH=../../../.. python drive.py enforce
./server.sh start observe                       # same data, observe mode
python drive.py observe
./server.sh start enforce fresh                 # credits (phase 5), from a fresh team
python drive.py credits
./server.sh start enforce fresh                 # personal API keys (phase 6), with the OpenAI SDK
python drive.py keys
./server.sh stop
```

`drive.py enforce` checks, in order: the owner on their own account; a member in the pool, their
standing at 88% of the share; past it, the worker model with its notice (answer and stream
header); failover within the pool tier, and from a limited own account to the pool; the
per-account cap with three requests at once; the pool spent until the refusal says when it frees
up and offers Claude Haiku on Anthropic; and the owner still on their locked reserve.
`drive.py observe` checks that the same member is served as asked, with the decision only
recorded. `drive.py credits` checks that Dan's Z.ai Max plan earns 200 credits, that money spent
through a key earns its owner 0.5 a dollar, that past his share Dan keeps GPT-6 on credits and is
charged for it, and that disconnecting the plan forfeits the rest. `drive.py keys` uses the
official OpenAI Python SDK with a key Cat made: it lists models, chats and streams, the use counts
in her share and shows on her keys page, the key reaches nothing else, its limits hold, and a
revoked key stops at once.
