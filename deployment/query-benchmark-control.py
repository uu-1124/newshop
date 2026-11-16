"""Read-only interference and resource evidence for the controlled query experiment."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def utc():
    return datetime.now(UTC).isoformat()


def interference_reason(name, command):
    """Classify command lines in memory; arguments never enter returned evidence."""
    name, command = name.lower(), command.lower()
    patterns = {
        "PYTHON_TESTS": [
            r"(?:^|\s)-m\s+(?:pytest|unittest)(?:\s|$)",
            r'(?:^|[\\/\s"])(?:pytest|py\.test)(?:\.exe)?(?:\s|"|$)',
        ],
        "MAVEN": [
            r"org\.codehaus\.plexus\.classworlds\.launcher\.launcher",
            r'(?:^|[\\/\s"])mvn(?:\.cmd|\.bat|\.exe)?(?:\s|"|$)',
        ],
        "GRADLE": [r"gradlelauncher|gradledaemon"],
        "FRONTEND_BUILD_OR_TEST": [
            r'(?:npm|pnpm|yarn)(?:\.cmd)?(?:"|\s)+(?:run\s+)?(?:build|test|type-check)'
        ],
        "BACKUP_OR_RESTORE": [
            r'(?:^|[\\/\s"])(?:backup-agent-consistent|restore-agent-isolated)\.py(?:"|\s|$)'
        ],
        "OTHER_ACCEPTANCE": [
            r'(?:^|[\\/\s"])(?:accept_capacity|evaluate_[a-z_]+|smoke_[a-z_]+)\.py(?:"|\s|$)'
        ],
    }
    if name in {"node", "node.exe"}:
        patterns["NODE_BUILD_OR_TEST"] = [
            r"(?:^|\s)--test(?:\s|=|$)",
            r'(?:^|[\\/\s"])(?:vite|vue-tsc|tsc)(?:\.js)?(?:"|\s)+(?:build|--build|--noemit)',
        ]
    return next(
        (
            key
            for key, rules in patterns.items()
            if any(re.search(rule, command) for rule in rules)
        ),
        None,
    )


def redact_processes(records):
    processes, interference = [], []
    for record in records:
        item = {
            "pid": int(record["ProcessId"]),
            "name": str(record.get("Name", "")),
            "cpu_seconds": (
                int(record.get("KernelModeTime") or 0)
                + int(record.get("UserModeTime") or 0)
            )
            / 10000000,
            "working_set_bytes": int(record.get("WorkingSetSize") or 0),
        }
        processes.append(item)
        reason = interference_reason(item["name"], str(record.get("CommandLine") or ""))
        if reason:
            interference.append(
                {"pid": item["pid"], "name": item["name"], "reason": reason}
            )
    return processes, interference


def windows_snapshot():
    if os.name != "nt":
        raise RuntimeError("The registered Windows experiment host is required")
    script = """$ErrorActionPreference='Stop'
$OutputEncoding=[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
$p=@(Get-CimInstance Win32_Process | Select-Object ProcessId,Name,CommandLine,KernelModeTime,UserModeTime,WorkingSetSize)
$o=Get-CimInstance Win32_OperatingSystem
$c=Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter \"Name='_Total'\"
@{processes=$p;memory_total_bytes=([long]$o.TotalVisibleMemorySize*1024);memory_free_bytes=([long]$o.FreePhysicalMemory*1024);cpu_percent=[int]$c.PercentProcessorTime}|ConvertTo-Json -Depth 4 -Compress
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=True,
    )
    if len(result.stdout) > 4000000:
        raise RuntimeError("Process inventory exceeded the observer bound")
    source = json.loads(result.stdout.decode("utf-8-sig"))
    processes, interference = redact_processes(source["processes"])
    return {
        key: source[key]
        for key in ("memory_total_bytes", "memory_free_bytes", "cpu_percent")
    } | {
        "processes": processes,
        "interference": interference,
    }


def backup_schedule(policy, state, now, window_seconds):
    if not policy.get("enabled", False):
        return {"enabled": False, "due_in_window": []}
    daily = now.replace(
        hour=int(policy.get("daily_hour_utc", 19)), minute=0, second=0, microsecond=0
    )
    if daily <= now:
        daily += timedelta(days=1)
    last = state.get("last_journal_at")
    journal = (
        datetime.fromisoformat(last)
        + timedelta(minutes=int(policy.get("journal_interval_minutes", 15)))
        if last
        else now
    )
    dates = {"daily_backup": daily, "erasure_journal": max(now, journal)}
    return {
        "enabled": True,
        "next_daily_at": daily.isoformat(),
        "next_journal_at": dates["erasure_journal"].isoformat(),
        "journal_interval_minutes": int(policy.get("journal_interval_minutes", 15)),
        "due_in_window": [
            key
            for key, value in dates.items()
            if value <= now + timedelta(seconds=window_seconds)
        ],
        "note": "Next daily window assumes the most recent daily job was handled; active jobs are checked separately.",
    }


