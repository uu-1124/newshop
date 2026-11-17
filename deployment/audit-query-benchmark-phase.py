"""Audit one completed query phase from public, saved evidence only.

No service probes or database access are performed. This supplements the runner's
summary validation; it does not establish A08 continuity or an ABBA comparison.
"""

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

RUN_ROOT = Path(__file__).resolve().parent / ".run"
NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
DB_STAGES = (
    "acquire", "physical_connect", "checkout_residual", "sql_execute",
    "transaction_end", "isolation_config", "connection_release", "pool_queue",
)
RESOURCE_GAP_SECONDS = 40.0


class AuditError(ValueError):
    """A fixed, non-sensitive rejection code."""


def check(condition, code):
    if not condition:
        raise AuditError(code)


def number(value, code, *, integer=False):
    check(type(value) in (int, float) and math.isfinite(value) and value >= 0, code)
    check(not integer or type(value) is int, code)
    return value


def timestamp(value):
    check(isinstance(value, str), "INVALID_TIMESTAMP")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise AuditError("INVALID_TIMESTAMP") from None
    check(parsed.tzinfo is not None and parsed.utcoffset() is not None, "INVALID_TIMESTAMP")
    return parsed.timestamp()


def uuid(value):
    try:
        check(isinstance(value, str) and str(UUID(value)) == value, "INVALID_UUID")
    except (ValueError, AttributeError):
        raise AuditError("INVALID_UUID") from None


def percentile(values, quantile):
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * quantile) - 1)], 3) if ordered else None


def timing(row, summary, route):
    if summary["variant"] == "A":
        check(row["timing_state"] == "unobserved" and row["server_timing"] is None,
              "A_TIMING_MUST_BE_UNOBSERVED")
        return
    value = row["server_timing"]
    check(row["timing_state"] == "observed" and isinstance(value, dict), "B_TIMING_MISSING")
    check((value["schema_version"], value["run_id"], value["request_id"],
           value["operation"], value["status_code"]) ==
          (1, summary["run_id"], row["request_id"], route["expected_operation"], 200),
          "B_TIMING_IDENTITY_MISMATCH")
    check(value["streaming"] is False and value["scope"] == "response_headers_ready",
          "B_TIMING_SCOPE_INVALID")
    for key in ("db_includes_metrics", "sql_includes_implicit_begin",
                "checkout_residual_includes_setup", "pool_queue_in_checkout_residual"):
        check(value.get(key) is True, "B_TIMING_DEFINITION_INVALID")
    for key in ("total_ms", "handler_ms", "metrics_write_ms", "java_bridge_ms"):
        number(value[key], "B_TIMING_NONFINITE_OR_NEGATIVE")
    db = value["db"]
    check(db["pool_queue_supported"] is True and db["pool_queue_reason"] is None,
          "B_REAL_QUEUE_UNAVAILABLE")
    check(type(db["physical_connect_incomplete_count"]) is int and
          db["physical_connect_incomplete_count"] == 0, "B_CONNECT_TIMING_INCOMPLETE")
    for stage in DB_STAGES:
        number(db[stage + "_ms"], "B_DB_TIMING_NONFINITE_OR_NEGATIVE")
        number(db[stage + "_count"], "B_DB_COUNT_INVALID", integer=True)


