"""Pure saved-evidence audit fixtures: no process, application or DB probes."""

import importlib.util
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

SPEC = importlib.util.spec_from_file_location(
    "query_benchmark_phase_audit",
    Path(__file__).resolve().parents[1] / "audit-query-benchmark-phase.py",
)
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def fixture(variant="B"):
    anchor = datetime(2026, 11, 17, tzinfo=UTC)

    def at(seconds):
        return (anchor + timedelta(seconds=seconds)).isoformat()

    names = ["member_list", "member_snapshot", "member_history", "member_orders", "staff_history"]
    operations = ["listConversations", "getConversation", "listMessages", "listMemberOrders", "adminListMessages"]
    plan = {
        "schema_version": 1, "base_url": "http://127.0.0.1:8201", "rate_per_second": 1.5,
        "samples_per_route": 1000, "warmup_seconds": 120, "max_in_flight": 32,
        "timeout_seconds": 30, "max_start_lag_seconds": 1,
        "routes": [{"name": name, "expected_operation": op, "weight": 1}
                   for name, op in zip(names, operations)],
        "streams": [{"name": "stream" + str(i)} for i in range(13)],
    }
    run_id = str(UUID(int=1))
    source = "a" * 40
    digest = "b" * 64
    plan_digest = "c" * 64
    summary = {
        "schema_version": 1, "result": "PASSED", "label": variant + "1", "variant": variant,
        "run_id": run_id, "source_commit": source, "fixture_sha256": digest,
        "plan_sha256": plan_digest, "started_at": at(-5),
        "configuration": {key: plan[key] for key in (
            "base_url", "rate_per_second", "samples_per_route", "warmup_seconds",
            "max_in_flight", "timeout_seconds", "max_start_lag_seconds",
        )} | {"stream_count": 13},
    }
    requests = []
    for warmup, count, start in ((True, 180, 0), (False, 5000, 121)):
        for sequence in range(count):
            scheduled = start + sequence / 1.5
            sent = scheduled + 0.001
            finished = sent + 0.1
            request_id = str(UUID(int=len(requests) + 10))
            timing = {
                "schema_version": 1, "run_id": run_id, "request_id": request_id,
                "operation": operations[sequence % 5], "status_code": 200,
                "streaming": False, "scope": "response_headers_ready", "total_ms": 90,
                "handler_ms": 80, "metrics_write_ms": 10, "java_bridge_ms": 15,
                "db_includes_metrics": True, "sql_includes_implicit_begin": True,
                "checkout_residual_includes_setup": True, "pool_queue_in_checkout_residual": True,
                "db": {stage + "_ms": 0.1 for stage in AUDIT.DB_STAGES}
                      | {stage + "_count": 1 for stage in AUDIT.DB_STAGES}
                      | {"pool_queue_supported": True, "pool_queue_reason": None,
                         "physical_connect_incomplete_count": 0},
            }
            requests.append({
                "record_type": "request", "sequence": sequence, "route": names[sequence % 5],
                "warmup": warmup, "request_id": request_id, "scheduled_at": at(scheduled),
                "sent_at": at(sent), "finished_at": at(finished),
                "scheduled_mono": scheduled + 10000, "sent_mono": sent + 10000,
                "finished_mono": finished + 10000, "scheduling_lag_ms": 1,
                "client_latency_ms": 100, "status_code": 200, "outcome": "success",
                "error": None, "server_timing": timing if variant == "B" else None,
                "timing_state": "observed" if variant == "B" else "unobserved",
            })
        summary["warmup" if warmup else "measurement"] = {
            "scheduled": count, "dispatched": count, "completed": count,
            "dropped": 0, "aborted": False, "offered_seconds": round(count / 1.5, 6),
            "drain_seconds": 0, "max_in_flight_observed": 1, "task_errors": {},
        }
    end = 121 + 5000 / 1.5
    summary["finished_at"] = at(end + 0.01)
    summary["routes"] = {}
    for name in names:
        summary["routes"][name] = {
            "planned": 1000, "scheduled": 1000, "dispatched": 1000, "completed": 1000,
            "successful": 1000, "errors": 0, "timeouts": 0, "dropped": 0,
            "status_counts": {"200": 1000},
            "latency_ms": {"samples": 1000, "p50": 100, "p95": 100, "p99": 100},
            "success_latency_ms": {"samples": 1000, "p50": 100, "p95": 100, "p99": 100},
            "timing_coverage": {"observed": 1000 if variant == "B" else 0,
                                "unobserved": 1000 if variant == "A" else 0, "invalid": 0},
            "throughput_rps": 0.3,
        }
    streams = [
        {"record_type": "streams", "stage": "warmup", "at": at(i), "online": 13, "required": 13}
        for i in range(0, 121, 5)
    ] + [
        {"record_type": "streams", "stage": "measured", "at": at(i), "online": 13, "required": 13}
        for i in range(121, int(end), 5)
    ] + [{"record_type": "streams", "stage": "drained", "at": at(end), "online": 13, "required": 13}]
    summary["streams"] = {
        "required": 13, "total_connections": 13, "all_measurement_samples_online": True,
        "unexpected_disconnects": 0, "duplicate_events": 0,
        "measurement_samples": sum(row["stage"] == "measured" for row in streams),
        "per_stream": {
            "stream" + str(i): {"connections": 1, "unexpected_disconnects": 0, "duplicate_events": 0,
                                 "error": None, "request_id": str(UUID(int=10000 + i)),
                                 "last_line_at": at(end - 1), "lines_received": 200,
                                 "maximum_line_gap_ms": 20000}
            for i in range(13)
        },
    }
    resources = [{
        "checked_at": at(i), "valid": True, "violations": [],
        "experiment_api": {"pid": 42, "source_commit": source}, "maintenance_marker_exists": False,
        "windows": {"interference": [], "processes": [{"pid": 42}]},
        "postgres": {"test_database_connections": 0, "active_runs": 0, "active_jobs": 0,
                     "active_actions": 0, "active_backup_jobs": 0,
                     "backup_schedule": {"enabled": False, "due_in_window": []},
                     "backup_executor": {"maintenance_stuck": False}},
        "managed": {"manager": 12, "children": [{"name": "gateway", "pid": 43,
                                                  "restarts": 0, "maintenance": False}]},
    } for i in range(110, int(end) + 1, 30)]
    guard = {"sha256": digest, "parts": {"conversations": {"sha256": "d" * 64, "count": 40}},
             "counters": {"model_calls": 10, "actions": 20, "active_runs": 0}}
    return {"summary": summary, "requests": requests, "streams": streams, "plan": plan,
            "resources": resources, "guard_before": guard, "guard_after": deepcopy(guard),
            "api_pid": 42, "plan_sha256": plan_digest}


