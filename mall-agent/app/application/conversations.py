from uuid import uuid4

from app.application.expiry import assert_cached_visible, assert_message_visible, visible_sql
from app.application.governance import read_policy
from app.application.views import (
    conversation_projection,
    conversation_view,
    message_view,
    messages_projection,
    run_view,
)
from app.domain.identity import DomainError
from app.infrastructure.postgres.database import append_events, digest, encoded, execute, one, rows

ACTIVE = "('QUEUED','RUNNING','STOP_REQUESTED')"


async def owned(conn, cid, member_id, lock=False):
    row = await one(
        conn,
        "SELECT * FROM ag_conversation WHERE id=:id AND member_id=:member "
        "AND deleted_at IS NULL" + (" FOR UPDATE" if lock else ""),
        id=cid,
        member=member_id,
    )
    if not row:
        raise DomainError("NOT_FOUND", "会话不存在", 404)
    return row


async def dedupe(conn, scope, member, key, body):
    # Same actor+key serializes the entire command, including across conversations.
    await execute(
        conn, "SELECT pg_advisory_xact_lock(hashtextextended(:key,0))", key=f"{scope}:{member}:{key}"
    )
    record = await one(
        conn,
        """SELECT * FROM ag_request_dedupe WHERE scope=:scope
        AND actor_kind='MEMBER' AND actor_id=:member AND idempotency_key=:key""",
        scope=scope,
        member=member,
        key=key,
    )
    if record and record["request_hash"] != digest(body):
        raise DomainError("IDEMPOTENCY_CONFLICT", "请求编号已经用于不同内容")
    if record:
        await assert_cached_visible(conn, record["response_ref"])
    return record["response_ref"] if record else None


async def save_dedupe(conn, scope, member, key, body, result):
    await execute(
        conn,
        """INSERT INTO ag_request_dedupe
        (scope,actor_kind,actor_id,idempotency_key,request_hash,response_ref,expires_at)
        VALUES (:scope,'MEMBER',:member,:key,:hash,CAST(:result AS jsonb),now()+interval '7 days')""",
        scope=scope,
        member=member,
        key=key,
        hash=digest(body),
        result=encoded(result),
    )


