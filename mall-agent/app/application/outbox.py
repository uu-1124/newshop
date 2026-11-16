from app.infrastructure.postgres.database import execute, rows


class Outbox:
    def __init__(self, db, notifications):
        self.db, self.notifications = db, notifications

    async def tick(self):
        async with self.db.transaction() as conn:
            events = await rows(
                conn,
                """SELECT event_id FROM ag_event_outbox WHERE published_at IS NULL
                AND next_publish_at<=now() ORDER BY event_id LIMIT 25""",
            )
            jobs = await rows(
                conn,
                """SELECT w.id,w.job_id,j.job_type FROM ag_job_wakeup w
                JOIN ag_job j ON j.id=w.job_id WHERE w.published_at IS NULL
                AND w.next_attempt_at<=now() ORDER BY w.next_attempt_at,w.id LIMIT 25""",
            )
        success = 0
        for topic, items, adapter in [
            ("events", events, self.notifications.redis),
            ("jobs", jobs, self.notifications.rabbitmq),
        ]:
            for item in items:
                try:
                    if topic == "events":
                        await adapter.require().publish(item["event_id"])
                    else:
                        await adapter.require().publish(item["job_id"], item["job_type"])
                except Exception:
                    async with self.db.transaction() as conn:
                        if topic == "events":
                            await execute(
                                conn,
                                """UPDATE ag_event_outbox SET publish_attempts=publish_attempts+1,
                                next_publish_at=now()+LEAST(60,power(2,LEAST(publish_attempts,5))) * interval '1 second'
                                WHERE event_id=:id AND published_at IS NULL""",
                                id=item["event_id"],
                            )
                        else:
                            await execute(
                                conn,
                                """UPDATE ag_job_wakeup SET publish_attempts=publish_attempts+1,
                                next_attempt_at=now()+LEAST(60,power(2,LEAST(publish_attempts,5))) * interval '1 second'
                                WHERE id=:id AND published_at IS NULL""",
                                id=item["id"],
                            )
                    break
                async with self.db.transaction() as conn:
                    if topic == "events":
                        await execute(
                            conn,
                            "UPDATE ag_event_outbox SET published_at=now() WHERE event_id=:id",
                            id=item["event_id"],
                        )
                    else:
                        await execute(
                            conn, "UPDATE ag_job_wakeup SET published_at=now() WHERE id=:id", id=item["id"]
                        )
                success += 1
        return success
