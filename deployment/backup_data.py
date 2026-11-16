"""Bounded data collection for consistent backups and isolated verification."""

import hashlib
import io
import json
import re
import tarfile
from pathlib import Path

import psycopg
from backup_archive import BackupError
from minio import Minio
from psycopg import sql
from psycopg.rows import dict_row

SCHEMAS = ("public", "langgraph_checkpoint")


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def pg_connection(root, read_only=False):
    config = load_json(root / "deployment/.run/agent-env.json")
    conn = psycopg.connect(
        config["migrationUrl"].replace("postgresql+psycopg://", "postgresql://"),
        connect_timeout=8,
        row_factory=dict_row,
    )
    conn.execute("SET TIME ZONE 'UTC'")
    conn.commit()
    if read_only:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    conn.execute("SET LOCAL lock_timeout='8s'")
    conn.execute("SET LOCAL statement_timeout='90s'")
    return conn


def preflight(conn, owned_job=None):
    checks = {
        "runs": "SELECT count(*) n FROM ag_run WHERE state IN ('QUEUED','RUNNING','STOP_REQUESTED')",
        "jobs": "SELECT count(*) n FROM ag_job WHERE state IN ('QUEUED','RUNNING')",
        "operations": "SELECT count(*) n FROM ag_action WHERE state IN ('EXECUTING','UNKNOWN')",
        "uploads": "SELECT count(*) n FROM ag_attachment WHERE state='UPLOADING' AND created_at>now()-interval '1 hour'",
    }
    counts = {key: conn.execute(query).fetchone()["n"] for key, query in checks.items()}
    if conn.execute("SELECT to_regclass('ag_media_request')").fetchone()["to_regclass"]:
        counts["media"] = conn.execute(
            "SELECT count(*) n FROM ag_media_request WHERE state='RUNNING' AND lease_until>now()"
        ).fetchone()["n"]
    if owned_job:
        row = conn.execute(
            "SELECT id FROM ag_job WHERE id=%s AND state='RUNNING' AND job_type='BACKUP_VERIFY' AND lease_owner=%s AND fencing_token=%s AND lease_until>now() AND parameters->>'mode'='CREATE'",
            owned_job,
        ).fetchone()
        if not row or not str(owned_job[1]).startswith("backup-executor-"):
            raise BackupError("Backup job ownership fence is no longer valid")
        counts["jobs"] -= 1
    if any(counts.values()):
        raise BackupError("Active work must drain before the registered maintenance window")
    if conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] != "0011":
        raise BackupError("Unreviewed source migration")
    return counts


