"""Optional terminal discovery must not prevent a chat from being submitted."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from open_webui.utils import terminals


class TerminalSkillsTests(unittest.IsolatedAsyncioTestCase):
    async def listing(self, handler):
        app = web.Application()
        app.router.add_get('/skills', handler)
        async with TestServer(app) as server:
            with patch.object(
                terminals,
                'get_terminal_request_info',
                AsyncMock(return_value=(str(server.make_url('/')), {'Authorization': 'Bearer test-key'}, {})),
            ):
                return await terminals.get_terminal_skills(None, None, {'terminal_id': 'mologate'})

    async def test_available_skills_keep_their_metadata(self):
        skill = {'id': 'terminal:review', 'name': 'review', 'description': 'Review code', 'location': '/skills/review'}

        async def handler(request):
            self.assertEqual(request.headers['Authorization'], 'Bearer test-key')
            return web.json_response([skill])

        self.assertEqual(await self.listing(handler), [skill])

    async def test_timed_out_listing_does_not_abort_the_chat(self):
        async def handler(request):
            await asyncio.sleep(0.1)
            return web.json_response([])

        with patch('open_webui.env.AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER_DATA', 0.01):
            self.assertEqual(await self.listing(handler), [])

    async def test_unavailable_terminal_is_optional(self):
        async def handler(request):
            return web.Response(status=503)

        self.assertEqual(await self.listing(handler), [])

    async def test_invalid_json_does_not_abort_the_chat(self):
        async def handler(request):
            return web.Response(text='invalid', content_type='application/json')

        self.assertEqual(await self.listing(handler), [])

    async def test_malformed_entries_are_ignored(self):
        async def handler(request):
            return web.json_response([None, 'skill', {}, {'id': 'terminal:x'}, {'id': '', 'name': 'x'}])

        self.assertEqual(await self.listing(handler), [])

    async def test_inaccessible_terminal_is_not_contacted(self):
        with patch.object(terminals, 'get_terminal_request_info', AsyncMock(return_value=None)):
            self.assertEqual(await terminals.get_terminal_skills(None, None, {}), [])

    async def test_cancellation_is_not_swallowed(self):
        with patch.object(terminals, 'get_terminal_request_info', AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await terminals.get_terminal_skills(None, None, {})


if __name__ == '__main__':
    unittest.main()