def request_segment(rows, expected_count, summary, plan, warmup):
    check(len(rows) == expected_count, "RAW_SEGMENT_COUNT_MISMATCH")
    ordered = sorted(rows, key=lambda row: row["sequence"])
    check([row["sequence"] for row in ordered] == list(range(expected_count)),
          "RAW_SEQUENCE_INVALID")
    first = ordered[0]["scheduled_mono"]
    first_wall = timestamp(ordered[0]["scheduled_at"])
    rate = plan["rate_per_second"]
    routes = plan["routes"]
    for row in ordered:
        check(row["record_type"] == "request" and row["warmup"] is warmup,
              "RAW_RECORD_TYPE_INVALID")
        check(type(row["sequence"]) is int, "RAW_SEQUENCE_INVALID")
        route = routes[row["sequence"] % len(routes)]
        check(row["route"] == route["name"], "RAW_ROUTE_PROPORTION_OR_ORDER_INVALID")
        uuid(row["request_id"])
        check(row["status_code"] == 200 and row["outcome"] == "success" and
              row["error"] is None, "RAW_REQUEST_FAILED")
        scheduled, sent, finished = (
            number(row[key], "RAW_CLOCK_INVALID")
            for key in ("scheduled_mono", "sent_mono", "finished_mono")
        )
        check(scheduled <= sent <= finished, "RAW_CLOCK_ORDER_INVALID")
        check(abs(scheduled - (first + row["sequence"] / rate)) <= 0.00001,
              "ARRIVAL_CLOCK_NOT_FIXED")
        wall = [timestamp(row[key]) for key in ("scheduled_at", "sent_at", "finished_at")]
        check(wall[0] <= wall[1] <= wall[2], "RAW_TIMESTAMP_ORDER_INVALID")
        for observed, monotonic in zip(wall, (scheduled, sent, finished)):
            check(abs((observed - first_wall) - (monotonic - first)) <= 0.002,
                  "WALL_MONOTONIC_CLOCK_MISMATCH")
        lag = number(row["scheduling_lag_ms"], "RAW_LAG_INVALID")
        latency = number(row["client_latency_ms"], "RAW_LATENCY_INVALID")
        check(lag <= plan["max_start_lag_seconds"] * 1000 + 0.001,
              "ARRIVAL_LAG_EXCEEDED")
        check(abs(lag - (sent - scheduled) * 1000) <= 0.002, "RAW_LAG_MISMATCH")
        check(abs(latency - (finished - sent) * 1000) <= 0.002, "RAW_LATENCY_MISMATCH")
        timing(row, summary, route)
    segment = summary["warmup" if warmup else "measurement"]
    for key in ("scheduled", "dispatched", "completed"):
        check(segment[key] == expected_count, "SUMMARY_SEGMENT_COUNT_MISMATCH")
    check(segment["dropped"] == 0 and segment["aborted"] is False and
          segment["task_errors"] == {}, "SUMMARY_SEGMENT_FAILED")
    offered = number(segment["offered_seconds"], "SUMMARY_DURATION_INVALID")
    drain = number(segment["drain_seconds"], "SUMMARY_DURATION_INVALID")
    check(abs(offered - expected_count / rate) <= 0.000002, "SUMMARY_OFFERED_TIME_MISMATCH")
    check(number(segment["max_in_flight_observed"], "SUMMARY_INFLIGHT_INVALID", integer=True)
          <= plan["max_in_flight"], "SUMMARY_INFLIGHT_INVALID")
    last_finish = max(row["finished_mono"] for row in rows)
    # The runner adds event-loop bookkeeping after draining tasks. Allow 100ms;
    # never infer a negative tail or silently substitute the response deadline.
    expected_drain = max(0, last_finish - (first + offered))
    check(expected_drain - 0.002 <= drain <= expected_drain + 0.1,
          "SUMMARY_DRAIN_TIME_MISMATCH")
    return ordered


