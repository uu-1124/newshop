"""Versioned administration; activation needs exact, successful adapter evidence."""

from uuid import uuid4

from app.application.commands import staff_dedupe
from app.application.jobs import job_view
from app.application.knowledge import save_staff
from app.domain.capabilities import JobType
from app.domain.identity import DomainError
from app.infrastructure.model_config import (
    ADAPTER_VERSION,
    ENDPOINTS,
    configuration_hash,
    configured,
    provider_for,
    secret_for,
    validate_profile,
)
from app.infrastructure.model_ledger import DEFAULT_PROMPTS, save_profile_version
from app.infrastructure.postgres.database import digest, encoded, execute, one, rows
from app.infrastructure.staff_auth import require_permission


async def config_lock(conn):
    await execute(conn, "SELECT pg_advisory_xact_lock(hashtextextended('model-prompt-config',0))")


def version_check(row, expected):
    if row["version"] != expected:
        raise DomainError("VERSION_CONFLICT", "配置版本已变化，请刷新", 409)


class ModelAdministration:
    def __init__(self, db, ledger, jobs, cursors, settings):
        self.db, self.ledger, self.jobs, self.cursors, self.settings = db, ledger, jobs, cursors, settings

    def profile_view(self, row):
        return {
            k: row[k]
            for k in (
                "name",
                "capability",
                "provider",
                "model_name",
                "public_options",
                "enabled",
                "is_active",
                "version",
            )
        } | {
            "id": str(row["id"]),
            "endpoint_ref": ENDPOINTS[row["capability"]],
            "secret_configured": configured(self.settings, row["capability"]),
        }

    async def registry(self, principal):
        require_permission(principal, "agent:model:read")
        return {
            "adapter_version": ADAPTER_VERSION,
            "entries": [
                {
                    "capability": cap,
                    "provider": provider_for(cap),
                    "endpoint_ref": endpoint,
                    "secret_ref": secret_for(cap),
                    "secret_configured": configured(self.settings, cap),
                }
                for cap, endpoint in ENDPOINTS.items()
            ],
        }

    async def listing(self, principal, limit, cursor, profile_id=None):
        require_permission(principal, "agent:model:read")
        scope = f"models:{principal.id}:{profile_id}"
        before = self.cursors.decode(cursor, scope) if cursor else None
        async with self.db.transaction(snapshot=True) as conn:
            if profile_id:
                found = await rows(
                    conn,
                    "SELECT * FROM ag_model_profile_version WHERE profile_id=:id AND version_no<:v ORDER BY version_no DESC LIMIT :limit",
                    id=profile_id,
                    v=int(before[0]) if before else 2147483647,
                    limit=limit + 1,
                )
                items = [
                    {
                        k: r[k]
                        for k in (
                            "name",
                            "capability",
                            "provider",
                            "model_name",
                            "public_options",
                            "enabled",
                            "is_active",
                            "snapshot_sha256",
                            "created_at",
                        )
                    }
                    | {
                        "profile_id": str(r["profile_id"]),
                        "version_no": r["version_no"],
                        "endpoint_ref": ENDPOINTS[r["capability"]],
                        "created_by": str(r["created_by"]) if r["created_by"] else None,
                        "compatibility_status": "PASSED"
                        if r["compatibility_evidence"].get("passed")
                        else "UNVERIFIED",
                    }
                    for r in found[:limit]
                ]
                position = [found[min(limit, len(found)) - 1]["version_no"]] if found else []
            else:
                found = await rows(
                    conn,
                    "SELECT * FROM ag_model_profile WHERE (CAST(:date AS timestamptz) IS NULL OR (created_at,id)>(CAST(:date AS timestamptz),CAST(:id AS uuid))) ORDER BY created_at,id LIMIT :limit",
                    date=before[0] if before else None,
                    id=before[1] if before else None,
                    limit=limit + 1,
                )
                items = [self.profile_view(r) for r in found[:limit]]
                position = (
                    [
                        found[min(limit, len(found)) - 1]["created_at"].isoformat(),
                        str(found[min(limit, len(found)) - 1]["id"]),
                    ]
                    if found
                    else []
                )
        return {
            "items": items,
            "has_more": len(found) > limit,
            "next_cursor": self.cursors.encode(position, scope) if len(found) > limit else None,
        }

    async def create(self, principal, key, body):
        require_permission(principal, "agent:model:write")
        validate_profile(self.settings, body)
        async with self.db.transaction() as conn:
            await config_lock(conn)
            cached = await staff_dedupe(conn, principal, "createModelProfile", key, body)
            if cached:
                return cached
            row = await one(
                conn,
                """INSERT INTO ag_model_profile(id,name,capability,provider,model_name,endpoint_ref,secret_ref,public_options)
                VALUES (:id,:name,:capability,:provider,:model_name,:endpoint_ref,:secret_ref,CAST(:options AS jsonb)) RETURNING *""",
                id=uuid4(),
                **{
                    k: body[k]
                    for k in ("name", "capability", "provider", "model_name", "endpoint_ref", "secret_ref")
                },
                options=encoded(body["public_options"]),
            )
            await save_profile_version(conn, row, principal.id)
            result = self.profile_view(row)
            await save_staff(conn, principal, "createModelProfile", key, body, result, row["id"])
            return result

    async def update(self, principal, pid, key, body):
        require_permission(principal, "agent:model:write")
        async with self.db.transaction() as conn:
            await config_lock(conn)
            scope = f"updateModelProfile:{pid}"
            cached = await staff_dedupe(conn, principal, scope, key, body)
            row = await self.profile(conn, pid)
            if cached:
                return cached
            version_check(row, body["expected_version"])
            if row["is_active"]:
                raise DomainError("MODEL_PROFILE_ACTIVE", "请先创建候选配置，验收后切换活动版本", 409)
            updated = (
                dict(row)
                | {k: v for k, v in body.items() if k not in {"expected_version", "secret_ref"}}
                | {"secret_ref": body.get("secret_ref") or row["secret_ref"]}
            )
            validate_profile(self.settings, updated)
            row = await one(
                conn,
                """UPDATE ag_model_profile SET name=:name,model_name=:model_name,endpoint_ref=:endpoint_ref,secret_ref=:secret_ref,
                public_options=CAST(:options AS jsonb),enabled=:enabled,version=version+1,updated_at=now() WHERE id=:id RETURNING *""",
                id=pid,
                **{k: updated[k] for k in ("name", "model_name", "endpoint_ref", "secret_ref", "enabled")},
                options=encoded(updated["public_options"]),
            )
            await save_profile_version(conn, row, principal.id)
            result = self.profile_view(row)
            await save_staff(conn, principal, scope, key, body, result, pid)
            return result

    async def profile(self, conn, pid):
        row = await one(conn, "SELECT * FROM ag_model_profile WHERE id=:id FOR UPDATE", id=pid)
        if not row:
            raise DomainError("NOT_FOUND", "模型配置不存在", 404)
        return row

    async def test(self, principal, pid, key, body):
        require_permission(principal, "agent:model:write")
        async with self.db.transaction() as conn:
            scope = f"testModelProfile:{pid}"
            cached = await staff_dedupe(conn, principal, scope, key, body)
            row = await self.profile(conn, pid)
            if cached:
                return cached
            version_check(row, body["expected_version"])
            validate_profile(self.settings, row, legacy=True)
            if not configured(self.settings, row["capability"]):
                raise DomainError("MODEL_CREDENTIAL_MISSING", "请先配置服务端凭据", 409)
            job = await self.jobs.enqueue_in(
                conn,
                JobType.MODEL_COMPATIBILITY,
                f"model-check:{pid}:{row['version']}:{key}",
                {
                    "profile_id": str(pid),
                    "profile_version": row["version"],
                    "configuration_sha256": configuration_hash(row),
                    "adapter_version": ADAPTER_VERSION,
                },
                principal.id,
                max_attempts=1,
            )
            result = job_view(job)
            await save_staff(conn, principal, scope, key, body, result, pid)
            return result

    async def activate(self, principal, pid, key, body):
        require_permission(principal, "agent:model:activate")
        async with self.db.transaction() as conn:
            await config_lock(conn)
            scope = f"activateModelProfile:{pid}"
            cached = await staff_dedupe(conn, principal, scope, key, body)
            row = await self.profile(conn, pid)
            if cached:
                return cached
            version_check(row, body["expected_version"])
            job = await one(
                conn,
                "SELECT * FROM ag_job WHERE id=:id AND job_type='MODEL_COMPATIBILITY'",
                id=body["compatibility_job_id"],
            )
            expected = {
                "profile_id": str(pid),
                "profile_version": row["version"],
                "configuration_sha256": configuration_hash(row),
                "adapter_version": ADAPTER_VERSION,
            }
            if (
                not job
                or job["state"] != "SUCCEEDED"
                or job["parameters"] != expected
                or not (job["result"] or {}).get("passed")
                or any((job["result"] or {}).get(k) != v for k, v in expected.items())
            ):
                raise DomainError(
                    "MODEL_COMPATIBILITY_REQUIRED", "需要当前配置版本通过真实适配器兼容验收", 409
                )
            validate_profile(self.settings, row, legacy=True)
            if (
                row["capability"] == "EMBEDDING"
                and not row["is_active"]
                and await one(
                    conn,
                    "SELECT d.id FROM ag_kb_document d JOIN ag_kb_version v ON v.id=d.active_version_id WHERE d.enabled LIMIT 1",
                )
            ):
                raise DomainError(
                    "KNOWLEDGE_REBUILD_REQUIRED", "请先停用已发布资料，切换向量模型后重新构建发布再启用", 409
                )
            evidence = {"passed": True, "job_id": str(job["id"]), **expected}
            if not row["is_active"]:
                previous = await rows(
                    conn,
                    "SELECT * FROM ag_model_profile WHERE capability=:cap AND is_active FOR UPDATE",
                    cap=row["capability"],
                )
                for old in previous:
                    retired = await one(
                        conn,
                        "UPDATE ag_model_profile SET is_active=false,version=version+1,updated_at=now() WHERE id=:id RETURNING *",
                        id=old["id"],
                    )
                    await save_profile_version(conn, retired, principal.id)
                row = await one(
                    conn,
                    "UPDATE ag_model_profile SET is_active=true,enabled=true,version=version+1,updated_at=now() WHERE id=:id RETURNING *",
                    id=pid,
                )
                await save_profile_version(conn, row, principal.id, evidence)
            result = self.profile_view(row)
            await save_staff(conn, principal, scope, key, body, result, pid)
            return result

    async def prompts(self, principal, limit, cursor, prompt_key):
        require_permission(principal, "agent:prompt:read")
        scope = f"prompts:{principal.id}:{prompt_key}"
        before = self.cursors.decode(cursor, scope) if cursor else None
        async with self.db.transaction(snapshot=True) as conn:
            found = await rows(
                conn,
                """SELECT * FROM ag_prompt_version WHERE (:key='' OR prompt_key=:key)
                AND (CAST(:date AS timestamptz) IS NULL OR (created_at,id)<(CAST(:date AS timestamptz),CAST(:id AS uuid))) ORDER BY created_at DESC,id DESC LIMIT :limit""",
                key=prompt_key,
                date=before[0] if before else None,
                id=before[1] if before else None,
                limit=limit + 1,
            )
        page = found[:limit]
        more = len(found) > limit
        return {
            "items": [prompt_view(r) for r in page],
            "has_more": more,
            "next_cursor": self.cursors.encode(
                [page[-1]["created_at"].isoformat(), str(page[-1]["id"])], scope
            )
            if more
            else None,
        }

    async def create_prompt(self, principal, key, body):
        require_permission(principal, "agent:prompt:write")
        if body["prompt_key"] not in DEFAULT_PROMPTS or not body["content"].strip():
            raise DomainError("VALIDATION_ERROR", "请选择支持的提示词职责并填写内容", 422)
        async with self.db.transaction() as conn:
            await config_lock(conn)
            cached = await staff_dedupe(conn, principal, "createPrompt", key, body)
            if cached:
                return cached
            version = await one(
                conn,
                "SELECT COALESCE(max(version_no),0)+1 AS n FROM ag_prompt_version WHERE prompt_key=:key",
                key=body["prompt_key"],
            )
            row = await one(
                conn,
                """INSERT INTO ag_prompt_version(id,prompt_key,version_no,content,sha256,created_by,change_note)
                VALUES (:id,:key,:version,:content,:sha,:actor,:note) RETURNING *""",
                id=uuid4(),
                key=body["prompt_key"],
                version=version["n"],
                content=body["content"],
                sha=digest(body["content"]),
                actor=principal.id,
                note=body["change_note"],
            )
            result = prompt_view(row)
            await save_staff(conn, principal, "createPrompt", key, body, result, row["id"])
            return result

    async def publish_prompt(self, principal, pid, key, body):
        require_permission(principal, "agent:prompt:publish")
        async with self.db.transaction() as conn:
            await config_lock(conn)
            scope = f"publishPrompt:{pid}"
            cached = await staff_dedupe(conn, principal, scope, key, body)
            row = await one(conn, "SELECT * FROM ag_prompt_version WHERE id=:id FOR UPDATE", id=pid)
            if not row:
                raise DomainError("NOT_FOUND", "提示词版本不存在", 404)
            if cached:
                return cached
            version_check(row, body["expected_version"])
            if row["prompt_key"] not in DEFAULT_PROMPTS or row["sha256"] != digest(row["content"]):
                raise DomainError("PROMPT_INVALID", "提示词职责或摘要校验失败", 409)
            await execute(
                conn,
                "UPDATE ag_prompt_version SET state='RETIRED',version=version+1,updated_at=now() WHERE prompt_key=:key AND state='PUBLISHED' AND id<>:id",
                key=row["prompt_key"],
                id=pid,
            )
            row = await one(
                conn,
                "UPDATE ag_prompt_version SET state='PUBLISHED',published_at=COALESCE(published_at,now()),version=version+1,updated_at=now() WHERE id=:id RETURNING *",
                id=pid,
            )
            result = prompt_view(row)
            await save_staff(conn, principal, scope, key, body, result, pid)
            return result


def prompt_view(row):
    return {k: row[k] for k in ("prompt_key", "version_no", "state", "content", "version", "created_at")} | {
        "id": str(row["id"]),
        "change_note": row["change_note"],
    }
