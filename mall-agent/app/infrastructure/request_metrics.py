"""Persist ordinary HTTP latency without paths, query strings, bodies or identities."""

import logging
import time
from uuid import UUID, uuid4

from app.infrastructure.performance_timing import phase
from app.infrastructure.postgres.database import execute

log = logging.getLogger(__name__)


async def record_request(request, status, started):
    svc = getattr(request.app.state, "services", None)
    route = request.scope.get("route")
    operation = getattr(route, "operation_id", None)
    # Streaming latency is a separate measure; ordinary P95 must exclude it.
    if not svc or not operation:
        return
    # SSE rows measure subscription setup/rejections only, never stream duration.
    # Ordinary P95 explicitly excludes these operations at aggregation time.
    if not svc.conversations.settings.governance_enabled:
        return
    duration = max(0, round((time.perf_counter() - started) * 1000))
    try:
        # One atomic INSERT needs no multi-statement transaction. Persist before
        # returning, without extra BEGIN/COMMIT round trips over the SSH tunnel.
        with phase("metrics_write_ms"):
            async with svc.db.connection("AUTOCOMMIT") as conn:
                await execute(
                    conn,
                    """INSERT INTO ag_request_metric(id,request_id,operation,method,status_code,duration_ms)
                    VALUES(:id,:request,:operation,:method,:status,:duration)""",
                    id=uuid4(),
                    request=UUID(request.state.request_id),
                    operation=operation,
                    method=request.method,
                    status=status,
                    duration=duration,
                )
    except Exception as error:
        # Metrics failure never changes an already-committed business response.
        log.warning("request metrics unavailable type=%s", type(error).__name__)
