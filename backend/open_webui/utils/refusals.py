"""Mologate: tell when a model declined a request, so the chat can flag it (docs/refusals.md).

Two kinds of evidence, checked once the whole response has finished:

- The provider says so: OpenAI's `refusal` field, Responses refusal parts, a `content_filter` or
  safety finish, or a moderation error (`utils/provider_errors.py` already sorts those out).
- The answer says so: it opens by declining ("I can't help with that", "Извините, но я не могу
  помочь", "抱歉，我无法提供…") in one of a dozen languages. Only the opening of the final answer
  counts; code, quotes and reasoning are ignored, and a limit of the model's tools ("I can't browse
  the web", "I can't open the file because it is missing") is not a refusal.

The result is stored on the message as `refusal` and shown as a warning under the answer.
"""

import os
import re

from open_webui.utils.provider_errors import clean_message, error_kind, provider_error

ENABLED = os.environ.get('MOLOGATE_REFUSAL_DETECTION', 'true').lower() not in ('0', 'false', 'no', 'off')

# Only the first sentences decide: a model that declines says so up front.
OPENING_CHARS = 600
OPENING_SENTENCES = 4
# More than this written after the declining sentence counts as help given anyway.
PARTIAL_CHARS = 700
QUOTE_CHARS = 200

FILTER_FINISHES = frozenset(
    {
        'content_filter',
        'safety',
        'prohibited_content',
        'blocklist',
        'spii',
        'recitation',
        'image_safety',
        'image_prohibited_content',
    }
)
REFUSAL_FINISHES = frozenset({'refusal'})
FILTER_CODES = FILTER_FINISHES | {'content_policy_violation', 'moderation_blocked', 'flagged', '1301'}
FILTER_WORDS = re.compile(r'(?i)filter|moderation|flagged|safety system')

_I = r'\bi\s*(?:(?:really|simply|just|honestly|unfortunately|still|definitely|absolutely|truly)\s+)?'
_CANT = (
    r"(?:can(?:no|')?t|cannot|can not|can no longer|won'?t be able to|will not be able to|won'?t|will not"
    r"|(?:'m|am) (?:unable|not able|not in a position|not allowed|not permitted) to)"
    r"(?:,?\s+and\s+(?:will not|won'?t|cannot|can'?t|should not|shouldn'?t))?"
)
_OBJECT = (
    r'(?:that|this|those|these|such|any|anything|it|content|instructions?|information|guidance|details|advice'
    r'|assistance|help|a guide|a tutorial|steps|step-by-step|directions|explicit|sexual|graphic|hateful|harmful'
    r'|malicious|dangerous|illegal|copyrighted|the (?:full |complete |entire )?(?:lyrics|text|instructions|steps'
    r'|details|content|recipe|code for))'
)