def stream_evidence(summary, streams, plan, start, end):
    states = summary["streams"]
    check(states["required"] == 13 and states["total_connections"] == 13 and
          states["all_measurement_samples_online"] is True and
          states["unexpected_disconnects"] == 0 and states["duplicate_events"] == 0,
          "SSE_SUMMARY_FAILED")
    check(set(states["per_stream"]) == {row["name"] for row in plan["streams"]},
          "SSE_IDENTITY_MISMATCH")
    stream_ids = []
    for value in states["per_stream"].values():
        check(value["connections"] == 1 and value["unexpected_disconnects"] == 0 and
              value["duplicate_events"] == 0 and value["error"] is None,
              "SSE_CONNECTION_FAILED")
        uuid(value["request_id"])
        stream_ids.append(value["request_id"])
        timestamp(value["last_line_at"])
        check(number(value["lines_received"], "SSE_LINE_EVIDENCE_INVALID", integer=True) > 0,
              "SSE_LINE_EVIDENCE_INVALID")
        number(value["maximum_line_gap_ms"], "SSE_LINE_EVIDENCE_INVALID")
    check(len(set(stream_ids)) == 13, "SSE_DUPLICATE_REQUEST_ID")
    check(bool(streams), "SSE_RAW_EVIDENCE_MISSING")
    times = []
    stages = []
    for row in streams:
        check(row["record_type"] == "streams" and row["required"] == 13 and
              row["online"] == 13, "SSE_RAW_OFFLINE")
        check(row["stage"] in ("warmup", "measured", "drained"), "SSE_STAGE_INVALID")
        times.append(timestamp(row["at"]))
        stages.append(row["stage"])
    check(times == sorted(times), "SSE_CLOCK_ORDER_INVALID")
    rank = {"warmup": 0, "measured": 1, "drained": 2}
    check([rank[x] for x in stages] == sorted(rank[x] for x in stages), "SSE_STAGE_ORDER_INVALID")
    measured = [time for row, time in zip(streams, times) if row["stage"] == "measured"]
    drained = [time for row, time in zip(streams, times) if row["stage"] == "drained"]
    check(len(measured) == states["measurement_samples"] and bool(measured) and bool(drained),
          "SSE_MEASUREMENT_COUNT_MISMATCH")
    # SSE timestamps use the live wall clock, while request timestamps are mapped
    # from a monotonic anchor. Permit a bounded one-second wall-clock difference.
    check(abs(measured[0] - start) <= 1 and abs(drained[0] - end) <= 1,
          "SSE_WINDOW_NOT_COVERED")
    window = measured + [drained[0]]
    check(all(0 <= b - a <= 7 for a, b in zip(window, window[1:])), "SSE_SAMPLE_GAP")
    return {"measurement_samples": len(measured), "connections": 13,
            "boundary_wall_clock_tolerance_seconds": 1,
            "maximum_sample_gap_seconds": round(max(b - a for a, b in zip(window, window[1:])), 6)}


def resource_evidence(resources, summary, api_pid, start, end):
    check(bool(resources), "RESOURCE_EVIDENCE_MISSING")
    times = [timestamp(row["checked_at"]) for row in resources]
    check(times == sorted(times) and len(set(times)) == len(times), "RESOURCE_CLOCK_ORDER_INVALID")
    before = [i for i, value in enumerate(times) if value <= start]
    check(bool(before), "RESOURCE_START_NOT_COVERED")
    first = before[-1]
    selected = [(row, time) for row, time in zip(resources[first:], times[first:]) if time <= end]
    check(len(selected) >= 2 and start - selected[0][1] <= RESOURCE_GAP_SECONDS and
          end - selected[-1][1] <= RESOURCE_GAP_SECONDS, "RESOURCE_BOUNDARY_GAP")
    check(all(0 < b[1] - a[1] <= RESOURCE_GAP_SECONDS for a, b in zip(selected, selected[1:])),
          "RESOURCE_SAMPLE_GAP")
    baseline = None
    for row, _ in selected:
        check(row["valid"] is True and row["violations"] == [], "RESOURCE_SAMPLE_INVALID")
        check(row["experiment_api"] == {"pid": api_pid, "source_commit": summary["source_commit"]},
              "RESOURCE_API_IDENTITY_MISMATCH")
        check(row["maintenance_marker_exists"] is False and
              row["windows"]["interference"] == [], "RESOURCE_INTERFERENCE")
        check(api_pid in {p["pid"] for p in row["windows"]["processes"]}, "RESOURCE_API_NOT_PRESENT")
        pg = row["postgres"]
        for key in ("test_database_connections", "active_runs", "active_jobs",
                    "active_actions", "active_backup_jobs"):
            check(pg[key] == 0, "RESOURCE_DATABASE_INTERFERENCE")
        check(pg["backup_schedule"]["enabled"] is False and
              pg["backup_schedule"]["due_in_window"] == [] and
              pg["backup_executor"]["maintenance_stuck"] is False, "RESOURCE_BACKUP_INTERFERENCE")
        current = (row["managed"]["manager"], sorted(
            (child["name"], child["pid"], child["restarts"], child.get("maintenance"))
            for child in row["managed"]["children"]
        ))
        if baseline is not None:
            check(current == baseline, "RESOURCE_MANAGED_PROCESS_CHANGED")
        baseline = current
    outside = [row for row, time in zip(resources, times)
               if time < selected[0][1] or time > end]
    return {"samples_checked": len(selected), "maximum_allowed_gap_seconds": RESOURCE_GAP_SECONDS,
            "start_boundary_gap_seconds": round(start - selected[0][1], 6),
            "end_boundary_gap_seconds": round(end - selected[-1][1], 6),
            "out_of_window_samples_excluded": len(resources) - len(selected),
            "out_of_window_invalid_samples": sum(row.get("valid") is not True for row in outside),
            "note": "Periodic evidence has up to 40 seconds of boundary uncertainty; "
                    "it cannot exclude shorter unobserved stalls. Excluded invalid records "
                    "remain in the hashed original file and are counted separately."}


