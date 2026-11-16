"""Fixed-arrival, read-only query measurement with thirteen independent SSE streams.

The public plan is reusable across A1/B1/B2/A2. Authorization is read only from a
separate private file and is never copied to results. This runner does not log in,
create fixtures, send messages, retry requests, or change a service configuration.
"""

import argparse
import asyncio
import hashlib
import json
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx

NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
HEX = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
CID = r"[0-9a-fA-F-]{36}"
READ_PATH = re.compile(rf"^/mall-agent/v1/(member|admin)/(orders|conversations(?:/{CID}(?:/messages)?)?)$")
MEMBER_STREAM = re.compile(rf"^/mall-agent/v1/member/conversations/({CID})/events$")
ADMIN_STREAM = re.compile(r"^/mall-agent/v1/admin/events$")
DB_TIMINGS = (
    "acquire",
    "physical_connect",
    "checkout_residual",
    "sql_execute",
    "transaction_end",
    "isolation_config",
    "connection_release",
    "pool_queue",
)
QUEUE_REASONS = {
    None,
    "not_observed",
    "disabled",
    "engine_not_instrumented",
    "sqlalchemy_version_unsupported",
    "queue_adapter_unsupported",
}


class PlanError(ValueError):
    """A public configuration or sanitized evidence invariant was rejected."""


def bounded_number(value, low, high, label, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanError(label)
    if not math.isfinite(value) or not low <= value <= high or (integer and type(value) is not int):
        raise PlanError(label)
    return value


def safe_name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise PlanError("Invalid public name")
    return value


def relative_path(value, stream=False):
    if not isinstance(value, str) or len(value) > 2048:
        raise PlanError("Invalid read-only path")
    parsed = urlsplit(value)
    allowed = (
        (MEMBER_STREAM.fullmatch(parsed.path) or ADMIN_STREAM.fullmatch(parsed.path))
        if stream
        else READ_PATH.fullmatch(parsed.path)
    )
    if parsed.scheme or parsed.netloc or parsed.fragment or not allowed:
        raise PlanError("Only the registered read-only query paths are accepted")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if stream:
        if set(query) - {"after", "conversation_id"}:
            raise PlanError("Unexpected stream query fields")
    elif set(query) - {"limit", "bucket", "before_seq", "query", "order_sn", "status"}:
        raise PlanError("Unexpected read query fields")
    if any(len(values) != 1 for values in query.values()):
        raise PlanError("Duplicate query fields")
    return value


@dataclass(frozen=True)
class Route:
    name: str
    actor_key: str
    path: str
    expected_operation: str
    weight: int = 1

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
            raise PlanError("Invalid route definition")
        try:
            result = cls(**value)
        except TypeError:
            raise PlanError("Incomplete route definition") from None
        safe_name(result.name)
        safe_name(result.actor_key)
        relative_path(result.path)
        if not isinstance(result.expected_operation, str) or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9]{0,99}", result.expected_operation
        ):
            raise PlanError("Invalid operation name")
        bounded_number(result.weight, 1, 100, "Invalid route weight", integer=True)
        return result


@dataclass(frozen=True)
class Stream:
    name: str
    actor_key: str
    path: str
    expected_owner_cid: str | None = None

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
            raise PlanError("Invalid stream definition")
        try:
            result = cls(**value)
        except TypeError:
            raise PlanError("Incomplete stream definition") from None
        safe_name(result.name)
        safe_name(result.actor_key)
        relative_path(result.path, stream=True)
        if result.expected_owner_cid is not None:
            if str(UUID(result.expected_owner_cid)) != result.expected_owner_cid:
                raise PlanError("Noncanonical stream owner ID")
            match = MEMBER_STREAM.fullmatch(urlsplit(result.path).path)
            if match and match[1] != result.expected_owner_cid:
                raise PlanError("Stream owner differs from path")
        return result


