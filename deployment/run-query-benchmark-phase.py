"""Supervise one fixed-arrival phase and its independent resource observer.

Only children created here are terminated. The already-started API is stopped by
its validated private control marker. Source rollback belongs to the caller.
"""

import argparse
import ctypes
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "deployment/.run"
POLL_SECONDS = 1.0


class PhaseFailure(Exception):
    """Only a fixed public reason, never an underlying exception message."""


def utc():
    return datetime.now(UTC).isoformat()


def inside_run(path):
    value = Path(path).resolve()
    if value == RUN.resolve() or not value.is_relative_to(RUN.resolve()):
        raise PhaseFailure("PATH_OUTSIDE_RUN")
    return value


def read_json(path):
    raw = path.read_bytes()
    if len(raw) > 4000000:
        raise PhaseFailure("JSON_INPUT_TOO_LARGE")
    return json.loads(raw.decode("utf-8-sig"))


def save_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(value, ensure_ascii=True, indent=2) + "\n")


def validate(args):
    if args.variant != args.label[0] or args.api_pid <= 0:
        raise PhaseFailure("INVALID_PHASE_IDENTITY")
    args.run_id = str(UUID(args.run_id))
    if not re.fullmatch(r"[a-f0-9]{40}", args.source_commit) or not re.fullmatch(
        r"[a-f0-9]{64}", args.fixture_sha256
    ):
        raise PhaseFailure("INVALID_PROVENANCE")
    for name in ("api_process_file", "api_stop_file", "plan", "auth_file", "output_dir"):
        setattr(args, name, inside_run(getattr(args, name)))
    if args.output_dir.exists() or not args.output_dir.parent.is_dir():
        raise PhaseFailure("OUTPUT_MUST_BE_NEW")
    if not args.auth_file.is_file() or args.api_stop_file.exists():
        raise PhaseFailure("INPUT_OR_CONTROL_UNAVAILABLE")
    record = read_json(args.api_process_file)
    phase = record.get("phase")
    if (
        record.get("schema_version") != 1
        or record.get("pid") != args.api_pid
        or phase not in {args.label, "canary"}
        or record.get("variant") != args.variant
        or record.get("commit") != args.source_commit
        or args.api_process_file.name != phase + ".process.json"
        or args.api_stop_file != args.api_process_file.with_name(phase + ".stop")
    ):
        raise PhaseFailure("API_CONTROL_IDENTITY_MISMATCH")
    # Reuse the existing complete plan validator rather than relaxing its gates.
    sys.path.insert(0, str(ROOT / "mall-agent"))
    from scripts.benchmark_queries import route_sequence, validate_plan

    plan = read_json(args.plan)
    routes, _ = validate_plan(plan)
    _, count = route_sequence(routes, plan["samples_per_route"])
    duration = math.ceil(
        max(45, 2 * plan["timeout_seconds"])
        + plan["warmup_seconds"]
        + count / plan["rate_per_second"]
        + 2 * plan["timeout_seconds"]
        + 180
    )
    return record, duration


def windows_api():
    """Documented Win32 handles; no process inventory or command-line capture."""
    if os.name != "nt":
        raise PhaseFailure("WINDOWS_HOST_REQUIRED")
    from ctypes import wintypes as w

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
        "CloseHandle": ([w.HANDLE], w.BOOL),
        "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
        "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
        "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
        "CreateToolhelp32Snapshot": ([w.DWORD, w.DWORD], w.HANDLE),
        "Thread32First": ([w.HANDLE, ctypes.c_void_p], w.BOOL),
        "Thread32Next": ([w.HANDLE, ctypes.c_void_p], w.BOOL),
        "OpenThread": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        "ResumeThread": ([w.HANDLE], w.DWORD),
    }
    for name, (arguments, result) in signatures.items():
        getattr(api, name).argtypes = arguments
        getattr(api, name).restype = result
    return api


