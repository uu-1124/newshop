"""Commit-first, at-least-once Redis/RabbitMQ notification publisher."""

import asyncio
import logging
from uuid import uuid4

from app.application.outbox import Outbox
from app.infrastructure.postgres.database import Database
from app.infrastructure.wakeups import NotificationBindings
from app.infrastructure.worker_status import worker_status
from app.runtime import run
from app.settings import get_settings


async def main():
    settings = get_settings()
    db = Database(settings.database_url.get_secret_value())
    notifications = NotificationBindings(settings)
    publisher = Outbox(db, notifications)
    owner = "outbox-" + str(uuid4())
    try:
        while True:
            try:
                published = await publisher.tick()
                await worker_status(db, owner, "outbox", "IDLE", {"last_batch_published": published})
                await asyncio.sleep(0.5 if published else 1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.warning("outbox unavailable type=%s", type(exc).__name__)
                await asyncio.sleep(2)
    finally:
        await notifications.close()
        await db.engine.dispose()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    for name in ["aio_pika", "aiormq"]:
        logging.getLogger(name).setLevel(logging.CRITICAL)
    if args.check:
        print('{"process":"outbox","mode":"LIVE"}')
    else:
        run(main())
