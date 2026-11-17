"""Supervisor tests with fake processes, clocks and filesystem operations only.

These do not establish Windows Job Object lifecycle correctness. The caller must
also verify that lifecycle with owned, network-free dummy parent/child processes.
"""

import importlib.util
import io
import json
import subprocess
import unittest
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location(
    "query_benchmark_phase",
    Path(__file__).resolve().parents[1] / "run-query-benchmark-phase.py",
)
PHASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PHASE)

VALID = {"checked_at": "2026-11-17T00:00:00+00:00", "valid": True, "violations": []}
INVALID = {**VALID, "valid": False, "violations": ["MANAGED_PROCESS_CHANGED"]}


class Clock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeChild:
    def __init__(self, name, codes, events):
        self.name, self.codes, self.events = name, iter(codes), events
        self.code, self.stopped, self.termination_requested = None, False, False
        self.process = SimpleNamespace(pid={"preflight": 11, "resources": 12, "load": 13}[name])

    def poll(self):
        if not self.stopped:
            self.code = next(self.codes, self.code)
        return self.code

    def stop(self):
        self.events.append(self.name + "_stop")
        if self.code is None:
            self.termination_requested, self.code = True, -15
        self.stopped = True


class Scenario:
    def __init__(self, *, rows=None, load_codes=(None, 0), sampler_codes=(None,), disappear=None):
        self.events, self.saved, self.children = [], {}, {}
        self.api_stopped, self.api_checks = False, 0
        self.clock = Clock()
        self.rows = iter(rows if rows is not None else [[VALID], [], []])
        self.codes = {"preflight": (0,), "resources": sampler_codes, "load": load_codes}
        self.disappear = disappear
        root = Path("mock-run")
        self.args = SimpleNamespace(
            output_dir=root / "new-phase", plan=root / "plan.json",
            auth_file=root / "auth.private.json", api_process_file=root / "A1.process.json",
            api_stop_file=root / "A1.stop", api_pid=42, label="A1", variant="A",
            run_id="11111111-1111-4111-8111-111111111111", source_commit="a" * 40,
            fixture_sha256="b" * 64,
        )
        self.record = {"pid": 42}
        self.summary = {
            key: getattr(self.args, key)
            for key in ("label", "variant", "run_id", "source_commit", "fixture_sha256")
        }
        self.summary.update(
            result="PASSED", finished_at="2026-11-17T00:01:00+00:00",
            plan_sha256=PHASE.hashlib.sha256(b"{}").hexdigest(),
        )
        self.watch = MagicMock()
        self.watch.alive.side_effect = self.alive
        self.tail = MagicMock(buffer=b"")
        self.tail.read.side_effect = self.read_rows

    def alive(self):
        self.api_checks += 1
        return not self.api_stopped and (
            self.disappear is None or self.api_checks < self.disappear
        )

    def read_rows(self):
        value = next(self.rows, [])
        if isinstance(value, BaseException):
            raise value
        return value

    def spawn(self, command, cwd, log):
        name = "preflight" if "preflight" in command else "resources" if "sample" in command else "load"
        self.events.append(name + "_start")
        child = FakeChild(name, self.codes[name], self.events)
        self.children[name] = child
        return child

    def open(self, path, mode="r", **kwargs):
        handle = MagicMock()
        handle.__enter__.return_value = handle
        if path == self.args.api_stop_file:
            def stop_marker(_):
                self.events.append("api_stop_marker")
                self.api_stopped = True
            handle.write.side_effect = stop_marker
        return handle

    def read_json(self, path):
        if path.name == "summary.json":
            return self.summary
        if path.name == "preflight.json":
            return {"valid": True, "violations": []}
        return self.record

    def run(self):
        def opened(path, *args, **kwargs):
            return self.open(path, *args, **kwargs)

        with (
            patch.object(PHASE, "ApiWatch", return_value=self.watch),
            patch.object(PHASE, "Child", side_effect=self.spawn),
            patch.object(PHASE, "ResourceTail", return_value=self.tail),
            patch.object(PHASE, "read_json", side_effect=self.read_json),
            patch.object(PHASE, "save_json", side_effect=lambda p, v: self.saved.update({p.name: deepcopy(v)})),
            patch.object(PHASE.time, "monotonic", side_effect=self.clock.monotonic),
            patch.object(PHASE.time, "sleep", side_effect=self.clock.sleep),
            patch.object(Path, "read_bytes", return_value=b"{}"),
            patch.object(Path, "open", new=opened),
            patch.object(Path, "exists", return_value=False),
        ):
            return PHASE.supervise(self.args, self.record, 4000)


