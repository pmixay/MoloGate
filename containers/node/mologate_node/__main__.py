import logging
import os

from aiohttp import web

from mologate_node.app import create_app
from mologate_node.config import load_settings


def main() -> None:
    logging.basicConfig(
        level=os.environ.get('NODE_LOG_LEVEL', 'INFO').upper(),
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )
    settings = load_settings()
    web.run_app(create_app(settings), host=settings.host, port=settings.port, access_log=None)


if __name__ == '__main__':
    main()
