"""Model-only tool result budgets. No database, provider, or optional dependencies."""

import ast
import copy
import unittest
from pathlib import Path

from open_webui.utils.tool_context import (
    TOOL_CONTEXT_MAX_CHARS,
    TOOL_RESULT_MAX_CHARS,
    bound_tool_context,
)


class ToolContextTests(unittest.TestCase):
    def test_small_results_are_unchanged(self):
        messages = [{'role': 'tool', 'tool_call_id': 'a', 'content': 'done'}]
        self.assertEqual(bound_tool_context(messages), messages)

    def test_large_result_keeps_head_tail_and_saved_result(self):
        text = 'HEADER\n' + 'x' * 500_000 + '\nERROR: build failed'
        messages = [{'role': 'tool', 'tool_call_id': 'a', 'content': text}]
        result = bound_tool_context(messages)[0]['content']
        self.assertLessEqual(len(result), TOOL_RESULT_MAX_CHARS)
        self.assertTrue(result.startswith('HEADER\n'))
        self.assertTrue(result.endswith('ERROR: build failed'))
        self.assertIn('full result remains in chat history', result)
        self.assertEqual(messages[0]['content'], text)

    def test_shared_budget_favors_recent_results_without_dropping_pairs(self):
        messages = []
        for index in range(60):
            messages.extend(
                [
                    {
                        'role': 'assistant',
                        'tool_calls': [
                            {
                                'id': str(index),
                                'function': {'name': 'write_file', 'arguments': '{"content":"do not modify"}'},
                            }
                        ],
                    },
                    {'role': 'tool', 'tool_call_id': str(index), 'content': 'x' * 40_000},
                ]
            )
        saved = copy.deepcopy(messages)
        bounded = bound_tool_context(messages)
        tools = [m for m in bounded if m['role'] == 'tool']
        self.assertLessEqual(sum(len(m['content']) for m in tools), TOOL_CONTEXT_MAX_CHARS)
        self.assertEqual(len(tools[-1]['content']), TOOL_RESULT_MAX_CHARS)
        self.assertLess(len(tools[0]['content']), len(tools[-1]['content']))
        self.assertEqual(len(bounded), len(messages))
        self.assertEqual(bounded[0], saved[0])
        self.assertEqual(messages, saved)
        self.assertEqual(bound_tool_context(bounded), bounded)

    def test_multimodal_results_preserve_images(self):
        image = {'type': 'input_image', 'image_url': 'data:image/png;base64,abc'}
        messages = [
            {
                'role': 'tool',
                'tool_call_id': 'a',
                'content': [
                    {'type': 'input_text', 'text': 'x' * 50_000},
                    image,
                    {'type': 'text', 'text': 'y' * 50_000},
                ],
            }
        ]
        saved = copy.deepcopy(messages)
        result = bound_tool_context(messages)[0]['content']
        self.assertLessEqual(sum(len(p.get('text', '')) for p in result), TOOL_RESULT_MAX_CHARS)
        self.assertEqual(result[1], image)
        self.assertTrue(all(p.get('text') for p in result if p.get('type') in ('text', 'input_text')))
        self.assertEqual(messages, saved)

    def test_user_text_and_call_arguments_are_never_shortened(self):
        messages = [
            {'role': 'user', 'content': 'x' * 100_000},
            {
                'role': 'assistant',
                'content': 'y' * 100_000,
                'tool_calls': [{'id': 'a', 'function': {'arguments': 'z' * 100_000}}],
            },
        ]
        self.assertEqual(bound_tool_context(messages), messages)

    def test_many_results_still_fit_shared_budget(self):
        messages = [{'role': 'tool', 'content': 'x' * 1_000} for _ in range(1_000)]
        self.assertLessEqual(sum(len(m['content']) for m in bound_tool_context(messages)), TOOL_CONTEXT_MAX_CHARS)

    def test_common_model_boundary_applies_projection(self):
        # Execute the real normalization function without importing the application
        # (which loads optional AI integrations and a database at import time).
        path = Path(__file__).parents[1] / 'utils' / 'middleware.py'
        tree = ast.parse(path.read_text())
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == 'normalize_messages_for_model'
        )
        namespace = {
            'bound_tool_context': bound_tool_context,
            'strip_empty_content_blocks': lambda messages: messages,
            'merge_system_messages': lambda messages: messages,
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), namespace)
        form = {'messages': [{'role': 'tool', 'content': 'x' * 200_000}]}
        normalized = namespace['normalize_messages_for_model'](form)
        self.assertLessEqual(len(normalized['messages'][0]['content']), TOOL_RESULT_MAX_CHARS)


if __name__ == '__main__':
    unittest.main()
