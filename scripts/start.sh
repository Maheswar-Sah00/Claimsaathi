#!/bin/sh
# Container entry point. Railway (or any host) sets PORT; APP picks the service.
set -e
if [ "$APP" = "portal" ]; then
    exec uvicorn portal.app:app --host 0.0.0.0 --port "${PORT:-8001}" --workers 1
fi
exec uvicorn agent.server:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1
