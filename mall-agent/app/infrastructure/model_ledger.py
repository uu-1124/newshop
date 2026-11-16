"""Bootstrap once; resolve immutable model and prompt versions for every run."""

from uuid import NAMESPACE_URL, uuid4, uuid5

from app.agents.answer_stream import FACT_SELECTION_PROMPT
from app.agents.dialog import PROMPT, PROMPT_VERSION
from app.agents.knowledge_answer import KNOWLEDGE_PROMPT
from app.agents.live_workflow import ROUTER_PROMPT
from app.domain.identity import DomainError
from app.infrastructure.model_call_context import current_model_call, source_actor
from app.infrastructure.model_config import ENDPOINTS, provider_for, resolve_settings, secret_for
from app.infrastructure.model_metering import finish_meter
from app.infrastructure.model_quota import reserve, settle
from app.infrastructure.postgres.database import digest, encoded, execute, one, rows

DEFAULT_PROMPTS = {
    "readonly-dialog": PROMPT,
    "workflow-router": ROUTER_PROMPT,
    "knowledge-answer": KNOWLEDGE_PROMPT,
    "fact-selector": FACT_SELECTION_PROMPT,
}


async def save_profile_version(conn, profile, actor=0, evidence=None):
    snapshot = {
        k: profile[k]
        for k in (
            "name",
            "capability",
            "provider",
            "model_name",
            "endpoint_ref",
            "secret_ref",
            "public_options",
            "enabled",
            "is_active",
        )
    }
    await execute(
        conn,
        """INSERT INTO ag_model_profile_version
        (profile_id,version_no,name,enabled,is_active,capability,provider,model_name,endpoint_ref,
        secret_version_ref,public_options,snapshot_sha256,compatibility_evidence,created_by)
        VALUES (:id,:version,:name,:enabled,:is_active,:capability,:provider,:model_name,:endpoint_ref,
        :secret_ref,CAST(:options AS jsonb),:sha,CAST(:evidence AS jsonb),:actor)""",
        **{
            k: profile[k]
            for k in (
                "id",
                "version",
                "name",
                "enabled",
                "is_active",
                "capability",
                "provider",
                "model_name",
                "endpoint_ref",
                "secret_ref",
            )
        },
        options=encoded(profile["public_options"]),
        sha=digest(snapshot),
        evidence=encoded(evidence or {}),
        actor=actor,
    )


