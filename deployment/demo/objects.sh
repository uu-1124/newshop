#!/bin/sh
set -eu
tries=0
until mc alias set demo http://minio:9000 demoadmin "$MINIO_PASSWORD" >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [ "$tries" -ge 45 ]; then echo 'MinIO did not become ready' >&2; exit 1; fi
    sleep 2
done
mc mb --ignore-existing demo/mall demo/mall-agent-private
mc anonymous set download demo/mall
mc admin user add demo demoagent "$AGENT_PASSWORD" >/dev/null
mc admin policy create demo demo-agent-objects /demo/agent-objects.json
if ! mc admin policy attach demo demo-agent-objects --user demoagent; then
    mc admin policy entities demo demo-agent-objects | grep -q demoagent
fi