class ApiWatch:
    def __init__(self, pid):
        self.api = windows_api()
        self.handle = self.api.OpenProcess(0x00100000 | 0x1000, False, pid)
        if not self.handle:
            raise PhaseFailure("API_HANDLE_UNAVAILABLE")

    def alive(self):
        result = self.api.WaitForSingleObject(self.handle, 0)
        if result not in (0, 258):
            raise PhaseFailure("API_HANDLE_WAIT_FAILED")
        return result == 258

    def close(self):
        self.api.CloseHandle(self.handle)


class OwnedJob:
    """A suspended child joins this job before it may spawn any descendants."""

    def __init__(self):
        from ctypes import wintypes as w

        class Limits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", w.DWORD), ("minimum", ctypes.c_size_t),
                ("maximum", ctypes.c_size_t), ("active", w.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD),
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("basic", Limits), ("io", ctypes.c_ulonglong * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t),
            ]

        self.api, self.handle = windows_api(), None
        self.handle = self.api.CreateJobObjectW(None, None)
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.api.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            self.close()
            raise PhaseFailure("OWNED_JOB_SETUP_FAILED")

    def attach_and_resume(self, process):
        from ctypes import wintypes as w

        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("size", w.DWORD), ("usage", w.DWORD), ("tid", w.DWORD),
                ("owner", w.DWORD), ("base_priority", w.LONG),
                ("delta_priority", w.LONG), ("flags", w.DWORD),
            ]

        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise PhaseFailure("OWNED_JOB_ASSIGN_FAILED")
        snapshot = self.api.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
        if snapshot in (None, ctypes.c_void_p(-1).value):
            raise PhaseFailure("OWNED_THREAD_SNAPSHOT_FAILED")
        resumed = False
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            available = self.api.Thread32First(snapshot, ctypes.byref(entry))
            while available:
                if entry.owner == process.pid:
                    thread = self.api.OpenThread(2, False, entry.tid)
                    if not thread:
                        raise PhaseFailure("OWNED_THREAD_OPEN_FAILED")
                    try:
                        if self.api.ResumeThread(thread) != 1:
                            raise PhaseFailure("OWNED_THREAD_RESUME_FAILED")
                        resumed = True
                    finally:
                        self.api.CloseHandle(thread)
                    break
                available = self.api.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            self.api.CloseHandle(snapshot)
        if not resumed:
            raise PhaseFailure("OWNED_THREAD_NOT_FOUND")

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class Child:
    def __init__(self, command, cwd, log):
        self.job, self.process, self.termination_requested = OwnedJob(), None, False
        try:
            self.process = subprocess.Popen(
                command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW | 0x00000004,  # CREATE_SUSPENDED
            )
            self.job.attach_and_resume(self.process)
        except BaseException:
            self.stop()
            raise

    def poll(self):
        return self.process.poll()

    def stop(self):
        try:
            if self.process is not None and self.process.poll() is None:
                self.termination_requested = True
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
        finally:
            # Also closes a venv redirector's actual interpreter descendants.
            self.job.close()
            if self.process is not None:
                self.process.wait(timeout=5)


class ResourceTail:
    def __init__(self, path):
        self.path, self.offset, self.buffer = path, 0, b""

    def read(self):
        if not self.path.exists():
            return []
        if self.path.stat().st_size < self.offset:
            raise PhaseFailure("RESOURCE_EVIDENCE_TRUNCATED")
        with self.path.open("rb") as source:
            source.seek(self.offset)
            block = source.read(4000000)
        self.offset += len(block)
        lines = (self.buffer + block).split(b"\n")
        self.buffer = lines.pop()
        if len(self.buffer) > 2000000:
            raise PhaseFailure("RESOURCE_EVIDENCE_INVALID")
        result = []
        for line in lines:
            row = json.loads(line)
            if (
                not isinstance(row, dict)
                or type(row.get("valid")) is not bool
                or not isinstance(row.get("violations"), list)
            ):
                raise PhaseFailure("RESOURCE_EVIDENCE_INVALID")
            checked = datetime.fromisoformat(row["checked_at"])
            if checked.tzinfo is None:
                raise PhaseFailure("RESOURCE_EVIDENCE_INVALID")
            result.append(row)
        return result