class Conversations:
    def __init__(self, database, runtime_auth, cursors, settings=None, models=None, bridge=None):
        self.db, self.auth, self.cursors = database, runtime_auth, cursors
        self.settings, self.models = settings, models
        self.bridge = bridge

    async def create(self, principal, key, body, token=None):
        kind = body["entry_context"]["kind"]
        if kind == "PRODUCT":
            if not self.bridge or not token:
                raise DomainError("FEATURE_NOT_ACTIVE", "商品咨询入口未接线", 409)
            await self.bridge.product(token, body["entry_context"]["product_id"])
        elif kind == "ORDER":
            from app.application.orders import OrderRead, query_orders

            if not self.bridge or not token or not self.settings.orders_enabled:
                raise DomainError("FEATURE_NOT_ACTIVE", "订单咨询入口尚未开放", 409)
            await query_orders(
                self.bridge, token, OrderRead(kind="ORDER", order_id=body["entry_context"]["order_id"])
            )
        elif kind != "GENERAL":
            raise DomainError("VALIDATION_ERROR", "咨询入口无效", 422)
        async with self.db.transaction() as conn:
            cached = await dedupe(conn, "createConversation", principal.id, key, body)
            if cached:
                if cached.get("resource_deleted"):
                    raise DomainError("NOT_FOUND", "原会话已删除，请新建会话", 404)
                await owned(conn, cached["id"], principal.id)
                return cached
            cid = uuid4()
            conv = await one(
                conn,
                """INSERT INTO ag_conversation(id,member_id,title,entry_context)
                VALUES (:id,:member,:title,CAST(:context AS jsonb)) RETURNING *""",
                id=cid,
                member=principal.id,
                title=body["title"],
                context=encoded(body["entry_context"]),
            )
            result = conversation_view(conv)
            await save_dedupe(conn, "createConversation", principal.id, key, body, result)
            await append_events(conn, conv, [("conversation.updated", {"conversation": result})])
            return result

    async def list(self, principal, limit, cursor, query):
        query = query.strip()
        before = self.cursors.decode(cursor, f"conversations:{principal.id}:{query}") if cursor else None
        async with self.db.transaction(snapshot=True) as conn:
            data = await rows(
                conn,
                f"""SELECT c.*,
                (SELECT COUNT(*) FROM ag_message m WHERE m.conversation_id=c.id AND {visible_sql()}
                    AND m.sender_kind IN ('ASSISTANT','STAFF') AND m.status<>'GENERATING'
                    AND m.seq>COALESCE(rc.last_seq,0)) AS projected_unread,
                (SELECT text_content FROM ag_message m WHERE m.conversation_id=c.id
                    AND {visible_sql()} AND m.text_content<>'' ORDER BY m.seq DESC LIMIT 1) AS projected_preview
                FROM ag_conversation c LEFT JOIN ag_read_cursor rc ON rc.conversation_id=c.id
                    AND rc.actor_kind='MEMBER' AND rc.actor_id=:member
                WHERE c.member_id=:member AND c.deleted_at IS NULL AND (:query='' OR c.title ILIKE :pattern)
                AND (CAST(:before AS text) IS NULL OR (c.updated_at,c.id)<(CAST(:before AS timestamptz),CAST(:id AS uuid)))
                ORDER BY c.updated_at DESC,c.id DESC LIMIT :limit""",
                member=principal.id,
                query=query,
                pattern="%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",
                before=before[0] if before else None,
                id=before[1] if before else None,
                limit=limit + 1,
            )
            clock = await one(conn, "SELECT last_event_id FROM ag_event_clock WHERE singleton_id=1")
            page = data[:limit]
            more = len(data) > limit
            return {
                "items": [await conversation_projection(conn, x) for x in page],
                "has_more": more,
                "next_cursor": self.cursors.encode(
                    [page[-1]["updated_at"].isoformat(), str(page[-1]["id"])],
                    f"conversations:{principal.id}:{query}",
                )
                if more
                else None,
                "event_cursor": str(clock["last_event_id"]),
            }

    async def snapshot(self, principal, cid):
        async with self.db.transaction(snapshot=True) as conn:
            conv = await owned(conn, cid, principal.id)
            return await self.snapshot_in(conn, conv, f"messages:{principal.id}:{cid}", actor=principal)

    async def snapshot_in(self, conn, conv, history_scope, actor=None):
        from app.application.handoffs import handoff_view, latest_handoff

        cid = conv["id"]
        data = await rows(
            conn,
            f"""SELECT * FROM ag_message m WHERE conversation_id=:id
            AND {visible_sql()} ORDER BY seq DESC LIMIT 21""",
            id=cid,
        )
        run = await one(conn, f"SELECT * FROM ag_run WHERE conversation_id=:id AND state IN {ACTIVE}", id=cid)
        # Read cursor, unread count, preview and event cursor share this snapshot.
        metadata = await one(
            conn,
            f"""SELECT clock.last_event_id,COALESCE(rc.last_seq,0) last_seq,
            (SELECT COUNT(*) FROM ag_message m WHERE m.conversation_id=:cid AND {visible_sql()}
                AND m.status<>'GENERATING' AND m.sender_kind IN ('MEMBER','ASSISTANT','STAFF')
                AND NOT(m.sender_kind=:kind AND m.sender_id=:actor)
                AND m.seq>COALESCE(rc.last_seq,0)) unread_count,
            (SELECT text_content FROM ag_message m WHERE m.conversation_id=:cid
                AND {visible_sql()} AND m.text_content<>'' ORDER BY seq DESC LIMIT 1) preview
            FROM ag_event_clock clock LEFT JOIN ag_read_cursor rc ON rc.conversation_id=:cid
                AND rc.actor_kind=:kind AND rc.actor_id=:actor WHERE clock.singleton_id=1""",
            cid=cid,
            kind=actor.kind if actor else "MEMBER",
            actor=actor.id if actor else conv["member_id"],
        )
        from app.application.actions import action_view

        pending = await rows(
            conn,
            "SELECT * FROM ag_action WHERE conversation_id=:id AND state IN ('PENDING_CONFIRMATION','EXECUTING','UNKNOWN') ORDER BY created_at DESC LIMIT 20",
            id=cid,
        )
        messages = list(reversed(data[:20]))
        position = {"last_seq": str(metadata["last_seq"]), "unread_count": metadata["unread_count"]}
        return {
            "conversation": await conversation_projection(
                conn, conv, preview=metadata["preview"] or "", actor=actor, position=position
            ),
            "recent_messages": await messages_projection(conn, messages),
            "latest_seq": str(max((m["seq"] for m in messages), default=0)),
            "event_cursor": str(metadata["last_event_id"]),
            "active_run": run_view(run),
            "pending_actions": [action_view(action) for action in pending],
            "handoff": handoff_view(await latest_handoff(conn, cid), conv),
            "read_position": position,
            "history_cursor": self.cursors.encode([messages[0]["seq"]], history_scope)
            if len(data) > 20
            else None,
        }

    async def messages(self, principal, cid, limit, before):
        async with self.db.transaction(snapshot=True) as conn:
            await owned(conn, cid, principal.id)
            data = await rows(
                conn,
                f"""SELECT * FROM ag_message m WHERE conversation_id=:id
                AND {visible_sql()} AND seq<:before ORDER BY seq DESC LIMIT :limit""",
                id=cid,
                before=before,
                limit=limit + 1,
            )
            more = len(data) > limit
            page = list(reversed(data[:limit]))
            return {
                "items": await messages_projection(conn, page),
                "has_more": more,
                "next_cursor": self.cursors.encode([page[0]["seq"]], f"messages:{principal.id}:{cid}")
                if more
                else None,
            }

    async def rename(self, principal, cid, key, body):
        scope = f"renameConversation:{cid}"
        async with self.db.transaction() as conn:
            cached = await dedupe(conn, scope, principal.id, key, body)
            conv = await owned(conn, cid, principal.id, lock=True)
            if cached is not None:
                return cached
            if conv["version"] != body["expected_version"]:
                raise DomainError("VERSION_CONFLICT", "会话已变化，请刷新后再修改")
            title = body["title"].strip()
            if not title or len(title) > 100:
                raise DomainError("VALIDATION_ERROR", "标题需为1到100个字符", 422)
            conv = await one(
                conn,
                """UPDATE ag_conversation SET title=:title,version=version+1,updated_at=now()
                WHERE id=:cid RETURNING *""",
                title=title,
                cid=cid,
            )
            result = await conversation_projection(conn, conv)
            await save_dedupe(conn, scope, principal.id, key, body, result)
            await append_events(conn, conv, [("conversation.updated", {"conversation": result})])
            return result

    async def mark_read(self, principal, cid, key, body):
        from app.application.read_positions import advance_read

        scope = f"updateReadCursor:{cid}"
        async with self.db.transaction() as conn:
            cached = await dedupe(conn, scope, principal.id, key, body)
            conv = await owned(conn, cid, principal.id, lock=True)
            if cached is not None:
                return cached
            result, changed = await advance_read(conn, cid, "MEMBER", principal.id, int(body["last_seq"]))
            await save_dedupe(conn, scope, principal.id, key, body, result)
            if changed:
                await append_events(conn, conv, [("read.updated", result)])
            return result

    async def delete(self, principal, cid, key, body):
        from app.application.jobs import wake_job

        scope = f"deleteConversation:{cid}"
        async with self.db.transaction() as conn:
            cached = await dedupe(conn, scope, principal.id, key, body)
            conv = await one(
                conn,
                "SELECT * FROM ag_conversation WHERE id=:cid AND member_id=:member FOR UPDATE",
                cid=cid,
                member=principal.id,
            )
            if not conv:
                raise DomainError("NOT_FOUND", "会话不存在", 404)
            if cached is not None:
                return cached
            if conv["deleted_at"]:
                raise DomainError("NOT_FOUND", "会话不存在", 404)
            if conv["version"] != body["expected_version"]:
                raise DomainError("VERSION_CONFLICT", "会话已变化，请刷新后再删除")
            conv = await one(
                conn,
                """UPDATE ag_conversation SET deleted_at=now(),state='CLOSED',waiting_ai_enabled=false,
                assigned_admin_id=NULL,waiting_since=NULL,closed_at=COALESCE(closed_at,now()),
                memory_epoch=memory_epoch+1,control_epoch=control_epoch+1,version=version+1,updated_at=now()
                WHERE id=:cid RETURNING *""",
                cid=cid,
            )
            stopped = await rows(
                conn,
                f"""UPDATE ag_run SET state='STOPPED',fencing_token=fencing_token+1,
                lease_owner=NULL,lease_until=NULL,finished_at=now(),error_code='CONVERSATION_DELETED',
                version=version+1,updated_at=now() WHERE conversation_id=:cid AND state IN {ACTIVE} RETURNING id""",
                cid=cid,
            )
            for run in stopped:
                await execute(
                    conn,
                    """UPDATE ag_generation_slot SET run_id=NULL,
                    lease_until=CASE WHEN :hold THEN lease_until ELSE NULL END WHERE run_id=:rid""",
                    rid=run["id"],
                    hold=bool(self.settings and self.settings.provider != "fixture"),
                )
                self.auth.discard(str(run["id"]))
            await execute(
                conn,
                "UPDATE ag_message SET status='STOPPED',updated_at=now() WHERE conversation_id=:cid AND status='GENERATING'",
                cid=cid,
            )
            await execute(
                conn,
                """UPDATE ag_action SET state='STALE',version=version+1,updated_at=now()
                WHERE conversation_id=:cid AND state='PENDING_CONFIRMATION'""",
                cid=cid,
            )
            await execute(
                conn,
                """UPDATE ag_handoff SET state='CLOSED',finished_at=now(),version=version+1,updated_at=now()
                WHERE conversation_id=:cid AND state IN ('WAITING','CLAIMED')""",
                cid=cid,
            )
            await execute(conn, "DELETE FROM ag_context_summary WHERE conversation_id=:cid", cid=cid)
            await execute(
                conn,
                """INSERT INTO ag_erasure_tombstone(resource_type,resource_id,memory_epoch)
                VALUES ('CONVERSATION',:cid,:epoch) ON CONFLICT (resource_type,resource_id)
                DO UPDATE SET memory_epoch=EXCLUDED.memory_epoch,erased_at=now()""",
                cid=str(cid),
                epoch=conv["memory_epoch"],
            )
            job = await one(
                conn,
                """INSERT INTO ag_job(id,job_type,dedupe_key,parameters,next_attempt_at)
                VALUES (:id,'RETENTION_EXECUTE',:key,CAST(:parameters AS jsonb),
                now()+make_interval(secs=>:delay)) RETURNING *""",
                id=uuid4(),
                key=f"conversation-delete:{cid}:{conv['memory_epoch']}",
                delay=self.settings.run_timeout_seconds + self.settings.run_lease_seconds + 10
                if self.settings and stopped
                else 0,
                parameters=encoded(
                    {
                        "kind": "CONVERSATION_DELETE",
                        "conversation_id": str(cid),
                        "memory_epoch": conv["memory_epoch"],
                    }
                ),
            )
            await wake_job(conn, job)
            await execute(
                conn,
                """INSERT INTO ag_audit_log(id,actor_kind,actor_id,operation,resource_type,resource_id,
                request_id,outcome,details) VALUES (:id,'MEMBER',:actor,'deleteConversation','CONVERSATION',
                :cid,:request,'SUCCEEDED',CAST(:details AS jsonb))""",
                id=uuid4(),
                actor=principal.id,
                cid=str(cid),
                request=key,
                details=encoded({"memory_epoch": conv["memory_epoch"], "cleanup_job_id": str(job["id"])}),
            )
            await save_dedupe(conn, scope, principal.id, key, body, {})
            return {}

    async def retry_run(self, principal, token, rid, key, body):
        scope = f"retryRun:{rid}"
        async with self.db.transaction() as conn:
            cached = await dedupe(conn, scope, principal.id, key, body)
            raw = await one(conn, "SELECT conversation_id FROM ag_run WHERE id=:rid", rid=rid)
            if not raw:
                raise DomainError("NOT_FOUND", "生成任务不存在", 404)
            conv = await owned(conn, raw["conversation_id"], principal.id, lock=True)
            if cached is not None:
                return cached
            old = await one(conn, "SELECT * FROM ag_run WHERE id=:rid FOR UPDATE", rid=rid)
            if old["version"] != body["expected_version"]:
                raise DomainError("VERSION_CONFLICT", "任务状态已变化，请刷新")
            if old["state"] not in {"FAILED", "INTERRUPTED", "STOPPED"}:
                raise DomainError("RUN_NOT_RETRYABLE", "只有失败、中断或停止的回复可以重试")
            if old["memory_epoch"] != conv["memory_epoch"] or not (
                conv["state"] == "AI_ACTIVE"
                or (conv["state"] == "WAITING_HUMAN" and conv["waiting_ai_enabled"])
            ):
                raise DomainError("CONVERSATION_NOT_ACTIVE", "当前会话不能重试 AI 回复")
            latest = await one(
                conn,
                "SELECT id,attempt FROM ag_run WHERE trigger_message_id=:mid ORDER BY attempt DESC LIMIT 1",
                mid=old["trigger_message_id"],
            )
            if latest["id"] != old["id"]:
                raise DomainError("RUN_ALREADY_RETRIED", "该回复已有新的重试，请查看最新结果")
            if await one(
                conn,
                f"SELECT id FROM ag_run WHERE conversation_id=:cid AND state IN {ACTIVE}",
                cid=conv["id"],
            ):
                raise DomainError("RUN_ALREADY_ACTIVE", "当前会话已有生成任务")
            trigger = await one(
                conn,
                "SELECT * FROM ag_message WHERE id=:mid AND conversation_id=:cid AND deleted_at IS NULL",
                mid=old["trigger_message_id"],
                cid=conv["id"],
            )
            if not trigger or trigger["sender_kind"] != "MEMBER" or trigger["sender_id"] != principal.id:
                raise DomainError("RUN_NOT_RETRYABLE", "原问题已失效")
            await assert_message_visible(conn, trigger["id"])
            if await one(
                conn,
                f"SELECT id FROM ag_message m WHERE conversation_id=:cid AND sender_kind='MEMBER' AND seq>:seq AND {visible_sql()} LIMIT 1",
                cid=conv["id"],
                seq=trigger["seq"],
            ):
                raise DomainError("RUN_NOT_RETRYABLE", "已有更新的问题或选择，请重试最新回复或发送新消息")
            for block in trigger["blocks"]:
                if block["type"] != "attachment":
                    continue
                if not (self.settings and self.settings.attachments_enabled and self.settings.vision_enabled):
                    raise DomainError("ATTACHMENT_UNAVAILABLE", "原图片当前不可用，请重新发送", 410)
                attachment = await one(
                    conn,
                    """SELECT id FROM ag_attachment WHERE id=:id AND owner_kind='MEMBER'
                    AND owner_id=:member AND purpose='CHAT_IMAGE' AND state='READY' AND retention_until>now()""",
                    id=block["attachment"]["id"],
                    member=principal.id,
                )
                if not attachment:
                    raise DomainError("ATTACHMENT_UNAVAILABLE", "原图片已失效，请重新发送", 410)
            new_id = uuid4()
            self.auth.put(str(new_id), token)
            config = {
                **(
                    await self.models.capture(conn)
                    if self.models and self.settings.provider != "fixture"
                    else {"provider": "fixture"}
                ),
                "graph_version": self.settings.build if self.settings else "fixture",
                "auth_context_id": self.auth.context_id,
            }
            run = await one(
                conn,
                """INSERT INTO ag_run(id,conversation_id,trigger_message_id,attempt,retry_of_run_id,
                conversation_version,memory_epoch,control_epoch,checkpoint_thread_id,config_snapshot)
                VALUES (:id,:cid,:mid,:attempt,:old,:version,:memory,:control,:thread,CAST(:config AS jsonb)) RETURNING *""",
                id=new_id,
                cid=conv["id"],
                mid=trigger["id"],
                attempt=old["attempt"] + 1,
                old=old["id"],
                version=conv["version"],
                memory=conv["memory_epoch"],
                control=conv["control_epoch"],
                thread=f"member:{principal.id}:run:{new_id}",
                config=encoded(config),
            )
            conv = await one(
                conn,
                "UPDATE ag_conversation SET version=version+1,updated_at=now() WHERE id=:cid RETURNING *",
                cid=conv["id"],
            )
            result = run_view(run)
            await save_dedupe(conn, scope, principal.id, key, body, result)
            await append_events(conn, conv, [("run.queued", {"run_id": str(new_id), "position": 1})])
            return result

    async def send(self, principal, token, cid, key, body):
        if body["attachment_ids"] and not (self.settings and self.settings.attachments_enabled):
            raise DomainError("FEATURE_NOT_ACTIVE", "图片发送尚未开放", 409)
        scope = f"sendMemberMessage:{cid}"
        async with self.db.transaction() as conn:
            cached = await dedupe(conn, scope, principal.id, key, body)
            conv = await owned(conn, cid, principal.id, lock=True)
            if cached:
                return cached
            previous = await one(
                conn,
                "SELECT * FROM ag_message WHERE conversation_id=:cid AND client_message_id=:id",
                cid=cid,
                id=body["client_message_id"],
            )
            if previous:
                await assert_message_visible(conn, previous["id"])
                previous_ids = [
                    block["attachment"]["id"] for block in previous["blocks"] if block["type"] == "attachment"
                ]
                if (
                    previous["sender_kind"] != "MEMBER"
                    or previous["sender_id"] != principal.id
                    or previous["text_content"] != body["text"]
                    or previous_ids != body["attachment_ids"]
                ):
                    raise DomainError("MESSAGE_ID_REUSED", "消息编号已经用于不同内容")
                run = await one(
                    conn,
                    "SELECT * FROM ag_run WHERE trigger_message_id=:id ORDER BY attempt DESC LIMIT 1",
                    id=previous["id"],
                )
                clock = await one(conn, "SELECT last_event_id FROM ag_event_clock WHERE singleton_id=1")
                return {
                    "message": message_view(previous),
                    "run": run_view(run),
                    "event_cursor": str(clock["last_event_id"]),
                }
            if conv["state"] == "CLOSED":
                raise DomainError("CONVERSATION_NOT_ACTIVE", "会话已结束，请新建会话")
            policy = (await read_policy(conn))["value"]
            if len(body["text"]) > policy["max_input_characters"]:
                raise DomainError("INPUT_TOO_LONG", "消息超过当前设置的字数上限", 422)
            ai_enabled = conv["state"] == "AI_ACTIVE" or (
                conv["state"] == "WAITING_HUMAN" and conv["waiting_ai_enabled"]
            )
            if (
                ai_enabled
                and body["attachment_ids"]
                and not (self.settings.vision_enabled and self.settings.provider == "openai_responses")
            ):
                raise DomainError("FEATURE_NOT_ACTIVE", "AI图片咨询尚未开放，可转人工发送图片", 409)
            if ai_enabled and await one(
                conn, f"SELECT id FROM ag_run WHERE conversation_id=:id AND state IN {ACTIVE}", id=cid
            ):
                raise DomainError("RUN_ALREADY_ACTIVE", "当前会话已有生成任务")
            from app.application.attachments import attachment_view, bind_message_attachments

            attachments = await bind_message_attachments(conn, principal.id, body["attachment_ids"])
            # New member context supersedes unconfirmed preparations immediately,
            # including when the next turn only clarifies and creates no new card.
            # Replayed messages returned above never revoke a replacement card.
            stale = await rows(
                conn,
                "UPDATE ag_action SET state='STALE',error_code='MEMBER_CONTEXT_CHANGED',version=version+1,updated_at=now() WHERE conversation_id=:cid AND state='PENDING_CONFIRMATION' RETURNING *",
                cid=cid,
            )
            if stale:
                await execute(
                    conn,
                    "UPDATE ag_attachment_ref SET protected=false,released_at=now(),updated_at=now(),version=version+1 WHERE action_id=ANY(CAST(:ids AS uuid[])) AND ref_kind='RETURN_PROOF' AND return_apply_id IS NULL AND released_at IS NULL",
                    ids=[row["id"] for row in stale],
                )
            blocks = [{"type": "attachment", "attachment": attachment_view(item)} for item in attachments]
            mid, rid = uuid4(), uuid4()
            # Credentials enter API memory before COMMIT, never a SQL parameter or checkpoint.
            if ai_enabled:
                self.auth.put(str(rid), token)
            message = await one(
                conn,
                """INSERT INTO ag_message
                (id,conversation_id,seq,sender_kind,sender_id,client_message_id,text_content,blocks,status,retention_until)
                VALUES (:id,:cid,:seq,'MEMBER',:member,:client,:text,CAST(:blocks AS jsonb),'COMPLETED',now()+make_interval(days=>:days)) RETURNING *""",
                id=mid,
                cid=cid,
                seq=conv["next_message_seq"],
                member=principal.id,
                client=body["client_message_id"],
                text=body["text"],
                blocks=encoded(blocks),
                days=policy["message_retention_days"],
            )
            for item in attachments:
                await execute(
                    conn,
                    """INSERT INTO ag_attachment_ref(id,attachment_id,ref_kind,message_id)
                    VALUES (:id,:attachment,'MESSAGE',:message)""",
                    id=uuid4(),
                    attachment=item["id"],
                    message=mid,
                )
            run = None
            if ai_enabled:
                run = await one(
                    conn,
                    """INSERT INTO ag_run
                    (id,conversation_id,trigger_message_id,conversation_version,memory_epoch,control_epoch,
                     checkpoint_thread_id,config_snapshot) VALUES (:id,:cid,:mid,:version,:memory,:epoch,:thread,
                     CAST(:config AS jsonb)) RETURNING *""",
                    id=rid,
                    cid=cid,
                    mid=mid,
                    version=conv["version"],
                    memory=conv["memory_epoch"],
                    epoch=conv["control_epoch"],
                    thread=f"member:{principal.id}:run:{rid}",
                    config=encoded(
                        {
                            **(
                                await self.models.capture(conn)
                                if self.models and self.settings.provider != "fixture"
                                else {"provider": "fixture"}
                            ),
                            "graph_version": self.settings.build if self.settings else "fixture",
                            "auth_context_id": self.auth.context_id,
                        }
                    ),
                )
            conv = await one(
                conn,
                """UPDATE ag_conversation SET next_message_seq=next_message_seq+1,
                updated_at=now(),version=version+1 WHERE id=:id RETURNING *""",
                id=cid,
            )
            result = {"message": message_view(message), "run": run_view(run), "event_cursor": "0"}
            # Store a replay reference before the final event lock; fill its cursor with the event result.
            await save_dedupe(conn, scope, principal.id, key, body, result)
            from app.application.actions import action_view

            result["event_cursor"] = await append_events(
                conn,
                conv,
                [("action.updated", {"action": action_view(action)}) for action in stale]
                + [
                    (
                        "message.accepted",
                        {
                            "message_id": str(mid),
                            "seq": str(message["seq"]),
                            "client_message_id": body["client_message_id"],
                        },
                    ),
                ]
                + ([("run.queued", {"run_id": str(rid), "position": 1})] if ai_enabled else []),
            )
            # Updating a row already locked by this transaction does not acquire a new business lock.
            await execute(
                conn,
                """UPDATE ag_request_dedupe SET response_ref=CAST(:result AS jsonb)
                WHERE scope=:scope AND actor_kind='MEMBER' AND actor_id=:member AND idempotency_key=:key""",
                result=encoded(result),
                scope=scope,
                member=principal.id,
                key=key,
            )
            return result
