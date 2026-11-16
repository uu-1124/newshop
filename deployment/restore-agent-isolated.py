"""Restore an authenticated backup into new, isolated and nonexecuting resources."""

import argparse
import hashlib
import importlib.util
import io
import json
import secrets
import subprocess
import sys
import tarfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pymysql
import urllib3
from backup_archive import ArchiveReader, ArchiveWriter, BackupError, canonical
from backup_data import capture_erasure_journal, pg_fingerprints
from minio import Minio
from minio.error import S3Error
from psycopg.rows import dict_row
from restore_safety import apply_deletions, freeze_runtime, verify_invariants

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "deployment/.run/consistent-backups"
RESTORES = ROOT / "deployment/.run/isolated-restores"
NODE = Path("D:/develop/nodejs/node.exe")
spec = importlib.util.spec_from_file_location(
    "consistent_backup", ROOT / "deployment/backup-agent-consistent.py"
)
operations = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operations)
log, transport = operations.log, operations.transport


@contextmanager
def tunnel(plan):
    process = None
    try:
        for attempt in range(3):
            process = subprocess.Popen(
                [str(NODE), str(ROOT / "deployment/backup-transport.cjs"), "restore-tunnel", plan],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            line = process.stdout.readline()
            if line:
                break
            diagnostic = process.stderr.read(2048).decode(errors="replace")
            process.wait(timeout=10)
            if attempt == 2:
                reason = next(
                    (
                        row
                        for row in diagnostic.splitlines()
                        if row.startswith("Isolated restore operation failed: ")
                    ),
                    None,
                )
                raise BackupError("Isolated restore tunnel unavailable" + (": " + reason if reason else ""))
            time.sleep(2)
        value = json.loads(line)
        if value["plan_id"] != plan:
            raise BackupError("Restore tunnel identity mismatch")
        yield value["ports"]
    finally:
        if process is not None and process.poll() is None:
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        process.wait(timeout=15)


def connect_pg(ports, credentials):
    conn = psycopg.connect(
        host="127.0.0.1",
        port=ports["pg"],
        dbname="mall_agent",
        user="mall_agent_owner",
        password=credentials["pg_password"],
        connect_timeout=5,
        row_factory=dict_row,
    )
    conn.execute("SET TIME ZONE 'UTC'")
    conn.execute("SET statement_timeout='120s'")
    conn.commit()
    return conn


def wait_databases(ports, credentials):
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        try:
            with connect_pg(ports, credentials):
                pass
            with pymysql.connect(
                host="127.0.0.1",
                port=ports["mysql"],
                user="root",
                database="mall",
                password=credentials["mysql_password"],
                connect_timeout=3,
            ) as mysql:
                with mysql.cursor() as cursor:
                    cursor.execute("SELECT @@event_scheduler")
                    if cursor.fetchone()[0] != "OFF":
                        raise BackupError("Isolated MySQL scheduler must remain disabled")
            return
        except BackupError:
            raise
        except Exception:
            time.sleep(2)
    raise BackupError("Isolated databases did not become ready")


def restore_objects(reader, ports, credentials):
    client = Minio(
        "127.0.0.1:" + str(ports["objects"]),
        "restoreroot",
        credentials["objects_password"],
        secure=False,
        http_client=urllib3.PoolManager(timeout=urllib3.Timeout(connect=5, read=30), retries=False),
    )
    inventory = reader.json("objects.json")
    bucket = inventory["bucket"]
    if bucket != "mall-agent-private":
        raise BackupError("Unregistered restored bucket")
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
    expected = {entry["member"]: entry for entry in inventory["objects"]}
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(reader.read("objects.tar")), mode="r:") as archive:
        for member in archive:
            entry = expected.get(member.name)
            if not entry or not member.isfile() or member.size != entry["bytes"] or member.name in seen:
                raise BackupError("Unexpected object archive entry")
            seen.add(member.name)
            data = archive.extractfile(member).read(member.size + 1)
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise BackupError("Restored object hash mismatch")
            client.put_object(
                bucket,
                entry["object_key"],
                io.BytesIO(data),
                len(data),
                content_type=entry["content_type"] or "application/octet-stream",
            )
            response = client.get_object(bucket, entry["object_key"])
            try:
                if hashlib.sha256(response.read(member.size + 1)).hexdigest() != entry["sha256"]:
                    raise BackupError("Restored object readback mismatch")
            finally:
                response.close()
                response.release_conn()
    if seen != set(expected):
        raise BackupError("Restored object archive incomplete")
    return client, bucket, len(seen)


