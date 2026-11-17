"""Opt-in, request-scoped timings without SQL text, parameters or identities.

The response header measures response preparation, not body/SSE delivery. Database
spans are nested inside handler/metrics spans and must not be added to them.
Checkout residual includes queueing and setup; it is not a pure queue measurement.
"""

import json
import math
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import __version__ as sqlalchemy_version
from sqlalchemy import event
from sqlalchemy.pool import AsyncAdaptedQueuePool
from sqlalchemy.util.queue import AsyncAdaptedQueue

HEADER = "X-Performance-Timing"
RUN_HEADER = "X-Performance-Run"
JAVA_BRIDGE_OPERATIONS = frozenset({"MEMBER_IDENTITY", "MEMBER_ORDERS", "STAFF_IDENTITY", "OTHER"})
JAVA_BRIDGE_NUMERIC_CODES = frozenset({200, 400, 401, 403, 404, 409, 422, 500, 502, 503, 504})
JAVA_BRIDGE_STRING_CODES = frozenset(
    {
        "OK",
        "BRIDGE_NONCE_UNAVAILABLE",
        "BRIDGE_SIGNATURE_INVALID",
        "AUTH_EXPIRED",
        "AUTH_REQUIRED",
        "FORBIDDEN",
        "JAVA_UNAVAILABLE",
        "JAVA_BRIDGE_REJECTED",
        "AUTH_SERVICE_UNAVAILABLE",
        "RETURN_RULE_UNAVAILABLE",
        "OTHER",
        "NONE",
        "NON_OBJECT",
    }
)
JAVA_BRIDGE_ERROR_TYPES = frozenset(
    {
        "NONE",
        "INVALID_JSON",
        "CONNECT_TIMEOUT",
        "READ_TIMEOUT",
        "WRITE_TIMEOUT",
        "POOL_TIMEOUT",
        "TIMEOUT_OTHER",
        "CONNECT_ERROR",
        "READ_ERROR",
        "WRITE_ERROR",
        "CLOSE_ERROR",
        "REMOTE_PROTOCOL_ERROR",
        "LOCAL_PROTOCOL_ERROR",
        "PROXY_ERROR",
        "UNSUPPORTED_PROTOCOL",
        "TOO_MANY_REDIRECTS",
        "HTTP_ERROR_OTHER",
    }
)
_OPERATION = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}\Z")
_DB_FIELDS = (
    "acquire_ms",
    "physical_connect_ms",
    "checkout_residual_ms",
    "sql_execute_ms",
    "transaction_end_ms",
    "isolation_config_ms",
    "connection_release_ms",
    "pool_queue_ms",
)


