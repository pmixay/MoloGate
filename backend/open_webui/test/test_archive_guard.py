"""Decompression-bomb guard: real bombs are refused, ordinary archives pass."""

import gzip
import io
import os
import struct
import tarfile
import tempfile
import unittest
import zipfile

from open_webui.utils.archive_guard import BoundedDecompressor, DecompressionBomb, Limits, check_file

SMALL = Limits(max_expanded=32 * 1024 * 1024, max_ratio=200, ratio_floor=4 * 1024 * 1024, max_entries=1000)


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def overlapping_zip() -> bytes:
    """Two names whose central directory entries point at the same compressed data."""
    data = zip_bytes({'a.txt': b'0' * 100000})
    end = data.rindex(b'PK\x05\x06')
    cd_offset = struct.unpack('<I', data[end + 16 : end + 20])[0]
    central = data[cd_offset:end]
    name_len = struct.unpack('<H', central[28:30])[0]
    second = central[:46] + b'b' + central[47 : 46 + name_len] + central[46 + name_len :]
    eocd = bytearray(data[end:])
    struct.pack_into('<HH', eocd, 8, 2, 2)
    struct.pack_into('<I', eocd, 12, len(central) * 2)
    return data[:cd_offset] + central + second + bytes(eocd)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def write(self, name: str, data: bytes) -> str:
        path = os.path.join(self.dir.name, name)
        with open(path, 'wb') as f:
            f.write(data)
        return path

    def test_ordinary_files_pass(self):
        self.assertIsNone(check_file(self.write('a.txt', b'hello'), limits=SMALL))
        docx = zip_bytes({'word/document.xml': b'<w:document>' + b'text ' * 2000 + b'</w:document>'})
        summary = check_file(self.write('a.docx', docx), limits=SMALL)
        self.assertEqual(summary['format'], 'zip')
        self.assertEqual(summary['entries'], 1)

    def test_high_ratio_zip_is_refused(self):
        bomb = zip_bytes({'zeros.bin': b'\0' * (64 * 1024 * 1024)})
        self.assertLess(len(bomb), 200 * 1024)
        with self.assertRaises(DecompressionBomb):
            check_file(self.write('bomb.zip', bomb), limits=SMALL)

    def test_lying_headers_are_caught_by_decompressing(self):
        data = bytearray(zip_bytes({'zeros.bin': b'\0' * (40 * 1024 * 1024)}))
        # Claim the entry is tiny in the central directory.
        end = data.rindex(b'PK\x05\x06')
        cd_offset = struct.unpack('<I', data[end + 16 : end + 20])[0]
        struct.pack_into('<I', data, cd_offset + 24, 10)
        with self.assertRaises(DecompressionBomb):
            check_file(self.write('liar.zip', bytes(data)), limits=SMALL)

    def test_nested_zips_are_refused(self):
        inner = zip_bytes({'zeros.bin': b'\0' * (12 * 1024 * 1024)})
        middle = zip_bytes({f'{i}.zip': inner for i in range(4)})
        outer = zip_bytes({f'{i}.zip': middle for i in range(4)})
        self.assertLess(len(outer), 1024 * 1024)
        with self.assertRaises(DecompressionBomb):
            check_file(self.write('nested.zip', outer), limits=SMALL)

    def test_overlapping_entries_are_refused(self):
        with self.assertRaises(DecompressionBomb):
            check_file(self.write('overlap.zip', overlapping_zip()), limits=SMALL)

    def test_gzip_bomb_is_refused(self):
        bomb = gzip.compress(b'\0' * (64 * 1024 * 1024))
        with self.assertRaises(DecompressionBomb):
            check_file(self.write('bomb.gz', bomb), limits=SMALL)

    def test_tar_gz_with_too_many_files_is_refused(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            for i in range(1200):
                info = tarfile.TarInfo(f'f{i}.txt')
                info.size = 1
                archive.addfile(info, io.BytesIO(b'x'))
        with self.assertRaises(DecompressionBomb):
            check_file(self.write('many.tar.gz', buffer.getvalue()), limits=SMALL)

    def test_small_tar_gz_passes(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            info = tarfile.TarInfo('readme.txt')
            info.size = 5
            archive.addfile(info, io.BytesIO(b'hello'))
        summary = check_file(self.write('ok.tar.gz', buffer.getvalue()), limits=SMALL)
        self.assertEqual(summary['format'], 'gzip')
        self.assertEqual(summary['entries'], 1)


class ChunkTests(unittest.TestCase):
    def test_chunk_within_its_size(self):
        data = b'abc' * 1000
        decompressor = BoundedDecompressor(len(data))
        out = decompressor.feed(gzip.compress(data)) + decompressor.finish()
        self.assertEqual(out, data)

    def test_chunk_bomb_stops_at_the_limit(self):
        decompressor = BoundedDecompressor(1024)
        with self.assertRaises(DecompressionBomb):
            decompressor.feed(gzip.compress(b'\0' * (50 * 1024 * 1024)))

    def test_truncated_chunk_is_refused(self):
        decompressor = BoundedDecompressor(10000)
        compressed = gzip.compress(b'x' * 10000)
        decompressor.feed(compressed[:-12])
        with self.assertRaises(ValueError):
            decompressor.finish()


if __name__ == '__main__':
    unittest.main()
