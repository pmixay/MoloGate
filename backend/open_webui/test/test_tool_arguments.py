"""A tool call that leaves out required arguments is answered with what's missing, not a Python error."""

import unittest

from open_webui.tools.builtin import search_web
from open_webui.utils.middleware import missing_arguments_error
from open_webui.utils.tools import get_builtin_tool_spec


class MissingArgumentsTest(unittest.TestCase):
    def setUp(self):
        self.spec = get_builtin_tool_spec(search_web)

    def test_a_call_without_its_query_is_told_what_to_send(self):
        error = missing_arguments_error('search_web', self.spec, {})

        self.assertEqual(
            error, 'Error: `search_web` was called without `query`. Call it again with the arguments filled in.'
        )

    def test_optional_arguments_can_be_left_out(self):
        self.assertIsNone(missing_arguments_error('search_web', self.spec, {'query': 'mologate'}))

    def test_a_tool_without_parameters_needs_nothing(self):
        self.assertIsNone(missing_arguments_error('list_agents', {'name': 'list_agents'}, {}))


if __name__ == '__main__':
    unittest.main()
