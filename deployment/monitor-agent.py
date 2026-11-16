"""Fixed deployment observer; privileged host/DB reads never become API authority."""
# ruff: noqa: E402 -- install this workspace's application path before imports.

import asyncio
import json
import logging
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mall-agent"))

import httpx
from app.application.backups import put_state
from app.application.observability import STATE_KEY, Cloud, Outbox, Postgres, RuntimeSnapshot
from app.infrastructure.dependency_probes import collect, measured
from app.infrastructure.postgres.database import Database
from app.infrastructure.worker_status import worker_status
from app.runtime import run
from app.settings import get_settings
from backup_data import pg_connection


def postgres():
    with pg_connection(ROOT, True) as conn:
        conn.execute("SET LOCAL statement_timeout='5s'")
        # Only counts, timing and clock watermarks; query text, IDs and payloads never leave SQL.
        row = conn.execute("""SELECT
            (SELECT count(*) FROM pg_stat_activity WHERE datname=current_database())::int connections,
            current_setting('max_connections')::int max_connections,
            (SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock')::int lock_waiters,
            COALESCE((SELECT GREATEST(0,extract(epoch FROM now()-min(xact_start)))::int FROM pg_stat_activity
                WHERE datname=current_database() AND pid<>pg_backend_pid()),0) longest_transaction_seconds,
            (SELECT count(*) FROM ag_generation_slot WHERE run_id IS NOT NULL)::int active_slots,
            (SELECT count(*) FROM ag_run WHERE state='QUEUED')::int queued_runs,
            COALESCE((SELECT GREATEST(0,extract(epoch FROM now()-min(created_at)))::int FROM ag_run WHERE state='QUEUED'),0) oldest_queued_seconds,
            (SELECT count(*) FROM ag_job WHERE state='QUEUED')::int queued_jobs,
            COALESCE((SELECT GREATEST(0,extract(epoch FROM now()-min(next_attempt_at)))::int FROM ag_job WHERE state='QUEUED' AND next_attempt_at<=now()),0) oldest_job_seconds,
            (SELECT count(*) FROM ag_job_wakeup WHERE published_at IS NULL)::int job_wakeups_pending,
            (SELECT count(*) FROM ag_event_outbox WHERE published_at IS NULL)::int event_notifications_pending,
            (SELECT last_event_id FROM ag_event_clock WHERE singleton_id=1)::bigint event_clock,
            (SELECT replay_floor FROM ag_event_clock WHERE singleton_id=1)::bigint event_replay_floor,
            ((SELECT count(*) FROM ag_event_outbox WHERE event_id>(SELECT last_event_id FROM ag_event_clock WHERE singleton_id=1))
             +(SELECT count(*) FROM ag_event_clock WHERE replay_floor>last_event_id))::int event_clock_violations,
            (SELECT count(*) FROM ag_request_metric WHERE operation IN ('memberEvents','adminEvents') AND status_code=410 AND created_at>now()-interval '5 minutes')::int sse_cursor_expired_5m,
            (SELECT count(*) FROM ag_request_metric WHERE operation IN ('memberEvents','adminEvents') AND status_code=400 AND created_at>now()-interval '5 minutes')::int sse_invalid_cursor_5m""").fetchone()
        timing = conn.execute("""WITH first_delta AS (
            SELECT payload->>'run_id' run_id,min(created_at) first_text FROM ag_event_outbox
            WHERE event_type='message.delta' AND created_at>now()-interval '24 hours' GROUP BY payload->>'run_id')
            SELECT count(*)::int first_text_samples,
            (percentile_disc(0.95) WITHIN GROUP(ORDER BY GREATEST(0,extract(epoch FROM d.first_text-r.created_at)*1000)))::bigint first_text_p95_ms,
            (percentile_disc(0.95) WITHIN GROUP(ORDER BY GREATEST(0,extract(epoch FROM r.started_at-r.created_at)*1000)))::bigint queue_p95_ms,
            (percentile_disc(0.95) WITHIN GROUP(ORDER BY GREATEST(0,extract(epoch FROM r.finished_at-r.created_at)*1000)))::bigint total_p95_ms
            FROM ag_run r JOIN first_delta d ON d.run_id=r.id::text WHERE r.created_at>now()-interval '24 hours'""").fetchone()
        pools = [
            record["db_pool"]
            for record in conn.execute(
                "SELECT details->'db_pool' db_pool FROM ag_worker_heartbeat WHERE last_seen_at>now()-interval '60 seconds' AND details ? 'db_pool'"
            )
        ]
        inputs = {
            "object_key": conn.execute(
                "SELECT object_key FROM ag_attachment WHERE state='READY' ORDER BY id LIMIT 1"
            ).fetchone(),
            "collections": [
                record["index_collection"]
                for record in conn.execute(
                    "SELECT v.index_collection FROM ag_kb_document d JOIN ag_kb_version v ON v.id=d.active_version_id WHERE d.enabled AND v.index_collection IS NOT NULL"
                )
            ],
        }
    values = (
        dict(row)
        | dict(timing)
        | {
            "pool_wait_p95_ms": max([pool.get("wait_p95_ms", 0) for pool in pools] or [0]),
            "pool_saturated_workers": sum(
                pool.get("checked_out", 0) >= pool.get("capacity", 7) for pool in pools
            ),
        }
    )
    return values, inputs