def resource_check(tail, state, cutoff=None):
    for row in tail.read():
        state["samples"] += 1
        state["last_checked_at"] = row["checked_at"]
        if not row["valid"] or row["violations"]:
            if cutoff is not None and datetime.fromisoformat(row["checked_at"]) > cutoff:
                state["post_load_invalid_samples"] += 1
                continue
            state["invalid_samples"] += 1
            state["violations"] = [
                item for item in row["violations"][:30]
                if isinstance(item, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", item)
            ]
            raise PhaseFailure("RESOURCE_INVALID")


def check_api(watch, args):
    if not watch.alive():
        raise PhaseFailure("API_DISAPPEARED")
    if args.api_stop_file.exists():
        raise PhaseFailure("API_STOP_MARKER_APPEARED")


def supervise(args, record, duration):
    output = args.output_dir
    state = {
        "schema_version": 1, "label": args.label, "variant": args.variant,
        "run_id": args.run_id, "source_commit": args.source_commit,
        "fixture_sha256": args.fixture_sha256,
        "plan_sha256": hashlib.sha256(args.plan.read_bytes()).hexdigest(),
        "api_pid": args.api_pid, "started_at": utc(), "poll_seconds": POLL_SECONDS,
        "result": "FAILED", "reason": "SUPERVISOR_FAILURE", "children": {},
        "resource": {"samples": 0, "invalid_samples": 0, "last_checked_at": None,
                     "violations": [], "post_load_invalid_samples": 0},
        "api_stop_requested": False, "cleanup_errors": [],
        "partial_evidence_preserved": True, "automatic_git_rollback": False,
        "raw_paths": {"queries": "queries", "resources": "resources.jsonl"},
    }
    save_json(output / "phase-start.json", state)
    watch, children, logs = None, {}, []
    tail = ResourceTail(output / "resources.jsonl")
    finished = None
    result = 2

    def spawn(name, command, cwd=ROOT):
        log = (output / (name + ".log")).open("xb")
        logs.append(log)
        child = Child(command, cwd, log)
        children[name] = child
        state["children"][name] = {"pid": child.process.pid}
        return child

    def common(command):
        return [sys.executable, "-B", str(ROOT / "deployment/query-benchmark-control.py"),
                command, "--window-seconds", "120", "--require-backup-disabled",
                "--api-pid", str(args.api_pid), "--source-commit", args.source_commit]

    try:
        watch = ApiWatch(args.api_pid)
        check_api(watch, args)
        preflight = spawn("preflight", common("preflight") + ["--output", str(output / "preflight.json")])
        deadline = time.monotonic() + 90
        while preflight.poll() is None:
            check_api(watch, args)
            if time.monotonic() > deadline:
                raise PhaseFailure("PREFLIGHT_TIMEOUT")
            time.sleep(POLL_SECONDS)
        preflight.stop()
        if preflight.poll() != 0:
            raise PhaseFailure("PREFLIGHT_REJECTED")
        precheck = read_json(output / "preflight.json")
        if precheck.get("valid") is not True or precheck.get("violations"):
            raise PhaseFailure("PREFLIGHT_REJECTED")
        sampler = spawn("resources", common("sample") + [
            "--duration", str(duration), "--interval", "30", "--output", str(output / "resources.jsonl")
        ])
        deadline = time.monotonic() + 90
        while not state["resource"]["samples"]:
            check_api(watch, args)
            resource_check(tail, state["resource"])
            if sampler.poll() is not None:
                raise PhaseFailure("SAMPLER_EXITED_EARLY")
            if time.monotonic() > deadline:
                raise PhaseFailure("RESOURCE_START_TIMEOUT")
            if not state["resource"]["samples"]:
                time.sleep(POLL_SECONDS)
        load = spawn("load", [sys.executable, "-B", "-m", "scripts.benchmark_queries", "run",
            "--plan", str(args.plan), "--auth-file", str(args.auth_file),
            "--output-dir", str(output / "queries"), "--label", args.label,
            "--variant", args.variant, "--run-id", args.run_id,
            "--source-commit", args.source_commit, "--fixture-sha256", args.fixture_sha256,
        ], ROOT / "mall-agent")
        last_count, last_sample = state["resource"]["samples"], time.monotonic()
        while True:
            check_api(watch, args)
            resource_check(tail, state["resource"])
            if state["resource"]["samples"] != last_count:
                last_count, last_sample = state["resource"]["samples"], time.monotonic()
            if time.monotonic() - last_sample > 90:
                raise PhaseFailure("RESOURCE_STALE")
            if sampler.poll() is not None:
                raise PhaseFailure("SAMPLER_EXITED_EARLY")
            code = load.poll()
            if code is not None:
                if code:
                    raise PhaseFailure("LOAD_EXIT_NONZERO")
                summary = read_json(output / "queries/summary.json")
                if any(summary.get(key) != state[key] for key in (
                    "label", "variant", "run_id", "source_commit", "fixture_sha256", "plan_sha256"
                )) or summary.get("result") != "PASSED":
                    raise PhaseFailure("LOAD_SUMMARY_INVALID")
                finished = datetime.fromisoformat(summary["finished_at"])
                if finished.tzinfo is None:
                    raise PhaseFailure("LOAD_SUMMARY_INVALID")
                sampler.stop()  # Keep the API alive until resource collection is stopped.
                resource_check(tail, state["resource"], cutoff=finished)
                state["result"], state["reason"], result = "PASSED", "LOAD_PASSED", 0
                break
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        state["reason"], result = "INTERRUPTED", 130
    except PhaseFailure as error:
        state["reason"] = str(error)
    except Exception:
        state["reason"] = "SUPERVISOR_FAILURE"
    finally:
        # Failures retain flushed request JSONL; a forcibly stopped runner may
        # have no summary. Do not fabricate one or discard partial evidence.
        for name in ("load", "resources", "preflight"):
            if name in children:
                try:
                    children[name].stop()
                    state["children"][name].update(
                        exit_code=children[name].poll(),
                        termination_requested=children[name].termination_requested,
                    )
                except BaseException:
                    state["cleanup_errors"].append("OWNED_CHILD_CLEANUP_FAILED")
        state["resource"]["partial_tail_bytes"] = len(tail.buffer)
        if watch is not None:
            try:
                if read_json(args.api_process_file) != record:
                    raise PhaseFailure("API_CONTROL_CHANGED")
                if not args.api_stop_file.exists():
                    with args.api_stop_file.open("x", encoding="ascii") as marker:
                        marker.write("query benchmark supervisor requested stop\n")
                state["api_stop_requested"] = True
                deadline = time.monotonic() + 30
                while watch.alive() and time.monotonic() < deadline:
                    time.sleep(POLL_SECONDS)
                if watch.alive():
                    raise PhaseFailure("API_STOP_TIMEOUT")
            except PhaseFailure as error:
                state["cleanup_errors"].append(str(error))
            except BaseException:
                state["cleanup_errors"].append("API_STOP_FAILED")
            finally:
                try:
                    watch.close()
                except BaseException:
                    state["cleanup_errors"].append("API_HANDLE_CLOSE_FAILED")
        for log in logs:
            try:
                log.close()
            except BaseException:
                state["cleanup_errors"].append("LOG_CLOSE_FAILED")
        if state["cleanup_errors"]:
            if state["result"] == "PASSED":
                state["reason"] = "CLEANUP_FAILED"
            state["result"], result = "FAILED", 2
        state["finished_at"] = utc()
        state["load_finished_at"] = finished.isoformat() if finished else None
        save_json(output / "phase-terminal.json", state)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("api-process-file", "api-stop-file", "plan", "auth-file", "output-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--api-pid", type=int, required=True)
    parser.add_argument("--label", choices=["A1", "B1", "B2", "A2"], required=True)
    parser.add_argument("--variant", choices=["A", "B"], required=True)
    for name in ("run-id", "source-commit", "fixture-sha256"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        record, duration = validate(args)
        args.output_dir.mkdir()  # Never overwrite or append an existing phase.
        result = supervise(args, record, duration)
    except KeyboardInterrupt:
        print("FAIL phase supervisor interrupted before launch")
        return 130
    except Exception:
        print("FAIL phase supervisor configuration or evidence operation")
        return 2
    print(json.dumps({"result": "PASSED" if result == 0 else "FAILED", "label": args.label}))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
