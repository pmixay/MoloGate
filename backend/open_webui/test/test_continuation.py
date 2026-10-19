"""Dependency-free regression tests for tool continuation projection/assembly."""

import ast
import copy
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('continuation', ROOT / 'utils/continuation.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def isolated_functions(path, names, namespace):
    tree = ast.parse(path.read_text())
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


def call(cid):
    return {'type': 'function_call', 'call_id': cid, 'status': 'completed', 'name': 'read_file', 'arguments': '{}'}


def result(cid):
    return {'type': 'function_call_output', 'call_id': cid, 'output': [{'type': 'input_text', 'text': cid}]}


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.ns = isolated_functions(
            ROOT / 'utils/misc.py',
            {'convert_output_to_messages', 'reconcile_tool_pairs'},
            {'JSONCodec': json, 'get_content_from_message': lambda m: m.get('content', '')},
        )
        self.convert = self.ns['convert_output_to_messages']

    def test_stateful_only_new_results_even_when_call_is_before_delta(self):
        output = [call('old'), result('old'), call('new')]
        start = len(output)
        output += [result('new'), {'type': 'agent_message', 'content': 'new input'}]
        saved = copy.deepcopy(output)
        self.assertEqual(self.convert(output[start:]), [{'role': 'user', 'content': 'new input'}])
        messages = module.stateful_tool_messages(output, start, self.convert, raw=True)
        self.assertEqual(
            messages,
            [
                {'role': 'tool', 'tool_call_id': 'new', 'content': 'new'},
                {'role': 'user', 'content': 'new input'},
            ],
        )
        self.assertEqual(output, saved)

    def test_parallel_stateful_results_preserve_images(self):
        output = [call('a'), call('b')]
        start = len(output)
        output += [result('a'), result('b')]
        output[-1]['output'].append({'type': 'input_image', 'image_url': 'https://example.com/image.png'})
        messages = module.stateful_tool_messages(output, start, self.convert, raw=True)
        self.assertEqual([m['tool_call_id'] for m in messages], ['a', 'b'])
        self.assertEqual(messages[-1]['content'][-1]['type'], 'input_image')

    def test_real_middleware_fold_keeps_continued_prefix_out_of_requests(self):
        # Execute the actual post-stream assignments, not a separately invented fold.
        tree = ast.parse((ROOT / 'utils/middleware.py').read_text())
        handler = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == 'streaming_chat_response_handler'
        )
        full = next(n for n in ast.walk(handler) if isinstance(n, ast.FunctionDef) and n.name == 'full_output')
        assignments = [
            n
            for n in ast.walk(handler)
            if isinstance(n, ast.Assign)
            and isinstance(n.value, ast.BinOp)
            and isinstance(n.value.left, ast.Name)
            and n.value.left.id == 'accumulated_output'
        ]
        self.assertEqual(len(assignments), 1)
        fold = assignments[0]
        parent = next(n for n in ast.walk(handler) if isinstance(n, ast.If) and fold in n.body)
        index = parent.body.index(fold)
        code = compile(ast.Module(body=[full, fold, parent.body[index + 1]], type_ignores=[]), '<fold>', 'exec')
        for continuing in (False, True):
            prefix = [call('prefix'), result('prefix')] if continuing else []
            ns = {
                'continuing': continuing,
                'continuation_prefix': prefix,
                'prior_output': prefix,
                'output': [],
                'accumulated_output': [],
            }
            accumulated = []
            for cid in ('one', 'two', 'three'):
                ns.update(accumulated_output=accumulated, output=[call(cid), result(cid)])
                exec(code, ns)
                accumulated = ns['output']
                ids = [m['tool_call_id'] for m in self.convert(accumulated) if m['role'] == 'tool']
                self.assertEqual(ids.count(cid), 1)
                self.assertNotIn('prefix', ids)
                stored_ids = [m['tool_call_id'] for m in self.convert(ns['full_output']()) if m['role'] == 'tool']
                self.assertEqual(stored_ids, (['prefix'] if continuing else []) + ['one', 'two', 'three'][: len(ids)])


if __name__ == '__main__':
    unittest.main()
