"""Lazy scoped Milvus clients. Each build attempt gets an isolated collection."""

import asyncio
import logging
import re
from threading import Lock

from pymilvus import MilvusClient

from app.domain.identity import DomainError
from app.infrastructure.milvus_candidates import MilvusCandidates

logging.getLogger("pymilvus").setLevel(logging.CRITICAL)


class KnowledgeIndexes:
    def __init__(self, settings):
        self.settings = settings
        self.reader = self.builder = None
        self._client_lock = Lock()

    def client(self, write=False):
        s = self.settings
        slot = "builder" if write else "reader"
        with self._client_lock:
            client = getattr(self, slot)
            if client is None:
                user = s.milvus_builder_user if write else s.milvus_reader_user
                password = s.milvus_builder_password if write else s.milvus_reader_password
                if not user or not password.get_secret_value():
                    raise DomainError("KNOWLEDGE_INDEX_UNAVAILABLE", "知识索引账号未配置", 503)
                client = MilvusClient(
                    uri=s.milvus_uri,
                    db_name=s.milvus_database,
                    user=user,
                    password=password.get_secret_value(),
                    timeout=10,
                    grpc_options={"grpc.enable_http_proxy": 0},
                )
                setattr(self, slot, client)
            return client

    def adapter(self, spec, write=False):
        return MilvusCandidates(self.client(write), **spec)

    async def create(self, spec):
        def operation():
            index = self.adapter(spec, True)
            if not index.client.has_collection(spec["collection"]):
                index.create()

        await self.safe(operation)

    async def upsert(self, spec, chunks, vectors):
        await self.safe(lambda: self.adapter(spec, True).upsert(chunks, vectors))

    async def search(self, spec, vector, version_id, visibility="MEMBER", limit=8):
        return await self.safe(
            lambda: self.adapter(spec).search(
                vector, "PUBLIC" if visibility == "MEMBER" else "STAFF", [version_id], limit
            )
        )

    async def collections(self):
        return await self.safe(lambda: self.client(True).list_collections())

    async def drop_unused(self, collection):
        if not re.fullmatch(r"agent_kb_[a-f0-9]{32}_[1-9][0-9]*", collection):
            raise DomainError("KNOWLEDGE_SCOPE_INVALID", "索引集合不属于受控构建命名空间", 422)

        def remove():
            client = self.client(True)
            if client.has_collection(collection):
                client.drop_collection(collection, timeout=30)

        await self.safe(remove)

    async def safe(self, operation):
        try:
            return await asyncio.to_thread(operation)
        except DomainError:
            raise
        except Exception:
            raise DomainError("KNOWLEDGE_INDEX_UNAVAILABLE", "知识索引暂不可用", 503) from None

    def close(self):
        for client in (self.reader, self.builder):
            if client:
                client.close()