class PhaseSupervisionTests(unittest.TestCase):
    def test_success_stops_observer_before_api_and_records_expected_termination(self):
        scenario = Scenario()
        self.assertEqual(scenario.run(), 0)
        terminal = scenario.saved["phase-terminal.json"]
        self.assertEqual(terminal["result"], "PASSED")
        self.assertTrue(terminal["children"]["resources"]["termination_requested"])
        self.assertEqual(terminal["children"]["resources"]["exit_code"], -15)
        self.assertLess(scenario.events.index("resources_stop"), scenario.events.index("api_stop_marker"))
        self.assertTrue(terminal["api_stop_requested"])
        serialized = json.dumps(terminal)
        for private in ("auth.private", "CommandLine", "--auth-file", str(scenario.args.auth_file)):
            self.assertNotIn(private, serialized)

    def test_resource_invalid_stops_live_load_and_preserves_failure(self):
        scenario = Scenario(rows=[[VALID], [INVALID]], load_codes=(None,))
        self.assertEqual(scenario.run(), 2)
        terminal = scenario.saved["phase-terminal.json"]
        self.assertEqual(terminal["reason"], "RESOURCE_INVALID")
        self.assertEqual(terminal["resource"]["violations"], ["MANAGED_PROCESS_CHANGED"])
        self.assertTrue(terminal["partial_evidence_preserved"])
        self.assertTrue(scenario.children["load"].stopped)
        self.assertTrue(scenario.api_stopped)

    def test_sampler_exit_zero_or_nonzero_before_load_completion_is_failure(self):
        for code in (0, 2):
            with self.subTest(code=code):
                scenario = Scenario(sampler_codes=(None, code))
                self.assertEqual(scenario.run(), 2)
                self.assertEqual(scenario.saved["phase-terminal.json"]["reason"], "SAMPLER_EXITED_EARLY")
                self.assertTrue(scenario.children["load"].stopped)

    def test_initial_resource_invalid_never_launches_load(self):
        scenario = Scenario(rows=[[INVALID]])
        self.assertEqual(scenario.run(), 2)
        self.assertNotIn("load", scenario.children)
        self.assertTrue(scenario.api_stopped)

    def test_load_failure_needs_no_fabricated_summary(self):
        scenario = Scenario(load_codes=(2,))
        self.assertEqual(scenario.run(), 2)
        self.assertEqual(scenario.saved["phase-terminal.json"]["reason"], "LOAD_EXIT_NONZERO")
        self.assertTrue(scenario.children["resources"].stopped)

    def test_api_disappearance_stops_owned_load(self):
        scenario = Scenario(disappear=3, load_codes=(None,))
        self.assertEqual(scenario.run(), 2)
        self.assertEqual(scenario.saved["phase-terminal.json"]["reason"], "API_DISAPPEARED")
        self.assertTrue(scenario.children["load"].stopped)

    def test_ctrl_c_stops_both_children_and_requests_api_stop(self):
        scenario = Scenario(rows=[[VALID], KeyboardInterrupt()], load_codes=(None,))
        self.assertEqual(scenario.run(), 130)
        self.assertEqual(scenario.saved["phase-terminal.json"]["reason"], "INTERRUPTED")
        self.assertTrue(scenario.children["load"].stopped)
        self.assertTrue(scenario.children["resources"].stopped)
        self.assertTrue(scenario.api_stopped)

    def test_live_but_stalled_observer_is_not_accepted(self):
        scenario = Scenario(load_codes=(None,))
        self.assertEqual(scenario.run(), 2)
        self.assertEqual(scenario.saved["phase-terminal.json"]["reason"], "RESOURCE_STALE")

    def test_mismatched_summary_is_rejected(self):
        scenario = Scenario(load_codes=(0,))
        scenario.summary["fixture_sha256"] = "f" * 64
        self.assertEqual(scenario.run(), 2)
        self.assertEqual(scenario.saved["phase-terminal.json"]["reason"], "LOAD_SUMMARY_INVALID")

    def test_changed_api_control_is_not_overwritten_or_stopped(self):
        scenario = Scenario(load_codes=(0,))
        # The supervisor receives the original already-validated identity.
        saved_reader = scenario.read_json
        scenario.read_json = lambda path: {"pid": 99} if path == scenario.args.api_process_file else saved_reader(path)
        self.assertEqual(scenario.run(), 2)
        terminal = scenario.saved["phase-terminal.json"]
        self.assertIn("API_CONTROL_CHANGED", terminal["cleanup_errors"])
        self.assertFalse(scenario.api_stopped)

    def test_unexpected_exception_is_sanitized(self):
        scenario = Scenario(rows=[[VALID], RuntimeError("private password and command line")])
        self.assertEqual(scenario.run(), 2)
        self.assertEqual(scenario.saved["phase-terminal.json"]["reason"], "SUPERVISOR_FAILURE")
        self.assertNotIn("private password", json.dumps(scenario.saved))


