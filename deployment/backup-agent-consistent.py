"""Create a real cross-store snapshot in an owned maintenance window.

No arbitrary paths/commands are accepted. A crash leaves maintenance fail-closed.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from backup_archive import ArchiveReader, ArchiveWriter, BackupError
from backup_data import (
    capture_erasure_journal,
    configuration_capsule,
    deletion_journal,
    export_objects,
    export_vectors,
    lock_postgres,
    pg_connection,
    pg_fingerprints,
    preflight,
    serializable,
    snapshot_identity,
)

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "deployment/.run/consistent-backups"
NODE = Path("D:/develop/nodejs/node.exe")
POWERSHELL = Path(
    "C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe"
)


def log(stage, **facts):
    print(json.dumps({"stage": stage, **facts}, ensure_ascii=False), flush=True)


def transport(operation, payload=None, plan=None):
    args = [str(NODE), str(ROOT / "deployment/backup-transport.cjs"), operation]
    if plan:
        args.append(plan)
    process = subprocess.run(args, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=240)
    if process.returncode:
        raise BackupError("Registered transport failed: " + operation)
    return process.stdout


def transport_chunks(operation):
    process = subprocess.Popen(
        [str(NODE), str(ROOT / "deployment/backup-transport.cjs"), operation],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        while True:
            chunk = process.stdout.read(65536)
            if not chunk:
                break
            yield chunk
        if process.wait(timeout=30):
            raise BackupError("Registered dump transport failed")
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


@contextmanager
def mysql_lock():
    process = subprocess.Popen(
        [str(NODE), str(ROOT / "deployment/backup-transport.cjs"), "mysql-lock"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        if process.stdout.readline().strip() != b"AGENT_LOCKED":
            raise BackupError("Could not establish MySQL global read lock")
        yield process
        if process.poll() is not None:
            raise BackupError("MySQL consistency lock was lost during snapshot")
    finally:
        if process.poll() is None:
            try:
                process.communicate(b"release\n", timeout=15)
            except Exception:
                process.kill()
        process.wait(timeout=15)


def maintenance(mode, plan):
    result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-File",
            str(ROOT / "deployment/agent-maintenance.ps1"),
            "-Mode",
            mode,
            "-PlanId",
            plan,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=100,
    )
    if result.returncode:
        raise BackupError("Owned maintenance transition failed: " + mode)


def manager_ready():
    with urllib.request.urlopen("http://127.0.0.1:18080/health", timeout=5) as response:
        state = json.load(response)
    if not state.get("sshConnected") or "maintenance" not in state:
        raise BackupError("Manager must be reloaded with the tested maintenance guard before backup")
    if state["maintenance"].get("active"):
        raise BackupError("Another maintenance window is active")


def backup(plan_id=None, owned_job=None):
    manager_ready()
    with pg_connection(ROOT, True) as conn:
        preflight(conn, owned_job)
    # Check source availability before starting a service interruption.
    mysql_state = transport("mysql-state").decode().splitlines()
    if len(mysql_state) < 2 or int(mysql_state[1]) != 0:
        raise BackupError("Unreviewed MySQL scheduled events must be handled before snapshot")
    if plan_id and (FOLDER / plan_id / "manifest.json").is_file():
        archive = ArchiveReader(FOLDER, plan_id)
        archive.verify()
        log("backup_already_sealed", backup_id=plan_id)
        return plan_id
    archive = ArchiveWriter(FOLDER, plan_id=plan_id)
    paused = False
    started = datetime.now(timezone.utc)
    try:
        log("maintenance_pausing", backup_id=archive.id)
        # Own the marker even if pause fails part way; finally releases only this owner.
        paused = True
        maintenance("pause", archive.id)
        with mysql_lock() as mysql_session:
            with pg_connection(ROOT) as conn:
                preflight(conn, owned_job)
                locked_tables = lock_postgres(conn)
                checked_at = conn.execute("SELECT clock_timestamp() now,pg_current_wal_lsn() lsn").fetchone()
                log("snapshot_locked", postgres_tables=len(locked_tables))
                fingerprints = pg_fingerprints(conn)
                mysql_canonical = hashlib.sha256(transport("mysql-data")).hexdigest()
                archive.add("postgres.dump", transport_chunks("pg-dump"))
                log("postgres_encrypted")
                archive.add("mysql.sql", transport_chunks("mysql-dump"))
                log("mysql_encrypted")
                object_inventory, object_tar = export_objects(ROOT, conn)
                archive.add("objects.tar", [object_tar])
                archive.json("objects.json", object_inventory)
                log("private_objects_encrypted", objects=len(object_inventory["objects"]))
                vectors = export_vectors(ROOT, conn)
                archive.json("vectors.json", vectors)
                log(
                    "knowledge_vectors_encrypted",
                    collections=vectors["collection_count"],
                    vectors=vectors["vector_count"],
                )
                journal = serializable(deletion_journal(conn))
                archive.json("deletions.json", journal)
                configuration = configuration_capsule(ROOT)
                identity = snapshot_identity(conn, configuration)
                archive.json("configuration.json", configuration)
                archive.json(
                    "inventory.json",
                    {
                        "postgres": fingerprints,
                        "mysql_data_sha256": mysql_canonical,
                        "event_clock": serializable(conn.execute("SELECT * FROM ag_event_clock").fetchone()),
                        "knowledge_versions": serializable(
                            list(
                                conn.execute(
                                    "SELECT id,state,index_collection,embedding_profile_id,embedding_profile_version FROM ag_kb_version ORDER BY id"
                                )
                            )
                        ),
                        "mysql_state": mysql_state,
                    },
                )
                # Revalidate the same connections and locks immediately before sealing.
                repeated_inventory, _ = export_objects(ROOT, conn)
                if repeated_inventory != object_inventory:
                    raise BackupError("Private object inventory changed during snapshot")
                if mysql_session.poll() is not None or pg_fingerprints(conn) != fingerprints:
                    raise BackupError("Consistency lock or source fingerprint changed")
                manifest = archive.seal(
                    {
                        "consistency": "ALL_MANAGED_WRITERS_PAUSED_MYSQL_GLOBAL_READ_LOCK_PG_SHARE_LOCKS",
                        "snapshot_at": checked_at["now"].isoformat(),
                        "postgres_lsn": str(checked_at["lsn"]),
                        **identity,
                        "object_count": len(object_inventory["objects"]),
                        "knowledge_collections": vectors["collection_count"],
                        "knowledge_vectors": vectors["vector_count"],
                        "deletion_markers": len(journal["tombstones"]),
                        "restore_verified": False,
                        "scope": [
                            "MYSQL_MALL_AND_OPERATION_OUTBOX_PROOF_LEDGERS",
                            "POSTGRES_AND_CHECKPOINTS",
                            "PRIVATE_OBJECT_BYTES",
                            "KNOWLEDGE_SOURCE_AND_DERIVED_VECTORS",
                            "DELETIONS",
                            "ENCRYPTED_CONFIGURATION",
                        ],
                    }
                )
        log("backup_sealed", backup_id=archive.id, artifacts=len(manifest["artifacts"]))
    finally:
        if paused:
            maintenance("resume", archive.id)
            log("maintenance_released", backup_id=archive.id)
    verified = ArchiveReader(FOLDER, archive.id).verify()
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    log(
        "backup_authenticated",
        backup_id=archive.id,
        artifacts=verified,
        maintenance_and_verification_seconds=round(elapsed, 2),
        restore_verified=False,
    )
    return archive.id


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["create", "verify", "capture-erasure-journal"])
    parser.add_argument("--backup-id")
    parser.add_argument("--plan-id")
    parser.add_argument("--job-id")
    parser.add_argument("--job-owner")
    parser.add_argument("--job-fence", type=int)
    arguments = parser.parse_args()
    try:
        if arguments.mode == "create":
            owned_job = (
                (arguments.job_id, arguments.job_owner, arguments.job_fence) if arguments.job_id else None
            )
            backup(arguments.plan_id, owned_job)
        elif arguments.mode == "capture-erasure-journal":
            journal_id, journal = capture_erasure_journal(ROOT)
            log("erasure_journal_encrypted", journal_id=journal_id, captured_at=journal["captured_at"])
        else:
            if not arguments.backup_id:
                raise BackupError("A registered backup identifier is required")
            count = ArchiveReader(FOLDER, arguments.backup_id).verify()
            log(
                "archive_authenticated",
                backup_id=arguments.backup_id,
                artifacts=count,
                restore_verified=False,
            )
    except Exception as error:
        # Safe error types/messages only: driver exceptions may contain private values.
        log("failed", error=str(error) if isinstance(error, BackupError) else type(error).__name__)
        sys.exit(1)
