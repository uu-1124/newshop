from app.infrastructure.postgres.database import encoded, execute


async def worker_status(db, worker, kind, state, details=None):
    async with db.transaction() as conn:
        await execute(
            conn,
            """INSERT INTO ag_worker_heartbeat(worker_id,process_kind,state,details)
            VALUES (:id,:kind,:state,CAST(:details AS jsonb)) ON CONFLICT(worker_id)
            DO UPDATE SET state=EXCLUDED.state,details=EXCLUDED.details,last_seen_at=now()""",
            id=worker,
            kind=kind,
            state=state,
            details=encoded({**(details or {}), "db_pool": db.pool_metrics()}),
        )
