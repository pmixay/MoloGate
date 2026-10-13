"""Mologate: decompression-bomb guard for uploads (docs/container-nodes.md).

A small archive can expand to gigabytes or more: a "zip bomb". Every uploaded file whose bytes say
it is an archive (zip and the formats built on it, such as docx, xlsx and epub; gzip, bzip2, xz;
tar) is checked before anything extracts it, and refused when it would expand past the limits.

Headers can lie, so declared sizes are only a first check: entries are then actually decompressed,
up to a shared byte budget and a time limit, and nested archives are opened up to a few levels
deep. Zip files whose entries overlap (the trick behind "non-recursive" bombs) are refused too.
"""

import bz2
import io
import lzma
import os
import struct
import tarfile
import time
import zipfile
import zlib
from dataclasses import dataclass

CHUNK = 1024 * 1024
# Nested archives up to this size are opened in memory and checked too; bigger ones are only counted.
NESTED_MAX = 256 * 1024 * 1024


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, '') or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Limits:
    # The most an upload may expand to, all levels together.
    max_expanded: int = _env_int('MOLOGATE_ARCHIVE_MAX_EXPANDED_MB', 4096) * 1024 * 1024
    # Expansion over this ratio is suspicious once the output is past `ratio_floor` bytes.
    max_ratio: int = _env_int('MOLOGATE_ARCHIVE_MAX_RATIO', 200)
    ratio_floor: int = 64 * 1024 * 1024
    max_entries: int = _env_int('MOLOGATE_ARCHIVE_MAX_ENTRIES', 50000)
    max_depth: int = 3
    max_seconds: float = 60.0


class DecompressionBomb(ValueError):
    """The archive would expand past the limits."""


ZIP_MAGIC = (b'PK\x03\x04', b'PK\x05\x06')
NESTED_EXTENSIONS = (
    '.zip',
    '.jar',
    '.docx',
    '.xlsx',
    '.pptx',
    '.odt',
    '.ods',
    '.odp',
    '.epub',
    '.gz',
    '.tgz',
    '.bz2',
    '.xz',
    '.tar',
)


def sniff(head: bytes) -> str | None:
    """The archive format of some leading bytes, or None."""
    if head.startswith(ZIP_MAGIC):
        return 'zip'
    if head.startswith(b'\x1f\x8b'):
        return 'gzip'
    if head.startswith(b'BZh'):
        return 'bzip2'
    if head.startswith(b'\xfd7zXZ\x00'):
        return 'xz'
    if len(head) > 262 and head[257:262] == b'ustar':
        return 'tar'
    return None


class _Budget:
    def __init__(self, limits: Limits, compressed: int):
        self.limits = limits
        self.compressed = max(compressed, 1)
        self.used = 0
        self.entries = 0
        self.deadline = time.monotonic() + limits.max_seconds

    def spend(self, size: int, what: str) -> None:
        self.used += size
        if self.used > self.limits.max_expanded:
            raise DecompressionBomb(
                f'{what} expands past {self.limits.max_expanded // (1024 * 1024)} MB, which looks like a '
                'decompression bomb.'
            )
        if self.used > self.limits.ratio_floor and self.used / self.compressed > self.limits.max_ratio:
            raise DecompressionBomb(
                f'{what} expands more than {self.limits.max_ratio} times its size, which looks like a '
                'decompression bomb.'
            )
        if time.monotonic() > self.deadline:
            raise DecompressionBomb(f'{what} takes too long to decompress.')

    def entry(self) -> None:
        self.entries += 1
        if self.entries > self.limits.max_entries:
            raise DecompressionBomb(f'The archive has more than {self.limits.max_entries} files.')


def _drain(read, budget: _Budget, what: str, keep: int = 0) -> bytes:
    """Read a decompressing stream to the end, counting every byte; keep the first ``keep`` bytes."""
    kept = bytearray()
    while True:
        chunk = read(CHUNK)
        if not chunk:
            return bytes(kept)
        budget.spend(len(chunk), what)
        if len(kept) < keep:
            kept += chunk[: keep - len(kept)]


