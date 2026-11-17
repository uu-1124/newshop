import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI

from app.api import member as member_api
from app.api import staff_events as staff_api
from app.application import expiry, governance
from app.domain.identity import DomainError, Principal
from app.infrastructure import stream_observation

PRIVATE = "private-token-message-path-identity-content"


def request_stub(enabled=True, headers=None):
    svc = SimpleNamespace(bridge=SimpleNamespace(settings=SimpleNamespace(performance_timing_enabled=enabled)))
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(services=svc)),
        headers=headers
        if headers is not None
        else {"X-Performance-Run": str(uuid4()), "X-Request-ID": str(uuid4()), "Authorization": PRIVATE},
    )


def observations(caplog):
    return [json.loads(record.getMessage()) for record in caplog.records if record.name == stream_observation.__name__]


@pytest.mark.parametrize("operation", ["MEMBER_EVENTS", "STAFF_EVENTS"])
@pytest.mark.parametrize(
    "code",
    [
        "AUTH_EXPIRED",
        "AUTH_REQUIRED",
        "FORBIDDEN",
        "JAVA_UNAVAILABLE",
        "JAVA_BRIDGE_REJECTED",
        "EVENT_CURSOR_EXPIRED",
        "INVALID_CURSOR",
        "NOT_FOUND",
    ],
)
def test_only_fixed_termination_fields_are_logged(operation, code, caplog):
    request = request_stub()
    error = DomainError(code, PRIVATE, 503)
    stream_observation.observe_stream_domain_end(request, operation, error)
    assert observations(caplog) == [
        {
            "event": "SSE_DOMAIN_END",
            "run_id": request.headers["X-Performance-Run"],
            "request_id": request.headers["X-Request-ID"],
            "operation": operation,
            "code": code,
            "status": 503,
        }
    ]
    assert PRIVATE not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("enabled", [False, None, 0, 1, "true"])
def test_disabled_or_nonboolean_flag_does_not_read_headers_or_error(enabled, monkeypatch):
    accesses = []

    class HeaderGuard:
        def get(self, name):
            accesses.append(name)
            raise RuntimeError(PRIVATE)

    class ErrorGuard:
        @property
        def code(self):
            accesses.append("error.code")
            raise RuntimeError(PRIVATE)

    warning = Mock()
    monkeypatch.setattr(stream_observation.log, "warning", warning)
    request = request_stub(enabled, HeaderGuard())
    stream_observation.observe_stream_domain_end(request, "MEMBER_EVENTS", ErrorGuard())
    assert accesses == []
    warning.assert_not_called()


@pytest.mark.parametrize("header", ["X-Performance-Run", "X-Request-ID"])
@pytest.mark.parametrize("invalid", [None, "", "1-1-1-1-1", "A1B2C3D4-1111-4222-8333-0123456789AB", PRIVATE])
def test_both_canonical_request_headers_are_required(header, invalid, caplog):
    request = request_stub()
    if invalid is None:
        request.headers.pop(header)
    else:
        request.headers[header] = invalid
    stream_observation.observe_stream_domain_end(request, "MEMBER_EVENTS", DomainError("FORBIDDEN", PRIVATE, 403))
    assert observations(caplog) == [] and PRIVATE not in caplog.text


@pytest.mark.parametrize("operation", [PRIVATE, None, [PRIVATE], "memberEvents"])
def test_unknown_operation_is_not_logged(operation, caplog):
    stream_observation.observe_stream_domain_end(request_stub(), operation, DomainError("FORBIDDEN", PRIVATE, 403))
    assert observations(caplog) == []


@pytest.mark.parametrize("code", [PRIVATE, {"secret": PRIVATE}, [PRIVATE], None, 403])
@pytest.mark.parametrize("status", [None, True, "503", 503.0, 99, 600, PRIVATE])
def test_unknown_code_is_fixed_other_and_invalid_status_is_null(code, status, caplog):
    stream_observation.observe_stream_domain_end(request_stub(), "STAFF_EVENTS", DomainError(code, PRIVATE, status))
    values = observations(caplog)
    assert len(values) == 1 and values[0]["code"] == "OTHER" and values[0]["status"] is None
    assert set(values[0]) == {"event", "run_id", "request_id", "operation", "code", "status"}
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize("status", [100, 200, 401, 410, 503, 599])
def test_legal_status_is_preserved_without_changing_error(status, caplog):
    error = DomainError("NOT_FOUND", PRIVATE, status)
    stream_observation.observe_stream_domain_end(request_stub(), "STAFF_EVENTS", error)
    assert observations(caplog)[0]["status"] == status
    assert (error.code, error.message, error.status) == ("NOT_FOUND", PRIVATE, status)