def restore_vectors(reader, ports, credentials):
    import logging

    from pymilvus import MilvusClient

    sys.path.insert(0, str(ROOT / "mall-agent"))
    from app.infrastructure.milvus_candidates import MilvusCandidates

    logging.getLogger("pymilvus").setLevel(logging.CRITICAL)
    started, deadline = time.monotonic(), time.monotonic() + 210
    client = None
    while time.monotonic() < deadline:
        try:
            client = MilvusClient(
                uri="http://127.0.0.1:" + str(ports["vectors"]),
                user="root",
                password="Milvus",
                timeout=5,
                grpc_options={"grpc.enable_http_proxy": 0},
            )
            client.list_databases(timeout=5)
            break
        except Exception:
            if client:
                client.close()
            client = None
            time.sleep(3)
    if client is None:
        raise BackupError("Isolated vector service did not become ready")
    try:
        client.update_password(
            user_name="root", old_password="Milvus", new_password=credentials["objects_password"]
        )
    finally:
        client.close()
    client = MilvusClient(
        uri="http://127.0.0.1:" + str(ports["vectors"]),
        user="root",
        password=credentials["objects_password"],
        timeout=20,
        grpc_options={"grpc.enable_http_proxy": 0},
    )
    data = reader.json("vectors.json")
    try:
        client.create_database(data["database"])
        client.use_database(data["database"])
        total = 0
        for entry in data["collections"]:
            spec, rows = entry["spec"], entry["rows"]
            MilvusCandidates(client, **spec).create()
            for offset in range(0, len(rows), 100):
                client.upsert(spec["collection"], rows[offset : offset + 100], timeout=30)
            client.flush(spec["collection"], timeout=30)
            returned = client.get(
                spec["collection"], ids=[row["chunk_id"] for row in rows], output_fields=["*"], timeout=20
            )
            found = {row["chunk_id"]: row for row in returned}
            if len(found) != len(rows):
                raise BackupError("Rebuilt vector count mismatch")
            for row in rows:
                copy = found[row["chunk_id"]]
                if any(copy[key] != value for key, value in row.items() if key != "vector"):
                    raise BackupError("Rebuilt vector source/version mismatch")
                if len(copy["vector"]) != len(row["vector"]) or any(
                    abs(a - b) > 1e-6 for a, b in zip(copy["vector"], row["vector"], strict=True)
                ):
                    raise BackupError("Rebuilt vector values mismatch")
            total += len(rows)
        return {
            "collections": len(data["collections"]),
            "vectors": total,
            "seconds": round(time.monotonic() - started, 2),
            "supplier_calls": 0,
        }
    finally:
        client.close()


def check_erasure_fixture(conn, objects, bucket, fixture, after):
    row = conn.execute(
        """SELECT c.member_id,c.title,c.deleted_at AS conversation_deleted,m.text_content,m.deleted_at AS message_deleted,
        a.state AS attachment_state FROM ag_conversation c JOIN ag_message m ON m.conversation_id=c.id
        JOIN ag_attachment a ON a.id=%s WHERE c.id=%s AND m.id=%s""",
        (fixture["attachment_id"], fixture["conversation_id"], fixture["message_id"]),
    ).fetchone()
    if not row or str(row["member_id"]) != fixture["member_id"]:
        raise BackupError("Erasure acceptance fixture ownership mismatch")
    if not after:
        if (
            row["conversation_deleted"]
            or row["message_deleted"]
            or row["attachment_state"] != "READY"
            or hashlib.sha256(row["text_content"].encode()).hexdigest() != fixture["content_sha256"]
        ):
            raise BackupError("Old backup does not contain the original visible acceptance fixture")
        return {"visible_in_old_backup": True, "private_image_in_old_backup": True}
    if (
        not row["conversation_deleted"]
        or not row["message_deleted"]
        or row["text_content"]
        or row["attachment_state"] != "DELETED"
    ):
        raise BackupError("Post-backup deletion was resurrected")
    try:
        objects.stat_object(bucket, fixture["object_key"])
    except S3Error as error:
        if error.code not in {"NoSuchKey", "NoSuchObject"}:
            raise BackupError("Could not verify erased object absence") from None
    else:
        raise BackupError("Post-backup deleted private bytes were restored")
    return {"conversation_hidden": True, "message_erased": True, "ordinary_attachment_bytes_absent": True}


