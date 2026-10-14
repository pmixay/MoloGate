"""Chunked uploads: chunks land in place (raw or gzip), bombs and wrong sizes are refused, only the
uploader can touch an upload, and a completed upload is stored without being read into memory.

Runs against an isolated DATA_DIR; the database and processing are mocked.
"""

import gzip
import hashlib
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from open_webui.routers import uploads
from open_webui.utils.auth import get_verified_user

ME = SimpleNamespace(id='me', email='me@example.com', name='Me', role='user')
OTHER = SimpleNamespace(id='other', email='o@example.com', name='O', role='user')


def make_app(user):
    app = FastAPI()
    app.include_router(uploads.router, prefix='/uploads')
    app.dependency_overrides[get_verified_user] = lambda: user
    return app


class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patches = [
            patch.object(uploads, 'CHUNK_SIZE', 1024),
            patch.object(uploads.Config, 'get', AsyncMock(return_value=None)),
        ]
        for p in self.patches:
            p.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(ME)), base_url='http://t')

    async def asyncTearDown(self):
        await self.client.aclose()
        for p in self.patches:
            p.stop()

    async def start(self, size: int, name='data.txt'):
        response = await self.client.post('/uploads', json={'name': name, 'size': size, 'content_type': 'text/plain'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_chunks_assemble_raw_and_gzip_and_resend_is_harmless(self):
        data = os.urandom(1024) + b'a' * 1024 + b'tail'
        upload = await self.start(len(data))
        self.assertEqual(upload['chunks'], 3)
        put = lambda i, body, **h: self.client.put(f'/uploads/{upload["id"]}/chunks/{i}', content=body, headers=h)
        self.assertEqual((await put(0, data[:1024])).status_code, 200)
        self.assertEqual(
            (await put(1, gzip.compress(data[1024:2048]), **{'Content-Encoding': 'gzip'})).status_code, 200
        )
        self.assertEqual((await put(1, data[1024:2048])).status_code, 200)  # resent
        self.assertEqual((await self.client.post(f'/uploads/{upload["id"]}/complete')).status_code, 409)
        self.assertEqual((await put(2, data[2048:])).status_code, 200)
        state = (await self.client.get(f'/uploads/{upload["id"]}')).json()
        self.assertEqual(state['received'], [0, 1, 2])

        stored = {}

        async def insert(user_id, form):
            stored['form'] = form
            return SimpleNamespace(model_dump=lambda: {'id': form.id, 'meta': form.meta, 'data': form.data})

        with (
            patch.object(uploads.Files, 'insert_new_file', side_effect=insert),
            patch('open_webui.routers.files.process_uploaded_file', AsyncMock()),
        ):
            response = await self.client.post(f'/uploads/{upload["id"]}/complete', json={'process': True})
        self.assertEqual(response.status_code, 200, response.text)
        form = stored['form']
        with open(form.path, 'rb') as f:
            self.assertEqual(f.read(), data)
        self.assertEqual(form.meta['file_hash'], hashlib.sha256(data).hexdigest())
        self.assertEqual(form.meta['size'], len(data))
        self.assertFalse(os.path.exists(os.path.join(uploads.ROOT, upload['id'])))
        os.unlink(form.path)

    async def test_bomb_and_wrong_sizes_are_refused(self):
        upload = await self.start(2048)
        url = f'/uploads/{upload["id"]}/chunks/0'
        bomb = gzip.compress(b'\0' * (10 * 1024 * 1024))
        response = await self.client.put(url, content=bomb, headers={'Content-Encoding': 'gzip'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('expands past', response.text)
        self.assertEqual((await self.client.put(url, content=b'x' * 10)).status_code, 400)
        self.assertEqual((await self.client.put(url, content=b'x' * 100000)).status_code, 413)
        self.assertEqual((await self.client.put(f'/uploads/{upload["id"]}/chunks/9', content=b'x')).status_code, 400)
        response = await self.client.put(url, content=b'x' * 1024, headers={'Content-Encoding': 'br'})
        self.assertEqual(response.status_code, 415)

    async def test_only_the_uploader_can_use_an_upload(self):
        upload = await self.start(10)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(OTHER)), base_url='http://t') as other:
            self.assertEqual((await other.get(f'/uploads/{upload["id"]}')).status_code, 404)
            self.assertEqual((await other.put(f'/uploads/{upload["id"]}/chunks/0', content=b'x' * 10)).status_code, 404)
            self.assertEqual((await other.delete(f'/uploads/{upload["id"]}')).status_code, 404)
        self.assertEqual((await self.client.delete(f'/uploads/{upload["id"]}')).status_code, 200)

    async def test_size_limit(self):
        response = await self.client.post('/uploads', json={'name': 'x', 'size': uploads.MAX_UPLOAD + 1})
        self.assertEqual(response.status_code, 413)
        self.assertEqual((await self.client.post('/uploads', json={'name': 'x', 'size': 0})).status_code, 400)
        self.assertEqual((await self.client.get('/uploads/../../etc')).status_code, 404)


if __name__ == '__main__':
    unittest.main()
