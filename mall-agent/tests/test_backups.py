import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from test_runtime import settings_for

from app.application.backups import (
    POLICY_KEY,
    RECORD_PREFIX,
    STATE_KEY,
    BackupPolicy,
    Backups,
    backup_alerts,
    put_state,
    register_backups,
)
from app.application.jobs import JobHandlers, Jobs
from app.domain.capabilities import JobType
from app.domain.identity import DomainError, Principal
from app.infrastructure.postgres.database import Database, encoded, execute, one


def principal(*permissions):
    return Principal(1900000000 + uuid4().int % 10000000, "backup test", "STAFF", frozenset(permissions))


def manager():
    return principal("agent:backup:verify", "agent:settings:write", "agent:job:read", "agent:job:retry")


async def setup(config):
    db = Database(config["databaseUrl"])
    settings = settings_for(config, backups_enabled=True)
    registry = JobHandlers()
    register_backups(registry)
    jobs = Jobs(db, settings, registry)
    async with db.transaction() as conn:
        await execute(conn, "DELETE FROM ag_setting WHERE key LIKE 'agent-backup-%'")
        await put_state(
            conn,
            STATE_KEY,
            {
                "last_backup_at": None,
                "last_restore_at": None,
                "last_journal_at": None,
                "last_error": None,
                "maintenance_stuck": False,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )
    return db, registry, jobs, Backups(db, jobs)


@pytest.mark.anyio
async def test_backup_commands_require_permission_registered_ids_current_policy_and_explicit_maintenance(
    integration_config,
):
    db, registry, jobs, svc = await setup(integration_config)
    actor = manager()
    bid = uuid4()
    key = str(uuid4())
    try:
        with pytest.raises(DomainError) as denied:
            await svc.listing(principal("agent:job:read"))
        assert denied.value.code == "FORBIDDEN"
        with pytest.raises(DomainError) as absent:
            await svc.request(actor, key, "VERIFY", bid, "test")
        assert absent.value.code == "NOT_FOUND"
        with pytest.raises(DomainError) as maintenance:
            await svc.request(actor, key, "CREATE", None, "test", 1, False)
        assert maintenance.value.code == "MAINTENANCE_CONFIRMATION_REQUIRED"
        async with db.transaction() as conn:
            await put_state(conn, RECORD_PREFIX + str(bid), {"id": str(bid), "state": "AVAILABLE"})
        first = await svc.request(actor, key, "VERIFY", bid, "test")
        assert await svc.request(actor, key, "VERIFY", bid, "test") == json.loads(encoded(first))
        with pytest.raises(DomainError) as changed:
            await svc.request(actor, key, "VERIFY", bid, "changed")
        assert changed.value.code == "IDEMPOTENCY_CONFLICT"
        assert await jobs.claim("ordinary-worker", first["id"]) is None
        dedicated = Jobs(db, jobs.settings, registry, lane="backups")
        claimed = await dedicated.claim("backup-executor-test", first["id"])
        assert claimed and claimed["job_type"] == "BACKUP_VERIFY"
        with pytest.raises(DomainError) as no_path:
            registry.validate(
                JobType.BACKUP_VERIFY,
                {"mode": "VERIFY", "backup_record_id": str(bid), "note": "test", "path": "C:/private"},
            )
        assert no_path.value.code == "VALIDATION_ERROR"
        policy = await svc.update_policy(
            actor, str(uuid4()), BackupPolicy(enabled=True).model_dump(mode="json"), 1
        )
        assert policy["version"] == 2
        with pytest.raises(DomainError) as stale:
            await svc.request(actor, str(uuid4()), "CREATE", None, "test", 1, True)
        assert stale.value.code == "VERSION_CONFLICT"
        async with db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_job SET state='FAILED',lease_owner=NULL,lease_until=NULL WHERE id=:id",
                id=claimed["id"],
            )
        with pytest.raises(DomainError) as retry_denied:
            await jobs.retry(
                Principal(
                    actor.id, actor.display_name, "STAFF", frozenset({"agent:job:read", "agent:job:retry"})
                ),
                claimed["id"],
                claimed["version"],
                str(uuid4()),
            )
        assert retry_denied.value.code == "FORBIDDEN"
    finally:
        await db.engine.dispose()