def database_snapshot(window_seconds):
    sys.path.insert(0, str(ROOT / "deployment"))
    from backup_data import pg_connection

    with pg_connection(ROOT, True) as conn:
        conn.execute("SET LOCAL statement_timeout='5s'")
        row = dict(
            conn.execute("""SELECT clock_timestamp() checked_at,
            (SELECT count(*) FROM pg_stat_activity) backends,
            (SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock') lock_waiters,
            (SELECT count(*) FROM pg_stat_activity WHERE state='active' AND pid<>pg_backend_pid()) active_backends,
            (SELECT count(*) FROM pg_stat_activity WHERE datname='mall_agent_architecture_test') test_database_connections,
            (SELECT count(*) FROM ag_run WHERE state IN ('QUEUED','RUNNING','STOP_REQUESTED')) active_runs,
            (SELECT count(*) FROM ag_job WHERE state IN ('QUEUED','RUNNING')) active_jobs,
            (SELECT count(*) FROM ag_action WHERE state IN ('EXECUTING','UNKNOWN')) active_actions,
            (SELECT count(*) FROM ag_job WHERE state IN ('QUEUED','RUNNING') AND job_type IN ('BACKUP_VERIFY','RESTORE_VERIFY')) active_backup_jobs,
            (SELECT value FROM ag_setting WHERE key='agent-backup-policy-v1') backup_policy,
            (SELECT value FROM ag_setting WHERE key='agent-backup-executor-v1') backup_state,
            (SELECT value FROM ag_setting WHERE key='agent-observability-v1') observation""").fetchone()
        )
    now = row.pop("checked_at")
    policy, state = row.pop("backup_policy") or {}, row.pop("backup_state") or {}
    observed = row.pop("observation") or {}
    row["checked_at"] = now.isoformat()
    row["backup_schedule"] = backup_schedule(policy, state, now, window_seconds)
    row["backup_executor"] = {
        "maintenance_stuck": state.get("maintenance_stuck", False),
        "last_error_present": bool(state.get("last_error")),
    }
    row["existing_cloud_observation"] = {
        key: observed.get(key) for key in ("checked_at", "duration_ms", "cloud")
    }
    return row


def managed_snapshot():
    source = json.loads(
        (ROOT / "deployment/.run/pids.json").read_text(encoding="utf-8-sig")
    )
    return {
        "manager": source.get("manager"),
        "children": [
            {key: row.get(key) for key in ("name", "pid", "restarts", "maintenance")}
            for row in source.get("children", [])
        ],
    }


def violations(value, require_backup_disabled=False):
    found = ["PROCESS_" + row["reason"] for row in value["windows"]["interference"]]
    if value["maintenance_marker_exists"]:
        found.append("MAINTENANCE_ACTIVE")
    pg = value["postgres"]
    for key in (
        "test_database_connections",
        "active_runs",
        "active_jobs",
        "active_actions",
        "active_backup_jobs",
    ):
        if pg[key]:
            found.append(key.upper())
    if pg["backup_executor"]["maintenance_stuck"]:
        found.append("BACKUP_MAINTENANCE_STUCK")
    if pg["backup_schedule"]["due_in_window"]:
        found.append("BACKUP_DUE_IN_WINDOW")
    if require_backup_disabled and pg["backup_schedule"]["enabled"]:
        found.append("BACKUP_POLICY_ENABLED")
    experiment = value["experiment_api"]
    if experiment["pid"] and experiment["pid"] not in {
        row["pid"] for row in value["windows"]["processes"]
    }:
        found.append("EXPERIMENT_API_MISSING")
    return sorted(set(found))


def snapshot(
    window_seconds, require_backup_disabled=False, api_pid=None, source_commit=None
):
    value = {
        "schema_version": 1,
        "checked_at": utc(),
        "windows": windows_snapshot(),
        "postgres": database_snapshot(window_seconds),
        "managed": managed_snapshot(),
        "maintenance_marker_exists": (
            ROOT / "deployment/.run/maintenance.json"
        ).exists(),
        "experiment_api": {"pid": api_pid, "source_commit": source_commit},
    }
    value["violations"] = violations(value, require_backup_disabled)
    value["valid"] = not value["violations"]
    return value