# Phrases that decline a request, by language. Matched case-insensitively on normalized text.
PATTERNS = {
    'en': [
        rf'{_I}{_CANT}\s+(?:help|assist|support)\s+(?:you\s+)?(?:with|in|on)\s+(?:that|this|it|your request'
        r'|such|anything like|requests? (?:like|of) (?:that|this)|\w+ing)\b',
        rf'{_I}{_CANT}\s+(?:help|assist)\s+(?:you\s+)?(?:here|there)\b',
        rf'{_I}{_CANT}\s+help\s+you\s+(?:to\s+)?(?!(?:right|now|today|further|more|anymore|yet|any)\b)\w+',
        rf'{_I}{_CANT}\s+(?:provide|give|share|offer|supply|recommend|describe|explain|detail|outline|list'
        rf'|engage in|participate in|assist in)\s+(?:you\s+)?(?:with\s+)?{_OBJECT}',
        rf'{_I}{_CANT}\s+(?:write|create|generate|produce|draft|compose)\s+(?:you\s+)?(?!(?:to|into|in|on|at)\b)\w+',
        rf'{_I}{_CANT}\s+(?:comply|fulfil+|honou?r|accommodate|go along|do (?:that|this|so))\b',
        rf"{_I}(?:won'?t|will not|refuse to|'m not going to|am not going to)\s+(?:help|assist|provide|write|create"
        r'|generate|produce|share|give|engage|participate|do (?:that|this|so))\b',
        r"\bi(?:'m| am| would be| feel)\s+(?:not |un)comfortable\s+(?:with\s+)?(?:help|assist|provid|creat|writ"
        r'|generat|produc|shar|engag|doing|making|that|this)',
        r"\bi\s*(?:must|have to|need to|'ll have to|will have to|'m going to|am going to|'d have to|would have to"
        r"|'ll|will)\s+(?:respectfully\s+|politely\s+)?decline\b",
        r'\bi\s+(?:respectfully\s+|politely\s+)?decline\s+to\b',
        r"\b(?:that|this|it)(?:'s| is)\s+not something i(?:'m| am)?\s*(?:able to|going to|can|will|would|could)"
        r'\s+(?:help|assist|do|provide|write|create|engage)',
        r'\b(?:against|violates?|violating|breach(?:es)?|goes against|conflicts? with)\s+(?:my|our)\s+(?:\w+\s+){0,2}'
        r'(?:guidelines|polic(?:y|ies)|principles|values|programming|rules)\b',
        r"\b(?:violates?|against|breach(?:es)?|not allowed (?:by|under))\s+(?:the\s+|our\s+|my\s+|openai'?s\s+"
        r"|anthropic'?s\s+|google'?s\s+)?(?:content|usage|safety|acceptable[- ]use)\s+polic(?:y|ies)\b",
        r'\bas an? (?:ai|artificial intelligence|(?:ai |large )?language model|ai assistant)\b[^.!?\n]{0,40}?'
        rf'{_I}{_CANT}',
    ],
    'ru': [
        r'\bне\s+(?:могу|смогу|буду|стану|имею права|вправе)\s+(?:вам\s+|тебе\s+)?(?:в\s+этом\s+|с\s+этим\s+)?'
        r'(?:помочь|помогать|содействовать|предоставить|предоставлять|дать|давать|создать|создавать|написать|писать'
        r'|сгенерировать|генерировать|участвовать|поддержать|рассказать|описать|объяснить|раскрыть'
        r'|выполнить (?:этот|эту|такой|такую|данный|данную|ваш|вашу|твой|твою)?\s*(?:запрос|просьбу|задание))\b',
        r'\b(?:вынужден[аы]?|должен|должна)\s+(?:вам\s+|тебе\s+)?отказать\b',
        r'\b(?:вынужден[аы]?|должен|должна)\s+отказаться\s+(?:от\s+)?(?:выполнения|ответа|помощи|этого запроса'
        r'|этой просьбы|этого)\b',
        r'\bотказываюсь\s+(?:от\s+(?:этого|выполнения|ответа)|помогать|отвечать|писать|выполнять|создавать)\b',
        r'\b(?:противоречит|нарушает|нарушило бы|противоречило бы)\s+(?:моим|моей|моему|правилам|политике|принципам'
        r'|этическим|условиям)',
        r'\bкак (?:ии|искусственный интеллект|языковая модель|ассистент)\b[^.!?\n]{0,40}\bне (?:могу|буду)\b',
    ],
    'uk': [
        r'\bне\s+(?:можу|зможу|буду|стану)\s+(?:вам\s+|тобі\s+)?(?:з\s+цим\s+)?(?:допомогти|допомагати|надати'
        r'|створити|написати|згенерувати|виконати (?:цей|цю|такий|таке)?\s*(?:запит|прохання))\b',
        r'\bзмушен(?:ий|а|і)\s+відмов',
    ],
    'es': [
        r'\bno\s+(?:puedo|podr[ée]|voy a|me es posible)\s+(?:ayudar(?:te|le|les)?|asistir(?:te|le)?'
        r'|proporcionar(?:te|le)?|darte|darle|crear|escribir|generar|cumplir|participar|ofrecer(?:te|le)?)\b',
        r'\b(?:debo|tengo que)\s+(?:rechazar|declinar)\b',
    ],
    'pt': [
        r'\bn[ãa]o\s+(?:posso|poderei|vou|consigo)\s+(?:te\s+|lhe\s+)?(?:ajudar|auxiliar|fornecer|dar|criar|escrever'
        r'|gerar|cumprir|atender|participar)\b',
        r'\b(?:devo|preciso|tenho que)\s+(?:recusar|declinar)\b',
    ],
    'fr': [
        r"\bje\s+ne\s+(?:peux|pourrai|vais|saurais|suis)\s+pas\s+(?:en mesure de\s+)?(?:vous\s+|t'|te\s+)?"
        r'(?:aider|assister|fournir|donner|cr[ée]er|[ée]crire|g[ée]n[ée]rer|participer'
        r'|r[ée]pondre (?:à|a) (?:cette|ce|votre|ta) (?:demande|requête))',
        r'\bje\s+dois\s+(?:respectueusement\s+|poliment\s+)?(?:refuser|d[ée]cliner)\b',
    ],
    'de': [
        r'\bich\s+kann\s+(?:ihnen|dir|euch)?\s*(?:dabei|damit|hierbei|hiermit|darin)?\s*(?:leider\s+)?nicht\s+'
        r'(?:helfen|unterst[üu]tzen|behilflich sein|nachkommen)',
        r'\bich\s+kann\s+(?:ihnen|dir|euch)?\s*(?:leider\s+)?(?:keine|keinen|kein)\s+(?:anleitung|hilfe|unterst[üu]tzung'
        r'|informationen|inhalte|anweisungen)\b',
        r'\bich\s+muss\s+(?:das|dies|diese anfrage|ihre anfrage|deine anfrage)?\s*(?:leider\s+)?ablehnen\b',
        r'\bich\s+werde\s+(?:ihnen|dir)?\s*(?:dabei|damit)?\s*nicht\s+helfen\b',
    ],
    'it': [
        r'\bnon\s+(?:posso|potr[òo]|sono in grado di)\s+(?:aiutarti|aiutarla|aiutare|assisterti|fornire|fornirti|creare'
        r'|scrivere|generare)\b',
        r'\bdevo\s+(?:rifiutare|declinare)\b',
    ],
    'tr': [
        r'\b(?:yard[ıi]mc[ıi]\s+olamam|yard[ıi]mc[ıi]\s+olam[ıi]yorum|sa[ğg]layamam|sa[ğg]layam[ıi]yorum'
        r'|reddetmek zorunday[ıi]m)',
    ],
    'zh': [
        r'(?:抱歉|对不起|很遗憾|不好意思)[，,]?\s*(?:但是?)?\s*我(?:无法|不能|没法|不会|不可以)',
        r'我(?:无法|不能|不会|不可以)(?:为你|为您|帮你|帮您|协助|帮助|提供|满足|生成|创作|编写|回答|参与)',
        r'(?:无法|不能)(?:满足|协助|帮助)(?:你|您)?的?(?:这个|此|该|这类|这种)?(?:请求|要求)',
        r'违反(?:了)?(?:我的|相关的?|内容|使用)?(?:政策|准则|规定|原则)',
    ],
    'ja': [
        r'(?:申し訳(?:ありません|ございません)が|すみませんが)[^。\n]{0,40}(?:でき(?:ません|かねます)|いたしかねます)',
        r'(?:お手伝い|お応え|ご協力|ご要望にお応え)(?:は|することは)?(?:でき(?:ません|かねます)|いたしかねます)',
    ],
    'ko': [
        r'(?:도와\s*드릴|도움을\s*드릴|제공해?\s*드릴|제공할|작성해?\s*드릴|작성할|응해?\s*드릴|응할)\s*수\s*없(?:습니다|어요|어)',
    ],
}