@pytest.mark.anyio
async def test_backup_stale_missing_and_failure_alerts_are_based_on_actual_state(integration_config):
    db, _, _, _ = await setup(integration_config)
    try:
        async with db.transaction() as conn:
            await put_state(conn, POLICY_KEY, BackupPolicy(enabled=True).model_dump(mode="json"))
            await put_state(
                conn,
                STATE_KEY,
                {
                    "last_backup_at": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
                    "last_restore_at": None,
                    "last_journal_at": None,
                    "last_error": "RESTORE_FAILED",
                    "maintenance_stuck": True,
                },
            )
            codes = {item["code"] for item in await backup_alerts(conn)}
            assert {
                "BACKUP_STALE",
                "RESTORE_VERIFICATION_STALE",
                "ERASURE_JOURNAL_STALE",
                "BACKUP_OPERATION_FAILED",
                "MAINTENANCE_REQUIRES_RECOVERY",
            } <= codes
            await execute(
                conn,
                "UPDATE ag_setting SET updated_at=now()-interval '2 minutes' WHERE key=:key",
                key=STATE_KEY,
            )
            assert "BACKUP_EXECUTOR_MISSING" in {item["code"] for item in await backup_alerts(conn)}
    finally:
        await db.engine.dispose()


def load_executor(monkeypatch):
    folder = Path(__file__).resolve().parents[2] / "deployment"
    monkeypatch.syspath_prepend(str(folder))
    spec = importlib.util.spec_from_file_location("backup_executor_test", folder / "backup-executor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.anyio
async def test_periodic_schedule_creates_one_daily_job_and_obeys_disabled_or_replaced_policy(
    integration_config, monkeypatch
):
    module = load_executor(monkeypatch)
    db, _, jobs, _ = await setup(integration_config)
    worker = module.Executor(db, jobs.settings)
    worker.state["last_journal_at"] = datetime.now(UTC).isoformat()
    try:
        async with db.transaction() as conn:
            await execute(
                conn,
                "DELETE FROM ag_job_wakeup WHERE job_id IN(SELECT id FROM ag_job WHERE dedupe_key LIKE 'daily-backup:%' OR dedupe_key LIKE 'weekly-restore:%')",
            )
            await execute(
                conn,
                "DELETE FROM ag_job WHERE dedupe_key LIKE 'daily-backup:%' OR dedupe_key LIKE 'weekly-restore:%'",
            )
            await put_state(
                conn,
                POLICY_KEY,
                BackupPolicy(
                    enabled=True, daily_hour_utc=0, restore_weekday_utc=(datetime.now(UTC).weekday() + 1) % 7
                ).model_dump(mode="json"),
            )
        await worker.schedule()
        await worker.schedule()
        async with db.transaction() as conn:
            count = await one(conn, "SELECT count(*) n FROM ag_job WHERE dedupe_key LIKE 'daily-backup:%'")
            job = await one(conn, "SELECT * FROM ag_job WHERE dedupe_key LIKE 'daily-backup:%'")
            assert count["n"] == 1
            await put_state(conn, POLICY_KEY, BackupPolicy(enabled=False).model_dump(mode="json"))
        with pytest.raises(DomainError) as changed:
            await worker.policy_matches(job)
        assert changed.value.code == "BACKUP_POLICY_CHANGED"
    finally:
        await db.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("hour,expected_date", [(2, "2026-11-14"), (19, "2026-11-15")])
async def test_late_start_schedules_latest_due_window_once(
    integration_config, monkeypatch, hour, expected_date
):
    module = load_executor(monkeypatch)
    db, _, jobs, _ = await setup(integration_config)
    worker = module.Executor(db, jobs.settings)
    now = datetime(2026, 11, 15, hour, 30, tzinfo=UTC)
    worker.state["last_journal_at"] = now.isoformat()
    original_one = module.one

    async def fixed_clock(conn, query, **params):
        if query == "SELECT clock_timestamp() n":
            return {"n": now}
        return await original_one(conn, query, **params)

    monkeypatch.setattr(module, "one", fixed_clock)
    try:
        async with db.transaction() as conn:
            await execute(
                conn,
                "DELETE FROM ag_job_wakeup WHERE job_id IN (SELECT id FROM ag_job WHERE dedupe_key LIKE 'daily-backup:%' OR dedupe_key LIKE 'weekly-restore:%')",
            )
            await execute(
                conn,
                "DELETE FROM ag_job WHERE dedupe_key LIKE 'daily-backup:%' OR dedupe_key LIKE 'weekly-restore:%'",
            )
            await put_state(
                conn,
                POLICY_KEY,
                BackupPolicy(enabled=True, daily_hour_utc=19, restore_weekday_utc=5).model_dump(mode="json"),
            )
        await worker.schedule()
        await worker.schedule()
        async with db.transaction() as conn:
            scheduled = await one(
                conn,
                "SELECT count(*) n,min(dedupe_key) key FROM ag_job WHERE dedupe_key LIKE 'daily-backup:%'",
            )
            assert scheduled == {"n": 1, "key": "daily-backup:" + expected_date}
    finally:
        await db.engine.dispose()


def test_retention_keeps_daily_weekly_and_newest_backup(monkeypatch):
    module = load_executor(monkeypatch)
    now = datetime(2026, 11, 13, 12, tzinfo=UTC)
    records = [{"id": str(i), "created_at": (now - timedelta(days=i)).isoformat()} for i in range(42)]
    kept = module.retained_ids(records, now, 7, 4)
    assert all(str(i) in kept for i in range(8))
    assert "41" not in kept
    assert len(kept) > 8


@pytest.mark.anyio
async def test_windows_selector_runs_fixed_subprocess_without_asyncio_proactor_requirement(
    monkeypatch, tmp_path
):
    module = load_executor(monkeypatch)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    (tmp_path / "deployment").mkdir()
    (tmp_path / "deployment" / "fixture.py").write_text("print('fixed-operation-ok')", encoding="utf-8")
    assert (await module.command("fixture.py")).strip() == b"fixed-operation-ok"


@pytest.mark.anyio
async def test_retention_removes_only_signed_expired_artifacts_and_preserves_queued_verification(
    integration_config, monkeypatch, tmp_path
):
    module = load_executor(monkeypatch)
    import backup_archive

    monkeypatch.setattr(backup_archive, "key_folder", lambda: tmp_path / "keys")
    folder = tmp_path / "registered-backups"
    monkeypatch.setattr(module, "BACKUPS", folder)
    db, _, jobs, _ = await setup(integration_config)
    worker = module.Executor(db, jobs.settings)
    archives = []
    try:
        async with db.transaction() as conn:
            await put_state(conn, POLICY_KEY, BackupPolicy(enabled=True).model_dump(mode="json"))
            for days in (0, 40, 41):
                archive = backup_archive.ArchiveWriter(folder)
                archive.json("fixture.json", {"test_only": True})
                archive.seal({"test_only": True})
                archives.append(archive)
                await put_state(
                    conn,
                    RECORD_PREFIX + archive.id,
                    {
                        "id": archive.id,
                        "state": "AVAILABLE",
                        "created_at": (datetime.now(UTC) - timedelta(days=days)).isoformat(),
                        "verified_at": None,
                    },
                )
        await worker.jobs.enqueue(
            JobType.BACKUP_VERIFY,
            "protected-test-" + str(uuid4()),
            {"mode": "VERIFY", "backup_record_id": archives[2].id, "note": "test queued protection"},
        )
        await worker.prune()
        assert (archives[0].folder / "fixture.json.enc").is_file()
        assert not (archives[1].folder / "fixture.json.enc").exists()
        assert (archives[1].folder / "manifest.json").is_file()
        assert (archives[2].folder / "fixture.json.enc").is_file()
        async with db.transaction() as conn:
            expired = await one(
                conn, "SELECT value FROM ag_setting WHERE key=:key", key=RECORD_PREFIX + archives[1].id
            )
            assert expired["value"]["state"] == "EXPIRED"
    finally:
        await db.engine.dispose()


@pytest.mark.anyio
async def test_queued_staff_permission_is_rechecked_before_privileged_execution(
    integration_config, monkeypatch
):
    module = load_executor(monkeypatch)
    db, _, jobs, _ = await setup(integration_config)
    worker = module.Executor(db, jobs.settings)
    monkeypatch.setattr(module, "staff_still_authorized", lambda actor: False)
    calls = []

    async def forbidden_command(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(module, "command", forbidden_command)
    from app.domain.capabilities import JobLease

    try:
        job = await worker.jobs.enqueue(
            JobType.BACKUP_VERIFY,
            "revoked-" + str(uuid4()),
            {"mode": "CREATE", "note": "test revoked"},
            created_by=manager().id,
        )
        job = await worker.jobs.claim("backup-executor-test", job["id"])
        worker.current_job = job
        lease = JobLease(
            job["id"], JobType.BACKUP_VERIFY, job["lease_owner"], job["fencing_token"], job["parameters"]
        )
        with pytest.raises(DomainError) as denied:
            await worker.dispatch(lease)
        assert denied.value.code == "FORBIDDEN" and not calls
    finally:
        await db.engine.dispose()
