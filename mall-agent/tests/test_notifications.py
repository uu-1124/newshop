import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from redis.exceptions import NoPermissionError

from app.infrastructure.wakeups import RabbitWakeups, RedisWakeups, wakeup_id
from app.settings import Settings


@pytest.mark.anyio
async def test_scoped_real_rabbit_redelivery_and_redis_reconnect(integration_config):
    path = Path(__file__).resolve().parents[2] / "deployment/.run/runtime-env.json"
    if not path.exists():
        pytest.skip("Scoped messaging accounts have not been prepared")
    cfg = json.loads(path.read_text())
    settings = Settings(
        _env_file=None,
        database_url=integration_config["databaseUrl"],
        bridge_secret="unit",
        broker_secret="unit",
        cursor_secret="unit",
        notifications_enabled=True,
        rabbit_url=f"amqp://{cfg['rabbitUser']}:{cfg['rabbitPassword']}@127.0.0.1:15672/%2Fmall-agent",
        redis_url=f"redis://{cfg['redisUser']}:{cfg['redisPassword']}@127.0.0.1:16379/0",
        notification_namespace="mall-agent-" + uuid4().hex[:10],
    )
    publisher, consumer = RabbitWakeups(settings), RabbitWakeups(settings)
    redis = RedisWakeups(settings)
    redis.listener = asyncio.create_task(redis.listen())
    job_id = uuid4()
    try:
        await publisher.publish(job_id, "RUNTIME_CHECK")
        first = await consumer.receive()
        assert first and wakeup_id(first.body) == job_id
        assert set(json.loads(first.body)) == {"schema_version", "job_id", "job_type"}
        # Reject and requeue emulates death before an ACK; the same ID must be redelivered.
        await first.reject(requeue=True)
        second = await consumer.receive()
        assert second and second.redelivered and wakeup_id(second.body) == job_id
        await second.ack()
        assert await consumer.receive() is None
        for _ in range(100):
            if redis.connected:
                break
            await asyncio.sleep(0.05)
        assert redis.connected
        version = redis.version
        await redis.publish(123)
        assert await redis.wait(version, timeout=3) > version
        # Disconnect local sockets only; listener must resubscribe without restarting Redis.
        redis.connected = False
        await redis.client.connection_pool.disconnect(inuse_connections=True)
        for _ in range(100):
            if redis.connected:
                break
            await asyncio.sleep(0.05)
        assert redis.connected
        version = redis.version
        await redis.publish(124)
        assert await redis.wait(version, timeout=3) > version
        with pytest.raises(NoPermissionError):
            await redis.client.publish("mall:unrelated:channel", "123")
        with pytest.raises(NoPermissionError):
            await redis.client.get("mall:auth:private")
    finally:
        if publisher.channel:
            await publisher.connect()
            queue = await publisher.channel.get_queue(publisher.queue_name)
            await queue.delete(if_unused=False, if_empty=False)
            await publisher.exchange.delete(if_unused=False)
        await consumer.close()
        await publisher.close()
        await redis.close()
