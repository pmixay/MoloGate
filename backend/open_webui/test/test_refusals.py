"""Refusal detection: what counts as a model declining, and what only looks like it."""

import unittest
import unittest.mock

from open_webui.utils import refusals

DECLINES = [
    "I'm sorry, but I can't help with that.",
    "I can't assist with creating malware or anything designed to harm systems.",
    'Sorry, I can’t help with that request.',
    'I cannot provide instructions for making weapons.',
    "I won't write that. It would target a real person.",
    "I'm not able to help with that request.",
    "I'm unable to provide that kind of content.",
    "I'm not comfortable writing explicit content about real people.",
    'I must respectfully decline this request.',
    "That's not something I can help with.",
    'Creating that would go against my guidelines, so I have to pass.',
    'Writing this would violate the content policy.',
    "As an AI developed by OpenAI, I can't help with that.",
    "I really can't help you hack into someone else's account.",
    "I can't fulfill this request.",
    "I'll have to decline writing that.",
    'I cannot and will not provide instructions for synthesizing illegal drugs.',
    "I can't write a phishing email, but I can help you plan security awareness training.",
    'Извините, но я не могу помочь с этим.',
    'К сожалению, я не могу предоставить такую информацию.',
    'Я не буду писать такой текст.',
    'Вынужден отказать в этой просьбе.',
    'Это противоречит моим принципам, поэтому я не стану этого делать.',
    'Вибачте, але я не можу допомогти з цим.',
    'Lo siento, pero no puedo ayudarte con eso.',
    'Desculpe, mas não posso ajudar com isso.',
    'Je suis désolé, mais je ne peux pas vous aider avec cette demande.',
    'Es tut mir leid, aber dabei kann ich dir nicht helfen. Ich kann dir dabei leider nicht helfen.',
    'Mi dispiace, ma non posso aiutarti con questa richiesta.',
    '抱歉，我无法帮助你完成这个请求。',
    '我不能提供这类内容。',
    '申し訳ありませんが、そのご依頼にはお応えできません。',
    '죄송하지만 그 요청은 도와드릴 수 없습니다.',
    'Üzgünüm, bu konuda yardımcı olamam.',
]

NOT_DECLINES = [
    # Ordinary answers.
    'Sure! Here is a Python function that reverses a string.',
    'The capital of France is Paris.',
    # Limits of reach, not refusals.
    "I can't access the internet, but based on my training data the answer is 42.",
    "I'm sorry, but I can't browse the web. Here is what I know about the topic.",
    "I'm unable to provide real-time stock prices.",
    "I can't help with that until you share the error log.",
    "I can't help with debugging without seeing the code.",
    "I can't open the file because the file is missing.",
    'Извините, у меня нет доступа к интернету, поэтому я не могу помочь с актуальными курсами.',
    '抱歉，我无法访问实时数据。',
    'No puedo ayudarte sin ver el código.',
    # Agent reports.
    'I ran the tests: 3 failed. The fixture cannot find the database.',
    "The build failed because npm isn't installed in the container. I installed it and reran the build.",
    # Declining words only in code, quotes or later in a long answer.
    'Here is the message box:\n\n```js\nalert("I can\'t help with that");\n```',
    'The bot should reply: "I can\'t help with that." when asked off-topic questions.',
    "Here's how to handle errors when the API says 'I can't help with that'.",
    '> I cannot provide that\n\nThis quote is from the old prompt; the new prompt fixes it.',
    'Answer:\n\n' + 'Lots of useful detail. ' * 60 + "\n\nI can't help with that last part, though.",
    "I can't make this faster than O(n log n) because it's a comparison sort.",
    "I can't create the branch: the repository is read-only.",
    "I can't generate images, but here's a detailed description you can use.",
    "I can't help but notice the loop never terminates.",
    'Не могу запустить тесты: pytest не установлен.',
    # Talking about refusals.
    'Models sometimes refuse borderline prompts; rephrasing usually helps.',
    "The style guide says this violates the project's naming rules.",
]


