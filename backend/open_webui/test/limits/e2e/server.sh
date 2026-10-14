#!/bin/bash
# Start Mologate for the end-to-end check: server.sh start <enforce|observe> [fresh] | server.sh stop
# Each account serves one request at once here, so the per-account cap shows quickly.
WORK=${E2E_DIR:-/tmp/mologate-e2e}
BACKEND=$(cd "$(dirname "$0")/../../../.." && pwd)
PYTHON=${PYTHON:-python}
PIDFILE=$WORK/server.pid
mkdir -p $WORK
if [ -f $PIDFILE ]; then kill $(cat $PIDFILE) 2>/dev/null; sleep 2; rm -f $PIDFILE; fi
[ "$1" = stop ] && exit 0
if curl -s localhost:8080/health >/dev/null 2>&1; then
  echo "Port 8080 is taken by another server; stop it first." >&2
  exit 1
fi
if [ "$3" = fresh ]; then rm -rf $WORK/data $WORK/static $WORK/state.json; fi
mkdir -p $WORK/data $WORK/static
export DATA_DIR=$WORK/data STATIC_DIR=$WORK/static WEBUI_SECRET_KEY=e2e-secret OFFLINE_MODE=True \
  CORS_ALLOW_ORIGIN='http://localhost:5173;http://localhost:8080' PYTHONPATH=$BACKEND
cd $BACKEND
MOLOGATE_LIMITS_MODE=$2 MOLOGATE_MAX_ACCOUNT_REQUESTS=1 nohup $PYTHON -m uvicorn open_webui.main:app --port 8080 \
  > $WORK/server.log 2>&1 &
echo $! > $PIDFILE
until curl -s localhost:8080/health >/dev/null 2>&1; do sleep 2; done
echo "Mologate is up in $2 mode."
