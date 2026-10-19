"""Auto-compaction at a share of the responding model's context window.

Pure logic, with the database mocked; no model calls or Docker needed. Heavy optional
dependencies are stubbed before import so the suite runs on a bare checkout too.
"""

import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

for _name in ('chromadb', 'sentence_transformers', 'posthog'):
    if _name in sys.modules:
        continue
    try:
        __import__(_name)
    except ImportError:
        sys.modules[_name] = types.ModuleType(_name)
    else:
        continue
    if _name == 'chromadb':
        sys.modules[_name].DEFAULT_TENANT = 'default-tenant'
        sys.modules[_name].DEFAULT_DATABASE = 'default-database'

from open_webui.utils import context_compaction  # noqa: E402


def config(**overrides):
    base = {
        'enable': True,
        'token_threshold': 80000,
        'token_cap': 80000,
        'auto_compact_percent': 75,
        'retention_percentage': 40,
        'prompt_template': '',
    }
    base.update(overrides)
    return base


MODEL_128K = {'id': 'big', 'context_window': 128000}
MODEL_UNKNOWN = {'id': 'mystery', 'info': {}}


class AutoThresholdTests(unittest.TestCase):
    def run_resolver(self, cfg, model_id, models):
        return asyncio.run(context_compaction._resolve_auto_threshold(cfg, model_id, models))

    def test_percent_of_model_window(self):
        self.assertEqual(self.run_resolver(config(), 'big', {'big': MODEL_128K}), 96000)

    def test_disabled_percent_returns_none(self):
        self.assertIsNone(self.run_resolver(config(auto_compact_percent=0), 'big', {'big': MODEL_128K}))

    def test_unknown_window_returns_none(self):
        self.assertIsNone(self.run_resolver(config(), 'mystery', {'mystery': MODEL_UNKNOWN}))

    def test_unknown_model_returns_none(self):
        self.assertIsNone(self.run_resolver(config(), 'missing', {'big': MODEL_128K}))

    def test_clamped_between_10_and_95(self):
        self.assertEqual(context_compaction._clamp_auto_compact_percent(150), 95)
        self.assertEqual(context_compaction._clamp_auto_compact_percent(5), 10)
        self.assertEqual(context_compaction._clamp_auto_compact_percent(None), 75)
        self.assertEqual(context_compaction._clamp_auto_compact_percent(0), 0)

    def test_request_compacts_at_75_percent_of_window(self):
        """A 128k model at 97k used compacts, even below the fixed 80k threshold."""
        messages = [
            {'id': f'm{i}', 'role': 'user' if i % 2 == 0 else 'assistant', 'content': 'x' * 400} for i in range(20)
        ]
        messages[-1]['usage'] = {'prompt_tokens': 97000, 'completion_tokens': 100}

        with (
            patch.object(context_compaction, '_load_config', new=AsyncMock(return_value=config())),
            patch.object(context_compaction, '_generate_summary', new=AsyncMock(return_value='summary')),
        ):
            compacted, summary, changed = asyncio.run(
                context_compaction.compact_messages_for_request(
                    request=None,
                    user=None,
                    messages=messages,
                    metadata={'chat_id': None, 'message_id': None},
                    model_id='big',
                    models={'big': MODEL_128K},
                )
            )
        self.assertEqual(summary, 'summary')
        self.assertTrue(changed)

    def test_below_threshold_does_not_compact(self):
        messages = [
            {'id': f'm{i}', 'role': 'user' if i % 2 == 0 else 'assistant', 'content': 'x' * 40} for i in range(6)
        ]
        with patch.object(context_compaction, '_load_config', new=AsyncMock(return_value=config())):
            compacted, summary, changed = asyncio.run(
                context_compaction.compact_messages_for_request(
                    request=None,
                    user=None,
                    messages=messages,
                    metadata={'chat_id': None, 'message_id': None},
                    model_id='big',
                    models={'big': MODEL_128K},
                )
            )
        self.assertIsNone(summary)
        self.assertFalse(changed)


if __name__ == '__main__':
    unittest.main()