def validate_plan(value):
    required = {
        "schema_version",
        "base_url",
        "rate_per_second",
        "samples_per_route",
        "warmup_seconds",
        "max_in_flight",
        "timeout_seconds",
        "max_start_lag_seconds",
        "routes",
        "streams",
    }
    if not isinstance(value, dict) or set(value) != required or value["schema_version"] != 1:
        raise PlanError("Invalid public plan schema")
    if not isinstance(value["base_url"], str) or len(value["base_url"]) > 256:
        raise PlanError("Invalid benchmark base URL")
    base = urlsplit(value["base_url"])
    if base.scheme not in {"http", "https"} or base.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise PlanError("This registered benchmark accepts loopback targets only")
    if base.username or base.password or base.path not in {"", "/"} or base.query or base.fragment:
        raise PlanError("Invalid benchmark base URL")
    bounded_number(value["rate_per_second"], 0.05, 100, "Invalid arrival rate")
    bounded_number(value["samples_per_route"], 1000, 1000000, "At least 1000 samples per route", True)
    bounded_number(value["warmup_seconds"], 0, 3600, "Invalid warmup")
    bounded_number(value["max_in_flight"], 1, 256, "Invalid concurrency limit", True)
    bounded_number(value["timeout_seconds"], 1, 120, "Invalid timeout")
    bounded_number(value["max_start_lag_seconds"], 0.01, 30, "Invalid arrival lag limit")
    if not isinstance(value["routes"], list) or not 1 <= len(value["routes"]) <= 20:
        raise PlanError("Invalid route count")
    routes = [Route.parse(x) for x in value["routes"]]
    if len({x.name for x in routes}) != len(routes):
        raise PlanError("Duplicate route names")
    if not isinstance(value["streams"], list) or len(value["streams"]) != 13:
        raise PlanError("Exactly thirteen authenticated streams are required")
    streams = [Stream.parse(x) for x in value["streams"]]
    if len({x.name for x in streams}) != 13 or len({(x.actor_key, x.path) for x in streams}) != 13:
        raise PlanError("Duplicate stream identity")
    members = [x for x in streams if MEMBER_STREAM.fullmatch(urlsplit(x.path).path)]
    if len(members) != 10 or len({x.actor_key for x in members}) != 10:
        raise PlanError("Ten distinct member streams and three staff streams are required")
    staff = [x for x in streams if ADMIN_STREAM.fullmatch(urlsplit(x.path).path)]
    if len(staff) != 3 or len({x.actor_key for x in staff}) != 3:
        raise PlanError("Three distinct staff identities are required")
    if {x.actor_key for x in members} & {x.actor_key for x in staff}:
        raise PlanError("Member and staff stream actors must be distinct")
    return routes, streams


def load_authorizations(path, actor_keys):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "actors"}
        or value["schema_version"] != 1
    ):
        raise PlanError("Invalid private authorization schema")
    if not isinstance(value["actors"], dict):
        raise PlanError("Invalid private actor mapping")
    result = {}
    for key in actor_keys:
        entry = value["actors"].get(key)
        if not isinstance(entry, dict) or set(entry) != {"authorization"}:
            raise PlanError("Missing private actor authorization")
        token = entry["authorization"]
        if not isinstance(token, str) or not token.startswith("Bearer ") or not 8 < len(token) < 16384:
            raise PlanError("Invalid actor authorization")
        if "\n" in token or "\r" in token:
            raise PlanError("Invalid actor authorization")
        result[key] = token
    return result


def route_sequence(routes, minimum):
    """Preserve exact integer proportions; the least frequent route meets minimum."""
    repeats = math.ceil(minimum / min(route.weight for route in routes))
    cycle = tuple(route for route in routes for _ in range(route.weight))
    return cycle, repeats * len(cycle)


def percentile(values, quantile):
    """Nearest-rank percentile, recomputed from raw samples rather than percentiles."""
    ordered = sorted(values)
    if not ordered:
        return None
    return round(ordered[max(0, math.ceil(len(ordered) * quantile) - 1)], 3)


def latency_summary(values):
    return {"samples": len(values), **{f"p{q}": percentile(values, q / 100) for q in (50, 95, 99)}}