class ModelLedger:
    def __init__(self, db, settings):
        self.db, self.settings, self.base_settings, self.profiles = db, settings, settings, {}
        self.prompt_id = self.routing_prompt_id = self.knowledge_prompt_id = self.fact_prompt_id = None
        self.prompts, self.records = {}, {}

    async def setup(self):
        s = self.base_settings
        defaults = {
            "DIALOG": (s.dialog_model, True, {"max_output_tokens": s.dialog_max_output_tokens}),
            "EMBEDDING": (s.embedding_model, True, {"dimension": s.embedding_dimension}),
            "RERANK": (s.rerank_model, True, {}),
            "VISION": (s.vision_model, s.vision_enabled, {"detail": "auto"}),
            "ASR": (s.asr_model, True, {}),
            "TTS": (s.tts_model, True, {"voice": "Cherry"}),
        }
        async with self.db.transaction() as conn:
            await execute(conn, "SELECT pg_advisory_xact_lock(hashtextextended('model-prompt-config',0))")
            for cap, (model, enabled, options) in defaults.items():
                pid = uuid5(NAMESPACE_URL, "mall-agent/model/" + cap)
                if await one(conn, "SELECT id FROM ag_model_profile WHERE id=:id", id=pid):
                    continue
                active = enabled and not await one(
                    conn, "SELECT id FROM ag_model_profile WHERE capability=:cap AND is_active", cap=cap
                )
                row = await one(
                    conn,
                    """INSERT INTO ag_model_profile(id,name,capability,provider,model_name,endpoint_ref,secret_ref,public_options,enabled,is_active)
                    VALUES (:id,:name,:cap,:provider,:model,:endpoint,:secret,CAST(:options AS jsonb),:enabled,:active) RETURNING *""",
                    id=pid,
                    name=cap + " " + model,
                    cap=cap,
                    provider=provider_for(cap),
                    model=model,
                    endpoint=ENDPOINTS[cap],
                    secret=secret_for(cap),
                    options=encoded(options),
                    enabled=enabled,
                    active=active,
                )
                await save_profile_version(conn, row)
            for key, content in DEFAULT_PROMPTS.items():
                if await one(
                    conn,
                    "SELECT id FROM ag_prompt_version WHERE prompt_key=:key AND state='PUBLISHED'",
                    key=key,
                ):
                    continue
                version = await one(
                    conn,
                    "SELECT COALESCE(max(version_no),0)+1 AS n FROM ag_prompt_version WHERE prompt_key=:key",
                    key=key,
                )
                await execute(
                    conn,
                    """INSERT INTO ag_prompt_version(id,prompt_key,version_no,content,state,sha256,created_by,published_at)
                    VALUES (:id,:key,:v,:content,'PUBLISHED',:sha,0,now())""",
                    id=uuid4(),
                    key=key,
                    v=version["n"],
                    content=content,
                    sha=digest(content),
                )
        frozen = await self.for_snapshot(await self.capture())
        self.profiles, self.records, self.prompts = frozen.profiles, frozen.records, frozen.prompts
        self.settings = frozen.settings
        self.prompt_id, self.routing_prompt_id, self.knowledge_prompt_id = (
            frozen.prompt_id,
            frozen.routing_prompt_id,
            frozen.knowledge_prompt_id,
        )
        self.fact_prompt_id = frozen.fact_prompt_id

    async def capture(self, conn=None):
        if conn is None:
            async with self.db.transaction() as connection:
                return await self.capture(connection)
        await execute(conn, "SELECT pg_advisory_xact_lock_shared(hashtextextended('model-prompt-config',0))")
        profiles = await rows(
            conn,
            "SELECT DISTINCT ON (capability) * FROM ag_model_profile WHERE is_active OR (NOT enabled AND id=ANY(CAST(:defaults AS uuid[]))) ORDER BY capability,is_active DESC,created_at,id",
            defaults=[str(uuid5(NAMESPACE_URL, "mall-agent/model/" + cap)) for cap in ENDPOINTS],
        )
        prompts = await rows(conn, "SELECT id,prompt_key FROM ag_prompt_version WHERE state='PUBLISHED'")
        ids = {p["prompt_key"]: str(p["id"]) for p in prompts}
        return {
            "provider": self.base_settings.provider,
            "model_profiles": {
                p["capability"]: {
                    "id": str(p["id"]),
                    "version": p["version"],
                    "model": p["model_name"],
                    "enabled": p["enabled"],
                }
                for p in profiles
            },
            "prompt_version_id": ids.get("readonly-dialog"),
            "prompt_key": PROMPT_VERSION,
            "routing_prompt_version_id": ids.get("workflow-router"),
            "knowledge_prompt_version_id": ids.get("knowledge-answer"),
            "fact_prompt_version_id": ids.get("fact-selector"),
        }

    async def for_snapshot(self, snapshot):
        frozen = ModelLedger(self.db, self.base_settings)
        frozen.profiles = {k: dict(v) for k, v in snapshot.get("model_profiles", {}).items()}
        async with self.db.transaction(snapshot=True) as conn:
            versions = await rows(
                conn,
                """SELECT v.* FROM ag_model_profile_version v JOIN
                jsonb_to_recordset(CAST(:profiles AS jsonb)) AS p(id uuid,version bigint)
                ON v.profile_id=p.id AND v.version_no=p.version""",
                profiles=encoded(
                    [{"id": p["id"], "version": p["version"]} for p in frozen.profiles.values()]
                ),
            )
            profiles_by_version = {(str(p["profile_id"]), p["version_no"]): p for p in versions}
            for cap, p in frozen.profiles.items():
                row = profiles_by_version.get((str(p["id"]), p["version"]))
                if (
                    not row
                    or row["capability"] != cap
                    or row["model_name"] != p["model"]
                    or row["enabled"] != p["enabled"]
                ):
                    raise DomainError("MODEL_CONFIG_UNAVAILABLE", "原模型配置版本不可用", 503)
                frozen.records[cap] = dict(row)
            prompt_fields = (
                ("readonly-dialog", "prompt_version_id"),
                ("workflow-router", "routing_prompt_version_id"),
                ("knowledge-answer", "knowledge_prompt_version_id"),
                ("fact-selector", "fact_prompt_version_id"),
            )
            prompts = await rows(
                conn,
                "SELECT * FROM ag_prompt_version WHERE id=ANY(CAST(:ids AS uuid[]))",
                ids=list({snapshot[field] for _, field in prompt_fields if snapshot.get(field)}),
            )
            prompts_by_id = {(str(row["id"]), row["prompt_key"]): row for row in prompts}
            for key, field in prompt_fields:
                pid = snapshot.get(field)
                if key == "fact-selector" and not pid:
                    # Pre-upgrade snapshots remain readable. Their running jobs
                    # are interrupted on reload; new runs capture this version.
                    continue
                row = prompts_by_id.get((str(pid), key)) if pid else None
                if not row or row["sha256"] != digest(row["content"]):
                    raise DomainError("PROMPT_VERSION_UNAVAILABLE", "原提示词版本不可用", 503)
                frozen.prompts[key] = row["content"]
        frozen.settings = resolve_settings(self.base_settings, frozen.records)
        frozen.is_frozen = True
        frozen.prompt_id = snapshot.get("prompt_version_id")
        frozen.routing_prompt_id = snapshot.get("routing_prompt_version_id")
        frozen.knowledge_prompt_id = snapshot.get("knowledge_prompt_version_id")
        frozen.fact_prompt_id = snapshot.get("fact_prompt_version_id")
        return frozen

    def snapshot(self):
        return {
            "provider": self.base_settings.provider,
            "model_profiles": self.profiles,
            "prompt_version_id": self.prompt_id,
            "prompt_key": PROMPT_VERSION,
            "routing_prompt_version_id": self.routing_prompt_id,
            "knowledge_prompt_version_id": self.knowledge_prompt_id,
            "fact_prompt_version_id": self.fact_prompt_id,
        }

    def recorder(self, capability, run_id=None, prompt_id=None, *, job_id=None):
        context = current_model_call.get()
        media_request_id = context.media_request_id if context else None
        if context:
            if (run_id is not None and str(run_id) != str(context.run_id)) or (
                job_id is not None and str(job_id) != str(context.job_id)
            ):
                raise DomainError("MODEL_CALL_CONTEXT_MISMATCH", "模型调用任务关联不匹配", 409)
            run_id, job_id = context.run_id, context.job_id
        if run_id is not None and job_id is not None:
            raise DomainError("MODEL_CALL_CONTEXT_MISMATCH", "模型调用只能关联一种任务", 409)
        profile = dict(self.profiles[capability])
        selected_prompt = (prompt_id or self.prompt_id) if capability == "DIALOG" else None

        async def record(data):
            if data.get("stage") == "reserve":
                if not profile["enabled"]:
                    raise DomainError("MODEL_CAPABILITY_DISABLED", "该模型能力尚未开放", 503)
                async with self.db.transaction() as conn:
                    limit = (
                        self.records.get(capability, {})
                        .get("public_options", {})
                        .get("daily_limit_units", self.base_settings.model_daily_requests)
                    )
                    subject = await source_actor(conn, context, run_id, job_id)
                    call_id = await reserve(
                        conn, capability, profile, limit, data.get("reservation_units"), subject
                    )
                    if context:
                        # Quota locking may have waited while stop/delete/revocation
                        # advanced the source. Roll back its reservations if so.
                        await source_actor(conn, context, run_id, job_id)
                    # Link the reservation before network IO. If the adapter dies,
                    # UNKNOWN still identifies the responsible run/job and version.
                    await execute(
                        conn,
                        """INSERT INTO ag_model_call
                        (id,run_id,job_id,media_request_id,profile_id,profile_version,prompt_version_id,capability,status,latency_ms,error_code)
                        VALUES(:id,:run,:job,:media,:profile,:version,:prompt,:cap,'UNKNOWN',0,'ADAPTER_IN_FLIGHT')""",
                        id=call_id,
                        run=run_id,
                        job=job_id,
                        media=media_request_id,
                        profile=profile["id"],
                        version=profile["version"],
                        prompt=selected_prompt,
                        cap=capability,
                    )
                    return call_id
            usage = data.get("usage") or {}
            async with self.db.transaction() as conn:
                # Match governance reconciliation's meter -> call lock order.
                await one(
                    conn,
                    "SELECT call_id FROM ag_model_meter WHERE call_id=:id FOR UPDATE",
                    id=data["call_id"],
                )
                original = await one(
                    conn, "SELECT * FROM ag_model_call WHERE id=:id FOR UPDATE", id=data["call_id"]
                )
                if not original or any(
                    str(original[key]) != str(value)
                    for key, value in {
                        "run_id": run_id,
                        "job_id": job_id,
                        "media_request_id": media_request_id,
                        "profile_id": profile["id"],
                        "profile_version": profile["version"],
                        "capability": capability,
                        "prompt_version_id": selected_prompt,
                    }.items()
                ):
                    raise DomainError("MODEL_CALL_CONTEXT_MISMATCH", "模型调用完成记录不匹配", 409)
                if original["status"] != "UNKNOWN" or original["error_code"] != "ADAPTER_IN_FLIGHT":
                    return  # The first completion or audited reconciliation is final.
                cost = await finish_meter(conn, data["call_id"], usage, data["status"] == "SUCCEEDED")
                await execute(
                    conn,
                    """UPDATE ag_model_call SET provider_request_id=:request,status=:status,
                    input_tokens=:input,output_tokens=:output,billable_units=:units,
                    latency_ms=:latency,error_code=:error,estimated_cost=:cost WHERE id=:id""",
                    id=data["call_id"],
                    request=(data.get("provider_request_id") or "")[:160] or None,
                    status=data["status"],
                    input=usage.get("input_tokens", usage.get("prompt_tokens")),
                    output=usage.get("output_tokens", usage.get("completion_tokens")),
                    units=usage.get("characters", usage.get("seconds")),
                    latency=data["latency_ms"],
                    error=data.get("error_code"),
                    cost=cost,
                )
                await settle(conn, data["call_id"], data["status"], usage)

        return record