def snapshot_identity(conn, configuration):
    from dotenv import dotenv_values

    # Read from the same configuration bytes that are encrypted into this snapshot.
    values = dotenv_values(stream=io.StringIO(configuration.get("mall-agent/.env", "")), interpolate=False)
    build = values.get("AGENT_BUILD")
    migration = conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
    if not isinstance(build, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", build):
        raise BackupError("Snapshot build identity is missing or invalid")
    return {"migration": migration, "build": build}


def pg_tables(conn):
    return [
        (row["schemaname"], row["tablename"])
        for row in conn.execute(
            "SELECT schemaname,tablename FROM pg_tables WHERE schemaname=ANY(%s) ORDER BY schemaname,tablename",
            (list(SCHEMAS),),
        )
    ]


def lock_postgres(conn):
    tables = pg_tables(conn)
    conn.execute(
        sql.SQL("LOCK TABLE {} IN SHARE MODE").format(
            sql.SQL(",").join(sql.Identifier(s, t) for s, t in tables)
        )
    )
    return tables


def pg_fingerprints(conn):
    result = {}
    for schema, table in pg_tables(conn):
        digest, count = hashlib.sha256(), 0
        query = sql.SQL("SELECT md5(row_to_json(t)::text) h FROM {} t ORDER BY h").format(
            sql.Identifier(schema, table)
        )
        with conn.cursor() as cursor:
            cursor.execute(query)
            for row in cursor:
                digest.update(row["h"].encode() + b"\n")
                count += 1
        result[schema + "." + table] = {"rows": count, "sha256": digest.hexdigest()}
    return result


def object_client(root):
    config = load_json(root / "deployment/.run/objects-env.json")
    if config["bucket"] != "mall-agent-private":
        raise BackupError("Only the registered private agent bucket may be backed up")
    secrets = load_json(root / "deployment/secrets.json")
    return Minio(config["endpoint"], "mallminio", secrets["minio"], secure=False), config["bucket"]


def export_objects(root, conn):
    client, bucket = object_client(root)
    inventory, payload = [], io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        for item in client.list_objects(bucket, recursive=True):
            if len(inventory) >= 20000 or not re.fullmatch(
                r"(?:chat|knowledge|speech)/[^\x00]+", item.object_name
            ):
                raise BackupError("Unregistered private object prefix or inventory budget exceeded")
            if item.size > 20 * 1024 * 1024 or payload.tell() + item.size > 400 * 1024 * 1024:
                raise BackupError("Private object backup exceeds budget")
            response = client.get_object(bucket, item.object_name)
            try:
                data = response.read(item.size + 1)
            finally:
                response.close()
                response.release_conn()
            if len(data) != item.size:
                raise BackupError("Private object changed during snapshot")
            entry = {
                "object_key": item.object_name,
                "member": f"objects/{len(inventory):08d}",
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "content_type": client.stat_object(bucket, item.object_name).content_type,
            }
            info = tarfile.TarInfo(entry["member"])
            info.size, info.mode, info.mtime = len(data), 0o600, 0
            archive.addfile(info, io.BytesIO(data))
            inventory.append(entry)
    by_key = {item["object_key"]: item for item in inventory}
    attachments = list(
        conn.execute("SELECT id,object_key,state,byte_size,sha256 FROM ag_attachment WHERE state='READY'")
    )
    for attachment in attachments:
        item = by_key.get(attachment["object_key"])
        if not item or item["bytes"] != attachment["byte_size"] or item["sha256"] != attachment["sha256"]:
            raise BackupError("Ready attachment metadata and stored bytes disagree")
    return {
        "bucket": bucket,
        "objects": inventory,
        "ready_attachments_checked": len(attachments),
    }, payload.getvalue()


def export_vectors(root, conn):
    import logging

    from pymilvus import MilvusClient

    logging.getLogger("pymilvus").setLevel(logging.CRITICAL)
    config = load_json(root / "deployment/.run/milvus-env.json")
    client = MilvusClient(
        uri="http://127.0.0.1:19530",
        db_name=config["database"],
        user=config["readerUser"],
        password=config["readerPassword"],
        timeout=20,
        grpc_options={"grpc.enable_http_proxy": 0},
    )
    result = []
    try:
        versions = list(
            conn.execute(
                "SELECT id,index_collection,quality_report,chunk_count FROM ag_kb_version WHERE index_collection IS NOT NULL ORDER BY id"
            )
        )
        for version in versions:
            collection = version["index_collection"]
            if not re.fullmatch(r"agent_kb_[a-f0-9]{32}_[1-9][0-9]*", collection):
                raise BackupError("Knowledge collection escaped its registered namespace")
            spec = version["quality_report"]["index"]
            ids = [
                str(row["id"])
                for row in conn.execute(
                    "SELECT id FROM ag_kb_chunk WHERE version_id=%s AND chunk_kind='CHILD' ORDER BY id",
                    (version["id"],),
                )
            ]
            if len(ids) > 20000:
                raise BackupError("Knowledge export exceeds vector budget")
            rows = []
            for start in range(0, len(ids), 100):
                rows.extend(
                    client.get(collection, ids=ids[start : start + 100], output_fields=["*"], timeout=30)
                )
            if {row["chunk_id"] for row in rows} != set(ids) or len(rows) != len(ids):
                raise BackupError("Knowledge vectors and source chunks disagree")
            for row in rows:
                row["vector"] = [float(value) for value in row["vector"]]
                if row["content_sha256"] != hashlib.sha256(row["text"].encode()).hexdigest():
                    raise BackupError("Knowledge vector source hash mismatch")
            result.append(
                {
                    "version_id": str(version["id"]),
                    "spec": spec,
                    "rows": sorted(rows, key=lambda row: row["chunk_id"]),
                }
            )
    finally:
        client.close()
    return {
        "database": config["database"],
        "collections": result,
        "collection_count": len(result),
        "vector_count": sum(len(value["rows"]) for value in result),
    }


def deletion_journal(conn):
    return {
        "captured_at": conn.execute("SELECT clock_timestamp() n").fetchone()["n"].isoformat(),
        "tombstones": [
            dict(row)
            for row in conn.execute("SELECT * FROM ag_erasure_tombstone ORDER BY resource_type,resource_id")
        ],
        "deleted_conversations": [
            dict(row)
            for row in conn.execute(
                "SELECT id,memory_epoch,control_epoch,deleted_at FROM ag_conversation WHERE deleted_at IS NOT NULL"
            )
        ],
        "deleted_messages": [
            dict(row)
            for row in conn.execute(
                "SELECT id,conversation_id,deleted_at FROM ag_message WHERE deleted_at IS NOT NULL"
            )
        ],
        "deleted_attachments": [
            dict(row)
            for row in conn.execute(
                "SELECT id,object_key,state FROM ag_attachment WHERE state IN ('DELETING','DELETED')"
            )
        ],
        "protected_refs": [
            dict(row) for row in conn.execute("SELECT * FROM ag_attachment_ref WHERE protected")
        ],
    }


def capture_erasure_journal(root):
    from backup_archive import ArchiveWriter

    with pg_connection(root, True) as conn:
        journal = serializable(deletion_journal(conn))
    archive = ArchiveWriter(root / "deployment/.run/erasure-journals")
    archive.json("deletions.json", journal)
    archive.seal(
        {
            "kind": "REGISTERED_ERASURE_JOURNAL",
            "environment": "newshop-demo",
            "captured_at": journal["captured_at"],
        }
    )
    return archive.id, journal


def serializable(value):
    return json.loads(json.dumps(value, default=str))


def configuration_capsule(root):
    names = [
        "mall-agent/.env",
        "deployment/.run/agent-env.json",
        "deployment/.run/runtime-env.json",
        "deployment/.run/objects-env.json",
        "deployment/.run/milvus-env.json",
    ]
    names += [
        "deployment/config/" + service + ".yaml"
        for service in ("portal", "admin", "gateway", "auth", "search", "monitor")
    ]
    return {name: (root / name).read_text(encoding="utf-8-sig") for name in names if (root / name).is_file()}