@dataclass(frozen=True)
class Arrival:
    sequence: int
    route: Route
    scheduled: float
    started: float
    warmup: bool


async def run_arrivals(
    cycle: tuple[Route, ...],
    count: int,
    rate: float,
    max_in_flight: int,
    max_start_lag: float,
    execute: Callable[[Arrival], Awaitable[None]],
    on_drop: Callable[[Arrival, str], None],
    *,
    warmup=False,
    clock=time.monotonic,
    sleep=asyncio.sleep,
    stop=None,
):
    """The arrival clock never waits for a request. Every launched task is drained."""
    started = clock()
    deadline = started + count / rate
    pending = set()
    all_tasks = []
    dispatched = dropped = maximum = considered = 0

    def done(task):
        if stop is not None and (task.cancelled() or task.exception() is not None):
            stop.set()

    try:
        for index in range(count):
            target = started + index / rate
            await sleep(max(0, target - clock()))
            if stop is not None and stop.is_set():
                break
            now = clock()
            arrival = Arrival(index, cycle[index % len(cycle)], target, now, warmup)
            considered += 1
            pending = {task for task in pending if not task.done()}
            reason = "schedule_lag" if now - target > max_start_lag else None
            if len(pending) >= max_in_flight:
                reason = "in_flight_limit"
            if reason:
                dropped += 1
                on_drop(arrival, reason)
                continue
            task = asyncio.create_task(execute(arrival))
            task.add_done_callback(done)
            all_tasks.append(task)
            pending.add(task)
            dispatched += 1
            maximum = max(maximum, len(pending))
        if considered == count:
            await sleep(max(0, deadline - clock()))
    finally:
        # Includes requests whose response arrives after the scheduled deadline.
        outcomes = await asyncio.gather(*all_tasks, return_exceptions=True)
    return {
        "scheduled": considered,
        "dispatched": dispatched,
        "completed": len(outcomes),
        "dropped": dropped,
        "aborted": considered != count,
        "offered_seconds": round(count / rate if considered == count else clock() - started, 6),
        "drain_seconds": round(max(0, clock() - deadline), 6),
        "max_in_flight_observed": maximum,
        "task_errors": dict(Counter(type(x).__name__ for x in outcomes if isinstance(x, BaseException))),
    }


class Evidence:
    def __init__(self, path, anchor_mono=None, anchor_utc=None):
        self.path = Path(path)
        self.handle = self.path.open("x", encoding="utf-8", newline="\n")
        self.anchor_mono = time.monotonic() if anchor_mono is None else anchor_mono
        self.anchor_utc = datetime.now(UTC) if anchor_utc is None else anchor_utc
        self.records = []

    def timestamp(self, mono):
        return (self.anchor_utc + timedelta(seconds=mono - self.anchor_mono)).isoformat()

    def write(self, record):
        self.handle.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
        self.handle.flush()
        self.records.append(record)

    def close(self):
        self.handle.close()


