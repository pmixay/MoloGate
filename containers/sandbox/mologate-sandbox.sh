#!/bin/sh
# Mologate: start Open Terminal on the Unix socket the container node mounts at /run/mologate.
# The socket keeps the API reachable whatever the container's network, including none.
set -eu

: "${OPEN_TERMINAL_API_KEY:?OPEN_TERMINAL_API_KEY is required}"

# Open Terminal 0.14 uses Python/framework tempfile defaults and passes its environment to
# command/PTY children. Keep those files off the small /tmp tmpfs, without redirecting /tmp
# itself or overriding operator-supplied temp paths. Logs already default to disk-backed
# ~/.local/state/open-terminal/logs (OPEN_TERMINAL_LOG_DIR remains configurable).
TMPDIR=${TMPDIR:-${TMP:-${TEMP:-/home/user/.local/state/open-terminal/tmp}}}
TMP=${TMP:-$TMPDIR}
TEMP=${TEMP:-$TMPDIR}
export TMPDIR TMP TEMP

mkdir -p "$TMPDIR" "$TMP" "$TEMP" /run/mologate /home/user
rm -f /run/mologate/terminal.sock
cd /home/user

exec /opt/open-terminal/bin/uvicorn open_terminal.main:app \
    --uds /run/mologate/terminal.sock \
    --no-access-log \
    --timeout-graceful-shutdown 3
