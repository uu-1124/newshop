"""Read-only fixed dependency probes. No buyer token, model call or business command."""

import asyncio
import logging
import time
from datetime import UTC, datetime
from uuid import uuid4

import aio_pika
import urllib3
from minio import Minio
from minio.error import S3Error
from pymilvus import MilvusClient
from redis.asyncio import Redis

from app.application.observability import Sample
from app.infrastructure.java_bridge import sign

for logger in ("aio_pika", "aiormq", "pymilvus"):
    logging.getLogger(logger).setLevel(logging.CRITICAL)


async def measured(operation, enabled=True):
    if not enabled:
        return Sample(state="DISABLED", latency_ms=0)
    started = time.perf_counter()
    try:
        await asyncio.wait_for(operation(), timeout=12)
        return Sample(state="UP", latency_ms=round((time.perf_counter() - started) * 1000))
    except Exception:
        return Sample(
            state="DOWN",
            latency_ms=min(300000, round((time.perf_counter() - started) * 1000)),
            error_code="DEPENDENCY_PROBE_FAILED",
        )


async def signed_bridge(client, settings, staff=False):
    path = "/agent-bridge/v1/identity"
    prefix = "/mall-admin" if staff else "/mall-portal"
    timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    nonce = str(uuid4())
    response = await client.get(
        "http://127.0.0.1:" + ("8080" if staff else "8085") + path,
        headers={
            "X-Agent-Timestamp": timestamp,
            "X-Agent-Nonce": nonce,
            "X-Request-ID": str(uuid4()),
            "X-Agent-Signature": sign(
                "GET",
                prefix + path,
                b"",
                timestamp,
                nonce,
                (settings.staff_bridge_secret if staff else settings.bridge_secret).get_secret_value(),
            ),
        },
    )
    # A valid service signature must still reject an unauthenticated identity.
    if response.status_code != 401 or response.json().get("code") != "AUTH_EXPIRED":
        raise ValueError("Bridge did not verify signature and preserve identity boundary")


async def gateway(client):
    response = await client.get("http://127.0.0.1:8201/mall-portal/agent-bridge/v1/identity")
    if response.status_code not in {200, 401} or response.json().get("code") not in {401, "AUTH_EXPIRED"}:
        raise ValueError("Gateway did not reject an unauthenticated bridge request")


def private_objects(settings, object_key):
    http = urllib3.PoolManager(timeout=urllib3.Timeout(connect=3, read=4), retries=False)
    client = Minio(
        settings.objects_endpoint,
        settings.objects_access_key.get_secret_value(),
        settings.objects_secret_key.get_secret_value(),
        secure=False,
        http_client=http,
    )
    try:
        client.stat_object(settings.objects_bucket, object_key or "chat/_readiness_missing_object")
    except S3Error as error:
        if object_key or error.code not in {"NoSuchKey", "NoSuchObject"}:
            raise
    finally:
        http.clear()


def milvus(settings, expected):
    client = MilvusClient(
        uri=settings.milvus_uri,
        db_name=settings.milvus_database,
        user=settings.milvus_reader_user,
        password=settings.milvus_reader_password.get_secret_value(),
        timeout=5,
        grpc_options={"grpc.enable_http_proxy": 0},
    )
    try:
        collections = set(client.list_collections(timeout=5))
        if not set(expected) <= collections:
            raise ValueError("Active knowledge collection missing")
    finally:
        client.close()


async def redis(settings):
    client = Redis.from_url(settings.redis_url.get_secret_value(), socket_timeout=4, socket_connect_timeout=3)
    try:
        if not await client.ping():
            raise ValueError("Redis did not answer authenticated ping")
    finally:
        await client.aclose()


async def rabbitmq(settings):
    connection = await aio_pika.connect(settings.rabbit_url.get_secret_value(), timeout=4)
    try:
        channel = await connection.channel()
        await channel.get_queue(settings.notification_namespace + ".jobs.v1", ensure=True)
    finally:
        await connection.close()


async def collect(settings, object_key, collections, client):
    operations = {
        "portal_bridge": (lambda: signed_bridge(client, settings), True),
        "admin_bridge": (
            lambda: signed_bridge(client, settings, True),
            bool(settings.staff_bridge_secret.get_secret_value()),
        ),
        "gateway": (lambda: gateway(client), True),
        "private_objects": (
            lambda: asyncio.to_thread(private_objects, settings, object_key),
            settings.attachments_enabled,
        ),
        "milvus": (lambda: asyncio.to_thread(milvus, settings, collections), settings.knowledge_enabled),
        "redis": (lambda: redis(settings), settings.notifications_enabled),
        "rabbitmq": (lambda: rabbitmq(settings), settings.notifications_enabled),
    }
    values = await asyncio.gather(*(measured(fn, enabled) for fn, enabled in operations.values()))
    return dict(zip(operations, values, strict=True))
