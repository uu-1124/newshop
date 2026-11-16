import hashlib
import json
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import create_async_engine

from app.infrastructure.performance_timing import (
    add_database_time,
    finish_acquisition,
    install_engine_timing,
    queue_timing_configuration,
    queue_timing_supported,
    start_acquisition,
)


def json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(type(value).__name__)


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=json_default, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class Database:
    def __init__(self, url: str, performance_timing_enabled=False):
        pool_options, self._queue_timing_reason = queue_timing_configuration(performance_timing_enabled)
        self.engine = create_async_engine(
            url, pool_size=5, max_overflow=2, hide_parameters=True, **pool_options
        )
        if performance_timing_enabled:
            install_engine_timing(self.engine)
        self._waits = deque(maxlen=300)
        self._timeouts = 0

    def pool_metrics(self):
        waits = sorted(self._waits)
        pool = self.engine.pool
        return {
            "checked_out": max(0, pool.checkedout()),
            "capacity": 7,
            "samples": len(waits),
            "wait_p95_ms": waits[min(len(waits) - 1, int(len(waits) * 0.95))] if waits else 0,
            "timeouts": self._timeouts,
        }

    @asynccontextmanager
    async def connection(self, isolation=None):
        started = time.perf_counter()
        acquisition = start_acquisition(
            queue_timing_supported(self.engine),
            getattr(self, "_queue_timing_reason", "engine_not_instrumented"),
        )
        release_started = None
        try:
            try:
                async with self.engine.connect() as conn:
                    finish_acquisition(acquisition)
                    self._waits.append(max(0, round((time.perf_counter() - started) * 1000)))
                    try:
                        if isolation:
                            isolation_started = time.perf_counter()
                            try:
                                conn = await conn.execution_options(isolation_level=isolation)
                            finally:
                                add_database_time("isolation_config_ms", isolation_started)
                        yield conn
                    finally:
                        release_started = time.perf_counter()
            finally:
                finish_acquisition(acquisition)
                if release_started is not None:
                    add_database_time("connection_release_ms", release_started)
        except PoolTimeout:
            self._timeouts += 1
            raise

    @asynccontextmanager
    async def transaction(self, snapshot=False):
        async with self.connection("REPEATABLE READ" if snapshot else None) as conn:
            end_started = None
            try:
                async with conn.begin():
                    try:
                        yield conn
                    finally:
                        end_started = time.perf_counter()
            finally:
                if end_started is not None:
                    add_database_time("transaction_end_ms", end_started)


async def one(conn, sql, **params):
    return (await conn.execute(text(sql), params)).mappings().first()


async def rows(conn, sql, **params):
    return list((await conn.execute(text(sql), params)).mappings())


async def execute(conn, sql, **params):
    return await conn.execute(text(sql), params)


async def ordered_advisory_locks(conn, keys):
    keys = sorted(set(keys))
    if not keys:
        return
    # One recursive step consumes the previous lock result before taking the next.
    # Read quota usage in a subsequent statement, after a waiter gets a fresh RC snapshot.
    await one(
        conn,
        """WITH RECURSIVE input AS (SELECT CAST(:keys AS text[]) keys), locked(n,held) AS (
            SELECT 1,pg_advisory_xact_lock(hashtextextended(keys[1],0)) FROM input
            UNION ALL
            SELECT locked.n+1,pg_advisory_xact_lock(hashtextextended(keys[locked.n+1],0))
            FROM locked CROSS JOIN input WHERE locked.n<cardinality(keys)
        ) SELECT count(*) AS acquired FROM locked""",
        keys=keys,
    )


async def append_events(conn, conversation, events: list[tuple[str, dict]]):
    from app.application.governance import read_policy

    retention = (await read_policy(conn))["value"]["event_retention_days"]
    # This is the last database lock in every command. No IO or other locks after this call.
    clock = await one(
        conn,
        """WITH clock AS (
            UPDATE ag_event_clock SET last_event_id=last_event_id+:count,updated_at=now()
            WHERE singleton_id=1 RETURNING last_event_id
        ), inserted AS (
            INSERT INTO ag_event_outbox
                (event_id,conversation_id,conversation_version,event_type,payload,visibility,control_epoch,retention_until)
            SELECT clock.last_event_id-:count+item.n,:cid,:version,item.value->>'type',
                item.value->'payload','CONVERSATION',:epoch,now()+make_interval(days=>:days)
            FROM clock CROSS JOIN jsonb_array_elements(CAST(:events AS jsonb))
                WITH ORDINALITY AS item(value,n)
            RETURNING event_id
        ) SELECT last_event_id,(SELECT count(*) FROM inserted) AS emitted FROM clock""",
        count=len(events),
        cid=conversation["id"],
        version=conversation["version"],
        epoch=conversation["control_epoch"],
        days=retention,
        events=encoded([{"type": kind, "payload": payload} for kind, payload in events]),
    )
    return str(clock["last_event_id"])
