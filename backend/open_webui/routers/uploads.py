"""Mologate: big uploads, up to 500 MB, in chunks (docs/container-nodes.md).

The browser starts an upload, sends it in 8 MiB chunks (several at once, retried on failure,
gzip-compressed when the file compresses well) and completes it. Chunks land straight in their
place in one file on disk, so nothing holds the whole upload in memory, and a dropped chunk is
simply sent again. On completion the file is checked for decompression bombs, moved into storage
without copying, and recorded like any other upload. Files too big to read in full are not
extracted to text: the agent works with them in the chat's container instead.
"""

import asyncio
import errno
import fcntl
import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from types import SimpleNamespace

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel

from open_webui.config import UPLOAD_DIR
from open_webui.constants import ERROR_MESSAGES
from open_webui.models.config import Config
from open_webui.models.files import FileForm, Files
from open_webui.storage.provider import LocalStorageProvider, Storage
from open_webui.utils.archive_guard import BoundedDecompressor, DecompressionBomb, check_file
from open_webui.utils.auth import get_verified_user

log = logging.getLogger(__name__)

router = APIRouter()

CHUNK_SIZE = 8 * 1024 * 1024
MAX_UPLOAD = int(os.environ.get('MOLOGATE_MAX_UPLOAD_MB', '500') or 500) * 1024 * 1024
# Bigger files are not turned into text for the prompt; the agent reads them in the container.
TEXT_MAX = int(os.environ.get('MOLOGATE_FILE_TEXT_MAX_MB', '20') or 20) * 1024 * 1024
STALE_SECONDS = 24 * 3600
ROOT = os.path.join(UPLOAD_DIR, '.chunked')
# A compressed chunk may be a little bigger than the data it holds, never much bigger.
COMPRESSED_SLACK = 64 * 1024


async def max_upload_size() -> int:
    """The upload limit: 500 MB, or the admin's lower file size limit."""
    configured = await Config.get('rag.file.max_size')
    try:
        configured = int(configured) * 1024 * 1024 if configured else 0
    except (TypeError, ValueError):
        configured = 0
    return min(MAX_UPLOAD, configured) if configured else MAX_UPLOAD


def _dir(upload_id: str) -> str:
    if not upload_id or any(c not in '0123456789abcdef-' for c in upload_id):
        raise HTTPException(status_code=404, detail='Upload not found.')
    return os.path.join(ROOT, upload_id)


def _read_meta(upload_id: str) -> dict:
    try:
        with open(os.path.join(_dir(upload_id), 'meta.json')) as f:
            return json.load(f)
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail='Upload not found.')


def _update_meta(upload_id: str, change) -> dict:
    """Read, change and write an upload's state under a file lock (safe across workers)."""
    path = os.path.join(_dir(upload_id), 'meta.json')
    with open(os.path.join(_dir(upload_id), '.lock'), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with open(path) as f:
            meta = json.load(f)
        change(meta)
        tmp = f'{path}.tmp'
        with open(tmp, 'w') as f:
            json.dump(meta, f)
        os.replace(tmp, path)
        return meta


def _own(meta: dict, user) -> None:
    if meta.get('user_id') != user.id:
        raise HTTPException(status_code=404, detail='Upload not found.')


def _sweep() -> None:
    """Forget uploads nobody finished within a day."""
    if not os.path.isdir(ROOT):
        return
    cutoff = time.time() - STALE_SECONDS
    for name in os.listdir(ROOT):
        path = os.path.join(ROOT, name)
        try:
            if os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def _public(meta: dict) -> dict:
    return {
        'id': meta['id'],
        'name': meta['name'],
        'size': meta['size'],
        'chunk_size': meta['chunk_size'],
        'chunks': meta['chunks'],
        'received': sorted(meta['received']),
    }


async def guard_file(path: str, name: str) -> dict | None:
    """Refuse decompression bombs before anything extracts them; returns the archive summary."""
    try:
        return await asyncio.to_thread(check_file, path, name)
    except DecompressionBomb as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f'Upload refused: {e}')


class StartForm(BaseModel):
    name: str
    size: int
    content_type: str | None = None
    metadata: dict | None = None


@router.post('')
async def start_upload(form_data: StartForm, user=Depends(get_verified_user)):
    limit = await max_upload_size()
    if form_data.size <= 0:
        raise HTTPException(status_code=400, detail=ERROR_MESSAGES.EMPTY_CONTENT)
    if form_data.size > limit:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=ERROR_MESSAGES.FILE_TOO_LARGE(size=f'{limit // (1024 * 1024)} MB'),
        )
    name = os.path.basename(form_data.name.replace('\\', '/')).strip() or 'file'
    await asyncio.to_thread(_sweep)
    upload_id = str(uuid.uuid4())
    directory = _dir(upload_id)
    os.makedirs(directory, exist_ok=True)
    # Free disk for the upload, plus room to spare, before taking it.
    free = shutil.disk_usage(directory).free
    if free < form_data.size * 2 + 1024**3:
        shutil.rmtree(directory, ignore_errors=True)
        raise HTTPException(status_code=507, detail='The server is short of disk space for this upload.')
    with open(os.path.join(directory, 'data.part'), 'wb') as f:
        f.truncate(form_data.size)
    meta = {
        'id': upload_id,
        'user_id': user.id,
        'name': name,
        'size': form_data.size,
        'content_type': form_data.content_type,
        'metadata': form_data.metadata or {},
        'chunk_size': CHUNK_SIZE,
        'chunks': (form_data.size + CHUNK_SIZE - 1) // CHUNK_SIZE,
        'received': [],
        'created_at': int(time.time()),
    }
    with open(os.path.join(directory, 'meta.json'), 'w') as f:
        json.dump(meta, f)
    return _public(meta)