def validate_abba(phases):
    if len(phases) != 4:
        return ["Exactly four phase summaries are required"]
    errors = []
    if [p.get("label") for p in phases] != ["A1", "B1", "B2", "A2"]:
        errors.append("Phase order is not A1-B1-B2-A2")
    for field in ("plan_sha256", "fixture_sha256", "configuration"):
        if any(p.get(field) != phases[0].get(field) for p in phases[1:]):
            errors.append(field + " differs between phases")
    for field in ("plan_sha256", "fixture_sha256"):
        if not re.fullmatch(r"[a-f0-9]{64}", str(phases[0].get(field, ""))):
            errors.append(field + " is missing or invalid")
    if len({p.get("run_id") for p in phases}) != 4:
        errors.append("Phase run IDs must be distinct")
    if phases[0].get("source_commit") != phases[3].get("source_commit") or phases[
        1
    ].get("source_commit") != phases[2].get("source_commit"):
        errors.append("Source commit differs within a variant")
    if phases[0].get("source_commit") == phases[1].get("source_commit"):
        errors.append("A and B source commits must differ")
    names = set(phases[0].get("routes", {}))
    if not names:
        errors.append("No query routes were measured")
    for phase in phases:
        label = phase.get("label", "INVALID")
        if phase.get("variant") != label[:1] or not re.fullmatch(
            r"[a-f0-9]{40}", str(phase.get("source_commit", ""))
        ):
            errors.append(label + ": invalid variant or source commit")
        if phase.get("result") != "PASSED":
            errors.append(label + ": phase did not pass")
        streams = phase.get("streams", {})
        if (
            streams.get("required") != 13
            or streams.get("total_connections") != 13
            or not streams.get("all_measurement_samples_online")
            or streams.get("unexpected_disconnects")
            or streams.get("duplicate_events")
        ):
            errors.append(label + ": SSE continuity failed")
        if set(phase.get("routes", {})) != names:
            errors.append(label + ": route set changed")
        for name, route in phase.get("routes", {}).items():
            if route.get("completed", 0) < 1000:
                errors.append(label + ": insufficient samples for " + name)
            if any(route.get(key, 0) for key in ("errors", "timeouts", "dropped")):
                errors.append(label + ": request errors for " + name)
            if label.startswith("B") and route.get("timing_coverage", {}).get(
                "observed"
            ) != route.get("completed"):
                errors.append(label + ": incomplete timing coverage for " + name)
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "sample"):
        command = commands.add_parser(name)
        command.add_argument("--window-seconds", type=int, default=0)
        command.add_argument("--require-backup-disabled", action="store_true")
        command.add_argument("--api-pid", type=int)
        command.add_argument("--source-commit")
        command.add_argument("--output", type=Path)
        if name == "sample":
            command.add_argument("--duration", type=int, required=True)
            command.add_argument("--interval", type=int, default=30)
    check = commands.add_parser("validate-abba")
    check.add_argument("--summary", action="append", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "validate-abba":
        phases = [json.loads(path.read_text(encoding="utf-8")) for path in args.summary]
        errors = validate_abba(phases)
        print(
            json.dumps(
                {
                    "result": "FAILED" if errors else "PASSED",
                    "violations": errors,
                    "note": "Summary validation only; phase percentiles are never averaged.",
                }
            )
        )
        return 2 if errors else 0
    if args.window_seconds < 0 or (
        args.source_commit and not re.fullmatch(r"[a-f0-9]{40}", args.source_commit)
    ):
        parser.error("Use a nonnegative window and a full source commit")
    if args.command == "sample" and (
        args.duration < 1 or args.interval < 10 or not args.output
    ):
        parser.error("sample requires --output, duration>=1 and interval>=10")
    if args.command == "sample" and args.output.exists():
        parser.error("sample requires a new output file")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
    started, next_sample, baseline = time.monotonic(), 0, None
    while True:
        value = snapshot(
            args.window_seconds,
            args.require_backup_disabled,
            args.api_pid,
            args.source_commit,
        )
        current = {
            row["name"]: (row["pid"], row["restarts"])
            for row in value["managed"]["children"]
        }
        if baseline is not None and current != baseline:
            value["violations"].append("MANAGED_PROCESS_CHANGED")
            value["valid"] = False
        baseline = current
        if args.output:
            with args.output.open(
                "a" if args.command == "sample" else "w", encoding="utf-8"
            ) as output:
                output.write(json.dumps(value, ensure_ascii=False) + "\n")
        if args.command == "preflight":
            print(json.dumps(value, ensure_ascii=False))
        elif not value["valid"]:
            print(json.dumps({"result": "INVALID", "violations": value["violations"]}))
        if not value["valid"]:
            return 2
        if args.command == "preflight" or time.monotonic() - started >= args.duration:
            return 0
        next_sample += args.interval
        time.sleep(
            max(
                0,
                min(started + next_sample, started + args.duration) - time.monotonic(),
            )
        )


if __name__ == "__main__":
    raise SystemExit(main())
