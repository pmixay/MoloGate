"""Request replay regression tests, stdlib-only: actual source, no DB/provider startup."""

import ast
import asyncio
from copy import deepcopy
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
import unittest
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('tool_context', ROOT / 'utils/tool_context.py')
tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tools)


def rounds(count=12, size=20000):
    messages = [
        {'role': 'system', 'content': 'Never change instructions.'},
        {'role': 'user', 'content': 'Fix the test.'},
    ]
    for i in range(count):
        messages.extend(
            [
                {
                    'role': 'assistant',
                    'tool_calls': [
                        {
                            'id': str(i),
                            'function': {'name': 'run_command', 'arguments': json.dumps({'command': 'x' * size})},
                        }
                    ],
                },
                {'role': 'tool', 'tool_call_id': str(i), 'content': 'y' * size},
            ]
        )
    return messages


class ClearingTests(unittest.TestCase):
    def test_reduces_large_history_without_modifying_transcript(self):
        original = rounds(100)
        before = deepcopy(original)
        reduced = tools.clear_tool_context(original)
        self.assertEqual(original, before)
        self.assertLess(sum(len(m['content']) for m in reduced if m['role'] == 'tool'), 65000)
        self.assertEqual(reduced[:2], original[:2])
        self.assertEqual([m.get('tool_call_id') for m in reduced], [m.get('tool_call_id') for m in original])
        for m in reduced:
            for c in m.get('tool_calls', []):
                self.assertIsInstance(json.loads(c['function']['arguments']), dict)

    def test_pending_arguments_never_changed(self):
        pending = rounds(1)[2]
        self.assertEqual(tools.clear_tool_context([pending]), [pending])

    def test_recent_results_larger_than_old_results(self):
        reduced = tools.clear_tool_context(rounds())
        self.assertEqual(len(reduced[-1]['content']), 12000)
        self.assertEqual(len(reduced[3]['content']), 1000)

    def test_multimodal_text_budget_preserves_images(self):
        image = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,abc'}}
        message = {'role': 'tool', 'content': [{'type': 'text', 'text': 'x' * 20000}, image]}
        reduced = tools.clear_tool_context([message])[0]['content']
        self.assertEqual(reduced[-1], image)
        self.assertLessEqual(len(reduced[0]['text']), 12000)

    def test_safe_boundary_keeps_parallel_results_together(self):
        messages = rounds()
        cut = tools.safe_tool_boundary(messages)
        self.assertEqual(messages[cut]['role'], 'assistant')
        retained_ids = {c['id'] for m in messages[cut:] for c in m.get('tool_calls', [])}
        self.assertTrue(all(m['tool_call_id'] in retained_ids for m in messages[cut:] if m['role'] == 'tool'))

    def test_summarizer_sees_structured_tool_results(self):
        prepared = tools.summary_messages(
            [
                {
                    'role': 'assistant',
                    'content': '',
                    'output': [{'type': 'function_call_output', 'output': [{'text': 'FAILED TEST regression'}]}],
                }
            ]
        )
        self.assertIn('FAILED TEST regression', prepared[0]['content'])

    def test_empty_budget_never_expands_payload(self):
        self.assertEqual(tools.shorten('abcdef', 0), '')
        for limit in range(0, 300):
            self.assertLessEqual(len(tools.shorten('x' * 1000, limit)), limit)


class ContinuationTests(unittest.IsolatedAsyncioTestCase):
    def setup_function(self, enabled=True, threshold=100):
        tree = ast.parse((ROOT / 'utils/context_compaction.py').read_text())
        selected = [
            n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'prepare_tool_continuation'
        ]
        self.summary = AsyncMock(return_value='Changed app.py; failing test still needs fixing.')
        self.config = AsyncMock(
            return_value={
                'enable': enabled,
                'token_threshold': threshold,
                'token_cap': threshold,
                'prompt_template': '',
            }
        )
        # Imports for optional UI reporting are stubbed only during execution below.
        self.ns = {
            'Config': type('Config', (), {'get': AsyncMock(return_value=True)}),
            'clear_tool_context': tools.clear_tool_context,
            'safe_tool_boundary': tools.safe_tool_boundary,
            'JSONCodec': type('JSONCodec', (), {'dumps': staticmethod(json.dumps)}),
            'hashlib': hashlib,
            '_load_config': self.config,
            '_resolve_auto_threshold': AsyncMock(return_value=None),
            '_resolve_token_threshold': lambda a, b, c: min(a, b),
            '_estimate_messages_tokens': lambda m: len(json.dumps(m)) // 4,
            '_estimate_tokens': lambda v: len(json.dumps(v)) // 4,
            '_generate_summary': self.summary,
            'log': logging.getLogger('test'),
        }
        exec(compile(ast.Module(body=selected, type_ignores=[]), 'context_compaction.py', 'exec'), self.ns)
        return self.ns['prepare_tool_continuation']

    async def test_compacts_inside_single_user_turn_and_reuses_checkpoint(self):
        fn = self.setup_function()
        messages = rounds(10, 40)
        payload = {'model': 'model', 'messages': messages}
        meta = {}
        result = await fn(None, None, payload, meta, {})
        self.assertLess(len(result['messages']), len(messages))
        self.assertIn(messages[0], result['messages'])
        self.assertIn(messages[1], result['messages'])
        self.assertEqual(payload['messages'], messages)
        # Raising threshold proves reuse, not another summary, performs the second reduction.
        self.config.return_value['token_threshold'] = 100000
        self.config.return_value['token_cap'] = 100000
        again = await fn(None, None, payload, meta, {})
        self.assertEqual(again['messages'], result['messages'])
        self.assertEqual(self.summary.await_count, 1)

    async def test_checkpoint_extends_with_additional_rounds(self):
        fn = self.setup_function()
        original = rounds(10, 40)
        metadata = {}
        await fn(None, None, {'model': 'm', 'messages': original}, metadata, {})
        larger = rounds(15, 40)
        result = await fn(None, None, {'model': 'm', 'messages': larger}, metadata, {})
        self.assertIn(original[0], result['messages'])
        self.assertIn(original[1], result['messages'])
        self.assertEqual(sum(m == original[0] for m in result['messages']), 1)
        self.config.return_value['token_threshold'] = 100000
        self.config.return_value['token_cap'] = 100000
        repeated = await fn(None, None, {'model': 'm', 'messages': larger}, metadata, {})
        self.assertEqual(repeated['messages'], result['messages'])

    async def test_summary_failure_keeps_pairs_and_bounded_results(self):
        fn = self.setup_function()
        self.summary.side_effect = RuntimeError('provider unavailable')
        original = rounds()
        result = await fn(None, None, {'model': 'm', 'messages': original}, {}, {})
        self.assertEqual(len(result['messages']), len(original))
        self.assertLess(len(json.dumps(result)), len(json.dumps(original)))

    async def test_disabled_summary_still_clears_tool_payloads(self):
        fn = self.setup_function(enabled=False)
        result = await fn(None, None, {'model': 'm', 'messages': rounds()}, {}, {})
        self.summary.assert_not_awaited()
        self.assertLessEqual(len(result['messages'][-1]['content']), 12000)

    async def test_editing_drops_stateful_reference(self):
        fn = self.setup_function(enabled=False)
        result = await fn(None, None, {'model': 'm', 'messages': rounds(), 'previous_response_id': 'old'}, {}, {})
        self.assertNotIn('previous_response_id', result)


if __name__ == '__main__':
    unittest.main()