def verify_business_ledgers(conn, ports, credentials):
    actions = list(
        conn.execute(
            "SELECT a.operation_no,a.member_id,a.action_type,a.parameters_hash,d.confirmation_audit_id FROM ag_action a LEFT JOIN ag_action_dispatch d ON d.action_id=a.id WHERE a.state='SUCCEEDED'"
        )
    )
    proofs = list(
        conn.execute(
            "SELECT return_apply_id FROM ag_attachment_ref WHERE protected AND return_apply_id IS NOT NULL"
        )
    )
    with pymysql.connect(
        host="127.0.0.1",
        port=ports["mysql"],
        user="root",
        database="mall",
        password=credentials["mysql_password"],
        connect_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
    ) as mysql:
        with mysql.cursor() as cursor:
            cursor.execute("SELECT @@event_scheduler AS state")
            if cursor.fetchone()["state"] != "OFF":
                raise BackupError("Restored event scheduler unexpectedly enabled")
            for action in actions:
                cursor.execute(
                    "SELECT state,member_id,action_type,parameters_hash,confirmation_audit_id FROM oms_agent_operation WHERE operation_no=%s",
                    (str(action["operation_no"]),),
                )
                operation = cursor.fetchone()
                if (
                    not operation
                    or operation["state"] != "SUCCEEDED"
                    or any(
                        str(operation[key]) != str(action[key])
                        for key in ("member_id", "action_type", "parameters_hash", "confirmation_audit_id")
                    )
                ):
                    raise BackupError(
                        "Restored successful action does not match original MySQL operation ledger"
                    )
            for proof in proofs:
                cursor.execute(
                    "SELECT count(*) n FROM oms_agent_return WHERE return_id=%s", (proof["return_apply_id"],)
                )
                if cursor.fetchone()["n"] != 1:
                    raise BackupError("Protected evidence lost its restored return ledger")
            cursor.execute(
                "SELECT count(*) n FROM oms_agent_business_outbox b LEFT JOIN oms_order o ON o.id=b.order_id WHERE o.id IS NULL"
            )
            if cursor.fetchone()["n"]:
                raise BackupError("Restored Java outbox lost its original order")
            cursor.execute(
                "SELECT count(*) n FROM oms_agent_return r LEFT JOIN oms_order_item i ON i.id=r.order_item_id WHERE i.id IS NULL OR i.order_id<>r.order_id"
            )
            if cursor.fetchone()["n"]:
                raise BackupError("Restored return and order line ownership differ")
    return {
        "successful_operations_matched": len(actions),
        "protected_return_refs_matched": len(proofs),
        "orphan_java_outbox": 0,
        "invalid_return_lines": 0,
        "mysql_events_enabled": False,
    }