# A limit of the model's reach, not a decision to decline: "I can't access the internet",
# "I can't help with that until you share the logs", "抱歉，我无法访问实时数据".
CAPABILITY = re.compile(
    r'\b(?:access (?:to )?(?:the internet|the web|real[- ]?time|external|links?|urls?|websites?|attachments?|images?'
    r'|files?|the file|that (?:file|link|url|page|site))|browse|browsing|go online|connect to the internet'
    r'|search (?:the )?(?:web|internet|online)|real[- ]?time|up[- ]to[- ]date|live data'
    r'|current (?:data|information|events|prices|weather|news)|see (?:the |your )?(?:image|photo|picture|screen'
    r'|attachment|file)s?|view (?:the |your )?(?:image|file|attachment|link)s?|open (?:the |your )?(?:link|url|file'
    r'|attachment|website|page)s?|without (?:more|additional|further|the|a|an|access|seeing|knowing|being able)'
    r'|unless|until|if you (?:can |could )?(?:share|provide|send|give|tell|upload|paste|clarify)'
    r'|because (?:the (?:file|command|tool|server|api|request|test|build|container|network|page|site|url|link|repo'
    r"|repository|service|endpoint|database|connection)|i (?:don'?t|do not|can'?t|cannot) (?:have|see|access|find"
    r"|reach|open|read)|there (?:is|are|was|were) no|it(?:'s| is) (?:not (?:installed|available|found|supported)"
    r'|missing|unavailable|offline|down|empty|private))|not (?:installed|available|found|supported)|permission denied'
    r"|read-only|doesn'?t exist|does not exist|timed? ?out|my (?:knowledge|training) (?:cutoff|cut-off|data)"
    r'|disk|permissions?|denied|quota exceeded|no space|is full|failed|crash(?:ed|es)?|exception|traceback'
    r'|(?:generate|create|draw|make|produce) (?:an? )?(?:images?|pictures?|photos?|audio|videos?|music|diagrams?)'
    r'|нет доступа|доступа к|в реальном времени|без (?:дополнительн|доступ|файл|информац|подробност|уточнени)'
    r'|пока (?:вы|ты) не|если (?:вы|ты) не|sin (?:ver|más|acceso|saber|conocer|el|la|los|las)|sem (?:mais|acesso|ver)'
    r'|sans (?:plus|accès|voir)|ohne (?:weitere|zugriff|mehr))',
    re.IGNORECASE,
)
CAPABILITY_CJK = re.compile(
    r'访问|浏览|联网|互联网|实时|查看|打开|读取|找到|运行|执行|インターネット|リアルタイム|閲覧|開く|開け|見る|アクセス|検索|実行'
    r'|실시간|인터넷|접근|열 수|찾을 수'
)

