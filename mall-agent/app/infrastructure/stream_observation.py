"""Optional fixed metadata for an existing SSE DomainError termination."""

import json
import logging
from uuid import UUID

log = logging.getLogger(__name__)

_OPERATIONS = frozenset({"MEMBER_EVENTS", "STAFF_EVENTS"})
_CODES = frozenset(
    {
        "AUTH_EXPIRED",
        "AUTH_REQUIRED",
        "FORBIDDEN",
        "JAVA_UNAVAILABLE",
        "JAVA_BRIDGE_REJECTED",
        "EVENT_CURSOR_EXPIRED",
        "INVALID_CURSOR",
        "NOT_FOUND",
        "OTHER",
    }
)


def _canonical_uuid(value):
    if not isinstance(value, str) or len(value) != 36:
        return None
    canonical = str(UUID(value))
    return canonical if canonical == value else None


def observe_stream_domain_end(request, operation, error):
    """Never change stream control flow; omit any failed or disabled observation."""
    try:
        # Both route groups already share this same application settings instance.
        if request.app.state.services.bridge.settings.performance_timing_enabled is not True:
            return
        if type(operation) is not str or operation not in _OPERATIONS:
            return
        run_id = _canonical_uuid(request.headers.get("X-Performance-Run"))
        request_id = _canonical_uuid(request.headers.get("X-Request-ID"))
        if run_id is None or request_id is None:
            return
        code = error.code
        status = error.status
        payload = {
            "event": "SSE_DOMAIN_END",
            "run_id": run_id,
            "request_id": request_id,
            "operation": operation,
            "code": code if type(code) is str and code in _CODES else "OTHER",
            "status": status if type(status) is int and 100 <= status <= 599 else None,
        }
        log.warning("%s", json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False))
    except Exception:
        # No error message, response payload, identity or credential is logged.
        return