def audit_loaded(summary, requests, streams, plan, resources, guard_before, guard_after,
                 api_pid, plan_sha256):
    """Pure audit entry point used by offline tests; never opens external services."""
    result = {"schema_version": 1, "result": "FAILED", "violations": [],
              "scope": "One saved query phase only; does not prove A08 or formal ABBA completion.",
              "percentile_method": "nearest-rank: sorted[ceil(n*q)-1], "
                                   "rounded to 3 milliseconds decimals",
              "evidence_limits": [
                  "SSE raw rows contain sampled online counts; connection, duplicate "
                  "and disconnect counts are runner per-stream observations.",
                  "No response bodies, credentials or original application logs are read.",
              ]}
    try:
        check(summary["schema_version"] == 1 and plan["schema_version"] == 1, "SCHEMA_UNSUPPORTED")
        check(summary["result"] == "PASSED", "RUNNER_SUMMARY_NOT_PASSED")
        check(summary["variant"] in ("A", "B") and
              summary["label"] in (summary["variant"] + "1", summary["variant"] + "2"),
              "PHASE_IDENTITY_INVALID")
        uuid(summary["run_id"])
        check(bool(COMMIT.fullmatch(summary["source_commit"])), "SOURCE_COMMIT_INVALID")
        check(type(api_pid) is int and api_pid > 0, "API_PID_INVALID")
        check(bool(DIGEST.fullmatch(summary["fixture_sha256"])), "FIXTURE_DIGEST_INVALID")
        check(summary["plan_sha256"] == plan_sha256, "PLAN_DIGEST_MISMATCH")
        check(plan["rate_per_second"] == 1.5 and plan["warmup_seconds"] == 120,
              "LOAD_CONTRACT_MISMATCH")
        minimum = number(plan["samples_per_route"], "PLAN_SAMPLE_COUNT_INVALID", integer=True)
        check(minimum >= 1000, "INSUFFICIENT_PLANNED_SAMPLES")
        check(len(plan["routes"]) == 5 and len(plan["streams"]) == 13, "PLAN_SCOPE_MISMATCH")
        names = [row["name"] for row in plan["routes"]]
        check(len(set(names)) == 5 and all(NAME.fullmatch(name) for name in names), "PLAN_ROUTE_NAMES_INVALID")
        check(all(type(row["weight"]) is int and row["weight"] == 1 for row in plan["routes"]),
              "PLAN_ROUTE_WEIGHTS_NOT_EQUAL")
        check(set(summary["routes"]) == set(names), "SUMMARY_ROUTE_SET_MISMATCH")
        for key in ("base_url", "rate_per_second", "samples_per_route", "warmup_seconds",
                    "max_in_flight", "timeout_seconds", "max_start_lag_seconds"):
            check(summary["configuration"][key] == plan[key], "SUMMARY_CONFIGURATION_MISMATCH")
        check(summary["configuration"]["stream_count"] == 13, "SUMMARY_CONFIGURATION_MISMATCH")
        number(plan["max_in_flight"], "PLAN_CONCURRENCY_INVALID", integer=True)
        number(plan["max_start_lag_seconds"], "PLAN_LAG_INVALID")
        check(all(type(row["warmup"]) is bool for row in requests), "RAW_WARMUP_FLAG_INVALID")
        warmup = request_segment([row for row in requests if row["warmup"]], 180,
                                 summary, plan, True)
        measured = request_segment([row for row in requests if not row["warmup"]], minimum * 5,
                                   summary, plan, False)
        check(len({row["request_id"] for row in requests}) == len(requests), "RAW_DUPLICATE_REQUEST_ID")
        start = timestamp(measured[0]["scheduled_at"])
        last_response = max(timestamp(row["finished_at"]) for row in measured)
        check(timestamp(warmup[-1]["scheduled_at"]) < start and
              max(timestamp(row["finished_at"]) for row in warmup) <= start,
              "WARMUP_MEASUREMENT_OVERLAP")
        offered = summary["measurement"]["offered_seconds"]
        elapsed = offered + summary["measurement"]["drain_seconds"]
        end = start + elapsed
        check(last_response <= end + 0.002, "SUMMARY_WINDOW_INVALID")
        check(timestamp(summary["started_at"]) <= timestamp(warmup[0]["scheduled_at"]) and
              timestamp(summary["finished_at"]) >= end - 1, "SUMMARY_WINDOW_INVALID")
        route_results = {}
        for name in names:
            rows = [row for row in measured if row["route"] == name]
            stated = summary["routes"][name]
            for key in ("planned", "scheduled", "dispatched", "completed", "successful"):
                check(stated[key] == len(rows) == minimum, "SUMMARY_ROUTE_COUNT_MISMATCH")
            check(all(stated[key] == 0 for key in ("errors", "timeouts", "dropped")),
                  "SUMMARY_ROUTE_FAILED")
            statuses = dict(Counter(str(row["status_code"]) for row in rows))
            check(stated["status_counts"] == statuses, "SUMMARY_STATUS_COUNT_MISMATCH")
            expected_coverage = {"observed": minimum if summary["variant"] == "B" else 0,
                                 "unobserved": minimum if summary["variant"] == "A" else 0, "invalid": 0}
            check(stated["timing_coverage"] == expected_coverage, "SUMMARY_TIMING_COUNT_MISMATCH")
            latency = [row["client_latency_ms"] for row in rows]
            quantiles = {"samples": minimum, **{f"p{q}": percentile(latency, q / 100) for q in (50, 95, 99)}}
            check(stated["latency_ms"] == quantiles and stated["success_latency_ms"] == quantiles,
                  "SUMMARY_PERCENTILE_MISMATCH")
            check(abs(stated["throughput_rps"] - minimum / offered) <= 0.000001,
                  "SUMMARY_THROUGHPUT_MISMATCH")
            route_results[name] = {"completed": len(rows), "successful": len(rows),
                                   "status_counts": statuses, "errors": 0, "timeouts": 0,
                                   "dropped": 0, "share": len(rows) / len(measured),
                                   "raw_latency_ms": quantiles,
                                   "successful_per_offered_second": round(minimum / offered, 9),
                                   "successful_per_offered_plus_drain_second": round(minimum / elapsed, 9)}
        result["streams"] = stream_evidence(summary, streams, plan, start, end)
        result["resources"] = resource_evidence(resources, summary, api_pid, start, end)
        check(guard_before == guard_after, "DATA_GUARD_CHANGED")
        check(guard_before["sha256"] == summary["fixture_sha256"] and
              bool(DIGEST.fullmatch(guard_before["sha256"])), "DATA_GUARD_DIGEST_MISMATCH")
        check(bool(guard_before["parts"]), "DATA_GUARD_SCHEMA_INVALID")
        for value in guard_before["parts"].values():
            check(bool(DIGEST.fullmatch(value["sha256"])), "DATA_GUARD_SCHEMA_INVALID")
            number(value["count"], "DATA_GUARD_SCHEMA_INVALID", integer=True)
        for key in ("model_calls", "actions", "active_runs"):
            number(guard_before["counters"][key], "DATA_GUARD_SCHEMA_INVALID", integer=True)
        check(guard_before["counters"]["active_runs"] == 0, "DATA_GUARD_ACTIVE_RUNS")
        result.update(result="PASSED", label=summary["label"], variant=summary["variant"],
                      run_id=summary["run_id"], source_commit=summary["source_commit"],
                      api_pid=api_pid, fixture_sha256=summary["fixture_sha256"],
                      measured_requests=len(measured), warmup_requests=len(warmup),
                      measurement_start=measured[0]["scheduled_at"],
                      measurement_end=datetime.fromtimestamp(end, UTC).isoformat(),
                      last_response_at=datetime.fromtimestamp(last_response, UTC).isoformat(),
                      offered_seconds=offered, drain_seconds=summary["measurement"]["drain_seconds"],
                      routes=route_results, fixed_data_unchanged=True)
    except AuditError as error:
        result["violations"] = [str(error)]
    except (KeyError, TypeError, ValueError, IndexError, AttributeError, OverflowError):
        result["violations"] = ["PUBLIC_EVIDENCE_SCHEMA_INVALID"]
    return result