class AuditTests(unittest.TestCase):
    def assert_rejected(self, data, code):
        result = AUDIT.audit_loaded(**data)
        self.assertEqual(result["result"], "FAILED")
        self.assertIn(code, result["violations"])

    def test_complete_B_raw_evidence_and_both_throughput_denominators(self):
        result = AUDIT.audit_loaded(**fixture())
        self.assertEqual(result["violations"], [])
        self.assertEqual(result["result"], "PASSED")
        self.assertEqual(result["measured_requests"], 5000)
        self.assertEqual(result["warmup_requests"], 180)
        route = result["routes"]["member_list"]
        self.assertEqual(route["raw_latency_ms"]["p95"], 100)
        self.assertEqual(route["share"], 0.2)
        self.assertEqual(route["successful_per_offered_second"], 0.3)
        self.assertEqual(route["successful_per_offered_plus_drain_second"], 0.3)

    def test_A_requires_null_timing_instead_of_zero(self):
        data = fixture("A")
        self.assertEqual(AUDIT.audit_loaded(**data)["result"], "PASSED")
        data["requests"][0]["server_timing"] = 0
        self.assert_rejected(data, "A_TIMING_MUST_BE_UNOBSERVED")

    def test_B_requires_observed_real_queue_and_complete_connect(self):
        cases = [
            ({"pool_queue_supported": False, "pool_queue_reason": "unsupported"}, "B_REAL_QUEUE_UNAVAILABLE"),
            ({"physical_connect_incomplete_count": 1}, "B_CONNECT_TIMING_INCOMPLETE"),
            ({"sql_execute_ms": float("nan")}, "B_DB_TIMING_NONFINITE_OR_NEGATIVE"),
            ({"connection_release_ms": -1}, "B_DB_TIMING_NONFINITE_OR_NEGATIVE"),
            ({"physical_connect_ms": None}, "B_DB_TIMING_NONFINITE_OR_NEGATIVE"),
        ]
        for replacement, code in cases:
            with self.subTest(replacement=replacement):
                data = fixture()
                data["requests"][200]["server_timing"]["db"].update(replacement)
                self.assert_rejected(data, code)

    def test_raw_failure_is_not_hidden_by_successful_summary(self):
        data = fixture()
        data["requests"][200]["status_code"] = 403
        self.assert_rejected(data, "RAW_REQUEST_FAILED")

    def test_clock_must_follow_arrival_rate_even_if_summary_claims_success(self):
        data = fixture()
        data["requests"][200]["scheduled_mono"] += 0.01
        self.assert_rejected(data, "RAW_CLOCK_ORDER_INVALID")

    def test_request_ids_are_unique_and_uuid_formatted(self):
        data = fixture()
        data["requests"][0]["request_id"] = "unsafe-value"
        self.assert_rejected(data, "INVALID_UUID")
        data = fixture("A")
        data["requests"][1]["request_id"] = data["requests"][0]["request_id"]
        self.assert_rejected(data, "RAW_DUPLICATE_REQUEST_ID")

    def test_missing_measured_sample_and_wrong_proportions_rejected(self):
        data = fixture()
        data["requests"].pop()
        self.assert_rejected(data, "RAW_SEGMENT_COUNT_MISMATCH")
        data = fixture()
        data["requests"][200]["route"] = "member_orders"
        self.assert_rejected(data, "RAW_ROUTE_PROPORTION_OR_ORDER_INVALID")

    def test_summary_percentiles_must_match_raw_nearest_rank(self):
        data = fixture()
        data["summary"]["routes"]["member_list"]["latency_ms"]["p95"] = 99
        self.assert_rejected(data, "SUMMARY_PERCENTILE_MISMATCH")

    def test_sse_raw_disconnect_and_reconnect_count_rejected(self):
        data = fixture()
        data["streams"][100]["online"] = 12
        self.assert_rejected(data, "SSE_RAW_OFFLINE")
        data = fixture()
        data["summary"]["streams"]["per_stream"]["stream0"]["connections"] = 2
        self.assert_rejected(data, "SSE_CONNECTION_FAILED")

    def test_resource_identity_gap_and_interference_are_independently_checked(self):
        for mutation, code in (
            (lambda row: row["experiment_api"].update(pid=43), "RESOURCE_API_IDENTITY_MISMATCH"),
            (lambda row: row["windows"].update(interference=[{"reason": "MAVEN"}]), "RESOURCE_INTERFERENCE"),
            (lambda row: row["postgres"].update(active_jobs=1), "RESOURCE_DATABASE_INTERFERENCE"),
            (lambda row: row.update(valid=False), "RESOURCE_SAMPLE_INVALID"),
        ):
            with self.subTest(code=code):
                data = fixture()
                mutation(data["resources"][4])
                self.assert_rejected(data, code)
        data = fixture()
        del data["resources"][4]
        self.assert_rejected(data, "RESOURCE_SAMPLE_GAP")

    def test_resource_invalid_after_measurement_is_retained_but_not_a_measurement_failure(self):
        data = fixture()
        later = deepcopy(data["resources"][-1])
        later["checked_at"] = data["summary"]["finished_at"]
        later["valid"] = False
        later["violations"] = ["EXPERIMENT_API_MISSING"]
        data["resources"].append(later)
        result = AUDIT.audit_loaded(**data)
        self.assertEqual(result["result"], "PASSED")
        self.assertEqual(result["resources"]["out_of_window_samples_excluded"], 1)

    def test_fixed_data_must_match_including_counters(self):
        data = fixture()
        data["guard_after"]["counters"]["model_calls"] += 1
        self.assert_rejected(data, "DATA_GUARD_CHANGED")

    def test_path_boundary_and_private_inputs_rejected(self):
        with TemporaryDirectory() as temporary:
            original = AUDIT.RUN_ROOT
            try:
                AUDIT.RUN_ROOT = Path(temporary) / "run"
                AUDIT.RUN_ROOT.mkdir()
                self.assertEqual(AUDIT.safe_path(AUDIT.RUN_ROOT / "public.json"),
                                 AUDIT.RUN_ROOT / "public.json")
                for path in (Path(temporary) / "outside.json", AUDIT.RUN_ROOT / "auth.private.json"):
                    with self.subTest(path=path), self.assertRaises(AUDIT.AuditError):
                        AUDIT.safe_path(path)
            finally:
                AUDIT.RUN_ROOT = original


if __name__ == "__main__":
    unittest.main()