def parse_performance_timing(raw, run_id, request_id, operation, status):
    if raw is None:
        return None, "unobserved"
    if len(raw) > 16384:
        return None, "invalid"
    try:
        value = json.loads(raw)
        required = {
            "schema_version",
            "run_id",
            "request_id",
            "operation",
            "status_code",
            "total_ms",
            "handler_ms",
            "metrics_write_ms",
            "java_bridge_ms",
            "db",
            "streaming",
            "scope",
            "db_includes_metrics",
            "sql_includes_implicit_begin",
            "checkout_residual_includes_setup",
        }
        if (
            not isinstance(value, dict)
            or not required <= set(value)
            or set(value) - required - {"pool_queue_in_checkout_residual"}
            or value["schema_version"] != 1
        ):
            raise ValueError()
        if (value["run_id"], value["request_id"], value["operation"], value["status_code"]) != (
            run_id,
            request_id,
            operation,
            status,
        ):
            raise ValueError()
        if value["streaming"] is not False or value["scope"] != "response_headers_ready":
            raise ValueError()
        if any(
            value[key] is not True
            for key in (
                "db_includes_metrics",
                "sql_includes_implicit_begin",
                "checkout_residual_includes_setup",
            )
        ):
            raise ValueError()
        if (
            "pool_queue_in_checkout_residual" in value
            and value["pool_queue_in_checkout_residual"] is not True
        ):
            raise ValueError()
        for key in ("total_ms", "handler_ms", "metrics_write_ms", "java_bridge_ms"):
            bounded_number(value[key], 0, 3600000, "Invalid timing")
        db = value["db"]
        allowed = {name + suffix for name in DB_TIMINGS for suffix in ("_ms", "_count")} | {
            "pool_queue_supported",
            "pool_queue_reason",
            "physical_connect_incomplete_count",
        }
        if not isinstance(db, dict) or set(db) != allowed:
            raise ValueError()
        queue_supported = db["pool_queue_supported"]
        if type(queue_supported) is not bool:
            raise ValueError()
        if db["pool_queue_reason"] not in QUEUE_REASONS:
            raise ValueError()
        if queue_supported and db["pool_queue_reason"] is not None:
            raise ValueError()
        incomplete = db["physical_connect_incomplete_count"]
        bounded_number(incomplete, 0, 3600000, "Invalid timing", integer=True)
        if db["pool_queue_ms"] is not None:
            bounded_number(db["pool_queue_ms"], 0, 3600000, "Invalid timing")
            if not queue_supported:
                raise ValueError()
        elif queue_supported:
            raise ValueError()
        for key, number in db.items():
            if key in {"pool_queue_ms", "pool_queue_supported", "pool_queue_reason"}:
                continue
            if key in {"physical_connect_ms", "checkout_residual_ms"} and number is None and incomplete:
                continue
            bounded_number(number, 0, 3600000, "Invalid timing", integer=key.endswith("_count"))
        return value, "observed"
    except (ValueError, TypeError, KeyError, PlanError, json.JSONDecodeError):
        return None, "invalid"


class Trace:
    """Only names/timestamps are retained; httpcore event info can contain secrets."""

    def __init__(self):
        self.started = time.monotonic()
        self.events = []
        self.overflow = 0

    async def __call__(self, name, info):
        if not re.fullmatch(r"[a-z0-9_.]{1,100}", name):
            return
        if len(self.events) >= 64:
            self.overflow += 1
            return
        self.events.append({"name": name, "at_ms": round((time.monotonic() - self.started) * 1000, 3)})

    def result(self):
        def elapsed(prefix):
            start, total, found = None, 0.0, False
            for item in self.events:
                if item["name"] == prefix + ".started":
                    start = item["at_ms"]
                elif item["name"] in {prefix + ".complete", prefix + ".failed"} and start is not None:
                    total += max(0, item["at_ms"] - start)
                    found, start = True, None
            return round(total, 3) if found else None

        return {
            "events": self.events,
            "overflow": self.overflow,
            "dns_ms": None,
            "pool_queue_ms": None,
            "connect_tcp_ms": elapsed("connection.connect_tcp"),
            "tls_ms": elapsed("connection.start_tls"),
            "response_headers_ms": elapsed("http11.receive_response_headers"),
            "response_body_ms": elapsed("http11.receive_response_body"),
            "note": "Event info is omitted. Connect includes backend DNS when applicable; absent events are unobserved, not zero.",
        }