class FindDecliningTests(unittest.TestCase):
    def test_declines_are_found(self):
        missed = [text for text in DECLINES if not refusals.find_declining(text)]
        self.assertEqual(missed, [])

    def test_look_alikes_are_not(self):
        flagged = [text for text in NOT_DECLINES if refusals.find_declining(text)]
        self.assertEqual(flagged, [])

    def test_quote_and_partial(self):
        found = refusals.find_declining("I can't help with that. If you're curious about security, try a CTF.")
        self.assertEqual(found['quote'], "I can't help with that.")
        self.assertFalse(found['partial'])
        long = "I can't write the exploit itself. However, here is how the vulnerability works:\n\n" + 'x ' * 600
        self.assertTrue(refusals.find_declining(long)['partial'])

    def test_reasoning_is_ignored(self):
        text = "<think>I can't help with that? No, this is fine.</think>Sure, here's the recipe."
        self.assertIsNone(refusals.find_declining(text))


class DetectTests(unittest.TestCase):
    def output(self, *texts, tool=False):
        items = [{'type': 'reasoning', 'content': [{'type': 'output_text', 'text': "I can't help with that."}]}]
        for text in texts:
            items.append({'type': 'message', 'content': [{'type': 'output_text', 'text': text}]})
            if tool:
                items.append({'type': 'function_call', 'name': 'run', 'arguments': '{}'})
        return items

    def test_wording(self):
        result = refusals.detect(self.output("I'm sorry, but I can't help with that."))
        self.assertEqual(result['source'], 'wording')
        self.assertEqual(result['kind'], 'refusal')
        self.assertEqual(result['quote'], "I'm sorry, but I can't help with that.")

    def test_final_answer_decides(self):
        output = self.output("I can't help with that without the file.", 'Done: the report is in out.md.', tool=True)
        self.assertIsNone(refusals.detect(output))
        self.assertIsNone(refusals.detect(self.output('Here you go.')))

    def test_provider_signals(self):
        result = refusals.detect(self.output('I cannot comply.'), {'refusal'})
        self.assertEqual((result['source'], result['kind']), ('provider', 'refusal'))
        self.assertEqual(result['quote'], 'I cannot comply.')
        result = refusals.detect([], {'filter'})
        self.assertEqual((result['kind'], result['partial']), ('filter', False))
        result = refusals.detect(self.output('Once upon a time'), {'filter'})
        self.assertTrue(result['partial'])
        # The model's own words in a refusal field, with no matching phrase.
        result = refusals.detect(text='Nope. Not doing it.', signals={'refusal'})
        self.assertEqual(result['quote'], 'Nope.')

    def test_refusal_parts_count_as_text(self):
        output = [{'type': 'message', 'content': [{'type': 'refusal', 'refusal': "I can't help with that."}]}]
        self.assertEqual(refusals.final_text(output), "I can't help with that.")

    def test_signals(self):
        self.assertEqual(refusals.signal_from_finish('content_filter'), 'filter')
        self.assertEqual(refusals.signal_from_finish('SAFETY'), 'filter')
        self.assertEqual(refusals.signal_from_finish('refusal'), 'refusal')
        self.assertIsNone(refusals.signal_from_finish('stop'))
        self.assertIsNone(refusals.signal_from_finish(None))
        self.assertEqual(refusals.signal_from_error({'type': 'content_filter', 'message': 'x'}), 'filter')
        self.assertEqual(refusals.signal_from_error({'type': 'refusal', 'message': 'The model declined.'}), 'refusal')
        self.assertEqual(
            refusals.signal_from_error("The model declined to answer: the provider's content filter stopped it."),
            'filter',
        )
        self.assertEqual(
            refusals.signal_from_error({'code': 'content_policy_violation', 'message': 'Blocked'}), 'filter'
        )
        self.assertEqual(refusals.signal_from_error({'message': 'Your request was flagged by moderation'}), 'filter')
        self.assertIsNone(refusals.signal_from_error({'type': 'length', 'message': 'cut off'}))
        self.assertIsNone(refusals.signal_from_error({'message': 'Rate limit exceeded'}))
        self.assertIsNone(refusals.signal_from_error(None))

    def test_chat_choice_moves_refusals_into_text(self):
        choice = {'delta': {'refusal': "I can't help with that."}, 'finish_reason': None}
        self.assertEqual(refusals.chat_choice_signal(choice), 'refusal')
        self.assertEqual(choice['delta'], {'content': "I can't help with that."})
        # A reply that already carries the words as content keeps them once.
        choice = {'message': {'content': 'No.', 'refusal': 'No.'}, 'finish_reason': 'stop'}
        self.assertEqual(refusals.chat_choice_signal(choice, 'message'), 'refusal')
        self.assertEqual(choice['message'], {'content': 'No.'})
        # Provider accounts mark refusals they already turned into text.
        choice = {'delta': {'content': 'No.'}}
        self.assertEqual(refusals.chat_choice_signal(choice, chunk={'mologate': {'refusal': True}}), 'refusal')
        self.assertEqual(refusals.chat_choice_signal({'delta': {}, 'finish_reason': 'content_filter'}), 'filter')
        self.assertEqual(
            refusals.chat_choice_signal(
                {'delta': {}, 'finish_reason': 'content_filter', 'native_finish_reason': 'refusal'}
            ),
            'refusal',
        )
        self.assertIsNone(refusals.chat_choice_signal({'delta': {'content': 'Hi'}, 'finish_reason': 'stop'}))

    def test_responses_events_read_as_text(self):
        event = {'type': 'response.refusal.delta', 'delta': 'No.'}
        self.assertEqual(refusals.responses_event_signal(event), 'refusal')
        self.assertEqual(event['type'], 'response.output_text.delta')
        event = {'type': 'response.refusal.done', 'refusal': 'No.'}
        self.assertEqual(refusals.responses_event_signal(event), 'refusal')
        self.assertEqual((event['type'], event['text']), ('response.output_text.done', 'No.'))
        event = {'type': 'response.content_part.added', 'part': {'type': 'refusal', 'refusal': ''}}
        self.assertEqual(refusals.responses_event_signal(event), 'refusal')
        self.assertEqual(event['part'], {'type': 'output_text', 'text': '', 'annotations': []})
        item = {'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'No.'}]}
        event = {'type': 'response.completed', 'response': {'output': [item]}}
        self.assertEqual(refusals.responses_event_signal(event), 'refusal')
        self.assertEqual(item['content'][0]['text'], 'No.')
        event = {'type': 'response.incomplete', 'response': {'incomplete_details': {'reason': 'content_filter'}}}
        self.assertEqual(refusals.responses_event_signal(event), 'filter')
        event = {'type': 'response.incomplete', 'response': {'incomplete_details': {'reason': 'max_output_tokens'}}}
        self.assertIsNone(refusals.responses_event_signal(event))
        event = {'type': 'response.failed', 'response': {'error': {'code': 'content_filter', 'message': 'Blocked'}}}
        self.assertEqual(refusals.responses_event_signal(event), 'filter')
        self.assertIsNone(refusals.responses_event_signal({'type': 'response.output_text.delta', 'delta': 'Hi'}))

    def test_outright_failures(self):
        result = refusals.detect_error(
            "The model declined to answer: the provider's content filter stopped this response."
        )
        self.assertEqual((result['source'], result['kind']), ('provider', 'filter'))
        self.assertEqual(refusals.detect_error({'detail': 'Your input was flagged by moderation'})['kind'], 'filter')
        self.assertIsNone(refusals.detect_error('The provider could not answer (HTTP 500).'))
        self.assertIsNone(refusals.detect_error(None))

    def test_switch(self):
        with unittest.mock.patch.object(refusals, 'ENABLED', False):
            self.assertIsNone(refusals.detect(self.output("I can't help with that.")))


if __name__ == '__main__':
    unittest.main()
