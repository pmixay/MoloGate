"""ask_user: up to five questions with up to five options each, and no timeout."""

import json
import unittest

from open_webui.utils.ask_user import (
    MAX_OPTIONS,
    MAX_QUESTIONS,
    get_ask_user_questions_schema,
    normalize_ask_user_request,
    stage_ask_user_tool_calls,
)


def question(index: int, options: int = 2) -> dict:
    return {
        'id': f'q{index}',
        'header': f'Question {index}',
        'question': f'Which one for question {index}?',
        'options': [{'label': f'Option {n}', 'description': f'Picks option {n}.'} for n in range(options)],
    }


def tool_call(arguments: dict, name: str = 'ask_user', call_id: str = 'call_1') -> dict:
    return {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}


class NormalizeTest(unittest.TestCase):
    def test_five_questions_with_five_options(self):
        request = normalize_ask_user_request({'questions': [question(n, options=5) for n in range(5)]})

        self.assertEqual(len(request['questions']), 5)
        self.assertTrue(all(len(item['options']) == 5 for item in request['questions']))

    def test_more_than_five_questions_is_refused(self):
        with self.assertRaisesRegex(ValueError, '1-5 questions'):
            normalize_ask_user_request({'questions': [question(n) for n in range(6)]})

    def test_more_than_five_options_is_refused(self):
        with self.assertRaisesRegex(ValueError, '2-5 options'):
            normalize_ask_user_request({'questions': [question(0, options=6)]})

    def test_one_option_is_refused(self):
        with self.assertRaisesRegex(ValueError, '2-5 options'):
            normalize_ask_user_request({'questions': [question(0, options=1)]})

    def test_no_timeout_is_passed_on(self):
        request = normalize_ask_user_request({'questions': [question(0)], 'timeout_ms': 60_000})

        self.assertNotIn('timeout_ms', request)

    def test_duplicate_ids_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate question id'):
            normalize_ask_user_request({'questions': [question(0), question(0)]})


class SchemaTest(unittest.TestCase):
    def test_schema_states_the_limits(self):
        schema = get_ask_user_questions_schema()

        self.assertEqual((schema['minItems'], schema['maxItems']), (1, MAX_QUESTIONS))
        options = schema['items']['properties']['options']
        self.assertEqual((options['minItems'], options['maxItems']), (2, MAX_OPTIONS))


class StageTest(unittest.TestCase):
    def make_id(self, prefix: str) -> str:
        return f'{prefix}_generated'

    def test_valid_call_waits_for_the_answer(self):
        output = []
        staged, error = stage_ask_user_tool_calls(
            [tool_call({'questions': [question(n, options=5) for n in range(5)]})], output, self.make_id
        )

        self.assertTrue(staged)
        self.assertIsNone(error)
        self.assertEqual([item['type'] for item in output], ['function_call'])
        self.assertEqual(output[0]['status'], 'pending')
        self.assertNotIn('timeout_ms', json.loads(output[0]['arguments']))

    def test_invalid_call_gets_an_error_result(self):
        output = []
        staged, error = stage_ask_user_tool_calls(
            [tool_call({'questions': [question(n) for n in range(6)]})], output, self.make_id
        )

        self.assertTrue(staged)
        self.assertIn('1-5 questions', error)
        self.assertEqual([item['type'] for item in output], ['function_call', 'function_call_output'])

    def test_ask_user_must_be_the_only_call(self):
        output = []
        _, error = stage_ask_user_tool_calls(
            [tool_call({'questions': [question(0)]}), tool_call({}, name='fetch_url', call_id='call_2')],
            output,
            self.make_id,
        )

        self.assertIn('only tool call', error)


if __name__ == '__main__':
    unittest.main()
