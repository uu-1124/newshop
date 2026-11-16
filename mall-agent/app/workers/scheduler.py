import asyncio
import logging
from uuid import uuid4

from app.application.actions import Actions
from app.application.jobs import Jobs
from app.application.knowledge import Knowledge
from app.application.runs import Runs
from app.application.runtime_check import runtime_handlers
from app.infrastructure.java_bridge import JavaBridge
from app.infrastructure.postgres.database import Database, one
from app.infrastructure.worker_status import worker_status
from app.runtime import run
from app.settings import get_settings


async def main():
    settings = get_settings()
    db = Database(settings.database_url.get_secret_value())
    runs = Runs(db, settings)
    bridge = JavaBridge(settings) if settings.actions_enabled else None
    actions = Actions(db, bridge, settings) if bridge else None
    jobs = Jobs(db, settings, runtime_handlers(db, settings))
    retention = None
    if settings.retention_enabled:
        from app.application.retention import Retention

        retention = Retention(db, settings, jobs)
    if settings.knowledge_enabled:
        # Recovery uses domain failure callbacks only; this process never dispatches handlers.
        Knowledge(db, jobs, None, None, None, None, None).register(jobs.handlers)
    owner = "scheduler-" + str(uuid4())
    try:
        while True:
            try:
                async with db.transaction() as conn:
                    leader = await one(
                        conn,
                        "SELECT pg_try_advisory_xact_lock(hashtextextended('mall-agent-scheduler',0)) AS acquired",
                    )
                    if leader["acquired"]:
                        await runs.recover_expired()
                        await jobs.recover_expired()
                if actions:
                    await actions.reconcile_due()
                if retention:
                    await retention.schedule()
                await worker_status(db, owner, "scheduler", "IDLE")
            except Exception as exc:
                logging.warning("scheduler unavailable type=%s", type(exc).__name__)
            await asyncio.sleep(2)
    finally:
        if bridge:
            await bridge.client.aclose()
        await db.engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run(main())