_COMPILED = [(lang, re.compile(pattern, re.IGNORECASE)) for lang, patterns in PATTERNS.items() for pattern in patterns]

_FENCE = re.compile(r'```.*?(?:```|$)|~~~.*?(?:~~~|$)', re.DOTALL)
_BLOCKS = re.compile(r'<(think|thinking|reasoning|details)\b[^>]*>.*?(?:</\1>|$)', re.DOTALL | re.IGNORECASE)
_INLINE_CODE = re.compile(r'`[^`\n]*`')
_QUOTE_LINE = re.compile(r'^\s*>.*$', re.MULTILINE)
_MARKUP = re.compile(r'[*_#]+|^\s*(?:[-+]|\d+[.)])\s+', re.MULTILINE)
_SENTENCE_END = re.compile(r'(?<=[.!?。！？])\s+|(?<=[。！？])|\n+')
QUOTES = '"“”«»„'
_SINGLE_OPEN = re.compile(r"(?:^|[\s:(\[])'\s*$")


def _normalize(text: str) -> str:
    text = text.replace('’', "'").replace('‘', "'").replace('ʼ', "'").replace('ё', 'е').replace('Ё', 'Е')
    text = _BLOCKS.sub(' ', text)
    text = _FENCE.sub(' ', text)
    text = _QUOTE_LINE.sub(' ', text)
    text = _INLINE_CODE.sub(' ', text)
    return _MARKUP.sub('', text)


