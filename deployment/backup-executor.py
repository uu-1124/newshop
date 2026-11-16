"""Privileged fixed-operation executor, outside the application's maintenance writers."""
# ruff: noqa: E402 -- workspace module path is installed before application imports.

import asyncio
import logging
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4, uuid5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mall-agent"))

import psycopg
import pymysql
from backup_archive import ArchiveReader, BackupError
from backup_data import load_json

from app.application.backups import (
    RECORD_PREFIX,
    STATE_KEY,
    put_state,
    read_backup_policy,
    register_backups,
)
from app.application.jobs import JobHandlers, Jobs
from app.domain.capabilities import JobType
from app.domain.identity import DomainError
from app.infrastructure.postgres.database import Database, execute, one, rows
from app.infrastructure.worker_status import worker_status
from app.runtime import run
from app.settings import get_settings
from app.workers.jobs import execute_job

BACKUPS = ROOT / "deployment/.run/consistent-backups"
RESTORES = ROOT / "deployment/.run/isolated-restores"
JOURNALS = ROOT / "deployment/.run/erasure-journals"


def utc():
    return datetime.now(UTC).isoformat()


def staff_still_authorized(actor):
    source = load_json(ROOT / "deployment/config/admin.yaml")["spring"]["datasource"]
    with pymysql.connect(
        host="127.0.0.1",
        port=13306,
        user=source["username"],
        password=source["password"],
        database="mall",
        connect_timeout=5,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """SELECT count(*) FROM ums_admin a JOIN ums_admin_role_relation ar ON ar.admin_id=a.id
                JOIN ums_role r ON r.id=ar.role_id AND r.status=1 JOIN ums_role_resource_relation rr ON rr.role_id=r.id
                JOIN ums_resource s ON s.id=rr.resource_id WHERE a.id=%s AND a.status=1
                AND s.name='agent:backup:verify' AND s.url LIKE '/mall-agent/v1/admin/%%'""",
                (actor,),
            )
            return cursor.fetchone()[0] > 0


def registered_directories(folder):
    if not folder.exists():
        return []
    return [p for p in folder.iterdir() if p.is_dir() and not p.is_symlink() and canonical_uuid(p.name)]


def canonical_uuid(value):
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def retained_ids(records, now, days, weeks):
    keep = {
        record["id"]
        for record in records
        if datetime.fromisoformat(record["created_at"]) >= now - timedelta(days=days)
    }
    buckets = {}
    for record in sorted(records, key=lambda row: row["created_at"], reverse=True):
        date = datetime.fromisoformat(record["created_at"])
        if date >= now - timedelta(weeks=weeks):
            buckets.setdefault(date.isocalendar()[:2], record["id"])
    keep.update(buckets.values())
    if records:
        keep.add(max(records, key=lambda row: row["created_at"])["id"])
    return keep


