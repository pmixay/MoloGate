"""Which AI lab makes each model, for bundled lab logos and grouping in the model dropdown.

Every model in the listing gets a ``lab`` entry ({'id', 'name'}) when its lab is recognized, from,
in order:

1. The provider's organization prefix, as OpenRouter-style ids carry it (``z-ai/glm-5.3-flash``).
2. The model family in its id or name (``claude-...``, ``gemini-...``, ``glm-...``).

Custom models built on a base model fall back to the base model's lab. ``owned_by`` is not used:
OpenAI-compatible connections and gateways report ``openai`` for every model they serve. A model without a profile
image of its own shows its lab's logo (``assets/lab_logos``, built by scripts/build-lab-logos.py).
"""

import re
from functools import cache
from pathlib import Path

LOGO_DIR = Path(__file__).resolve().parent.parent / 'assets' / 'lab_logos'

# id: (display name, lobehub icon the logo is built from)
LABS = {
    'openai': ('OpenAI', 'openai'),
    'anthropic': ('Anthropic', 'claude'),
    'google': ('Google', 'gemini'),
    'meta': ('Meta', 'meta'),
    'mistral': ('Mistral AI', 'mistral'),
    'deepseek': ('DeepSeek', 'deepseek'),
    'qwen': ('Qwen', 'qwen'),
    'zai': ('Z.ai', 'zai'),
    'moonshot': ('Moonshot AI', 'kimi'),
    'xai': ('xAI', 'xai'),
    'minimax': ('MiniMax', 'minimax'),
    'cohere': ('Cohere', 'cohere'),
    'microsoft': ('Microsoft', 'microsoft'),
    'nvidia': ('NVIDIA', 'nvidia'),
    'amazon': ('Amazon', 'nova'),
    'perplexity': ('Perplexity', 'perplexity'),
    'bytedance': ('ByteDance', 'bytedance'),
    'baidu': ('Baidu', 'baidu'),
    'tencent': ('Tencent', 'hunyuan'),
    '01ai': ('01.AI', 'yi'),
    'stepfun': ('StepFun', 'stepfun'),
    'ibm': ('IBM', 'ibm'),
    'ai21': ('AI21 Labs', 'ai21'),
    'liquid': ('Liquid AI', 'liquid'),
    'nousresearch': ('Nous Research', 'nousresearch'),
}

# Organization prefixes that name a lab.
ORGANIZATIONS = {
    'openai': 'openai',
    'anthropic': 'anthropic',
    'google': 'google',
    'meta': 'meta',
    'meta-llama': 'meta',
    'mistral': 'mistral',
    'mistralai': 'mistral',
    'deepseek': 'deepseek',
    'deepseek-ai': 'deepseek',
    'qwen': 'qwen',
    'alibaba': 'qwen',
    'z-ai': 'zai',
    'zai': 'zai',
    'zai-org': 'zai',
    'zhipu': 'zai',
    'zhipuai': 'zai',
    'thudm': 'zai',
    'moonshot': 'moonshot',
    'moonshotai': 'moonshot',
    'x-ai': 'xai',
    'xai': 'xai',
    'minimax': 'minimax',
    'cohere': 'cohere',
    'microsoft': 'microsoft',
    'nvidia': 'nvidia',
    'amazon': 'amazon',
    'perplexity': 'perplexity',
    'bytedance': 'bytedance',
    'bytedance-seed': 'bytedance',
    'baidu': 'baidu',
    'tencent': 'tencent',
    '01-ai': '01ai',
    'stepfun': 'stepfun',
    'stepfun-ai': 'stepfun',
    'ibm': 'ibm',
    'ibm-granite': 'ibm',
    'ai21': 'ai21',
    'liquid': 'liquid',
    'nousresearch': 'nousresearch',
}

