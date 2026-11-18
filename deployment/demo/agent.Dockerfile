FROM ghcr.io/astral-sh/uv:0.11.8 AS uv
FROM python:3.13.5-slim-bookworm
COPY --from=uv /uv /bin/uv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/workspace/mall-agent/.venv/bin:$PATH"
WORKDIR /workspace/mall-agent
COPY mall-agent/pyproject.toml mall-agent/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv UV_LINK_MODE=copy uv sync --locked --no-dev --no-install-project
COPY mall-agent/alembic.ini ./
COPY mall-agent/app ./app
COPY mall-agent/migrations ./migrations
COPY mall-agent/scripts/check_framework.py ./scripts/
COPY docs/api/openapi.json /workspace/docs/api/
COPY docs/architecture/coverage.json /workspace/docs/architecture/
COPY mall-app-web/src/services/agent-contracts.ts /workspace/mall-app-web/src/services/
COPY mall-admin-web/src/router/agent.ts /workspace/mall-admin-web/src/router/
COPY mall-admin-web/src/views/agent/Workspace.vue /workspace/mall-admin-web/src/views/agent/
COPY mall-swarm/mall-portal/src/main/java/com/macro/mall/portal/controller/AgentMemberBridgeController.java /workspace/mall-swarm/mall-portal/src/main/java/com/macro/mall/portal/controller/
COPY mall-swarm/mall-common/src/main/java/com/macro/mall/common/agent/Agent*Port.java /workspace/mall-swarm/mall-common/src/main/java/com/macro/mall/common/agent/
COPY deployment/demo/agent-init.py /app/agent-init.py
USER 10002:10002
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8099"]