@router.get('/{upload_id}')
async def get_upload(upload_id: str, user=Depends(get_verified_user)):
    meta = _read_meta(upload_id)
    _own(meta, user)
    return _public(meta)


@router.put('/{upload_id}/chunks/{index}')
async def put_chunk(upload_id: str, index: int, request: Request, user=Depends(get_verified_user)):
    """One chunk, raw or gzip-compressed (Content-Encoding: gzip); sending it again is harmless."""
    meta = _read_meta(upload_id)
    _own(meta, user)
    if index < 0 or index >= meta['chunks']:
        raise HTTPException(status_code=400, detail='No such chunk.')
    offset = index * meta['chunk_size']
    expected = min(meta['chunk_size'], meta['size'] - offset)
    encoding = (request.headers.get('content-encoding') or 'identity').lower()
    if encoding not in ('identity', 'gzip'):
        raise HTTPException(status_code=415, detail='Chunks are sent raw or gzip-compressed.')
    decompressor = BoundedDecompressor(expected) if encoding == 'gzip' else None
    received = 0
    body = bytearray()
    try:
        async for piece in request.stream():
            received += len(piece)
            if received > expected + COMPRESSED_SLACK:
                raise HTTPException(status_code=413, detail='The chunk is bigger than it should be.')
            body += decompressor.feed(piece) if decompressor else piece
            if len(body) > expected:
                raise HTTPException(status_code=413, detail='The chunk is bigger than it should be.')
        if decompressor:
            body += decompressor.finish()
    except DecompressionBomb as e:
        raise HTTPException(status_code=400, detail=f'Chunk refused: {e}')
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if len(body) != expected:
        raise HTTPException(status_code=400, detail=f'The chunk has {len(body)} bytes, not {expected}.')

    path = os.path.join(_dir(upload_id), 'data.part')

    def write():
        fd = os.open(path, os.O_WRONLY)
        try:
            os.pwrite(fd, bytes(body), offset)
        finally:
            os.close(fd)
        return _update_meta(upload_id, lambda m: m['received'].append(index) if index not in m['received'] else None)

    meta = await asyncio.to_thread(write)
    return {'index': index, 'received': len(meta['received']), 'chunks': meta['chunks']}


class CompleteForm(BaseModel):
    process: bool = True


@router.post('/{upload_id}/complete')
async def complete_upload(
    upload_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    form_data: CompleteForm | None = None,
    user=Depends(get_verified_user),
):
    from open_webui.routers.files import process_uploaded_file

    process = form_data.process if form_data else True
    meta = _read_meta(upload_id)
    _own(meta, user)
    missing = meta['chunks'] - len(set(meta['received']))
    if missing:
        raise HTTPException(status_code=409, detail=f'{missing} chunks are still missing.')

    directory = _dir(upload_id)
    part = os.path.join(directory, 'data.part')
    name = meta['name']

    def digest() -> str:
        sha = hashlib.sha256()
        with open(part, 'rb') as f:
            for block in iter(lambda: f.read(4 * 1024 * 1024), b''):
                sha.update(block)
        return sha.hexdigest()

    file_hash = await asyncio.to_thread(digest)
    try:
        archive = await guard_file(part, name)
    except HTTPException:
        shutil.rmtree(directory, ignore_errors=True)
        raise

    file_id = str(uuid.uuid4())
    tags = {
        'OpenWebUI-User-Email': user.email,
        'OpenWebUI-User-Id': user.id,
        'OpenWebUI-User-Name': user.name,
        'OpenWebUI-File-Id': file_id,
    }

    def store() -> str:
        for filename in (f'{file_id}_{name}', f'{file_id}{os.path.splitext(name)[1]}'):
            try:
                if isinstance(Storage, LocalStorageProvider):
                    target = os.path.join(UPLOAD_DIR, filename)
                    os.replace(part, target)  # Same disk: no copy.
                    return target
                with open(part, 'rb') as f:
                    return Storage.upload_file(f, filename, tags)[1]
            except OSError as e:
                if e.errno != errno.ENAMETOOLONG:
                    raise
        raise OSError(errno.ENAMETOOLONG, 'File name too long')

    try:
        file_path = await asyncio.to_thread(store)
    except OSError as e:
        log.exception(e)
        raise HTTPException(status_code=400, detail=ERROR_MESSAGES.DEFAULT(e.strerror or 'Error uploading file'))
    finally:
        shutil.rmtree(directory, ignore_errors=True)

    text_skipped = process and meta['size'] > TEXT_MAX
    file_item = await Files.insert_new_file(
        user.id,
        FileForm(
            id=file_id,
            filename=name,
            path=file_path,
            data={'status': 'completed' if text_skipped else 'pending'} if process else {},
            meta={
                'name': name,
                'content_type': meta.get('content_type'),
                'size': meta['size'],
                'file_hash': file_hash,
                'data': meta.get('metadata') or {},
                **({'archive': archive} if archive else {}),
                **({'text_skipped': True} if text_skipped else {}),
            },
        ),
    )
    if process and not text_skipped:
        background_tasks.add_task(
            process_uploaded_file,
            request,
            SimpleNamespace(content_type=meta.get('content_type')),
            file_path,
            file_item,
            meta.get('metadata') or {},
            user,
        )
    return {'status': True, **file_item.model_dump()}


@router.delete('/{upload_id}')
async def cancel_upload(upload_id: str, user=Depends(get_verified_user)):
    meta = _read_meta(upload_id)
    _own(meta, user)
    shutil.rmtree(_dir(upload_id), ignore_errors=True)
    return {'ok': True}