def restore(backup_id, fixture=None, journal_id=None, plan_id=None):
    if plan_id and (RESTORES / plan_id / "result.json").is_file():
        result = ArchiveReader(RESTORES, plan_id).result()
        if (
            result["backup_id"] != backup_id
            or result["result"] != "RESTORED_DATA_MATCH_AND_NONEXECUTING_ISOLATION_PASSED"
        ):
            raise BackupError("Existing registered restore did not pass; resources remain isolated")
        log("restore_already_verified", restore_id=plan_id)
        return result
    reader = ArchiveReader(FOLDER, backup_id)
    reader.verify()
    receipt = ArchiveWriter(RESTORES, plan_id=plan_id)
    credentials = {
        key: secrets.token_hex(24) for key in ("pg_password", "mysql_password", "objects_password")
    }
    receipt.json("credentials.json", credentials)
    if journal_id:
        captured = ArchiveReader(ROOT / "deployment/.run/erasure-journals", journal_id)
        captured.verify()
        if captured.manifest["metadata"].get("environment") != "newshop-demo":
            raise BackupError("Erasure journal belongs to another registered environment")
        journal = captured.json("deletions.json")
    else:
        journal_id, journal = capture_erasure_journal(ROOT)
    if datetime.fromisoformat(journal["captured_at"]) < datetime.fromisoformat(
        reader.manifest["metadata"]["snapshot_at"]
    ):
        raise BackupError("Registered deletion watermark is older than the backup")
    receipt.json("latest-deletions.json", journal)
    receipt.seal({"backup_id": backup_id, "operation": "ISOLATED_RESTORE", "execute_allowed": False})
    plan, started, result = (
        receipt.id,
        time.monotonic(),
        {
            "backup_id": backup_id,
            "restore_id": receipt.id,
            "deletion_journal_id": journal_id,
            "result": "STARTED",
        },
    )
    result["recovery_started_at"] = datetime.now(timezone.utc).isoformat()
    try:
        log("isolated_restore_creating", restore_id=plan)
        transport("restore-create", canonical(credentials), plan)
        with tunnel(plan) as ports:
            wait_databases(ports, credentials)
            log("isolated_databases_ready")
            transport("restore-pg", reader.read("postgres.dump"), plan)
            transport("restore-mysql", reader.read("mysql.sql"), plan)
            inventory = reader.json("inventory.json")
            with connect_pg(ports, credentials) as conn:
                actual = pg_fingerprints(conn)
                if actual != inventory["postgres"]:
                    changed = sorted(
                        key
                        for key in set(actual) | set(inventory["postgres"])
                        if actual.get(key) != inventory["postgres"].get(key)
                    )
                    raise BackupError("Restored PostgreSQL differs from the snapshot: " + ",".join(changed))
            mysql_hash = hashlib.sha256(transport("restore-mysql-data", plan=plan)).hexdigest()
            if mysql_hash != inventory["mysql_data_sha256"]:
                raise BackupError("Restored MySQL differs from the snapshot")
            result["postgres_tables_exact"] = len(actual)
            result["mysql_all_table_data_exact"] = True
            log("database_fingerprints_matched", postgres_tables=len(actual), mysql_exact=True)
            objects, bucket, count = restore_objects(reader, ports, credentials)
            result["private_objects_readback"] = count
            log("private_objects_restored", objects=count)
            if fixture:
                with connect_pg(ports, credentials) as conn:
                    result["erasure_fixture_before_overlay"] = check_erasure_fixture(
                        conn, objects, bucket, fixture, False
                    )
            result["knowledge_rebuild"] = restore_vectors(reader, ports, credentials)
            log("knowledge_rebuilt", **result["knowledge_rebuild"])
            with connect_pg(ports, credentials) as conn:
                overlay = apply_deletions(conn, journal)
                for key in overlay.pop("delete_object_keys"):
                    objects.remove_object(bucket, key)
                result["deletion_overlay"] = overlay
                result["runtime_freeze"] = freeze_runtime(conn)
                result["invariants"] = verify_invariants(conn)
                result["cross_store_ledgers"] = verify_business_ledgers(conn, ports, credentials)
                if fixture:
                    result["erasure_fixture_after_overlay"] = check_erasure_fixture(
                        conn, objects, bucket, fixture, True
                    )
            # Verify the network/resources again after all writes. There are no app processes.
            result["isolation"] = json.loads(transport("restore-status", b"{}", plan))
        result["result"] = "RESTORED_DATA_MATCH_AND_NONEXECUTING_ISOLATION_PASSED"
        result["restore_seconds"] = round(time.monotonic() - started, 2)
        result["snapshot_at"] = reader.manifest["metadata"]["snapshot_at"]
        result["snapshot_age_at_recovery_start_seconds"] = round(
            (
                datetime.fromisoformat(result["recovery_started_at"])
                - datetime.fromisoformat(result["snapshot_at"])
            ).total_seconds(),
            2,
        )
        result["deletion_overlay_at"] = journal["captured_at"]
        result["checked_at"] = datetime.now(timezone.utc).isoformat()
        result["rpo_snapshot_age_at_recovery_seconds"] = round(
            (datetime.now(timezone.utc) - datetime.fromisoformat(result["snapshot_at"])).total_seconds(), 2
        )
        result["automatic_reopen"] = False
        result["remaining_validation"] = [
            "ACTIVE_RUN_UNKNOWN_RECOVERY_FIXTURES",
            "PERIODIC_POLICY_AND_UI",
        ]
        if not fixture:
            result["remaining_validation"].append("DELETE_AFTER_BACKUP_FIXTURE")
        log(
            "isolated_restore_passed",
            restore_id=plan,
            restore_seconds=result["restore_seconds"],
            automatically_reopened=False,
        )
    except Exception as error:
        result["result"] = "FAILED_ISOLATION_RETAINED_NO_REOPEN"
        result["error"] = str(error) if isinstance(error, BackupError) else type(error).__name__
        log("isolated_restore_failed", restore_id=plan, error=result["error"])
        raise
    finally:
        ArchiveReader(RESTORES, plan).record_result(result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["restore", "status", "destroy"])
    parser.add_argument("--backup-id")
    parser.add_argument("--restore-id")
    parser.add_argument("--verify-erasure-fixture", action="store_true")
    parser.add_argument("--deletion-journal-id")
    parser.add_argument("--plan-id")
    arguments = parser.parse_args()
    try:
        if arguments.mode == "restore":
            if not arguments.backup_id:
                raise BackupError("Registered backup identifier required")
            fixture = None
            if arguments.verify_erasure_fixture:
                fixture = json.loads(
                    (ROOT / "deployment/.run/completion-backup-erasure-fixture-20261113.json").read_text(
                        encoding="utf-8"
                    )
                )
                if (
                    fixture.get("backup_id") != arguments.backup_id
                    or fixture.get("phase") != "DELETED_AFTER_BACKUP"
                ):
                    raise BackupError("Registered erasure fixture is not bound to this backup")
            restore(arguments.backup_id, fixture, arguments.deletion_journal_id, arguments.plan_id)
        else:
            # Authenticate local ownership registration before remote status/removal.
            owner = ArchiveReader(RESTORES, arguments.restore_id)
            owner.verify()
            print(transport("restore-" + arguments.mode, b"{}", arguments.restore_id).decode(), flush=True)
    except Exception as error:
        log("failed", error=str(error) if isinstance(error, BackupError) else type(error).__name__)
        sys.exit(1)
