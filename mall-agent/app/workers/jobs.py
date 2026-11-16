"""Durable PG task consumer with optional RabbitMQ wakeups and bounded retries."""

import asyncio
import logging
import time
from uuid import uuid4

from app.application.jobs import Jobs
from app.application.runtime_check import runtime_handlers
from app.domain.capabilities import JobLease, JobType
from app.domain.identity import DomainError
from app.infrastructure.cursors import Cursors
from app.infrastructure.knowledge_runtime import KnowledgeRuntime
from app.infrastructure.model_call_context import ModelCallContext, current_model_call
from app.infrastructure.model_ledger import ModelLedger
from app.infrastructure.model_quota import model_actor
from app.infrastructure.postgres.database import Database
from app.infrastructure.private_objects import PrivateObjects
from app.infrastructure.wakeups import NotificationBindings, wakeup_id
from app.infrastructure.worker_status import worker_status
from app.runtime import run
from app.settings import get_settings

log = logging.getLogger(__name__)


async def execute_job(jobs, registry, job):
    actor_context = model_actor.set(
        ("STAFF", job["created_by"]) if job.get("created_by") else ("SYSTEM", None)
    )
    lease = JobLease(
        job["id"], JobType(job["job_type"]), job["lease_owner"], job["fencing_token"], job["parameters"]
    )
    call_context = current_model_call.set(
        ModelCallContext(job_id=lease.id, lease_owner=lease.owner, fencing_token=lease.fencing_token)
    )

    async def renew():
        while True:
            await asyncio.sleep(max(1, jobs.settings.job_lease_seconds / 3))
            if not await jobs.heartbeat(job):
                raise DomainError("LEASE_LOST", "后台任务租约已失效", 409)
            await worker_status(
                jobs.db,
                job["lease_owner"],
                "backups" if jobs.lane == "backups" else "jobs",
                "RUNNING",
                {"job_id": str(job["id"])},
            )

    task = asyncio.create_task(registry.dispatch(lease))
    heart = asyncio.create_task(renew())
    result, error, retryable = None, None, True
    try:
        done, _ = await asyncio.wait(
            {task, heart},
            timeout=registry.timeouts.get(lease.kind) or jobs.settings.job_timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            result = await task
        elif heart in done:
            await heart
        else:
            error = "JOB_TIMEOUT"
    except DomainError as exc:
        error = exc.code
        retryable = exc.status >= 500
    except Exception:
        error = "JOB_EXECUTION_FAILED"
    finally:
        task.cancel()
        heart.cancel()
        await asyncio.gather(task, heart, return_exceptions=True)
        model_actor.reset(actor_context)
        current_model_call.reset(call_context)
    return await jobs.finish(job, result, error, retryable)


async def main():
    settings = get_settings()
    db = Database(settings.database_url.get_secret_value())
    registry = runtime_handlers(db, settings)
    jobs = Jobs(db, settings, registry)
    objects = (
        PrivateObjects(settings) if settings.knowledge_enabled and settings.attachments_enabled else None
    )
    knowledge = (
        KnowledgeRuntime(
            db,
            settings,
            jobs,
            Cursors(settings.cursor_secret.get_secret_value()),
            objects,
            ModelLedger(db, settings),
        )
        if settings.knowledge_enabled
        else None
    )
    notifications = NotificationBindings(settings)
    owner = "jobs-" + str(uuid4())
    next_index_cleanup = 0
    try:
        while True:
            message = None
            try:
                await worker_status(db, owner, "jobs", "IDLE", {"handlers": list(registry.supported)})
                if knowledge and time.monotonic() >= next_index_cleanup:
                    next_index_cleanup = time.monotonic() + 300
                    try:
                        await knowledge.knowledge.schedule_index_cleanup()
                    except DomainError as error:
                        log.warning("knowledge index cleanup scan deferred code=%s", error.code)
                job = None
                if notifications.rabbitmq.bound:
                    try:
                        message = await notifications.rabbitmq.require().receive()
                    except Exception:
                        pass
                    if message:
                        try:
                            job_id = wakeup_id(message.body)
                        except (ValueError, TypeError, KeyError):
                            await message.reject(requeue=False)
                            message = None
                        else:
                            job = await jobs.claim(owner, job_id)
                if not job:
                    job = await jobs.claim(owner)
                if job:
                    await worker_status(db, owner, "jobs", "RUNNING", {"job_id": str(job["id"])})
                    await execute_job(jobs, registry, job)
                if message:
                    await message.ack()
                if not job:
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("job consumer unavailable type=%s", type(exc).__name__)
                if message:
                    try:
                        await message.reject(requeue=True)
                    except Exception:
                        pass
                await asyncio.sleep(2)
    finally:
        if knowledge:
            await knowledge.close()
        if objects:
            objects.close()
        await notifications.close()
        await db.engine.dispose()


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    for name in ["aio_pika", "aiormq"]:
        logging.getLogger(name).setLevel(logging.CRITICAL)
    if args.check:
        print(json.dumps({"process": "jobs", "mode": "LIVE", "types": [kind.value for kind in JobType]}))
    else:
        run(main())