# Model families, matched at the start of the id (after any organization prefix).
FAMILIES = [
    ('openai', r'(gpt|chatgpt|o\d|codex|dall-e|whisper|tts-\d|text-embedding|davinci|babbage)(\b|[-.\d])'),
    ('openai', r'.*\bluna\b'),
    ('anthropic', r'claude'),
    ('google', r'(gemini|gemma|palm|imagen|veo)'),
    ('meta', r'llama'),
    ('mistral', r'(mistral|mixtral|codestral|devstral|magistral|ministral|pixtral|voxtral)'),
    ('deepseek', r'deepseek'),
    ('qwen', r'(qwen|qwq|qvq)'),
    ('zai', r'(glm|chatglm|codegeex)'),
    ('moonshot', r'(kimi|moonshot)'),
    ('xai', r'grok'),
    ('minimax', r'(minimax|abab)'),
    ('cohere', r'(command-[ra]|command-light|c4ai|aya)'),
    ('microsoft', r'(phi-?\d|wizardlm)'),
    ('nvidia', r'.*nemotron'),
    ('amazon', r'(nova-|titan)'),
    ('perplexity', r'(sonar|pplx)'),
    ('bytedance', r'(doubao|seed-|seedream|seededit)'),
    ('baidu', r'ernie'),
    ('tencent', r'hunyuan'),
    ('01ai', r'yi-'),
    ('stepfun', r'step-?\d'),
    ('ibm', r'granite'),
    ('ai21', r'jamba'),
    ('liquid', r'lfm'),
    ('nousresearch', r'(hermes|nous)'),
]
_FAMILY_PATTERNS = [(lab, re.compile(pattern)) for lab, pattern in FAMILIES]


def _normalize(value) -> str:
    return re.sub(r'[\s_]+', '-', str(value or '').strip().lower())


def detect_lab_from_text(text: str) -> str | None:
    """The lab an id or display name points to, or None."""
    text = _normalize(text)
    if not text:
        return None

    if '/' in text:
        organization, _, text = text.rpartition('/')
        # The segment before the slash may itself carry a connection prefix ("openrouter.z-ai").
        if lab := ORGANIZATIONS.get(organization.rsplit('/', 1)[-1].rsplit('.', 1)[-1]):
            return lab

    # Bedrock-style "vendor.model", or a connection prefix ("work.claude-...").
    head, _, tail = text.partition('.')
    if tail and (lab := ORGANIZATIONS.get(head)):
        return lab

    for candidate in (text, tail):
        for lab, pattern in _FAMILY_PATTERNS:
            if candidate and pattern.match(candidate):
                return lab
    return None


def _detect(model: dict, models_by_id: dict, seen: set) -> str | None:
    seen.add(model.get('id'))
    raw = model.get('openai') if isinstance(model.get('openai'), dict) else {}
    info = model.get('info') if isinstance(model.get('info'), dict) else {}

    for text in (model.get('id'), raw.get('id'), model.get('name')):
        if lab := detect_lab_from_text(text):
            return lab

    base_model_id = info.get('base_model_id')
    if base_model_id:
        base_model = models_by_id.get(base_model_id)
        if base_model and base_model.get('id') not in seen:
            if lab := _detect(base_model, models_by_id, seen):
                return lab
        elif lab := detect_lab_from_text(base_model_id):
            return lab

    return None


def detect_lab(model: dict, models_by_id: dict | None = None) -> str | None:
    return _detect(model, models_by_id or {}, set())


def apply_model_labs(models: list[dict]) -> None:
    """Set or clear ``lab`` on each model dict, in place."""
    models_by_id = {model.get('id'): model for model in models}
    for model in models:
        lab = detect_lab(model, models_by_id)
        if lab:
            model['lab'] = {'id': lab, 'name': LABS[lab][0]}
        else:
            model.pop('lab', None)


@cache
def lab_logo(lab_id: str | None) -> bytes | None:
    """The bundled logo (SVG) for a lab, or None."""
    if lab_id not in LABS:
        return None
    path = LOGO_DIR / f'{lab_id}.svg'
    return path.read_bytes() if path.is_file() else None