class StreamMonitor:
    def __init__(self, base_url, streams, auth, evidence, stop, timeout, run_id=None):
        self.base_url, self.streams, self.auth = base_url, streams, auth
        self.evidence, self.stop, self.timeout = evidence, stop, timeout
        self.run_id = str(UUID(run_id)) if run_id is not None else None
        self.stage = "startup"
        self.closing = False
        self.tasks = []
        self.monitor = None
        self.states = {
            s.name: {
                "connected": False,
                "connections": 0,
                "events": 0,
                "duplicate_events": 0,
                "unexpected_disconnects": 0,
                "error": None,
                "request_id": None,
                "lines_received": 0,
                "last_line_at": None,
                "maximum_line_gap_ms": 0.0,
            }
            for s in streams
        }
        self.ready = {s.name: asyncio.Event() for s in streams}

    async def consume(self, stream):
        state = self.states[stream.name]
        seen = set()
        try:
            timeout = httpx.Timeout(
                max(90, self.timeout * 2), connect=self.timeout, write=self.timeout, pool=self.timeout
            )
            request_id = str(uuid4())
            state["request_id"] = request_id
            headers = {"Authorization": self.auth[stream.actor_key], "X-Request-ID": request_id}
            if self.run_id is not None:
                headers["X-Performance-Run"] = self.run_id
            async with httpx.AsyncClient(
                base_url=self.base_url, trust_env=False, timeout=timeout, follow_redirects=False
            ) as client:
                async with client.stream("GET", stream.path, headers=headers) as response:
                    if response.status_code != 200 or not response.headers.get("content-type", "").startswith(
                        "text/event-stream"
                    ):
                        state["error"] = "stream_response_rejected"
                        self.stop.set()
                        return
                    state["connections"] += 1
                    state["connected"] = True
                    self.ready[stream.name].set()
                    last_line = time.monotonic()
                    event_id, data, data_size = None, [], 0
                    async for line in response.aiter_lines():
                        now = time.monotonic()
                        state["lines_received"] += 1
                        state["last_line_at"] = datetime.now(UTC).isoformat()
                        state["maximum_line_gap_ms"] = round(
                            max(state["maximum_line_gap_ms"], (now - last_line) * 1000), 3
                        )
                        last_line = now
                        if len(line) > 262144:
                            state["error"] = "stream_event_too_large"
                            self.stop.set()
                            return
                        if line.startswith("id:"):
                            event_id = line[3:].strip()
                        elif line.startswith("data:"):
                            data.append(line[5:].strip())
                            data_size += len(line)
                            if data_size > 262144:
                                state["error"] = "stream_event_too_large"
                                self.stop.set()
                                return
                        elif not line:
                            if event_id:
                                if not re.fullmatch(r"[0-9]{1,19}", event_id) or event_id in seen:
                                    state["duplicate_events"] += 1
                                    state["error"] = "duplicate_or_invalid_event_id"
                                    self.stop.set()
                                    return
                                seen.add(event_id)
                                state["events"] += 1
                                if len(seen) > 100000:
                                    state["error"] = "stream_event_limit"
                                    self.stop.set()
                                    return
                                if stream.expected_owner_cid is not None:
                                    payload = json.loads("\n".join(data))
                                    if payload.get("conversation_id") != stream.expected_owner_cid:
                                        state["error"] = "stream_owner_mismatch"
                                        self.stop.set()
                                        return
                            event_id, data, data_size = None, [], 0
                    if not self.closing:
                        state["unexpected_disconnects"] += 1
                        state["error"] = "unexpected_stream_end"
                        self.stop.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            state["error"] = "stream_transport_or_protocol_error"
            self.stop.set()
        finally:
            state["connected"] = False
            self.ready[stream.name].set()

    def sample(self):
        self.evidence.write(
            {
                "record_type": "streams",
                "stage": self.stage,
                "at": datetime.now(UTC).isoformat(),
                "online": sum(x["connected"] for x in self.states.values()),
                "required": 13,
            }
        )

    async def sample_loop(self):
        while not self.closing:
            self.sample()
            await asyncio.sleep(5)

    async def start(self):
        self.tasks = [asyncio.create_task(self.consume(s)) for s in self.streams]
        await asyncio.wait_for(
            asyncio.gather(*(x.wait() for x in self.ready.values())), max(45, self.timeout * 2)
        )
        if self.stop.is_set() or not all(x["connected"] for x in self.states.values()):
            raise PlanError("Thirteen authenticated streams did not become ready")
        self.monitor = asyncio.create_task(self.sample_loop())

    def mark(self, stage):
        self.stage = stage
        self.sample()

    async def close(self):
        self.closing = True
        for task in [*self.tasks, self.monitor]:
            if task:
                task.cancel()
        await asyncio.gather(*(x for x in [*self.tasks, self.monitor] if x), return_exceptions=True)

    def summary(self):
        measured = [x for x in self.evidence.records if x["stage"] == "measured"]
        return {
            "required": 13,
            "all_measurement_samples_online": bool(measured) and all(x["online"] == 13 for x in measured),
            "measurement_samples": len(measured),
            "total_connections": sum(x["connections"] for x in self.states.values()),
            "unexpected_disconnects": sum(x["unexpected_disconnects"] for x in self.states.values()),
            "duplicate_events": sum(x["duplicate_events"] for x in self.states.values()),
            "per_stream": self.states,
        }


