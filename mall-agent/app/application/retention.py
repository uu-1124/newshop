"""Policy-bound previews and resumable retention. No arbitrary deletion scope."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from app.application.commands import staff_dedupe
from app.application.governance import PublicPolicy, governance_audit, read_policy
from app.application.jobs import Jobs, job_view
from app.domain.capabilities import JobType
from app.domain.identity import DomainError
from app.infrastructure.postgres.database import append_events, digest, encoded, execute, one, rows
from app.infrastructure.staff_auth import require_permission


class PlanParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    plan_id: UUID
    settings_sha256: str
    settings_version: int


def validate_plan(parameters):
    try:
        value = PlanParameters.model_validate(parameters)
        if value.kind != "RETENTION_POLICY" or value.settings_version < 1 or len(value.settings_sha256) != 64:
            raise ValueError()
        return value.model_dump(mode="json")
    except ValueError:
        raise DomainError("VALIDATION_ERROR", "留存任务参数无效", 422) from None


async def locked_job(conn, lease):
    job = await one(
        conn,
        """SELECT id FROM ag_job WHERE id=:id AND state='RUNNING'
        AND lease_owner=:owner AND fencing_token=:fence AND lease_until>now() FOR UPDATE""",
        id=lease.id,
        owner=lease.owner,
        fence=lease.fencing_token,
    )
    if not job:
        raise DomainError("LEASE_LOST", "留存任务租约已失效", 409)


class Retention:
    def __init__(self, db, settings, jobs=None):
        self.db, self.settings, self.jobs = db, settings, jobs

    async def preview(self, principal, key, proposed):
        require_permission(principal, "agent:retention:preview")
        value = PublicPolicy.model_validate(proposed).model_dump(mode="json")
        async with self.db.transaction() as conn:
            cached = await staff_dedupe(conn, principal, "previewRetention", key, value)
            if cached:
                return cached
            result = await self.create_preview(conn, value, principal.id)
            await self.receipt(conn, principal, "previewRetention", key, value, result)
            return result

    async def receipt(self, conn, principal, scope, key, body, result):
        await execute(
            conn,
            """INSERT INTO ag_request_dedupe(scope,actor_kind,actor_id,idempotency_key,request_hash,response_ref,expires_at)
            VALUES(:scope,'STAFF',:actor,:key,:hash,CAST(:result AS jsonb),now()+interval '7 days')""",
            scope=scope,
            actor=principal.id,
            key=key,
            hash=digest(body),
            result=encoded(result),
        )
        await governance_audit(conn, principal, scope, result["id"], key)

    async def create_preview(self, conn, value, actor=None, scheduled=False):
        current = await read_policy(conn, lock=True)
        plan_id, sha = uuid4(), digest(value)
        parameters = {
            "kind": "RETENTION_POLICY",
            "plan_id": str(plan_id),
            "settings_sha256": sha,
            "settings_version": current["version"],
        }
        await execute(
            conn,
            """INSERT INTO ag_retention_plan(id,created_by,proposed_settings,settings_version,settings_sha256,cutoff,expires_at,scheduled)
            VALUES(:id,:actor,CAST(:value AS jsonb),:version,:sha,now(),now()+interval '24 hours',:scheduled)""",
            id=plan_id,
            actor=actor,
            value=encoded(value),
            version=current["version"],
            sha=sha,
            scheduled=scheduled,
        )
        job = await self.jobs.enqueue_in(
            conn, JobType.RETENTION_PREVIEW, f"retention-preview:{plan_id}", parameters, actor
        )
        await execute(
            conn, "UPDATE ag_retention_plan SET preview_job_id=:job WHERE id=:id", id=plan_id, job=job["id"]
        )
        return job_view(job)

    async def execute(self, principal, key, preview_id, expected_version):
        require_permission(principal, "agent:retention:execute")
        body = {"preview_job_id": str(preview_id), "expected_settings_version": expected_version}
        async with self.db.transaction() as conn:
            cached = await staff_dedupe(conn, principal, "executeRetention", key, body)
            if cached:
                return cached
            plan = await one(
                conn,
                "SELECT * FROM ag_retention_plan WHERE preview_job_id=:id AND created_by=:actor FOR UPDATE",
                id=preview_id,
                actor=principal.id,
            )
            if not plan:
                raise DomainError("NOT_FOUND", "未找到本人完成的留存试算", 404)
            result = await self.enqueue_execution(conn, plan, expected_version)
            await self.receipt(conn, principal, "executeRetention", key, body, result)
            return result

    async def enqueue_execution(self, conn, plan, expected_version):
        current = await read_policy(conn)
        preview = await one(conn, "SELECT state FROM ag_job WHERE id=:id", id=plan["preview_job_id"])
        if not preview or preview["state"] != "SUCCEEDED" or plan["expires_at"] <= datetime.now(UTC):
            raise DomainError("RETENTION_PREVIEW_EXPIRED", "须先完成24小时内有效的影响试算", 409)
        if current["version"] != expected_version or digest(current["value"]) != plan["settings_sha256"]:
            raise DomainError("SETTINGS_CHANGED", "请先保存与试算一致的设置，再按当前版本执行", 409)
        parameters = {
            "kind": "RETENTION_POLICY",
            "plan_id": str(plan["id"]),
            "settings_sha256": plan["settings_sha256"],
            "settings_version": expected_version,
        }
        job = await self.jobs.enqueue_in(
            conn, JobType.RETENTION_EXECUTE, f"retention-execute:{plan['id']}", parameters, plan["created_by"]
        )
        await execute(
            conn,
            "UPDATE ag_retention_plan SET execution_job_id=:job WHERE id=:id",
            id=plan["id"],
            job=job["id"],
        )
        return job_view(job)

    async def schedule(self):
        async with self.db.transaction() as conn:
            leader = await one(
                conn, "SELECT pg_try_advisory_xact_lock(hashtextextended('retention-schedule',0)) held"
            )
            if not leader["held"]:
                return
            current = await read_policy(conn)
            old = await one(
                conn,
                "SELECT id FROM ag_retention_plan WHERE scheduled AND created_at>=date_trunc('day',now()) LIMIT 1",
            )
            if not old:
                await self.create_preview(conn, current["value"], scheduled=True)
            plans = await rows(
                conn,
                """SELECT p.* FROM ag_retention_plan p JOIN ag_job j ON j.id=p.preview_job_id
                WHERE p.scheduled AND p.execution_job_id IS NULL AND p.expires_at>now() AND j.state='SUCCEEDED'
                AND p.settings_sha256=:sha ORDER BY p.created_at LIMIT 1 FOR UPDATE OF p""",
                sha=digest(current["value"]),
            )
            for plan in plans:
                await self.enqueue_execution(conn, plan, current["version"])

    async def preview_job(self, lease):
        args = validate_plan(lease.parameters)
        async with self.db.transaction(snapshot=True) as conn:
            await locked_job(conn, lease)
            plan = await one(conn, "SELECT * FROM ag_retention_plan WHERE id=:id", id=args["plan_id"])
            if (
                not plan
                or plan["preview_job_id"] != lease.id
                or plan["settings_sha256"] != args["settings_sha256"]
            ):
                raise DomainError("RETENTION_PLAN_INVALID", "留存试算绑定不匹配", 409)
            policy = PublicPolicy.model_validate(plan["proposed_settings"])
            cutoff = plan["cutoff"]
            messages = await one(
                conn,
                """SELECT count(*) count,count(DISTINCT conversation_id) conversations FROM ag_message
                WHERE deleted_at IS NULL AND LEAST(retention_until,created_at+make_interval(days=>:days))<=:cutoff""",
                days=policy.message_retention_days,
                cutoff=cutoff,
            )
            notes = await one(
                conn,
                "SELECT count(*) count FROM ag_staff_note WHERE body<>'已到期' AND LEAST(retention_until,created_at+make_interval(days=>:days))<=:cutoff",
                days=policy.message_retention_days,
                cutoff=cutoff,
            )
            files = await one(
                conn,
                """SELECT count(*) count,count(*) FILTER(WHERE EXISTS(SELECT 1 FROM ag_attachment_ref r WHERE r.attachment_id=a.id AND (r.protected OR r.released_at IS NULL))) protected
                FROM ag_attachment a WHERE a.state IN ('READY','FAILED','UPLOADING') AND a.created_at<=:cutoff
                AND (LEAST(a.retention_until,a.created_at+make_interval(days=>CASE a.purpose WHEN 'RECORDING' THEN :audio WHEN 'SPEECH' THEN :tts ELSE :image END))<=:cutoff
                    OR (a.created_at<:cutoff-interval '1 day' AND (a.state IN ('FAILED','UPLOADING') OR (a.purpose='KNOWLEDGE' AND NOT EXISTS(SELECT 1 FROM ag_attachment_ref r WHERE r.attachment_id=a.id AND r.released_at IS NULL)))))""",
                cutoff=cutoff,
                audio=policy.audio_retention_days,
                tts=policy.tts_retention_days,
                image=policy.image_retention_days,
            )
            summary = {
                "plan_id": str(plan["id"]),
                "cutoff": cutoff,
                "settings_version": plan["settings_version"],
                "settings_sha256": plan["settings_sha256"],
                "messages": messages["count"],
                "conversations": messages["conversations"],
                "notes": notes["count"],
                "attachments": files["count"],
                "protected_attachments": files["protected"],
                "execution_rechecks_protection": True,
            }
            await execute(
                conn,
                "UPDATE ag_retention_plan SET summary=CAST(:summary AS jsonb) WHERE id=:id",
                id=plan["id"],
                summary=encoded(summary),
            )
        return summary

    async def execute_job(self, lease):
        args = validate_plan(lease.parameters)
        while True:
            async with self.db.transaction() as conn:
                await locked_job(conn, lease)
                plan = await one(
                    conn, "SELECT * FROM ag_retention_plan WHERE id=:id FOR UPDATE", id=args["plan_id"]
                )
                current = await read_policy(conn, lock=True)
                if (
                    not plan
                    or plan["execution_job_id"] != lease.id
                    or current["version"] != args["settings_version"]
                    or digest(current["value"]) != args["settings_sha256"]
                ):
                    raise DomainError("SETTINGS_CHANGED", "留存计划或设置已变化，请重新试算", 409)
                policy = PublicPolicy.model_validate(plan["proposed_settings"])
                progress = plan["progress"] or {
                    "messages": 0,
                    "conversations": 0,
                    "notes": 0,
                    "attachments_scheduled": 0,
                }
                conversation = await one(
                    conn,
                    """SELECT conversation_id FROM ag_message WHERE deleted_at IS NULL
                    AND LEAST(retention_until,created_at+make_interval(days=>:days))<=:cutoff
                    ORDER BY conversation_id,seq LIMIT 1""",
                    days=policy.message_retention_days,
                    cutoff=plan["cutoff"],
                )
                if conversation:
                    amount = await self.erase_messages(
                        conn, conversation["conversation_id"], policy, plan["cutoff"]
                    )
                    progress["messages"] += amount
                    if progress.get("last_conversation") != str(conversation["conversation_id"]):
                        progress["conversations"] += 1
                    progress["last_conversation"] = str(conversation["conversation_id"])
                else:
                    notes = await rows(
                        conn,
                        """UPDATE ag_staff_note SET body='已到期'
                        WHERE id IN (SELECT id FROM ag_staff_note WHERE body<>'已到期' AND LEAST(retention_until,created_at+make_interval(days=>:days))<=:cutoff ORDER BY id LIMIT 100 FOR UPDATE) RETURNING id""",
                        days=policy.message_retention_days,
                        cutoff=plan["cutoff"],
                    )
                    for note in notes:
                        await execute(
                            conn,
                            "INSERT INTO ag_erasure_tombstone(resource_type,resource_id) VALUES('NOTE',:id) ON CONFLICT DO NOTHING",
                            id=str(note["id"]),
                        )
                    if notes:
                        await execute(
                            conn,
                            "UPDATE ag_request_dedupe SET response_ref=CAST(:marker AS jsonb) WHERE scope LIKE 'createStaffNote:%' AND response_ref->>'id'=ANY(:ids)",
                            marker=encoded({"resource_expired": True}),
                            ids=[str(n["id"]) for n in notes],
                        )
                    progress["notes"] += len(notes)
                    attachments = await self.sweep_attachments(conn, policy, plan["cutoff"], progress)
                    progress["attachments_scheduled"] += attachments
                await execute(
                    conn,
                    "UPDATE ag_retention_plan SET progress=CAST(:progress AS jsonb) WHERE id=:id",
                    id=plan["id"],
                    progress=encoded(progress),
                )
                total = max(1, plan["summary"].get("messages", 1))
                await execute(
                    conn,
                    "UPDATE ag_job SET progress=:progress,updated_at=now() WHERE id=:id",
                    id=lease.id,
                    progress=min(99, progress["messages"] * 100 // total),
                )
                if not conversation and not notes and not attachments:
                    await self.purge_event_prefix(conn, policy, plan["cutoff"])
                    return {k: v for k, v in progress.items() if k != "last_conversation"} | {
                        "plan_id": str(plan["id"]),
                        "cutoff": plan["cutoff"],
                        "settings_version": current["version"],
                    }
            await asyncio.sleep(0)

    async def erase_messages(self, conn, cid, policy, cutoff):
        conv = await one(conn, "SELECT * FROM ag_conversation WHERE id=:id FOR UPDATE", id=cid)
        expired = await rows(
            conn,
            """SELECT id FROM ag_message WHERE conversation_id=:cid AND deleted_at IS NULL
            AND LEAST(retention_until,created_at+make_interval(days=>:days))<=:cutoff ORDER BY seq LIMIT 200 FOR UPDATE""",
            cid=cid,
            days=policy.message_retention_days,
            cutoff=cutoff,
        )
        if not expired:
            return 0
        ids = [r["id"] for r in expired]
        conv = await one(
            conn,
            "UPDATE ag_conversation SET memory_epoch=memory_epoch+1,control_epoch=control_epoch+1,version=version+1,updated_at=now() WHERE id=:id RETURNING *",
            id=cid,
        )
        stopped = await rows(
            conn,
            """UPDATE ag_run SET state='INTERRUPTED',error_code='CONTEXT_EXPIRED',fencing_token=fencing_token+1,
            lease_owner=NULL,lease_until=NULL,finished_at=now(),version=version+1,updated_at=now()
            WHERE conversation_id=:cid AND state IN ('QUEUED','RUNNING','STOP_REQUESTED') RETURNING id""",
            cid=cid,
        )
        for run in stopped:
            await execute(
                conn,
                "UPDATE ag_generation_slot SET run_id=NULL,lease_until=CASE WHEN :hold THEN now()+make_interval(secs=>:seconds) ELSE NULL END WHERE run_id=:id",
                id=run["id"],
                hold=self.settings.provider != "fixture",
                seconds=self.settings.model_timeout_seconds + 5,
            )
        await execute(
            conn,
            "UPDATE ag_message SET status='INTERRUPTED',updated_at=now() WHERE conversation_id=:cid AND status='GENERATING'",
            cid=cid,
        )
        await execute(
            conn,
            "UPDATE ag_action SET state='STALE',version=version+1,updated_at=now() WHERE conversation_id=:cid AND state='PENDING_CONFIRMATION'",
            cid=cid,
        )
        await execute(conn, "DELETE FROM ag_context_summary WHERE conversation_id=:cid", cid=cid)
        # A delayed fenced sweep follows the maximum lifetime of an old worker,
        # preventing a late checkpointer write from resurrecting expired context.
        await execute(
            conn,
            """INSERT INTO ag_erasure_tombstone(resource_type,resource_id,memory_epoch)
            VALUES('CONTEXT',:id,:epoch) ON CONFLICT(resource_type,resource_id)
            DO UPDATE SET memory_epoch=EXCLUDED.memory_epoch,erased_at=now()""",
            id=str(cid),
            epoch=conv["memory_epoch"],
        )
        cleanup = await self.jobs.enqueue_in(
            conn,
            JobType.RETENTION_EXECUTE,
            f"context-expiry:{cid}:{conv['memory_epoch']}",
            {"kind": "CONTEXT_EXPIRY", "conversation_id": str(cid), "memory_epoch": conv["memory_epoch"]},
        )
        if self.settings.provider != "fixture":
            await execute(
                conn,
                "UPDATE ag_job SET next_attempt_at=now()+make_interval(secs=>:seconds) WHERE id=:id",
                id=cleanup["id"],
                seconds=self.settings.run_timeout_seconds + self.settings.run_lease_seconds + 10,
            )
        await execute(
            conn,
            "DELETE FROM ag_image_observation WHERE run_id IN(SELECT id FROM ag_run WHERE conversation_id=:cid)",
            cid=cid,
        )
        await execute(conn, "DELETE FROM ag_kb_citation WHERE message_id=ANY(:ids)", ids=ids)
        await execute(conn, "DELETE FROM ag_feedback WHERE message_id=ANY(:ids)", ids=ids)
        await execute(
            conn,
            """UPDATE ag_request_dedupe SET response_ref=CAST(:marker AS jsonb)
            WHERE scope LIKE 'submitFeedback:%' AND response_ref->>'message_id'=ANY(:ids)""",
            ids=[str(mid) for mid in ids],
            marker=encoded({"resource_expired": True}),
        )
        await execute(
            conn,
            "UPDATE ag_attachment_ref SET released_at=now(),updated_at=now(),version=version+1 WHERE message_id=ANY(:ids) AND NOT protected AND released_at IS NULL",
            ids=ids,
        )
        await execute(
            conn,
            "UPDATE ag_message SET text_content='',blocks='[]'::jsonb,sender_display_name=NULL,deleted_at=now(),updated_at=now() WHERE id=ANY(:ids)",
            ids=ids,
        )
        for mid in ids:
            await execute(
                conn,
                "INSERT INTO ag_erasure_tombstone(resource_type,resource_id,memory_epoch) VALUES('MESSAGE',:id,:epoch) ON CONFLICT DO NOTHING",
                id=str(mid),
                epoch=conv["memory_epoch"],
            )
        # Remove copies from durable replay/idempotent responses. Business action ledgers stay intact.
        await execute(conn, "DELETE FROM ag_event_outbox WHERE conversation_id=:cid", cid=cid)
        await execute(
            conn,
            """UPDATE ag_request_dedupe SET response_ref=CAST(:marker AS jsonb)
            WHERE scope LIKE :suffix AND (response_ref->'message'->>'id'=ANY(:ids) OR response_ref->>'id'=ANY(:ids))""",
            suffix=f"%:{cid}",
            marker=encoded({"resource_expired": True}),
            ids=[str(mid) for mid in ids],
        )
        if not conv["deleted_at"]:
            from app.application.views import conversation_projection

            await append_events(
                conn,
                conv,
                [("conversation.updated", {"conversation": await conversation_projection(conn, conv)})],
            )
        return len(ids)

    async def sweep_attachments(self, conn, policy, cutoff, progress=None):
        await execute(
            conn,
            "UPDATE ag_media_request SET state='UNKNOWN',error_code='MEDIA_REQUEST_INTERRUPTED' WHERE state='RUNNING' AND lease_until<=:cutoff",
            cutoff=cutoff,
        )
        # Audio retention is independent of the longer lifetime of its source message.
        await execute(
            conn,
            """UPDATE ag_attachment_ref r SET released_at=now(),updated_at=now(),version=r.version+1
            FROM ag_attachment a WHERE r.attachment_id=a.id AND r.ref_kind='SPEECH' AND NOT r.protected
            AND r.released_at IS NULL AND LEAST(a.retention_until,a.created_at+make_interval(days=>:days))<=:cutoff""",
            days=policy.tts_retention_days,
            cutoff=cutoff,
        )
        await execute(
            conn,
            """UPDATE ag_media_request r SET result=NULL,
            state=CASE WHEN state='RUNNING' THEN 'UNKNOWN' ELSE state END
            WHERE result IS NOT NULL AND (retention_until<=:cutoff OR
                EXISTS(SELECT 1 FROM ag_attachment a WHERE a.id=r.attachment_id AND
                    (a.state IN ('DELETING','DELETED') OR LEAST(a.retention_until,a.created_at+make_interval(days=>:audio))<=:cutoff)) OR
                EXISTS(SELECT 1 FROM ag_message m JOIN ag_conversation c ON c.id=m.conversation_id
                    WHERE m.id=r.message_id AND (m.deleted_at IS NOT NULL OR c.deleted_at IS NOT NULL)))""",
            cutoff=cutoff,
            audio=policy.audio_retention_days,
        )
        data = await rows(
            conn,
            """SELECT a.* FROM ag_attachment a WHERE state IN ('UPLOADING','FAILED','READY') AND created_at<=:cutoff
            AND (state<>'UPLOADING' OR updated_at<:cutoff-interval '10 minutes')
            AND NOT EXISTS(SELECT 1 FROM ag_attachment_ref r WHERE r.attachment_id=a.id AND (r.protected OR r.released_at IS NULL))
            AND (LEAST(retention_until,created_at+make_interval(days=>CASE purpose WHEN 'RECORDING' THEN :audio WHEN 'SPEECH' THEN :tts ELSE :image END))<=:cutoff
                OR (created_at<:cutoff-interval '1 day' AND (state IN ('UPLOADING','FAILED') OR purpose='KNOWLEDGE')))
            ORDER BY id LIMIT 100 FOR UPDATE""",
            cutoff=cutoff,
            audio=policy.audio_retention_days,
            tts=policy.tts_retention_days,
            image=policy.image_retention_days,
        )
        # The scan's READ COMMITTED snapshot may predate a proof transaction
        # that held an attachment lock. A subsequent statement sees its commit;
        # new binders also require these attachment locks. Batch the recheck.
        referenced = (
            {
                ref["attachment_id"]
                for ref in await rows(
                    conn,
                    "SELECT DISTINCT attachment_id FROM ag_attachment_ref WHERE attachment_id=ANY(CAST(:ids AS uuid[])) AND (protected OR released_at IS NULL)",
                    ids=[item["id"] for item in data],
                )
            }
            if data
            else set()
        )
        scheduled = 0
        if progress is not None:
            # Only candidates whose newly committed references were invisible
            # to the first scan count here; already-protected rows never qualify.
            progress["protected_attachments"] = progress.get("protected_attachments", 0) + len(referenced)
        for item in data:
            if item["id"] in referenced:
                continue
            await execute(
                conn,
                "UPDATE ag_attachment SET state='DELETING',version=version+1,updated_at=now() WHERE id=:id",
                id=item["id"],
            )
            await execute(
                conn,
                "INSERT INTO ag_erasure_tombstone(resource_type,resource_id) VALUES('ATTACHMENT',:id) ON CONFLICT DO NOTHING",
                id=str(item["id"]),
            )
            await self.jobs.enqueue_in(
                conn,
                JobType.ATTACHMENT_DELETE,
                f"attachment-delete:{item['id']}",
                {"attachment_id": str(item["id"])},
            )
            scheduled += 1
        return scheduled

    async def purge_event_prefix(self, conn, policy, cutoff):
        # Last lock, same order as append_events; only a contiguous prefix advances the floor.
        clock = await one(conn, "SELECT * FROM ag_event_clock WHERE singleton_id=1 FOR UPDATE")
        first = await one(
            conn,
            "SELECT min(event_id) id FROM ag_event_outbox WHERE LEAST(retention_until,created_at+make_interval(days=>:days))>:cutoff",
            days=policy.event_retention_days,
            cutoff=cutoff,
        )
        floor = (first["id"] - 1) if first["id"] is not None else clock["last_event_id"]
        floor = max(clock["replay_floor"], floor)
        await execute(conn, "DELETE FROM ag_event_outbox WHERE event_id<=:floor", floor=floor)
        await execute(
            conn,
            "UPDATE ag_event_clock SET replay_floor=:floor,updated_at=now(),version=version+1 WHERE singleton_id=1",
            floor=floor,
        )


def register_retention(registry, db, settings):
    service = Retention(db, settings, Jobs(db, settings, registry))
    registry.register(
        JobType.RETENTION_PREVIEW,
        service.preview_job,
        permission="agent:retention:preview",
        validator=validate_plan,
        timeout=180,
    )
    return service