@dataclass
class RequestTiming:
    run_id: str
    request_id: str
    started: float = field(default_factory=time.perf_counter)
    closed: bool = False
    phases: dict = field(default_factory=dict)
    db: dict = field(default_factory=lambda: dict.fromkeys(_DB_FIELDS, 0.0))
    counts: dict = field(
        default_factory=lambda: {name.removesuffix("_ms") + "_count": 0 for name in _DB_FIELDS}
    )
    statements: dict = field(default_factory=dict)
    incomplete_connects: int = 0
    queue_supported: bool | None = None
    queue_reason: str | None = None
    java_bridge_calls: list = field(default_factory=list)
    java_bridge_calls_overflow: int = 0

    def add(self, name, elapsed, *, database=False):
        if self.closed:
            return
        target = self.db if database else self.phases
        target[name] = target.get(name, 0.0) + max(0.0, elapsed * 1000)
        if database:
            count = name.removesuffix("_ms") + "_count"
            self.counts[count] = self.counts.get(count, 0) + 1

    def record_java_bridge_call(
        self, *, bridge_request_id, operation, upstream_http_status, upstream_code, error_type, elapsed_ms
    ):
        """Keep only bounded fixed metadata; a failed observation is silently omitted."""
        try:
            if self.closed:
                return
            if len(self.java_bridge_calls) >= 4:
                self.java_bridge_calls_overflow = min(self.java_bridge_calls_overflow + 1, 3600000)
                return
            if not isinstance(bridge_request_id, str) or len(bridge_request_id) != 36:
                return
            canonical = str(UUID(bridge_request_id))
            if canonical != bridge_request_id:
                return
            if type(elapsed_ms) not in (int, float) or not math.isfinite(elapsed_ms) or not 0 <= elapsed_ms <= 3600000:
                return
            status = upstream_http_status
            if type(status) is not int or not 100 <= status <= 599:
                status = None
            if type(upstream_code) is int and upstream_code in JAVA_BRIDGE_NUMERIC_CODES:
                code = upstream_code
            elif type(upstream_code) is str and upstream_code in JAVA_BRIDGE_STRING_CODES:
                code = upstream_code
            else:
                code = "OTHER"
            self.java_bridge_calls.append(
                {
                    "bridge_request_id": canonical,
                    "operation": operation if operation in JAVA_BRIDGE_OPERATIONS else "OTHER",
                    "upstream_http_status": status,
                    "upstream_code": code,
                    "error_type": error_type if error_type in JAVA_BRIDGE_ERROR_TYPES else "HTTP_ERROR_OTHER",
                    "elapsed_ms": round(elapsed_ms, 3),
                }
            )
        except Exception:
            # Optional diagnostics never replace a business response or exception.
            return

    def header(self, operation, status_code, streaming=False):
        elapsed = max(0, (time.perf_counter() - self.started) * 1000)
        self.closed = True  # A streaming child task must not keep collecting.
        database = {name: round(value, 3) for name, value in self.db.items()}
        database.update(self.counts)
        database["pool_queue_supported"] = bool(self.queue_supported)
        database["pool_queue_reason"] = self.queue_reason if self.queue_supported is False else None
        if self.queue_supported is None:
            database["pool_queue_reason"] = "not_observed"
        if not self.queue_supported:
            database["pool_queue_ms"] = None
        database["physical_connect_incomplete_count"] = self.incomplete_connects
        if self.incomplete_connects:
            database["checkout_residual_ms"] = None
            database["physical_connect_ms"] = None
        payload = {
            "schema_version": 1,
            "run_id": self.run_id,
            "request_id": self.request_id,
            "operation": operation
            if isinstance(operation, str) and _OPERATION.fullmatch(operation)
            else None,
            "status_code": status_code,
            "streaming": streaming,
            "total_ms": round(elapsed, 3),
            "handler_ms": round(self.phases.get("handler_ms", 0), 3),
            "metrics_write_ms": round(self.phases.get("metrics_write_ms", 0), 3),
            "java_bridge_ms": round(self.phases.get("java_bridge_ms", 0), 3),
            "db": database,
            "scope": "response_headers_ready",
            "db_includes_metrics": True,
            "sql_includes_implicit_begin": True,
            "checkout_residual_includes_setup": True,
            "pool_queue_in_checkout_residual": True,
        }
        if self.java_bridge_calls or self.java_bridge_calls_overflow:
            payload["java_bridge_calls"] = self.java_bridge_calls
            payload["java_bridge_calls_overflow"] = self.java_bridge_calls_overflow
        result = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        return result if len(result) <= 4096 else None


_current: ContextVar[RequestTiming | None] = ContextVar("request_performance_timing", default=None)


def current_timing():
    timing = _current.get()
    return timing if timing is not None and not timing.closed else None


def start_request(enabled, run_id, request_id):
    if not enabled or not isinstance(run_id, str) or len(run_id) > 36:
        return None, None
    try:
        run_id = str(UUID(run_id))
        request_id = str(UUID(request_id))
    except (ValueError, TypeError, AttributeError):
        return None, None
    timing = RequestTiming(run_id, request_id)
    return timing, _current.set(timing)


def finish_request(timing, token):
    if timing is not None:
        timing.closed = True
    if token is not None:
        _current.reset(token)


@contextmanager
def phase(name):
    timing = current_timing()
    started = time.perf_counter() if timing else None
    try:
        yield
    finally:
        if timing is not None:
            timing.add(name, time.perf_counter() - started)


@dataclass
class Acquisition:
    timing: RequestTiming
    started: float = field(default_factory=time.perf_counter)
    physical_ms: float = 0
    connects: dict = field(default_factory=dict)
    token: object = None
    finished: bool = False