async def query_request(client, route, arrival, run_id, variant, evidence, stop):
    request_id = str(uuid4())
    trace = Trace()
    started = time.monotonic()
    status = None
    outcome, error, timing, coverage = "error", None, None, "unobserved"
    try:
        response = await client.get(
            route.path,
            headers={"X-Performance-Run": run_id, "X-Request-ID": request_id},
            extensions={"trace": trace},
        )
        status = response.status_code
        if status != 200:
            error = "http_status"
        elif len(response.content) > 2097152:
            error = "response_too_large"
        else:
            try:
                envelope = response.json()
                if not isinstance(envelope, dict) or envelope.get("code") != "OK":
                    error = "api_envelope"
            except ValueError:
                error = "api_envelope"
        timing, coverage = parse_performance_timing(
            response.headers.get("X-Performance-Timing"), run_id, request_id, route.expected_operation, status
        )
        if error is None and coverage == "invalid":
            error = "invalid_timing_header"
        if error is None and variant == "B" and coverage != "observed":
            error = "required_timing_unavailable"
        if error is None:
            outcome = "success"
    except httpx.TimeoutException:
        error = "timeout"
    except httpx.HTTPError:
        error = "transport"
    except asyncio.CancelledError:
        error = "cancelled"
        raise
    except Exception:
        error = "client_internal"
    finally:
        finished = time.monotonic()
        evidence.write(
            {
                "record_type": "request",
                "sequence": arrival.sequence,
                "route": route.name,
                "warmup": arrival.warmup,
                "request_id": request_id,
                "scheduled_at": evidence.timestamp(arrival.scheduled),
                "sent_at": evidence.timestamp(started),
                "finished_at": evidence.timestamp(finished),
                "scheduled_mono": arrival.scheduled,
                "sent_mono": started,
                "finished_mono": finished,
                "scheduling_lag_ms": round((started - arrival.scheduled) * 1000, 3),
                "client_latency_ms": round((finished - started) * 1000, 3),
                "status_code": status,
                "outcome": outcome,
                "error": error,
                "server_timing": timing,
                "timing_state": coverage,
                "client_trace": trace.result(),
            }
        )
        if error:
            stop.set()


def summarize_routes(records, routes, offered_seconds, planned):
    result = {}
    for route in routes:
        subset = [r for r in records if r["route"] == route.name and not r["warmup"]]
        sent = [r for r in subset if r["outcome"] != "dropped"]
        successful = [r for r in sent if r["outcome"] == "success"]
        result[route.name] = {
            "planned": planned[route.name],
            "scheduled": len(subset),
            "dispatched": len(sent),
            "completed": len(sent),
            "successful": len(successful),
            "errors": sum(r["outcome"] == "error" for r in sent),
            "timeouts": sum(r["error"] == "timeout" for r in sent),
            "dropped": len(subset) - len(sent),
            "status_counts": dict(Counter(str(r["status_code"]) for r in sent)),
            "latency_ms": latency_summary([r["client_latency_ms"] for r in sent]),
            "success_latency_ms": latency_summary([r["client_latency_ms"] for r in successful]),
            "timing_coverage": {
                key: sum(r["timing_state"] == key for r in sent)
                for key in ("observed", "unobserved", "invalid")
            },
            "throughput_rps": round(len(successful) / offered_seconds, 6) if offered_seconds else 0,
        }
    return result


