from uuid import uuid4

from app.application.context_memory import earlier_member_context
from app.application.conversations import ACTIVE, dedupe, owned, save_dedupe
from app.application.expiry import assert_message_visible, deadline_sql, visible_sql
from app.application.governance import read_policy
from app.application.views import message_view, run_view
from app.domain.identity import DomainError
from app.infrastructure.postgres.database import append_events, encoded, execute, one, rows


class Runs:
    def __init__(self, database, settings):
        self.db, self.settings = database, settings

    async def get(self, principal, rid):
        async with self.db.transaction() as conn:
            row = await one(conn, "SELECT * FROM ag_run WHERE id=:id", id=rid)
            if not row:
                raise DomainError("NOT_FOUND", "生成任务不存在", 404)
            await owned(conn, row["conversation_id"], principal.id)
            return run_view(row)

    async def stop(self, principal, rid, key, body):
        async with self.db.transaction() as conn:
            cached = await dedupe(conn, f"stopRun:{rid}", principal.id, key, body)
            raw = await one(conn, "SELECT conversation_id FROM ag_run WHERE id=:id", id=rid)
            if not raw:
                raise DomainError("NOT_FOUND", "生成任务不存在", 404)
            conv = await owned(conn, raw["conversation_id"], principal.id, lock=True)
            if cached:
                return cached
            run = await one(conn, "SELECT * FROM ag_run WHERE id=:id FOR UPDATE", id=rid)
            if run["version"] != body["expected_version"]:
                raise DomainError("VERSION_CONFLICT", "任务状态已变化，请刷新")
            events = []
            if run["state"] in {"QUEUED", "RUNNING"}:
                queued = run["state"] == "QUEUED"
                run = await one(
                    conn,
                    """UPDATE ag_run SET state=:state,version=version+1,updated_at=now(),
                    finished_at=CASE WHEN :queued THEN now() ELSE finished_at END
                    WHERE id=:id RETURNING *""",
                    id=rid,
                    state="STOPPED" if queued else "STOP_REQUESTED",
                    queued=queued,
                )
                conv = await one(
                    conn,
                    """UPDATE ag_conversation SET control_epoch=control_epoch+1,
                    version=version+1,updated_at=now() WHERE id=:id RETURNING *""",
                    id=conv["id"],
                )
                stale = await rows(
                    conn,
                    "UPDATE ag_action SET state='STALE',version=version+1,updated_at=now() WHERE conversation_id=:id AND state='PENDING_CONFIRMATION' RETURNING *",
                    id=conv["id"],
                )
                from app.application.actions import action_view

                events.extend(("action.updated", {"action": action_view(action)}) for action in stale)
                if queued:
                    events.append(("run.stopped", {"run_id": str(rid), "reason": "USER"}))
            result = run_view(run)
            await save_dedupe(conn, f"stopRun:{rid}", principal.id, key, body, result)
            await append_events(conn, conv, events)
            return result

    async def claim(self, owner):
        async with self.db.transaction() as conn:
            candidates = await rows(
                conn,
                "SELECT id,conversation_id FROM ag_run WHERE state='QUEUED' ORDER BY created_at,id LIMIT 20",
            )
            for candidate in candidates:
                # Global order: conversation -> run -> slot -> message -> event clock.
                conv = await one(
                    conn,
                    "SELECT * FROM ag_conversation WHERE id=:id FOR UPDATE SKIP LOCKED",
                    id=candidate["conversation_id"],
                )
                if not conv:
                    continue
                run = await one(conn, "SELECT * FROM ag_run WHERE id=:id FOR UPDATE", id=candidate["id"])
                if run["state"] != "QUEUED":
                    continue
                try:
                    await assert_message_visible(conn, run["trigger_message_id"])
                except DomainError:
                    await execute(
                        conn,
                        "UPDATE ag_run SET state='INTERRUPTED',error_code='MESSAGE_EXPIRED',finished_at=now(),version=version+1 WHERE id=:id",
                        id=run["id"],
                    )
                    continue
                if (
                    conv["deleted_at"]
                    or conv["control_epoch"] != run["control_epoch"]
                    or not (
                        conv["state"] == "AI_ACTIVE"
                        or (conv["state"] == "WAITING_HUMAN" and conv["waiting_ai_enabled"])
                    )
                ):
                    await execute(
                        conn,
                        "UPDATE ag_run SET state='STOPPED',finished_at=now(),version=version+1 WHERE id=:id",
                        id=run["id"],
                    )
                    continue
                slot = await one(
                    conn,
                    "SELECT * FROM ag_generation_slot WHERE run_id IS NULL "
                    "AND (lease_until IS NULL OR lease_until<=now()) "
                    "ORDER BY slot_no FOR UPDATE SKIP LOCKED LIMIT 1",
                )
                if not slot:
                    return None
                run = await one(
                    conn,
                    """UPDATE ag_run SET state='RUNNING',lease_owner=:owner,
                    lease_until=now()+make_interval(secs=>:seconds),fencing_token=fencing_token+1,
                    started_at=now(),version=version+1 WHERE id=:id RETURNING *""",
                    owner=owner,
                    seconds=self.settings.run_lease_seconds,
                    id=run["id"],
                )
                await execute(
                    conn,
                    """UPDATE ag_generation_slot SET run_id=:run,
                    lease_until=:until+make_interval(secs=>:grace),fencing_token=fencing_token+1 WHERE slot_no=:slot""",
                    run=run["id"],
                    until=run["lease_until"],
                    grace=self.settings.model_timeout_seconds + 5
                    if self.settings.provider != "fixture"
                    else 0,
                    slot=slot["slot_no"],
                )
                assistant = await one(
                    conn,
                    """INSERT INTO ag_message
                    (id,conversation_id,seq,sender_kind,sender_id,status,run_id,retention_until)
                    VALUES (:id,:cid,:seq,'ASSISTANT',0,'GENERATING',:run,now()+make_interval(days=>:days)) RETURNING *""",
                    days=(await read_policy(conn))["value"]["message_retention_days"],
                    id=uuid4(),
                    cid=conv["id"],
                    seq=conv["next_message_seq"],
                    run=run["id"],
                )
                conv = await one(
                    conn,
                    """UPDATE ag_conversation SET next_message_seq=next_message_seq+1,
                    version=version+1,updated_at=now() WHERE id=:id RETURNING *""",
                    id=conv["id"],
                )
                question = await one(
                    conn,
                    "SELECT text_content,blocks FROM ag_message WHERE id=:id",
                    id=run["trigger_message_id"],
                )
                await append_events(
                    conn,
                    conv,
                    [
                        ("run.started", {"run_id": str(run["id"])}),
                        (
                            "message.accepted",
                            {
                                "message_id": str(assistant["id"]),
                                "seq": str(assistant["seq"]),
                                "client_message_id": None,
                            },
                        ),
                    ],
                )
                return dict(run) | {
                    "entry_context": conv["entry_context"],
                    "member_id": conv["member_id"],
                    "assistant_id": assistant["id"],
                    "question": question["text_content"],
                    "has_images": any(block["type"] == "attachment" for block in question["blocks"]),
                    "image_count": sum(block["type"] == "attachment" for block in question["blocks"]),
                }
            return None

    async def append(self, claimed, chunk, source_refs=()):
        async with self.db.transaction() as conn:
            conv, run = await self._lock(conn, claimed)
            if not self._valid(conv, run, claimed):
                return False
            # Serialize the visible delta with publication/withdrawal. A prior
            # process-level recheck cannot close the gap before this commit.
            from app.application.retrieval import current_source

            citations = await rows(
                conn,
                "SELECT * FROM ag_kb_citation WHERE message_id=:id ORDER BY document_id,chunk_id",
                id=claimed["assistant_id"],
            )
            for citation in citations:
                ref = {
                    key: str(citation[key])
                    for key in ("document_id", "version_id", "chunk_id", "content_sha256")
                }
                supplied = next(
                    (
                        source
                        for source in source_refs
                        if all(
                            str(source.get(key)) == ref[key]
                            for key in ("document_id", "version_id", "chunk_id", "content_sha256")
                        )
                    ),
                    None,
                )
                if supplied and supplied.get("scope_context"):
                    scope = supplied["scope_context"]
                    if conv["entry_context"].get("kind") != "PRODUCT" or conv["entry_context"].get(
                        "product_id"
                    ) != scope.get("product_id"):
                        raise DomainError("SOURCE_UNAVAILABLE", "来源不属于当前商品", 410)
                    ref["scope_context"] = scope
                if not await current_source(conn, ref, lock=True):
                    # Scoped references are stored separately below; inability to
                    # prove a source always blocks this delta.
                    raise DomainError("SOURCE_UNAVAILABLE", "展示前知识来源已失效", 410)
            msg = await one(
                conn, "SELECT * FROM ag_message WHERE id=:id FOR UPDATE", id=claimed["assistant_id"]
            )
            offset = len(msg["text_content"])
            await execute(
                conn,
                "UPDATE ag_message SET text_content=text_content||:chunk,updated_at=now() WHERE id=:id",
                chunk=chunk,
                id=msg["id"],
            )
            await execute(
                conn,
                "UPDATE ag_run SET lease_until=now()+make_interval(secs=>:seconds) WHERE id=:id",
                id=run["id"],
                seconds=self.settings.run_lease_seconds,
            )
            await append_events(
                conn,
                conv,
                [
                    (
                        "message.delta",
                        {
                            "message_id": str(msg["id"]),
                            "run_id": str(run["id"]),
                            "offset": offset,
                            "text": chunk,
                        },
                    )
                ],
            )
            return True

    async def heartbeat(self, claimed):
        async with self.db.transaction() as conn:
            conv, run = await self._lock(conn, claimed)
            if not self._valid(conv, run, claimed):
                return False
            await execute(
                conn,
                "UPDATE ag_run SET lease_until=now()+make_interval(secs=>:secs) WHERE id=:id",
                id=run["id"],
                secs=self.settings.run_lease_seconds,
            )
            await execute(
                conn,
                "UPDATE ag_generation_slot SET lease_until=now()+make_interval(secs=>:secs) WHERE run_id=:id",
                id=run["id"],
                secs=self.settings.model_timeout_seconds + 5,
            )
            return True

    async def append_products(self, claimed, answer, products):
        return await self.append_business_blocks(
            claimed, answer, [{"type": "product", "product": product} for product in products]
        )

    async def append_orders(self, claimed, answer, orders):
        return await self.append_business_blocks(
            claimed, answer, [{"type": "order", "order": order} for order in orders]
        )

    async def append_business_blocks(self, claimed, answer, blocks):
        async with self.db.transaction() as conn:
            conv, run = await self._lock(conn, claimed)
            if not self._valid(conv, run, claimed):
                raise DomainError("LEASE_LOST", "运行任务已失效")
            message = await one(
                conn, "SELECT * FROM ag_message WHERE id=:id FOR UPDATE", id=claimed["assistant_id"]
            )
            if message["text_content"]:
                if message["text_content"] == answer and (
                    [block for block in message["blocks"] if block.get("type") != "source"] == blocks
                    or not message["blocks"]
                    or all(block.get("type") == "source" for block in message["blocks"])
                ):
                    if message["blocks"] != blocks:
                        blocks = [b for b in message["blocks"] if b.get("type") == "source"] + blocks
                        await execute(
                            conn,
                            "UPDATE ag_message SET blocks=CAST(:blocks AS jsonb) WHERE id=:id",
                            blocks=encoded(blocks),
                            id=message["id"],
                        )
                    return
                raise DomainError("WORKFLOW_PROTOCOL_ERROR", "商品准备卡已经写入")
            await execute(
                conn,
                "UPDATE ag_message SET text_content=:text,blocks=CAST(:blocks AS jsonb),updated_at=now() WHERE id=:id",
                id=message["id"],
                text=answer,
                blocks=encoded(blocks),
            )
            await append_events(
                conn,
                conv,
                [
                    (
                        "message.delta",
                        {
                            "message_id": str(message["id"]),
                            "run_id": str(run["id"]),
                            "offset": 0,
                            "text": answer,
                        },
                    )
                ],
            )

    async def append_knowledge(self, claimed, answer, refs):
        from app.application.retrieval import current_source, source_view

        async with self.db.transaction() as conn:
            conv, run = await self._lock(conn, claimed)
            if not self._valid(conv, run, claimed):
                raise DomainError("LEASE_LOST", "运行任务已失效", 409)
            sources = []
            # Sorted shared locks serialize withdrawal/publication with this complete output.
            for ref in sorted(refs, key=lambda r: (r["document_id"], r["chunk_id"])):
                context = ref.get("scope_context")
                if context and (
                    conv["entry_context"].get("kind") != "PRODUCT"
                    or conv["entry_context"].get("product_id") != context.get("product_id")
                ):
                    raise DomainError("SOURCE_UNAVAILABLE", "知识来源不属于当前商品上下文", 410)
                row = await current_source(conn, ref, lock=True)
                if not row:
                    raise DomainError("SOURCE_UNAVAILABLE", "知识来源已撤回或更新，请重新提问", 410)
                sources.append((ref, source_view(row)))
            msg = await one(
                conn, "SELECT * FROM ag_message WHERE id=:id FOR UPDATE", id=claimed["assistant_id"]
            )
            if msg["text_content"] and msg["text_content"] != answer:
                # Durable node replay cannot append a second copy after a checkpoint interruption.
                raise DomainError("WORKFLOW_PROTOCOL_ERROR", "知识回复已经写入", 409)
            already_streamed = bool(msg["text_content"])
            blocks = [block for block in msg["blocks"] if block.get("type") != "source"] + [
                {"type": "source", "source": source} for _, source in sources
            ]
            await execute(
                conn,
                "UPDATE ag_message SET text_content=:text,blocks=CAST(:blocks AS jsonb),updated_at=now() WHERE id=:id",
                text=answer,
                blocks=encoded(blocks),
                id=msg["id"],
            )
            for ref, _ in sources:
                await execute(
                    conn,
                    """INSERT INTO ag_kb_citation(message_id,document_id,version_id,chunk_id,content_sha256)
                    VALUES (:message,:document,:version,:chunk,:sha) ON CONFLICT DO NOTHING""",
                    message=msg["id"],
                    document=ref["document_id"],
                    version=ref["version_id"],
                    chunk=ref["chunk_id"],
                    sha=ref["content_sha256"],
                )
            if not already_streamed:
                await append_events(
                    conn,
                    conv,
                    [
                        (
                            "message.delta",
                            {
                                "message_id": str(msg["id"]),
                                "run_id": str(run["id"]),
                                "offset": 0,
                                "text": answer,
                            },
                        )
                    ],
                )

    async def bind_stream_sources(self, claimed, refs):
        """Bind current citations before their text enters durable SSE.

        STOPPED/FAILED/INTERRUPTED messages then retain the source blocks for
        text already shown, even when N12 never reaches final append_knowledge.
        """
        from app.application.retrieval import current_source, source_view

        async with self.db.transaction() as conn:
            conv, run = await self._lock(conn, claimed)
            if not self._valid(conv, run, claimed):
                raise DomainError("LEASE_LOST", "运行任务已失效", 409)
            blocks = []
            for ref in sorted(refs, key=lambda r: (r["document_id"], r["chunk_id"])):
                context = ref.get("scope_context")
                if context and (
                    conv["entry_context"].get("kind") != "PRODUCT"
                    or conv["entry_context"].get("product_id") != context.get("product_id")
                ):
                    raise DomainError("SOURCE_UNAVAILABLE", "来源不属于本轮商品上下文", 410)
                row = await current_source(conn, ref, lock=True)
                if not row:
                    raise DomainError("SOURCE_UNAVAILABLE", "知识来源已撤回或更新", 410)
                blocks.append({"type": "source", "source": source_view(row)})
                await execute(
                    conn,
                    """INSERT INTO ag_kb_citation(message_id,document_id,version_id,chunk_id,content_sha256)
                    VALUES(:message,:document,:version,:chunk,:sha) ON CONFLICT DO NOTHING""",
                    message=claimed["assistant_id"],
                    document=ref["document_id"],
                    version=ref["version_id"],
                    chunk=ref["chunk_id"],
                    sha=ref["content_sha256"],
                )
            message = await one(
                conn, "SELECT blocks FROM ag_message WHERE id=:id FOR UPDATE", id=claimed["assistant_id"]
            )
            blocks = [block for block in message["blocks"] if block.get("type") != "source"] + blocks
            await execute(
                conn,
                "UPDATE ag_message SET blocks=CAST(:blocks AS jsonb),updated_at=now() WHERE id=:id",
                blocks=encoded(blocks),
                id=claimed["assistant_id"],
            )

    async def history(self, claimed):
        async with self.db.transaction(snapshot=True) as conn:
            conversation = await owned(conn, claimed["conversation_id"], claimed["member_id"], lock=True)
            if conversation["memory_epoch"] != claimed["memory_epoch"]:
                raise DomainError("LEASE_LOST", "会话记忆已失效", 409)
            await assert_message_visible(conn, claimed["trigger_message_id"])
            history = await rows(
                conn,
                f"""SELECT m.seq,m.sender_kind,m.text_content,
                    COALESCE((SELECT string_agg('第' || b.position::text || '张图片：' || o.summary,
                        E'\\n' ORDER BY b.position) FROM ag_image_observation o
                        JOIN ag_run r ON r.id=o.run_id JOIN ag_attachment a ON a.id=o.attachment_id
                        JOIN LATERAL jsonb_array_elements(m.blocks) WITH ORDINALITY AS b(block,position)
                            ON b.block->>'type'='attachment' AND b.block->'attachment'->>'id'=a.id::text
                        WHERE r.trigger_message_id=m.id AND r.state='COMPLETED'
                        AND a.state='READY' AND {deadline_sql("a", "image_retention_days")}>now()),'') AS image_summary
                FROM ag_message m
                WHERE m.conversation_id=:id AND {visible_sql()} AND m.status='COMPLETED'
                AND m.seq<(SELECT seq FROM ag_message WHERE id=:trigger)
                AND NOT (m.sender_kind='ASSISTANT' AND EXISTS (SELECT 1 FROM ag_kb_citation k WHERE k.message_id=m.id))
                AND m.sender_kind IN ('MEMBER','ASSISTANT') ORDER BY m.seq DESC LIMIT 12""",
                id=claimed["conversation_id"],
                trigger=claimed["trigger_message_id"],
            )
            summary = (
                await earlier_member_context(conn, conversation, min(item["seq"] for item in history))
                if history
                else ""
            )
            result, remaining = [], 12000 - len(summary)
            for item in history:
                text = item["text_content"]
                if item["image_summary"]:
                    text += "\n[此前图片观察，仅为线索，非商城事实] " + item["image_summary"]
                text = text[-min(3000, remaining) :]
                result.append(
                    {"role": "user" if item["sender_kind"] == "MEMBER" else "assistant", "content": text}
                )
                remaining -= len(text)
                if remaining <= 0:
                    break
            return ([{"role": "user", "content": summary}] if summary else []) + list(reversed(result))

    async def save_image_observation(self, claimed, attachment, summary, profile):
        async with self.db.transaction() as conn:
            conv, run = await self._lock(conn, claimed)
            if not self._valid(conv, run, claimed):
                raise DomainError("LEASE_LOST", "运行任务已失效")
            await execute(
                conn,
                """INSERT INTO ag_image_observation
                (run_id,attachment_id,profile_id,profile_version,summary,content_sha256)
                VALUES (:run,:attachment,:profile,:version,:summary,:sha)
                ON CONFLICT (run_id,attachment_id) DO NOTHING""",
                run=claimed["id"],
                attachment=attachment["id"],
                profile=profile["id"],
                version=profile["version"],
                summary=summary,
                sha=attachment["sha256"],
            )

    async def finish(self, claimed, error=None):
        async with self.db.transaction() as conn:
            conv, run = await self._lock(conn, claimed)
            if (
                not run
                or run["fencing_token"] != claimed["fencing_token"]
                or run["state"] not in {"RUNNING", "STOP_REQUESTED"}
            ):
                return
            stopped = run["state"] == "STOP_REQUESTED" or conv["control_epoch"] != run["control_epoch"]
            expired = not run["lease_valid"]
            state = "INTERRUPTED" if expired else "STOPPED" if stopped else "FAILED" if error else "COMPLETED"
            if expired:
                error = "WORKER_LEASE_EXPIRED"
            run = await one(
                conn,
                """UPDATE ag_run SET state=:state,error_code=:error,finished_at=now(),
                lease_owner=NULL,lease_until=NULL,version=version+1 WHERE id=:id RETURNING *""",
                state=state,
                error=error,
                id=run["id"],
            )
            await execute(
                conn,
                """UPDATE ag_generation_slot SET run_id=NULL,
                lease_until=CASE WHEN :hold THEN GREATEST(lease_until,now()+make_interval(secs=>:secs)) ELSE NULL END
                WHERE run_id=:id""",
                id=run["id"],
                hold=self.settings.provider != "fixture" and (state != "COMPLETED"),
                secs=self.settings.model_timeout_seconds + 5,
            )
            msg = await one(
                conn,
                "UPDATE ag_message SET status=:state,updated_at=now() WHERE id=:id RETURNING *",
                state=state,
                id=claimed["assistant_id"],
            )
            events = [("message.completed", {"message": message_view(msg, run)})]
            if state == "STOPPED":
                events.append(("run.stopped", {"run_id": str(run["id"]), "reason": "USER"}))
            elif error:
                events.append(
                    ("run.failed", {"run_id": str(run["id"]), "error_code": error, "retryable": True})
                )
            await append_events(conn, conv, events)

    async def recover_expired(self, auth_context=None):
        condition = (
            "lease_until<now()"
            if auth_context is None
            else "config_snapshot->>'auth_context_id' IS DISTINCT FROM :context"
        )
        error_code = "WORKER_LEASE_EXPIRED" if auth_context is None else "AUTH_CONTEXT_LOST"
        async with self.db.transaction() as conn:
            candidates = await rows(
                conn,
                f"SELECT * FROM ag_run WHERE state IN {ACTIVE} AND ({condition}) ORDER BY created_at LIMIT 10",
                **({"context": auth_context} if auth_context is not None else {}),
            )
            for candidate in candidates:
                conv = await one(
                    conn,
                    "SELECT * FROM ag_conversation WHERE id=:id FOR UPDATE"
                    + (" SKIP LOCKED" if auth_context is None else ""),
                    id=candidate["conversation_id"],
                )
                if not conv:
                    continue
                run = await one(
                    conn,
                    f"SELECT * FROM ag_run WHERE id=:id AND ({condition}) FOR UPDATE",
                    id=candidate["id"],
                    **({"context": auth_context} if auth_context is not None else {}),
                )
                if not run or run["state"] not in {"QUEUED", "RUNNING", "STOP_REQUESTED"}:
                    continue
                run = await one(
                    conn,
                    """UPDATE ag_run SET state='INTERRUPTED',error_code=:error,
                    fencing_token=fencing_token+1,finished_at=now(),lease_until=NULL,lease_owner=NULL,
                    version=version+1 WHERE id=:id RETURNING *""",
                    id=run["id"],
                    error=error_code,
                )
                await execute(
                    conn,
                    """UPDATE ag_generation_slot SET run_id=NULL,
                    lease_until=CASE WHEN :hold THEN lease_until ELSE NULL END WHERE run_id=:id""",
                    id=run["id"],
                    hold=self.settings.provider != "fixture",
                )
                msg = await one(
                    conn,
                    "UPDATE ag_message SET status='INTERRUPTED' WHERE run_id=:id RETURNING *",
                    id=run["id"],
                )
                events = [
                    (
                        "run.failed",
                        {"run_id": str(run["id"]), "error_code": error_code, "retryable": True},
                    )
                ]
                if msg:
                    events.insert(0, ("message.completed", {"message": message_view(msg, run)}))
                await append_events(conn, conv, events)
                # One recovered run per transaction keeps the event clock the last lock.
                return True
        return False

    async def _lock(self, conn, claimed):
        conv = await one(
            conn, "SELECT * FROM ag_conversation WHERE id=:id FOR UPDATE", id=claimed["conversation_id"]
        )
        run = await one(
            conn,
            f"SELECT *,lease_until>now() AS lease_valid,EXISTS(SELECT 1 FROM ag_message m WHERE m.id=ag_run.trigger_message_id AND {visible_sql()}) AS trigger_visible FROM ag_run WHERE id=:id FOR UPDATE",
            id=claimed["id"],
        )
        return conv, run

    @staticmethod
    def _valid(conv, run, claimed):
        return (
            run
            and run["state"] == "RUNNING"
            and run["fencing_token"] == claimed["fencing_token"]
            and run["lease_owner"] == claimed["lease_owner"]
            and run["lease_valid"]
            and run["trigger_visible"]
            and conv["memory_epoch"] == run["memory_epoch"]
            and conv["control_epoch"] == run["control_epoch"]
            and not conv["deleted_at"]
            and (
                conv["state"] == "AI_ACTIVE"
                or (conv["state"] == "WAITING_HUMAN" and conv["waiting_ai_enabled"])
            )
        )
