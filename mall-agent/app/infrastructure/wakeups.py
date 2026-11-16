"""ID-only wakeups. PostgreSQL remains authoritative during outages and redelivery."""

import asyncio
import json
import logging
from uuid import UUID

import aio_pika
from redis.asyncio import Redis

from app.application.extensions import Extension
from app.domain.capabilities import Wakeups
from app.infrastructure.postgres.database import encoded

log = logging.getLogger(__name__)
# Dependency reconnect diagnostics can include connection URLs. Emit our own safe diagnostics.
for _logger in ("aio_pika", "aiormq"):
    logging.getLogger(_logger).setLevel(logging.CRITICAL)


class RabbitWakeups:
    def __init__(self, settings):
        self.url = settings.rabbit_url
        self.queue_name = settings.notification_namespace + ".jobs.v1"
        self.exchange_name = settings.notification_namespace + ".jobs"
        self.exchange = None
        self.connection = None
        self.channel = None

    async def connect(self):
        if self.connection is None or self.connection.is_closed:
            self.connection = await aio_pika.connect_robust(
                self.url.get_secret_value(), timeout=3, heartbeat=15
            )
        if self.channel is None or self.channel.is_closed:
            self.channel = await self.connection.channel(publisher_confirms=True, on_return_raises=True)
            await self.channel.set_qos(prefetch_count=1)
            self.exchange = await self.channel.declare_exchange(
                self.exchange_name, aio_pika.ExchangeType.DIRECT, durable=True
            )
            queue = await self.channel.declare_queue(self.queue_name, durable=True)
            await queue.bind(self.exchange, routing_key="jobs")
        return self.channel

    async def publish(self, job_id, kind):
        await self.connect()
        message = aio_pika.Message(
            encoded({"schema_version": 1, "job_id": str(job_id), "job_type": str(kind)}).encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=str(job_id),
        )
        await self.exchange.publish(message, routing_key="jobs", mandatory=True, timeout=3)

    async def receive(self):
        channel = await self.connect()
        queue = await channel.get_queue(self.queue_name)
        return await queue.get(fail=False, timeout=1)

    async def close(self):
        if self.connection:
            await self.connection.close()


def wakeup_id(body):
    if len(body) > 1024:
        raise ValueError("Invalid wakeup")
    data = json.loads(body)
    if set(data) != {"schema_version", "job_id", "job_type"} or data["schema_version"] != 1:
        raise ValueError("Invalid wakeup")
    from app.domain.capabilities import JobType

    JobType(data["job_type"])
    return UUID(data["job_id"])


class RedisWakeups:
    def __init__(self, settings):
        self.client = Redis.from_url(
            settings.redis_url.get_secret_value(),
            socket_timeout=3,
            socket_connect_timeout=3,
            decode_responses=True,
        )
        self.channel = settings.notification_namespace + ":events"
        self.condition = asyncio.Condition()
        self.version = 0
        self.listener = None
        self.connected = False

    async def publish(self, event_id, kind=None):
        await self.client.publish(self.channel, str(event_id))

    async def listen(self):
        while True:
            try:
                async with self.client.pubsub() as subscription:
                    await subscription.subscribe(self.channel)
                    self.connected = True
                    while True:
                        message = await subscription.get_message(ignore_subscribe_messages=True, timeout=1)
                        if message and str(message.get("data", "")).isdigit():
                            async with self.condition:
                                self.version += 1
                                self.condition.notify_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("redis listener unavailable type=%s", type(exc).__name__)
                await asyncio.sleep(2)
            finally:
                self.connected = False

    async def wait(self, version, timeout=1):
        async with self.condition:
            try:
                await asyncio.wait_for(self.condition.wait_for(lambda: self.version > version), timeout)
            except TimeoutError:
                pass
            return self.version

    async def close(self):
        if self.listener:
            self.listener.cancel()
            await asyncio.gather(self.listener, return_exceptions=True)
        await self.client.aclose()


class NotificationBindings:
    def __init__(self, settings=None):
        self.rabbitmq = Extension[Wakeups]("M14", "任务消息通知")
        self.redis = Extension[Wakeups]("M03", "事件即时通知")
        if settings and settings.notifications_enabled:
            self.rabbitmq.implementation = RabbitWakeups(settings)
            self.redis.implementation = RedisWakeups(settings)

    async def start(self):
        if self.redis.bound:
            adapter = self.redis.require()
            adapter.listener = asyncio.create_task(adapter.listen())

    async def wait(self, version=0):
        if self.redis.bound:
            return await self.redis.require().wait(version)
        await asyncio.sleep(1)
        return version

    async def close(self):
        for adapter in (self.rabbitmq, self.redis):
            if adapter.bound:
                await adapter.require().close()

    def describe(self):
        return {"rabbitmq": self.rabbitmq.bound, "redis": self.redis.bound, "sse_fallback": "POSTGRES_POLL"}
