#!/bin/sh
# Mologate: start Open Terminal on the Unix socket the container node mounts at /run/mologate.
# The socket keeps the API reachable whatever the container's network, including none.
set -eu

: "${OPEN_TERMINAL_API_KEY:?OPEN_TERMINAL_API_KEY is required}"

mkdir -p /run/mologate /home/user
rm -f /run/mologate/terminal.sock
cd /home/user

exec /opt/open-terminal/bin/uvicorn open_terminal.main:app \
    --uds /run/mologate/terminal.sock \
    --no-access-log \
    --timeout-graceful-shutdown 3