async def command(script, *args, timeout=840):
    import subprocess

    process = subprocess.Popen(
        [sys.executable, "-u", "-B", str(ROOT / "deployment" / script), *map(str, args)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        output, _ = await asyncio.to_thread(process.communicate, timeout=timeout)
    except BaseException:
        if process.poll() is None:
            if os.name == "nt":
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                process.kill()
            await asyncio.to_thread(process.wait)
        raise
    if process.returncode:
        raise DomainError("BACKUP_OPERATION_FAILED", "备份或恢复执行失败，原环境保持受控状态", 503)
    if len(output) > 65536:
        raise DomainError("BACKUP_PROTOCOL_ERROR", "备份执行器结果超限", 503)
    return output


class Executor:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.state = {
            "last_backup_at": None,
            "last_restore_at": None,
            "last_journal_at": None,
            "last_error": None,
            "maintenance_stuck": False,
        }
        self.registry = JobHandlers()
        register_backups(self.registry, self.dispatch)
        self.jobs = Jobs(db, settings, self.registry, lane="backups")
        self.current_job = None
        self.next_inventory = 0
        self.next_journal_retry = 0
        self.next_prune = 0

    async def status(self):
        marker = ROOT / "deployment/.run/maintenance.json"
        self.state["maintenance_stuck"] = marker.exists() and self.current_job is None
        self.state["updated_at"] = utc()
        async with self.db.transaction() as conn:
            await put_state(conn, STATE_KEY, self.state)

    async def inventory(self):
        latest_restore = {}
        for folder in registered_directories(RESTORES):
            if not (folder / "result.json").is_file():
                continue
            try:
                result = await asyncio.to_thread(ArchiveReader(RESTORES, folder.name).result)
            except (BackupError, ValueError, KeyError, FileNotFoundError):
                continue
            if result.get("result") != "RESTORED_DATA_MATCH_AND_NONEXECUTING_ISOLATION_PASSED":
                continue
            bid = result["backup_id"]
            if result["checked_at"] > latest_restore.get(bid, {}).get("checked_at", ""):
                latest_restore[bid] = result
        for folder in registered_directories(BACKUPS):
            if not (folder / "manifest.json").is_file():
                continue
            async with self.db.transaction() as conn:
                existing = await one(
                    conn, "SELECT value FROM ag_setting WHERE key=:key", key=RECORD_PREFIX + folder.name
                )
            if existing and existing["value"].get("state") == "EXPIRED":
                continue
            try:
                archive = ArchiveReader(BACKUPS, folder.name)
                await asyncio.to_thread(archive.verify)
                metadata = archive.manifest["metadata"]
                result = latest_restore.get(folder.name)
                record = {
                    "id": folder.name,
                    "state": "AVAILABLE",
                    "created_at": metadata["snapshot_at"],
                    "artifacts": len(archive.manifest["artifacts"]),
                    "object_count": metadata["object_count"],
                    "verified_at": result["checked_at"] if result else None,
                    "restore_seconds": result["restore_seconds"] if result else None,
                    "last_restore_id": result["restore_id"] if result else None,
                }
                self.state["last_backup_at"] = max(
                    self.state["last_backup_at"] or "", metadata["snapshot_at"]
                )
                if result:
                    self.state["last_restore_at"] = max(
                        self.state["last_restore_at"] or "", result["checked_at"]
                    )
                async with self.db.transaction() as conn:
                    await put_state(conn, RECORD_PREFIX + folder.name, record)
            except (BackupError, ValueError, KeyError, FileNotFoundError):
                self.state["last_error"] = "REGISTERED_BACKUP_AUTHENTICATION_FAILED"
        self.next_inventory = time.monotonic() + 300

    async def policy_matches(self, job):
        params = job["parameters"]
        if not params["scheduled"]:
            return
        async with self.db.transaction() as conn:
            policy = await read_backup_policy(conn)
        if not policy["value"]["enabled"] or policy["version"] != params["policy_version"]:
            raise DomainError("BACKUP_POLICY_CHANGED", "备份策略已变化，旧调度不再执行", 409)

    async def dispatch(self, lease):
        job = self.current_job
        if not job or job["id"] != lease.id:
            raise DomainError("LEASE_LOST", "备份任务租约失效", 409)
        params = job["parameters"]
        await self.policy_matches(job)
        if job["created_by"] is not None and not await asyncio.to_thread(
            staff_still_authorized, job["created_by"]
        ):
            raise DomainError("FORBIDDEN", "创建任务的员工已无备份验证权限", 403)
        await self.status()
        operation_id = str(uuid5(job["id"], str(job["attempts"])))
        mode = params["mode"]
        bid = params["backup_record_id"]
        if mode == "CREATE":
            await command(
                "backup-agent-consistent.py",
                "create",
                "--plan-id",
                operation_id,
                "--job-id",
                str(job["id"]),
                "--job-owner",
                job["lease_owner"],
                "--job-fence",
                str(job["fencing_token"]),
            )
            bid = operation_id
            await self.inventory()
            self.state["last_error"] = None
            return {"mode": "CREATE", "backup_record_id": bid, "verified": False}
        async with self.db.transaction() as conn:
            record = await one(conn, "SELECT value FROM ag_setting WHERE key=:key", key=RECORD_PREFIX + bid)
        if not record or record["value"]["state"] != "AVAILABLE":
            raise DomainError("NOT_FOUND", "登记备份不存在", 404)
        archive = ArchiveReader(BACKUPS, bid)
        count = await asyncio.to_thread(archive.verify)
        if mode == "VERIFY":
            # Archive authentication and actual isolated restore remain separate,
            # durable tasks. A passed checksum is never shown as a passed restore.
            child = await self.jobs.enqueue(
                JobType.RESTORE_VERIFY,
                "restore-after:" + str(job["id"]),
                {**params, "mode": "RESTORE"},
                job["created_by"],
                max_attempts=1,
            )
            return {
                "mode": "VERIFY",
                "backup_record_id": bid,
                "authenticated_artifacts": count,
                "restore_job_id": str(child["id"]),
                "verified": False,
            }
        try:
            await command(
                "restore-agent-isolated.py", "restore", "--backup-id", bid, "--plan-id", operation_id
            )
        except BaseException:
            if (RESTORES / operation_id / "manifest.json").is_file():
                try:
                    await command(
                        "restore-agent-isolated.py", "destroy", "--restore-id", operation_id, timeout=90
                    )
                except Exception:
                    self.state["last_error"] = "RESTORE_CLEANUP_REQUIRED"
            raise
        restored = ArchiveReader(RESTORES, operation_id).result()
        if restored["result"] != "RESTORED_DATA_MATCH_AND_NONEXECUTING_ISOLATION_PASSED":
            raise DomainError("RESTORE_FAILED", "隔离恢复核对未通过", 503)
        await command("restore-agent-isolated.py", "destroy", "--restore-id", operation_id, timeout=90)
        await self.inventory()
        self.state["last_error"] = None
        return {
            "mode": "RESTORE",
            "backup_record_id": bid,
            "restore_id": operation_id,
            "restore_seconds": restored["restore_seconds"],
            "verified_at": restored["checked_at"],
            "isolation_destroyed": True,
            "verified": True,
        }

    async def schedule(self):
        async with self.db.transaction() as conn:
            policy = await read_backup_policy(conn)
            current = await one(conn, "SELECT clock_timestamp() n")
        value, now = policy["value"], current["n"]
        if not value["enabled"]:
            return
        last = self.state.get("last_journal_at")
        if (
            not last
            or datetime.fromisoformat(last) < now - timedelta(minutes=value["journal_interval_minutes"])
        ) and time.monotonic() >= self.next_journal_retry:
            self.next_journal_retry = time.monotonic() + 60
            await command("backup-agent-consistent.py", "capture-erasure-journal", timeout=60)
            self.state["last_journal_at"] = utc()
        # Select the latest due window even when the executor starts after UTC midnight.
        # Retain its calendar key so repeated starts never replay the same window.
        scheduled_at = now.replace(hour=value["daily_hour_utc"], minute=0, second=0, microsecond=0)
        if scheduled_at > now:
            scheduled_at -= timedelta(days=1)
        date = scheduled_at.date().isoformat()
        params = {
            "mode": "CREATE",
            "backup_record_id": None,
            "policy_version": policy["version"],
            "scheduled": True,
            "note": "按已启用每日策略执行一致备份",
        }
        async with self.db.transaction() as conn:
            daily = await one(
                conn,
                "SELECT * FROM ag_job WHERE job_type='BACKUP_VERIFY' AND dedupe_key=:key",
                key="daily-backup:" + date,
            )
        if not daily:
            daily = await self.jobs.enqueue(
                JobType.BACKUP_VERIFY, "daily-backup:" + date, params, max_attempts=3
            )
        if scheduled_at.weekday() == value["restore_weekday_utc"] and daily["state"] == "SUCCEEDED":
            async with self.db.transaction() as conn:
                candidates = await rows(
                    conn,
                    "SELECT value FROM ag_setting WHERE key LIKE 'agent-backup-record:%' AND value->>'state'='AVAILABLE'",
                )
            if candidates:
                newest = max(candidates, key=lambda row: row["value"]["created_at"])["value"]
                params = {
                    **params,
                    "mode": "RESTORE",
                    "backup_record_id": newest["id"],
                    "note": "按每周策略验证隔离恢复",
                }
                async with self.db.transaction() as conn:
                    existing = await one(
                        conn,
                        "SELECT id FROM ag_job WHERE job_type='RESTORE_VERIFY' AND dedupe_key=:key",
                        key="weekly-restore:" + date,
                    )
                if not existing:
                    await self.jobs.enqueue(
                        JobType.RESTORE_VERIFY, "weekly-restore:" + date, params, max_attempts=1
                    )

    async def prune(self):
        if time.monotonic() < self.next_prune:
            return
        self.next_prune = time.monotonic() + 3600
        async with self.db.transaction() as conn:
            policy = await read_backup_policy(conn)
            if not policy["value"]["enabled"]:
                return
            records = [
                row["value"]
                for row in await rows(
                    conn,
                    "SELECT value FROM ag_setting WHERE key LIKE 'agent-backup-record:%' AND value->>'state'='AVAILABLE'",
                )
            ]
            protected = {
                row["bid"]
                for row in await rows(
                    conn,
                    "SELECT parameters->>'backup_record_id' bid FROM ag_job WHERE state IN ('QUEUED','RUNNING') AND job_type IN ('BACKUP_VERIFY','RESTORE_VERIFY')",
                )
            }
        keep = (
            retained_ids(
                records,
                datetime.now(UTC),
                policy["value"]["retention_days"],
                policy["value"]["weekly_retention_weeks"],
            )
            | protected
        )
        verified = [row for row in records if row.get("verified_at")]
        if verified:
            keep.add(max(verified, key=lambda row: row["verified_at"])["id"])
        for record in records:
            if record["id"] in keep:
                continue
            archive = ArchiveReader(BACKUPS, record["id"])
            await asyncio.to_thread(archive.verify)
            # Only signed registered artifact basenames; no recursive delete.
            async with self.db.transaction() as conn:
                await one(
                    conn,
                    "SELECT key FROM ag_setting WHERE key=:key FOR UPDATE",
                    key=RECORD_PREFIX + record["id"],
                )
                pending = await one(
                    conn,
                    "SELECT id FROM ag_job WHERE state IN ('QUEUED','RUNNING') AND job_type IN ('BACKUP_VERIFY','RESTORE_VERIFY') AND parameters->>'backup_record_id'=:id LIMIT 1",
                    id=record["id"],
                )
                if pending:
                    continue
                for artifact in archive.manifest["artifacts"].values():
                    target = archive.folder / artifact["file"]
                    if target.is_symlink() or target.resolve().parent != archive.folder.resolve():
                        raise BackupError("Invalid registered pruning target")
                    target.unlink(missing_ok=True)
                record["state"] = "EXPIRED"
                await put_state(conn, RECORD_PREFIX + record["id"], record)


async def main():
    settings = get_settings().model_copy(
        update={"job_lease_seconds": 300, "job_retry_base_seconds": 60, "backups_enabled": True}
    )
    if not get_settings().backups_enabled:
        return
    config = load_json(ROOT / "deployment/.run/agent-env.json")
    # Dedicated OS/SSH authority is separate from API and ordinary worker processes.
    leader = psycopg.connect(
        config["databaseUrl"].replace("postgresql+psycopg://", "postgresql://"), autocommit=True
    )
    acquired = leader.execute(
        "SELECT pg_try_advisory_lock(hashtextextended('agent-backup-executor',0))"
    ).fetchone()[0]
    if not acquired:
        raise RuntimeError("Another registered backup executor is active")
    db = Database(settings.database_url.get_secret_value())
    worker = Executor(db, settings)
    owner = "backup-executor-" + str(uuid4())
    try:
        async with db.transaction() as conn:
            old = await one(conn, "SELECT value FROM ag_setting WHERE key=:key", key=STATE_KEY)
        if old:
            worker.state.update(old["value"])
        while True:
            leader.execute("SELECT 1")
            try:
                if time.monotonic() >= worker.next_inventory:
                    await worker.inventory()
                await worker.status()
                if not worker.state["maintenance_stuck"]:
                    await worker.schedule()
                    job = await worker.jobs.claim(owner)
                    if job:
                        worker.current_job = job

                        async def report_status():
                            while True:
                                await asyncio.sleep(15)
                                await worker.status()

                        heartbeat = asyncio.create_task(report_status())
                        try:
                            outcome = await execute_job(worker.jobs, worker.registry, job)
                        finally:
                            heartbeat.cancel()
                            await asyncio.gather(heartbeat, return_exceptions=True)
                        worker.current_job = None
                        if outcome["state"] == "FAILED":
                            worker.state["last_error"] = outcome["error_code"]
                        async with db.transaction() as conn:
                            await execute(
                                conn,
                                """INSERT INTO ag_audit_log(id,actor_kind,actor_id,operation,resource_type,resource_id,request_id,outcome,details)
                                VALUES(:id,:kind,:actor,'backupExecutor','BACKUP',:resource,:request,:outcome,'{}'::jsonb)""",
                                id=uuid4(),
                                kind="STAFF" if job["created_by"] else "SYSTEM",
                                actor=job["created_by"],
                                resource=str(job["id"]),
                                request=uuid4(),
                                outcome="SUCCEEDED" if outcome["state"] == "SUCCEEDED" else "FAILED",
                            )
                        await worker.status()
                    else:
                        await worker.prune()
                await worker_status(db, owner, "backups", "IDLE")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                worker.current_job = None
                worker.state["last_error"] = type(error).__name__
                logging.warning("backup executor deferred error_type=%s", type(error).__name__)
            await asyncio.sleep(5)
    finally:
        leader.close()
        await db.engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run(main())
