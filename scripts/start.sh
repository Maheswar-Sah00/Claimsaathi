#!/bin/sh
# Container entry point. The host sets PORT; APP picks the service.
# Hugging Face Spaces (SPACE_ID set) or APP=space: run the portal privately on 127.0.0.1:8001
# and the agent publicly on PORT (7860 on Spaces) in the same container.
set -e

if [ -n "$SPACE_ID" ] || [ "$APP" = "space" ]; then
    export PORTAL_BASE_URL="${PORTAL_BASE_URL:-http://127.0.0.1:8001}"
    export AGENT_BASE_URL="${AGENT_BASE_URL:-https://${SPACE_HOST:-localhost}}"
    export AGENT_HTTPS_ONLY_COOKIES="${AGENT_HTTPS_ONLY_COOKIES:-true}"
    uvicorn portal.app:app --host 127.0.0.1 --port 8001 --workers 1 &
    exec uvicorn agent.server:app --host 0.0.0.0 --port "${PORT:-7860}" --workers 1
fi

if [ "$APP" = "portal" ]; then
    exec uvicorn portal.app:app --host 0.0.0.0 --port "${PORT:-8001}" --workers 1
fi
exec uvicorn agent.server:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1