_acquiring: ContextVar[Acquisition | None] = ContextVar("performance_pool_acquisition", default=None)


def start_acquisition(queue_supported=False, queue_reason="engine_not_instrumented"):
    timing = current_timing()
    if timing is None:
        return None
    if timing.queue_supported is None or not queue_supported:
        timing.queue_supported = queue_supported
        timing.queue_reason = None if queue_supported else queue_reason
    acquisition = Acquisition(timing)
    acquisition.token = _acquiring.set(acquisition)
    return acquisition


def finish_acquisition(acquisition):
    if acquisition is None or acquisition.finished:
        return
    acquisition.finished = True
    elapsed = time.perf_counter() - acquisition.started
    acquisition.timing.add("acquire_ms", elapsed, database=True)
    acquisition.timing.add(
        "checkout_residual_ms", max(0, elapsed - acquisition.physical_ms / 1000), database=True
    )
    acquisition.timing.incomplete_connects += len(acquisition.connects)
    _acquiring.reset(acquisition.token)


def add_database_time(name, started):
    timing = current_timing()
    if timing is not None:
        timing.add(name, time.perf_counter() - started, database=True)


class TimingQueue(AsyncAdaptedQueue):
    """Measure the existing queue get without changing its wait/timeout behavior."""

    def get(self, block=True, timeout=None):
        acquisition = _acquiring.get()
        if (
            not block
            or acquisition is None
            or acquisition.finished
            or current_timing() is not acquisition.timing
        ):
            return super().get(block, timeout)
        started = time.perf_counter()
        try:
            return super().get(block, timeout)
        finally:
            acquisition.timing.add("pool_queue_ms", time.perf_counter() - started, database=True)


class TimingPool(AsyncAdaptedQueuePool):
    # _queue_class is a private SQLAlchemy extension point. Only the pinned,
    # inspected version is enabled below; a dependency upgrade must be validated
    # before it can produce queue measurements. All pool decisions stay upstream.
    _queue_class = TimingQueue


def queue_timing_configuration(enabled):
    if not enabled:
        return {}, "disabled"
    if sqlalchemy_version != "2.1.8":
        return {}, "sqlalchemy_version_unsupported"
    if AsyncAdaptedQueuePool._queue_class is not AsyncAdaptedQueue:
        return {}, "queue_adapter_unsupported"
    return {"poolclass": TimingPool}, None


def queue_timing_supported(engine):
    return isinstance(getattr(engine, "pool", None), TimingPool)


def install_engine_timing(engine):
    # No driver/pool arguments or execution semantics are changed. These public
    # SQLAlchemy hooks do no work beyond reading a ContextVar when timing is off.
    target = engine.sync_engine

    def connecting(dialect, record, args, kwargs):
        acquisition = _acquiring.get()
        if acquisition is not None and not acquisition.finished and current_timing() is acquisition.timing:
            acquisition.connects[id(record)] = time.perf_counter()

    def connected(connection, record):
        acquisition = _acquiring.get()
        if acquisition is None or acquisition.finished:
            return
        started = acquisition.connects.pop(id(record), None)
        if started is not None:
            elapsed = time.perf_counter() - started
            acquisition.physical_ms += elapsed * 1000
            acquisition.timing.add("physical_connect_ms", elapsed, database=True)

    def executing(connection, cursor, statement, parameters, context, many):
        timing = current_timing()
        if timing is not None:
            timing.statements[id(context)] = time.perf_counter()

    def executed(connection, cursor, statement, parameters, context, many):
        timing = current_timing()
        if timing is not None:
            started = timing.statements.pop(id(context), None)
            if started is not None:
                timing.add("sql_execute_ms", time.perf_counter() - started, database=True)

    def failed(context):
        executed(None, None, None, None, context.execution_context, False)

    event.listen(target, "do_connect", connecting)
    event.listen(target, "connect", connected)
    event.listen(target, "before_cursor_execute", executing)
    event.listen(target, "after_cursor_execute", executed)
    event.listen(target, "handle_error", failed)
