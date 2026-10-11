"""Install the pinned official Codex native binary; verify the published npm integrity."""

import base64
import hashlib
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

VERSION = '0.162.1'
TARGETS = {
    'amd64': (
        'x64',
        'x86_64',
        'cjK2mdHIfEepZ1lQ1MX3aTQC+8ncIsvnacl/6vI2dIIcfP8/MrhTQ2+qRlkXFNDzJSj1d36quIszP+3/4CUJWQ==',
    ),
    'arm64': (
        'arm64',
        'aarch64',
        'Ss7W/SrKtQFNaMtSojNoiykJgX+iDC8daM+YrcvgmJADtQPk0cd3w6Dna4CYH2CYvf9nQranj69fpcTzZ3sQIQ==',
    ),
}


def install(architecture, destination):
    npm_arch, rust_arch, integrity = TARGETS[architecture]
    url = f'https://registry.npmjs.org/@openai/codex/-/codex-{VERSION}-linux-{npm_arch}.tgz'
    with tempfile.TemporaryFile() as archive:
        digest = hashlib.sha512()
        with urllib.request.urlopen(url, timeout=90) as response:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                archive.write(chunk)
        if base64.b64encode(digest.digest()).decode() != integrity:
            raise RuntimeError('Codex archive integrity check failed')
        archive.seek(0)
        with tarfile.open(fileobj=archive, mode='r:gz') as tar:
            member = tar.getmember(f'package/vendor/{rust_arch}-unknown-linux-musl/bin/codex')
            if not member.isfile():
                raise RuntimeError('Codex archive does not contain the expected binary')
            with tar.extractfile(member) as source, open(destination, 'wb') as target:
                shutil.copyfileobj(source, target)
    Path(destination).chmod(0o755)


if __name__ == '__main__':
    install(sys.argv[1], sys.argv[2])