def _data_offset(fp, info: zipfile.ZipInfo) -> int:
    fp.seek(info.header_offset)
    header = fp.read(30)
    if len(header) < 30 or header[:4] != b'PK\x03\x04':
        raise DecompressionBomb('The archive is malformed.')
    name_length, extra_length = struct.unpack('<HH', header[26:30])
    return info.header_offset + 30 + name_length + extra_length


def _check_overlap(archive: zipfile.ZipFile) -> None:
    """Refuse zip files whose entries share data: one compressed stream counted many times."""
    spans = sorted(
        (info.header_offset, _data_offset(archive.fp, info) + info.compress_size) for info in archive.infolist()
    )
    for (_, end), (next_start, _) in zip(spans, spans[1:]):
        if next_start < end:
            raise DecompressionBomb('The archive has overlapping files, a decompression bomb trick.')


def _inflate(archive: zipfile.ZipFile, info: zipfile.ZipInfo, budget: _Budget, what: str, keep: int) -> bytes:
    """Decompress one entry's raw stream, ignoring what its headers claim, and count every byte."""
    fp = archive.fp
    if info.compress_type == zipfile.ZIP_STORED:
        budget.spend(info.compress_size, what)
        if not keep:
            return b''
        fp.seek(_data_offset(fp, info))
        return fp.read(min(keep, info.compress_size))
    if info.compress_type != zipfile.ZIP_DEFLATED:
        with archive.open(info) as entry:
            return _drain(entry.read, budget, what, keep=keep)
    fp.seek(_data_offset(fp, info))
    remaining = info.compress_size
    inflater = zlib.decompressobj(-15)
    kept = bytearray()
    size = 0
    while remaining > 0 and not inflater.eof:
        data = fp.read(min(CHUNK, remaining))
        if not data:
            break
        remaining -= len(data)
        while data and not inflater.eof:
            out = inflater.decompress(data, CHUNK)
            size += len(out)
            budget.spend(len(out), what)
            if len(kept) < keep:
                kept += out[: keep - len(kept)]
            data = inflater.unconsumed_tail
    out = inflater.flush()
    size += len(out)
    budget.spend(len(out), what)
    if size != info.file_size:
        raise DecompressionBomb(f'{what}: {info.filename} is {size} bytes but claims {info.file_size}.')
    return bytes(kept + out[: max(0, keep - len(kept))])


def _check_zip(fileobj, budget: _Budget, depth: int, what: str) -> None:
    try:
        archive = zipfile.ZipFile(fileobj)
    except (zipfile.BadZipFile, OSError, ValueError):
        return  # Not a readable zip: nothing will extract it either.
    with archive:
        infos = archive.infolist()
        for _ in infos:
            budget.entry()
        declared = sum(info.file_size for info in infos)
        if declared > budget.limits.max_expanded - budget.used:
            raise DecompressionBomb(f'{what} declares {declared // (1024 * 1024)} MB of contents.')
        _check_overlap(archive)
        for info in infos:
            if info.is_dir():
                continue
            nested = (
                depth < budget.limits.max_depth
                and info.filename.lower().endswith(NESTED_EXTENSIONS)
                and info.file_size <= NESTED_MAX
            )
            if info.flag_bits & 0x1:
                budget.spend(info.compress_size, what)  # Encrypted: nothing can extract it unasked.
                continue
            try:
                data = _inflate(archive, info, budget, what, keep=(info.file_size + 1) if nested else 0)
            except DecompressionBomb:
                raise
            except (zipfile.BadZipFile, NotImplementedError, RuntimeError, zlib.error, EOFError, OSError):
                continue  # Unsupported or corrupt entry: extractors will fail on it too.
            if nested:
                _check_bytes(data, budget, depth + 1, f'{what} › {info.filename}')