@pytest.mark.parametrize("fault", ["logger", "json", "headers", "settings", "error"])
def test_diagnostic_failures_are_omitted_without_changing_the_original_error(fault, monkeypatch):
    request = request_stub()
    error = DomainError("JAVA_UNAVAILABLE", PRIVATE, 503)
    warning = Mock()
    monkeypatch.setattr(stream_observation.log, "warning", warning)

    def fail(*args, **kwargs):
        raise RuntimeError(PRIVATE)

    if fault == "logger":
        warning.side_effect = RuntimeError(PRIVATE)
    elif fault == "json":
        monkeypatch.setattr(stream_observation, "json", SimpleNamespace(dumps=fail))
    elif fault == "headers":
        request.headers = SimpleNamespace(get=fail)
    elif fault == "settings":
        request.app.state.services.bridge = SimpleNamespace()
    else:
        error = SimpleNamespace(status=503)
    stream_observation.observe_stream_domain_end(request, "MEMBER_EVENTS", error)
    assert warning.call_count == (1 if fault == "logger" else 0)
    if isinstance(error, DomainError):
        assert (error.code, error.message, error.status) == ("JAVA_UNAVAILABLE", PRIVATE, 503)


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["member", "staff"])
@pytest.mark.parametrize("scenario", ["domain", "disabled", "logger_failure", "unhandled"])
async def test_real_stream_generator_keeps_domain_end_and_other_exception_behavior(
    monkeypatch, caplog, kind, scenario
):
    """Execute the real registered generators using isolated service/DB stubs, not an AST copy."""
    settings = SimpleNamespace(performance_timing_enabled=scenario != "disabled")
    failure = RuntimeError(PRIVATE) if scenario == "unhandled" else DomainError("JAVA_UNAVAILABLE", PRIVATE, 503)
    identity = AsyncMock(side_effect=failure)
    staff_identity = SimpleNamespace(authenticate=AsyncMock(side_effect=failure))
    transactions = []

    @asynccontextmanager
    async def transaction(**options):
        transactions.append(options)
        yield object()

    svc = SimpleNamespace(
        db=SimpleNamespace(transaction=transaction),
        bridge=SimpleNamespace(settings=settings, identity=identity),
        extensions=SimpleNamespace(staff_identity=SimpleNamespace(require=lambda: staff_identity)),
        notifications=SimpleNamespace(wait=AsyncMock()),
    )
    app = FastAPI()
    app.state.services = svc
    request = SimpleNamespace(
        app=app,
        headers={"X-Performance-Run": str(uuid4()), "X-Request-ID": str(uuid4()), "Authorization": PRIVATE},
        is_disconnected=AsyncMock(return_value=False),
    )
    monkeypatch.setattr(governance, "read_policy", AsyncMock(return_value={"value": {"heartbeat_seconds": 30}}))
    if kind == "member":
        member_api.register_member_routes(app, settings)
        monkeypatch.setattr(member_api, "owned", AsyncMock())
        monkeypatch.setattr(member_api, "one", AsyncMock(return_value={"replay_floor": 0, "last_event_id": 0}))
        monkeypatch.setattr(member_api, "rows", AsyncMock(return_value=[]))
        monkeypatch.setattr(expiry, "filter_events", AsyncMock(return_value=[]))
        clock_values = iter([0.0, 0.0, 31.0])
        monkeypatch.setattr(member_api, "time", SimpleNamespace(monotonic=lambda: next(clock_values)))
        endpoint = next(route.endpoint for route in app.routes if getattr(route, "operation_id", None) == "memberEvents")
        response = await endpoint(
            uuid4(), request, principal=Principal(1, "test"), after=None, last_event_id=None
        )
    else:
        staff_api.register_staff_events(app)
        monkeypatch.setattr(staff_api, "staff_event_batch", AsyncMock(return_value=([], 0)))
        clock_values = iter([0.0, 11.0])
        monkeypatch.setattr(staff_api, "time", SimpleNamespace(monotonic=lambda: next(clock_values)))
        principal = Principal(1, "test", "STAFF", frozenset({"agent:events:subscribe", "agent:conversation:read"}))
        endpoint = next(route.endpoint for route in app.routes if getattr(route, "operation_id", None) == "adminEvents")
        response = await endpoint(request, principal=principal, conversation_id=None, after=None, last_event_id=None)
    if scenario == "logger_failure":
        def fail_log(*args, **kwargs):
            raise RuntimeError(PRIVATE)

        monkeypatch.setattr(stream_observation.log, "warning", fail_log)

    assert response.status_code == 200 and response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    if scenario == "unhandled":
        with pytest.raises(RuntimeError) as raised:
            _ = [chunk async for chunk in response.body_iterator]
        assert raised.value is failure
    else:
        assert [chunk async for chunk in response.body_iterator] == []
    values = observations(caplog)
    if scenario == "domain":
        assert len(values) == 1
        assert values[0]["operation"] == ("MEMBER_EVENTS" if kind == "member" else "STAFF_EVENTS")
        assert values[0]["run_id"] == request.headers["X-Performance-Run"]
        assert values[0]["request_id"] == request.headers["X-Request-ID"]
        assert values[0]["code"] == "JAVA_UNAVAILABLE" and values[0]["status"] == 503
    else:
        assert values == []
    svc.notifications.wait.assert_not_awaited()
    if kind == "member":
        identity.assert_awaited_once_with(PRIVATE)
        staff_identity.authenticate.assert_not_awaited()
        assert transactions == [{"snapshot": True}, {}]
    else:
        staff_identity.authenticate.assert_awaited_once_with(PRIVATE)
        identity.assert_not_awaited()
        assert transactions == [{}]
    assert PRIVATE not in caplog.text
