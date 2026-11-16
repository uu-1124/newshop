"""Derived candidates only. Callers must recheck PG authorization and active versions before use."""

import hashlib
import math
import re
from uuid import UUID

from pydantic import BaseModel, Field
from pymilvus import DataType

from app.domain.identity import DomainError


class CandidateChunk(BaseModel):
    chunk_id: UUID
    parent_chunk_id: UUID
    document_id: UUID
    version_id: UUID
    visibility: str = Field(pattern="^(PUBLIC|STAFF)$")
    text: str = Field(min_length=1, max_length=4000)


class MilvusCandidates:
    def __init__(self, client, collection, dimension, profile_id, profile_version, generation):
        if not re.fullmatch(r"agent_[a-z0-9_]{1,100}", collection):
            raise ValueError("Collection must belong to the agent namespace")
        self.client, self.collection, self.dimension = client, collection, dimension
        self.profile_id, self.profile_version, self.generation = (
            str(UUID(str(profile_id))),
            profile_version,
            generation,
        )

    def create(self):
        if self.client.has_collection(self.collection):
            raise ValueError("Use a fresh generation instead of overwriting an existing collection")
        schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=36)
        for field in ("parent_chunk_id", "document_id", "version_id", "embedding_profile_id"):
            schema.add_field(field, DataType.VARCHAR, max_length=36)
        schema.add_field("visibility", DataType.VARCHAR, max_length=16)
        schema.add_field("profile_version", DataType.INT64)
        schema.add_field("generation", DataType.VARCHAR, max_length=80)
        schema.add_field("content_sha256", DataType.VARCHAR, max_length=64)
        schema.add_field("text", DataType.VARCHAR, max_length=16000)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self.dimension)
        indexes = self.client.prepare_index_params()
        indexes.add_index(field_name="vector", index_type="FLAT", metric_type="COSINE")
        self.client.create_collection(self.collection, schema=schema, index_params=indexes, timeout=30)

    def _validate_vectors(self, vectors):
        if not vectors or any(
            len(v) != self.dimension or any(not math.isfinite(x) for x in v) for v in vectors
        ):
            raise DomainError("MODEL_DIMENSION_MISMATCH", "向量与集合维度不匹配", 422)

    def upsert(self, chunks: list[CandidateChunk], vectors):
        if not chunks or len(chunks) != len(vectors) or len(chunks) > 100:
            raise ValueError("Chunk/vector batch mismatch")
        self._validate_vectors(vectors)
        rows = []
        for chunk, vector in zip(chunks, vectors, strict=True):
            rows.append(
                chunk.model_dump(mode="json")
                | {
                    "embedding_profile_id": self.profile_id,
                    "profile_version": self.profile_version,
                    "generation": self.generation,
                    "content_sha256": hashlib.sha256(chunk.text.encode()).hexdigest(),
                    "vector": vector,
                }
            )
        self.client.upsert(self.collection, rows, timeout=20)
        self.client.flush(self.collection, timeout=30)

    def search(self, vector, visibility, version_ids: list[UUID], limit=5):
        self._validate_vectors([vector])
        if visibility not in {"PUBLIC", "STAFF"} or not version_ids or not 1 <= limit <= 20:
            raise ValueError("Explicit authorized scope and bounded limit are required")
        versions = [str(UUID(str(value))) for value in version_ids]
        hits = self.client.search(
            self.collection,
            data=[vector],
            anns_field="vector",
            filter=(
                "visibility == {visibility} and version_id in {versions} "
                "and embedding_profile_id == {profile} and profile_version == {profile_version} "
                "and generation == {generation}"
            ),
            filter_params={
                "visibility": visibility,
                "versions": versions,
                "profile": self.profile_id,
                "profile_version": self.profile_version,
                "generation": self.generation,
            },
            limit=limit,
            output_fields=[
                "chunk_id",
                "parent_chunk_id",
                "document_id",
                "version_id",
                "text",
                "content_sha256",
            ],
            search_params={"metric_type": "COSINE"},
            consistency_level="Strong",
            timeout=20,
        )[0]
        # PyMilvus 3 uses the actual primary field name rather than the legacy "id" key.
        return [
            {"id": hit["chunk_id"], "distance": hit["distance"], "entity": dict(hit["entity"])}
            for hit in hits
        ]