class ResourceEvidenceTests(unittest.TestCase):
    def state(self):
        return {"samples": 0, "invalid_samples": 0, "last_checked_at": None,
                "violations": [], "post_load_invalid_samples": 0}

    def test_invalid_inside_window_fails_but_post_load_record_is_retained_separately(self):
        tail = MagicMock()
        state = self.state()
        tail.read.return_value = [{**INVALID, "checked_at": "2026-11-17T00:02:00+00:00"}]
        PHASE.resource_check(tail, state, cutoff=datetime(2026, 11, 17, 0, 1, tzinfo=UTC))
        self.assertEqual(state["post_load_invalid_samples"], 1)
        tail.read.return_value = [INVALID]
        with self.assertRaisesRegex(PHASE.PhaseFailure, "RESOURCE_INVALID"):
            PHASE.resource_check(tail, state, cutoff=datetime(2026, 11, 17, 0, 1, tzinfo=UTC))

    def test_resource_tail_defers_partial_line_and_never_reparses_old_rows(self):
        raw = json.dumps(VALID).encode() + b"\n"
        path = MagicMock()
        path.exists.return_value = True
        path.stat.return_value.st_size = len(raw)
        path.open.return_value.__enter__.side_effect = [io.BytesIO(raw[:12]), io.BytesIO(raw)]
        tail = PHASE.ResourceTail(path)
        self.assertEqual(tail.read(), [])
        self.assertEqual(tail.read(), [VALID])
        self.assertEqual(tail.offset, len(raw))
        self.assertEqual(tail.buffer, b"")

    def test_truncated_resource_file_is_rejected(self):
        path = MagicMock()
        path.exists.return_value = True
        path.stat.return_value.st_size = 2
        tail = PHASE.ResourceTail(path)
        tail.offset = 3
        with self.assertRaisesRegex(PHASE.PhaseFailure, "RESOURCE_EVIDENCE_TRUNCATED"):
            tail.read()

    def test_process_stop_escalates_only_its_owned_handle_then_closes_job(self):
        child = object.__new__(PHASE.Child)
        child.job, child.process, child.termination_requested = MagicMock(), MagicMock(), False
        child.process.poll.return_value = None
        child.process.wait.side_effect = [subprocess.TimeoutExpired("owned", 5), None]
        child.stop()
        child.process.terminate.assert_called_once_with()
        child.process.kill.assert_called_once_with()
        child.job.close.assert_called_once_with()
        self.assertTrue(child.termination_requested)

    def test_identity_rejection_precedes_any_process_or_file_mutation(self):
        args = SimpleNamespace(variant="B", label="A1", api_pid=42)
        with patch.object(PHASE, "inside_run") as filesystem, patch.object(PHASE, "Child") as child:
            with self.assertRaisesRegex(PHASE.PhaseFailure, "INVALID_PHASE_IDENTITY"):
                PHASE.validate(args)
            filesystem.assert_not_called()
            child.assert_not_called()


if __name__ == "__main__":
    unittest.main()