async def run_phase(args):
    plan_bytes = args.plan.read_bytes()
    plan = json.loads(plan_bytes.decode("utf-8-sig"))
    routes, streams = validate_plan(plan)
    run_id = str(UUID(args.run_id))
    if not COMMIT.fullmatch(args.source_commit) or not HEX.fullmatch(args.fixture_sha256):
        raise PlanError("Explicit source commit and fixture digest are required")
    if not re.fullmatch(r"A[12]|B[12]", args.label) or args.label[0] != args.variant:
        raise PlanError("Label and variant differ")
    auth = load_authorizations(args.auth_file, {x.actor_key for x in [*routes, *streams]})
    args.output_dir.mkdir(parents=True, exist_ok=False)
    evidence = Evidence(args.output_dir / "requests.jsonl")
    stream_evidence = Evidence(args.output_dir / "streams.jsonl")
    stop = asyncio.Event()
    monitor = StreamMonitor(
        plan["base_url"], streams, auth, stream_evidence, stop, plan["timeout_seconds"], run_id=run_id
    )
    clients = {}
    summary = {
        "schema_version": 1,
        "run_id": run_id,
        "label": args.label,
        "variant": args.variant,
        "source_commit": args.source_commit,
        "fixture_sha256": args.fixture_sha256,
        "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "started_at": datetime.now(UTC).isoformat(),
        "configuration": {
            k: plan[k]
            for k in [
                "base_url",
                "rate_per_second",
                "samples_per_route",
                "warmup_seconds",
                "max_in_flight",
                "timeout_seconds",
                "max_start_lag_seconds",
            ]
        }
        | {"stream_count": 13},
        "result": "FAILED",
        "raw_files": {"requests": "requests.jsonl", "streams": "streams.jsonl"},
        "timing_definitions": {
            "client_latency": "Client GET through complete body and safe response validation; timeout is retained",
            "server": "Response preparation only; DB SQL includes implicit BEGIN when the driver performs it",
            "checkout_residual": "Not pure pool queue time; see server instrumentation definitions",
            "A_missing": "Unobserved phases are null, never zero",
            "load": "Constant scheduled arrival clock; no response-coupled next-arrival delay",
        },
    }
    cycle, count = route_sequence(routes, plan["samples_per_route"])
    planned = dict(Counter(cycle[index % len(cycle)].name for index in range(count)))

    def dropped(arrival, reason):
        now = time.monotonic()
        evidence.write(
            {
                "record_type": "request",
                "sequence": arrival.sequence,
                "route": arrival.route.name,
                "warmup": arrival.warmup,
                "request_id": None,
                "scheduled_at": evidence.timestamp(arrival.scheduled),
                "sent_at": None,
                "finished_at": evidence.timestamp(now),
                "scheduled_mono": arrival.scheduled,
                "sent_mono": None,
                "finished_mono": now,
                "scheduling_lag_ms": round((now - arrival.scheduled) * 1000, 3),
                "client_latency_ms": None,
                "status_code": None,
                "outcome": "dropped",
                "error": reason,
                "server_timing": None,
                "timing_state": "unobserved",
                "client_trace": None,
            }
        )
        stop.set()

    async def execute(arrival):
        await query_request(
            clients[arrival.route.actor_key], arrival.route, arrival, run_id, args.variant, evidence, stop
        )

    async def segment(number, warmup):
        return await run_arrivals(
            cycle,
            number,
            plan["rate_per_second"],
            plan["max_in_flight"],
            plan["max_start_lag_seconds"],
            execute,
            dropped,
            warmup=warmup,
            stop=stop,
        )

    try:
        for actor_key in {r.actor_key for r in routes}:
            clients[actor_key] = httpx.AsyncClient(
                base_url=plan["base_url"],
                trust_env=False,
                timeout=plan["timeout_seconds"],
                follow_redirects=False,
                headers={"Authorization": auth[actor_key]},
                limits=httpx.Limits(
                    max_connections=plan["max_in_flight"], max_keepalive_connections=plan["max_in_flight"]
                ),
            )
        await monitor.start()
        monitor.mark("warmup")
        summary["warmup"] = await segment(math.ceil(plan["warmup_seconds"] * plan["rate_per_second"]), True)
        if summary["warmup"]["task_errors"]:
            stop.set()
        if not stop.is_set():
            monitor.mark("measured")
            summary["measurement"] = await segment(count, False)
            monitor.mark("drained")
        else:
            summary["measurement"] = {
                "scheduled": 0,
                "dispatched": 0,
                "completed": 0,
                "dropped": 0,
                "aborted": True,
                "offered_seconds": 0,
                "drain_seconds": 0,
                "max_in_flight_observed": 0,
                "task_errors": {},
            }
        summary["routes"] = summarize_routes(
            evidence.records, routes, summary["measurement"]["offered_seconds"], planned
        )
        summary["streams"] = monitor.summary()
        valid_routes = all(
            r["successful"] >= plan["samples_per_route"]
            and r["successful"] == r["planned"]
            and not r["errors"]
            and not r["dropped"]
            for r in summary["routes"].values()
        )
        if (
            not stop.is_set()
            and valid_routes
            and summary["streams"]["all_measurement_samples_online"]
            and not summary["measurement"]["task_errors"]
        ):
            summary["result"] = "PASSED"
    except (PlanError, TimeoutError):
        summary["failure"] = "stream_setup_or_measurement_invariant"
    finally:
        summary["streams"] = monitor.summary()
        await monitor.close()
        await asyncio.gather(*(c.aclose() for c in clients.values()), return_exceptions=True)
        summary["finished_at"] = datetime.now(UTC).isoformat()
        (args.output_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
        )
        evidence.close()
        stream_evidence.close()
    print(
        json.dumps(
            {
                "result": summary["result"],
                "label": args.label,
                "run_id": run_id,
                "summary": str(args.output_dir / "summary.json"),
            }
        )
    )
    return 0 if summary["result"] == "PASSED" else 2


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--routes-file", type=Path, required=True)
    plan.add_argument("--streams-file", type=Path, required=True)
    plan.add_argument("--base-url", default="http://127.0.0.1:8201")
    plan.add_argument("--rate", type=float, default=1.5)
    plan.add_argument("--samples-per-route", type=int, default=1000)
    plan.add_argument("--warmup-seconds", type=float, default=120)
    plan.add_argument("--max-in-flight", type=int, default=32)
    plan.add_argument("--timeout", type=float, default=30)
    plan.add_argument("--max-start-lag-seconds", type=float, default=1)
    run = commands.add_parser("run")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--auth-file", type=Path, required=True)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--label", required=True)
    run.add_argument("--variant", choices=["A", "B"], required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--source-commit", required=True)
    run.add_argument("--fixture-sha256", required=True)
    return root


def main():
    args = parser().parse_args()
    try:
        if args.command == "plan":
            value = {
                "schema_version": 1,
                "base_url": args.base_url.rstrip("/"),
                "rate_per_second": args.rate,
                "samples_per_route": args.samples_per_route,
                "warmup_seconds": args.warmup_seconds,
                "max_in_flight": args.max_in_flight,
                "timeout_seconds": args.timeout,
                "max_start_lag_seconds": args.max_start_lag_seconds,
                "routes": json.loads(args.routes_file.read_text(encoding="utf-8-sig")),
                "streams": json.loads(args.streams_file.read_text(encoding="utf-8-sig")),
            }
            validate_plan(value)
            with args.output.open("x", encoding="utf-8", newline="\n") as output:
                output.write(json.dumps(value, ensure_ascii=True, indent=2) + "\n")
            print("PASS fixed-arrival public plan created")
            return 0
        return asyncio.run(run_phase(args))
    except (PlanError, ValueError, KeyError, TypeError, OSError):
        # Exceptions can contain file contents or secrets; do not serialize them.
        print("FAIL benchmark configuration, private input, or evidence output")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