def _sentences(text: str) -> list[tuple[int, int]]:
    """Spans of the sentences in `text`, in order."""
    spans, start = [], 0
    for match in _SENTENCE_END.finditer(text):
        if match.start() > start:
            spans.append((start, match.start()))
        start = match.end()
    if start < len(text):
        spans.append((start, len(text)))
    return [(a, b) for a, b in spans if text[a:b].strip()]


def _inside_quotes(sentence: str, offset: int) -> bool:
    """Whether the phrase is quoted rather than said: inside double quotes, or opening single ones."""
    before = sentence[:offset]
    return sum(before.count(mark) for mark in QUOTES) % 2 == 1 or bool(_SINGLE_OPEN.search(before))


def find_declining(text: str) -> dict | None:
    """The sentence where the answer declines, with whether more help followed; None if it doesn't."""
    if not isinstance(text, str) or not text.strip():
        return None
    clean = _normalize(text)
    spans = _sentences(clean)
    opening = [span for span in spans[:OPENING_SENTENCES] if span[0] < OPENING_CHARS]
    for start, end in opening:
        sentence = clean[start:end]
        for lang, pattern in _COMPILED:
            match = pattern.search(sentence)
            if not match or _inside_quotes(sentence, match.start()):
                continue
            if CAPABILITY.search(sentence) or CAPABILITY_CJK.search(sentence):
                break
            rest = clean[end:].strip()
            return {
                'quote': _quote(sentence),
                'language': lang,
                'partial': len(rest) > PARTIAL_CHARS,
            }
    return None


def _quote(sentence: str) -> str | None:
    text = clean_message(sentence.strip())
    if text and len(text) > QUOTE_CHARS:
        text = text[: QUOTE_CHARS - 1].rstrip() + '…'
    return text


def final_text(output) -> str:
    """The text of the last answer in a response's output (after any tool calls)."""
    for item in reversed(output if isinstance(output, list) else []):
        if not isinstance(item, dict) or item.get('type') != 'message':
            continue
        text = ''.join(
            str(part.get('text') or part.get('refusal') or '')
            for part in item.get('content') or []
            if isinstance(part, dict)
        )
        if text.strip():
            return text
    return ''


def signal_from_finish(finish_reason) -> str | None:
    """'refusal' or 'filter' for a finish reason that means the provider stopped the answer."""
    reason = str(finish_reason or '').lower()
    if reason in REFUSAL_FINISHES:
        return 'refusal'
    if reason in FILTER_FINISHES:
        return 'filter'
    return None


def signal_from_error(error) -> str | None:
    """'refusal' or 'filter' for a provider error (or our own stream note) that means the model declined."""
    if isinstance(error, str):
        error = {'message': error}
    if not isinstance(error, dict):
        return None
    kind = str(error.get('type') or '').lower()
    message = error.get('message') if isinstance(error.get('message'), str) else ''
    if kind == 'content_filter':
        return 'filter'
    if kind == 'refusal' or message.startswith('The model declined to answer') or error_kind(None, error) == 'refusal':
        codes = {str(error.get(key)).lower() for key in ('code', 'type') if error.get(key) is not None}
        filtered = codes & FILTER_CODES or FILTER_WORDS.search(message)
        return 'filter' if filtered else 'refusal'
    return None