def remote():
    result = subprocess.run(
        ["D:/develop/nodejs/node.exe", str(ROOT / "deployment/observe-remote.cjs")],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=30,
        creationflags=0x08000000 if sys.platform == "win32" else 0,
    )
    if result.returncode or len(result.stdout) > 16384:
        raise ValueError("Remote observer unavailable")
    data = json.loads(result.stdout)
    return Cloud.model_validate(data["cloud"]), Outbox.model_validate(data["java_outbox"]) if data[
        "java_outbox"
    ] is not None else None


async def observation(settings, client):
    started = time.perf_counter()
    pg_values, inputs = None, {"object_key": None, "collections": []}

    async def database_probe():
        nonlocal pg_values, inputs
        pg_values, inputs = await asyncio.to_thread(postgres)

    pg_sample = await measured(database_probe)
    dependencies = await collect(
        settings,
        inputs["object_key"]["object_key"] if inputs["object_key"] else None,
        inputs["collections"],
        client,
    )
    dependencies["postgres"] = pg_sample
    cloud, outbox = None, None

    async def remote_probe():
        nonlocal cloud, outbox
        cloud, outbox = await asyncio.to_thread(remote)
        if outbox is None:
            raise ValueError("Java outbox observer unavailable")

    dependencies["cloud"] = await measured(remote_probe)
    if pg_values:
        try:
            pool = (await client.get("http://127.0.0.1:8099/internal/pool")).json()
            pg_values["pool_wait_p95_ms"] = max(pg_values["pool_wait_p95_ms"], int(pool["wait_p95_ms"]))
            pg_values["pool_saturated_workers"] += int(pool["checked_out"] >= pool["capacity"])
        except Exception:
            pass
    return RuntimeSnapshot(
        checked_at=datetime.now(UTC),
        duration_ms=round((time.perf_counter() - started) * 1000),
        dependencies=dependencies,
        cloud=cloud,
        java_outbox=outbox,
        postgres=Postgres.model_validate(pg_values) if pg_values else None,
    )


async def main():
    settings = get_settings()
    if not settings.observability_enabled:
        return
    db = Database(settings.database_url.get_secret_value())
    owner = "observability-" + str(uuid4())
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        try:
            while True:
                try:
                    snapshot = await observation(settings, client)
                    async with db.transaction() as conn:
                        await put_state(conn, STATE_KEY, snapshot.model_dump(mode="json"))
                    await worker_status(db, owner, "observability", "IDLE")
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logging.warning("Observer deferred error_type=%s", type(error).__name__)
                await asyncio.sleep(20)
        finally:
            await db.engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    run(main())
