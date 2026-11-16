"""Pure controller tests: no process probes, database access or network calls."""

import importlib.util
import json
import unittest
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "query_benchmark_control",
    Path(__file__).resolve().parents[1] / "query-benchmark-control.py",
)
CONTROL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTROL)


class ControllerTests(unittest.TestCase):
    def test_classification_flags_actual_build_test_backup_invocations(self):
        samples = [
            ("python.exe", '"D:\\python.exe" -m pytest -x', "PYTHON_TESTS"),
            (
                "java.exe",
                "java org.codehaus.plexus.classworlds.launcher.Launcher package",
                "MAVEN",
            ),
            ("cmd.exe", 'cmd /c "D:\\npm.cmd" run build', "FRONTEND_BUILD_OR_TEST"),
            ("node.exe", "node --test file.test.cjs", "NODE_BUILD_OR_TEST"),
            (
                "python.exe",
                "python D:/newshop/deployment/backup-agent-consistent.py capture-erasure-journal",
                "BACKUP_OR_RESTORE",
            ),
            ("python.exe", "python -m app.workers.generation", None),
            ("python.exe", "python deployment/backup-executor.py", None),
            (
                "node.exe",
                "node D:/newshop/mall-admin-web/node_modules/vite/bin/vite.js --port 5100",
                None,
            ),
            ("python.exe", "python mall-agent/scripts/benchmark_queries.py run", None),
        ]
        for name, command, expected in samples:
            with self.subTest(command=command):
                self.assertEqual(CONTROL.interference_reason(name, command), expected)

    def test_command_arguments_are_never_persisted(self):
        records = [
            {
                "ProcessId": 42,
                "Name": "python.exe",
                "CommandLine": "python -m pytest --password=private-value",
                "KernelModeTime": "10000000",
                "UserModeTime": "20000000",
                "WorkingSetSize": "4096",
            }
        ]
        safe, problems = CONTROL.redact_processes(records)
        self.assertNotIn("private-value", json.dumps([safe, problems]))
        self.assertNotIn("CommandLine", json.dumps([safe, problems]))
        self.assertEqual(safe[0]["cpu_seconds"], 3)
        self.assertEqual(problems[0]["reason"], "PYTHON_TESTS")

    def test_backup_schedule_exposes_journal_window_and_keeps_input_unchanged(self):
        policy = {"enabled": True, "daily_hour_utc": 19, "journal_interval_minutes": 15}
        original = deepcopy(policy)
        now = datetime(2026, 11, 16, 9, 12, tzinfo=UTC)
        actual = CONTROL.backup_schedule(
            policy, {"last_journal_at": "2026-11-16T09:08:00Z"}, now, 14400
        )
        self.assertEqual(actual["due_in_window"], ["erasure_journal"])
        self.assertEqual(actual["next_journal_at"], "2026-11-16T09:23:00+00:00")
        self.assertEqual(policy, original)
        self.assertEqual(
            CONTROL.backup_schedule({"enabled": False}, {}, now, 14400)[
                "due_in_window"
            ],
            [],
        )

    def test_abba_requires_same_fixture_enough_samples_and_complete_b_observation(self):
        phases = [
            {
                "label": label,
                "variant": label[0],
                "run_id": label,
                "source_commit": ("a" if label.startswith("A") else "b") * 40,
                "plan_sha256": "c" * 64,
                "fixture_sha256": "d" * 64,
                "configuration": {"rate_per_second": 1.5},
                "result": "PASSED",
                "streams": {
                    "required": 13,
                    "all_measurement_samples_online": True,
                    "total_connections": 13,
                    "unexpected_disconnects": 0,
                    "duplicate_events": 0,
                },
                "routes": {
                    "member_list": {
                        "completed": 1000,
                        "errors": 0,
                        "timeouts": 0,
                        "dropped": 0,
                        "timing_coverage": {
                            "observed": 1000 if label.startswith("B") else 0
                        },
                    }
                },
            }
            for label in ("A1", "B1", "B2", "A2")
        ]
        self.assertEqual(CONTROL.validate_abba(phases), [])
        changed = deepcopy(phases)
        changed[1]["fixture_sha256"] = "e" * 64
        changed[2]["routes"]["member_list"]["completed"] = 999
        changed[1]["routes"]["member_list"]["timing_coverage"]["observed"] = 999
        errors = CONTROL.validate_abba(changed)
        for expected in ("fixture_sha256", "insufficient samples", "incomplete timing"):
            self.assertTrue(any(expected in error for error in errors))


if __name__ == "__main__":
    unittest.main()