def chat_choice_signal(choice: dict, key: str = 'delta', chunk: dict | None = None) -> str | None:
    """Move a Chat Completions refusal into the answer's text (the chat shows `content` only) and
    return 'refusal' or 'filter' when the choice says the model declined. `chunk` is the chunk or
    reply holding the choice: provider accounts mark refusals they already turned into text."""
    marked = isinstance(chunk, dict) and isinstance(chunk.get('mologate'), dict) and chunk['mologate'].get('refusal')
    signal = 'refusal' if marked else None
    part = choice.get(key)
    if isinstance(part, dict) and isinstance(part.get('refusal'), str) and part['refusal']:
        content = part.get('content') if isinstance(part.get('content'), str) else ''
        refusal = part.pop('refusal')
        # Some replies already carry the words as content too.
        part['content'] = content if refusal in content else content + refusal
        signal = 'refusal'
    return (
        signal
        or signal_from_finish(choice.get('native_finish_reason'))
        or signal_from_finish(choice.get('finish_reason'))
    )


def _parts_to_text(parts) -> bool:
    """Turn Responses refusal parts into text parts, in place; whether there were any."""
    found = False
    for part in parts if isinstance(parts, list) else []:
        if isinstance(part, dict) and part.get('type') == 'refusal':
            part['type'] = 'output_text'
            part['text'] = part.pop('refusal', '') or ''
            part.setdefault('annotations', [])
            found = True
    return found


def _items_to_text(items) -> bool:
    found = False
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get('type') == 'message':
            found = _parts_to_text(item.get('content')) or found
    return found


def responses_event_signal(event: dict) -> str | None:
    """Rewrite a Responses stream event so a refusal reads as the answer's text (the chat renders
    text parts only), and return 'refusal' or 'filter' when the event says the model declined."""
    kind = event.get('type')
    if kind == 'response.refusal.delta':
        event['type'] = 'response.output_text.delta'
        return 'refusal'
    if kind == 'response.refusal.done':
        event['type'] = 'response.output_text.done'
        event['text'] = event.pop('refusal', '') or ''
        return 'refusal'
    if kind in ('response.content_part.added', 'response.content_part.done'):
        return 'refusal' if _parts_to_text([event.get('part')]) else None
    if kind in ('response.output_item.added', 'response.output_item.done'):
        return 'refusal' if _items_to_text([event.get('item')]) else None
    if kind in ('response.completed', 'response.incomplete'):
        result = event.get('response') if isinstance(event.get('response'), dict) else {}
        refused = _items_to_text(result.get('output'))
        reason = (result.get('incomplete_details') or {}).get('reason') if kind == 'response.incomplete' else None
        return 'refusal' if refused else signal_from_finish(reason)
    if kind == 'response.failed':
        return signal_from_error(provider_error(event))
    return None


def detect_error(error) -> dict | None:
    """The refusal record for a request that failed outright, when the failure was a refusal."""
    if isinstance(error, dict) and isinstance(error.get('detail'), str) and not error.get('message'):
        error = {**error, 'message': error['detail']}
    signal = signal_from_error(error)
    return detect(signals={signal}, text='') if signal else None


def detect(output=None, signals=(), text: str | None = None) -> dict | None:
    """What the finished response shows about a refusal, for the message; None when it shows none.

    `signals` are 'refusal' or 'filter' as the provider reported them during the response; `text`
    stands in for the output when only the answer's text is known.
    """
    if not ENABLED:
        return None
    signals = {signal for signal in signals or () if signal}
    answer = text if text is not None else final_text(output)
    found = find_declining(answer)
    if signals:
        kind = 'refusal' if 'refusal' in signals else 'filter'
        result = {'source': 'provider', 'kind': kind, 'partial': bool(answer.strip()) and kind == 'filter'}
        if found:
            result.update(quote=found['quote'], partial=found['partial'])
        elif kind == 'refusal' and answer.strip():
            sentence = next((answer[a:b] for a, b in _sentences(_normalize(answer))), '')
            result['quote'] = _quote(sentence) if sentence else None
        return result
    if found:
        return {'source': 'wording', 'kind': 'refusal', 'partial': found['partial'], 'quote': found['quote']}
    return None