def _check_stream(open_stream, budget: _Budget, depth: int, what: str) -> None:
    """gzip, bzip2, xz: decompress, and look inside if the result is a tar or another archive."""
    try:
        with open_stream() as stream:
            head = stream.read(CHUNK)
            budget.spend(len(head), what)
            inner = sniff(head)
            if inner == 'tar' and depth < budget.limits.max_depth:
                # Re-open to read the tar from the start, entry by entry.
                budget.used -= len(head)
                with open_stream() as again:
                    _check_tar(again, budget, depth + 1, what)
                return
            keep = NESTED_MAX if inner and depth < budget.limits.max_depth else 0
            rest = _drain(stream.read, budget, what, keep=keep)
    except DecompressionBomb:
        raise
    except (OSError, EOFError, lzma.LZMAError, zlib.error, ValueError):
        return
    if inner and inner != 'tar' and depth < budget.limits.max_depth and len(rest) < NESTED_MAX:
        _check_bytes(head + rest, budget, depth + 1, what)


def _check_tar(fileobj, budget: _Budget, depth: int, what: str) -> None:
    try:
        with tarfile.open(fileobj=fileobj, mode='r|') as archive:
            for member in archive:
                budget.entry()
                if not member.isfile():
                    continue
                nested = (
                    depth < budget.limits.max_depth
                    and member.name.lower().endswith(NESTED_EXTENSIONS)
                    and member.size <= NESTED_MAX
                )
                entry = archive.extractfile(member)
                if entry is None:
                    continue
                data = _drain(entry.read, budget, what, keep=(member.size + 1) if nested else 0)
                if nested:
                    _check_bytes(data, budget, depth + 1, f'{what} › {member.name}')
    except DecompressionBomb:
        raise
    except (tarfile.TarError, OSError, EOFError, zlib.error, lzma.LZMAError, ValueError):
        return


def _check_bytes(data: bytes, budget: _Budget, depth: int, what: str) -> None:
    kind = sniff(data[:512])
    if kind == 'zip':
        _check_zip(io.BytesIO(data), budget, depth, what)
    elif kind == 'gzip':
        _check_stream(lambda: _gzip_reader(io.BytesIO(data)), budget, depth, what)
    elif kind == 'bzip2':
        _check_stream(lambda: bz2.BZ2File(io.BytesIO(data)), budget, depth, what)
    elif kind == 'xz':
        _check_stream(lambda: lzma.LZMAFile(io.BytesIO(data)), budget, depth, what)
    elif kind == 'tar':
        _check_tar(io.BytesIO(data), budget, depth, what)


def _gzip_reader(fileobj):
    import gzip

    return gzip.GzipFile(fileobj=fileobj)


def check_file(path: str, name: str | None = None, limits: Limits | None = None) -> dict | None:
    """Inspect an uploaded file. Returns a summary for archives (None for anything else) and raises
    ``DecompressionBomb`` when it would expand past the limits."""
    limits = limits or Limits()
    with open(path, 'rb') as f:
        head = f.read(512)
    kind = sniff(head)
    if kind is None:
        return None
    what = name or os.path.basename(path)
    budget = _Budget(limits, os.path.getsize(path))
    if kind == 'zip':
        with open(path, 'rb') as f:
            _check_zip(f, budget, 1, what)
    elif kind == 'gzip':
        _check_stream(lambda: _gzip_reader(open(path, 'rb')), budget, 1, what)
    elif kind == 'bzip2':
        _check_stream(lambda: bz2.BZ2File(path), budget, 1, what)
    elif kind == 'xz':
        _check_stream(lambda: lzma.LZMAFile(path), budget, 1, what)
    elif kind == 'tar':
        with open(path, 'rb') as f:
            _check_tar(f, budget, 1, what)
    return {'format': kind, 'expanded': budget.used, 'entries': budget.entries}


class BoundedDecompressor:
    """gzip-decompress one upload chunk without letting it grow past ``limit`` bytes."""

    def __init__(self, limit: int):
        self.limit = limit
        self.size = 0
        self._d = zlib.decompressobj(wbits=31)

    def feed(self, data: bytes) -> bytes:
        out = self._d.decompress(data, self.limit - self.size + 1)
        self.size += len(out)
        if self.size > self.limit or self._d.unconsumed_tail:
            raise DecompressionBomb('A compressed upload chunk expands past its size.')
        return out

    def finish(self) -> bytes:
        out = self._d.flush()
        self.size += len(out)
        if self.size > self.limit:
            raise DecompressionBomb('A compressed upload chunk expands past its size.')
        if not self._d.eof:
            raise ValueError('A compressed upload chunk is incomplete.')
        return out