def safe_path(value):
    path = Path(value).resolve()
    check(path.is_relative_to(RUN_ROOT.resolve()) and path != RUN_ROOT.resolve(), "PATH_OUTSIDE_RUN_ROOT")
    check(not any(part.startswith(".") or "private" in part.lower() or part.lower() in
                  {"auth.json", "config", "config.json"} for part in path.relative_to(RUN_ROOT.resolve()).parts),
          "PRIVATE_INPUT_PATH_REJECTED")
    return path


def read_public(path, manifest, label, *, jsonl=False):
    check(path.is_file() and path.stat().st_size <= 128_000_000, "PUBLIC_INPUT_SIZE_INVALID")
    raw = path.read_bytes()
    manifest[label] = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    if jsonl:
        lines = raw.decode("utf-8-sig").splitlines()
        check(len(lines) <= 250000 and all(0 < len(line) <= 2_000_000 for line in lines),
              "PUBLIC_JSONL_SIZE_INVALID")
        return [json.loads(line) for line in lines]
    return json.loads(raw.decode("utf-8-sig"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase-dir", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--resources", required=True)
    parser.add_argument("--guard-before", required=True)
    parser.add_argument("--guard-after", required=True)
    parser.add_argument("--api-pid", type=int, required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    manifest = {}
    try:
        output = safe_path(args.output)
        check(not output.exists() and output.parent.is_dir(), "NEW_OUTPUT_REQUIRED")
        folder = safe_path(args.phase_dir)
        paths = {"summary": safe_path(folder / "summary.json"),
                 "requests": safe_path(folder / "requests.jsonl"),
                 "streams": safe_path(folder / "streams.jsonl"),
                 "plan": safe_path(args.plan), "resources": safe_path(args.resources),
                 "guard_before": safe_path(args.guard_before), "guard_after": safe_path(args.guard_after)}
        values = {key: read_public(path, manifest, key, jsonl=key in {"requests", "streams", "resources"})
                  for key, path in paths.items()}
        result = audit_loaded(**values, api_pid=args.api_pid, plan_sha256=manifest["plan"]["sha256"])
        result["recorded_at"] = datetime.now(UTC).isoformat()
        result["input_manifest"] = manifest
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False) + "\n")
        print(json.dumps({"result": result["result"], "violations": result["violations"], "audit": str(output)}))
        return 0 if result["result"] == "PASSED" else 2
    except (AuditError, OSError, ValueError, TypeError):
        # Input exceptions may contain file contents; never echo their messages.
        print(json.dumps({"result": "FAILED", "violations": ["PUBLIC_INPUT_OR_OUTPUT_REJECTED"]}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
