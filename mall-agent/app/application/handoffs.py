"""Human reception commands. Lock staff in ID order before a conversation.

Note contents never enter messages, conversation events, or model context. Assignment
survives presence expiry. Every command rechecks permissions and ownership.
"""

from datetime import UTC, datetime
from uuid import uuid4

from app.application.commands import staff_dedupe
from app.application.conversations import ACTIVE, dedupe, owned, save_dedupe
from app.application.expiry import deadline_sql, visible_sql
from app.application.governance import read_policy
from app.application.views import conversation_projection, message_view, messages_projection
from app.domain.identity import DomainError
from app.infrastructure.postgres.database import append_events, digest, encoded, execute, one, rows
from app.infrastructure.staff_auth import require_permission


def handoff_view(row, conv):
    if not row:
        return None
    return {
        "id": str(row["id"]),
        "conversation_id": str(row["conversation_id"]),
        "state": row["state"],
        "version": row["version"],
        "waiting_ai_enabled": conv["waiting_ai_enabled"],
        "staff_id": str(row["claimed_admin_id"]) if row["claimed_admin_id"] else None,
        "created_at": row["created_at"],
    }


def presence(row):
    expiry = row["presence_expires_at"]
    return row["presence"] if expiry and expiry > datetime.now(UTC) else "OFFLINE"


async def staff_view(conn, row):
    count = await one(
        conn,
        "SELECT count(*) AS n FROM ag_conversation WHERE assigned_admin_id=:id AND state='HUMAN_ACTIVE' AND deleted_at IS NULL",
        id=row["admin_id"],
    )
    return {
        "id": str(row["admin_id"]),
        "display_name": row["display_name"],
        "presence": presence(row),
        "capacity": row["capacity"],
        "active_count": count["n"],
        "version": row["version"],
        "presence_expires_at": row["presence_expires_at"],
    }


def check_version(row, expected):
    if row["version"] != expected:
        raise DomainError("VERSION_CONFLICT", "状态已变化，请刷新后重试")


async def audit(conn, principal, scope, key, resource, details=None):
    await execute(
        conn,
        """INSERT INTO ag_audit_log(id,actor_kind,actor_id,operation,resource_type,resource_id,request_id,outcome,details)
        VALUES (:id,:kind,:actor,:operation,'HANDOFF',:resource,:request,'SUCCEEDED',CAST(:details AS jsonb))""",
        id=uuid4(),
        kind=principal.kind,
        actor=principal.id,
        operation=scope.split(":")[0],
        resource=str(resource),
        request=key,
        details=encoded(details or {}),
    )


async def save_command(conn, principal, scope, key, body, result, resource, details=None):
    if principal.kind == "MEMBER":
        await save_dedupe(conn, scope, principal.id, key, body, result)
    else:
        await execute(
            conn,
            """INSERT INTO ag_request_dedupe(scope,actor_kind,actor_id,idempotency_key,request_hash,response_ref,expires_at)
            VALUES (:scope,'STAFF',:actor,:key,:hash,CAST(:result AS jsonb),now()+interval '7 days')""",
            scope=scope,
            actor=principal.id,
            key=key,
            hash=digest(body),
            result=encoded(result),
        )
    await audit(conn, principal, scope, key, resource, details)


async def latest_handoff(conn, cid, lock=False):
    return await one(
        conn,
        "SELECT * FROM ag_handoff WHERE conversation_id=:id ORDER BY created_at DESC,id DESC LIMIT 1"
        + (" FOR UPDATE" if lock else ""),
        id=cid,
    )


async def visible_conversation(conn, principal, cid, lock=False, mutate=False):
    require_permission(principal, "agent:conversation:read")
    conv = await one(
        conn,
        "SELECT * FROM ag_conversation WHERE id=:id AND deleted_at IS NULL" + (" FOR UPDATE" if lock else ""),
        id=cid,
    )
    if not conv:
        raise DomainError("NOT_FOUND", "会话不存在", 404)
    assigned = conv["assigned_admin_id"] == principal.id
    waiting = conv["state"] == "WAITING_HUMAN" and "agent:handoff:claim" in principal.permissions
    former = False
    if conv["state"] == "CLOSED":
        former = bool(
            await one(
                conn,
                "SELECT id FROM ag_handoff WHERE conversation_id=:id AND claimed_admin_id=:actor AND state='CLOSED'",
                id=cid,
                actor=principal.id,
            )
        )
    broad = "agent:conversation:history:all" in principal.permissions
    if not (assigned or (not mutate and (waiting or former or broad))):
        raise DomainError("NOT_FOUND", "会话不存在或不属于当前接待", 404)
    return conv


class Handoffs:
    def __init__(self, database, conversations, cursors, settings, bridge=None):
        self.bridge = bridge
        self.db, self.conversations, self.cursors, self.settings = database, conversations, cursors, settings

    async def ensure_profile(self, principal):
        require_permission(principal, "agent:presence:write")
        async with self.db.transaction() as conn:
            await execute(
                conn,
                "INSERT INTO ag_staff_profile(admin_id,display_name) VALUES (:id,:name) ON CONFLICT DO NOTHING",
                id=principal.id,
                name=principal.display_name[:80],
            )

    async def _staff_locks(self, conn, ids, shared=False):
        result = {}
        for actor in sorted(set(ids)):
            row = await one(
                conn,
                "SELECT * FROM ag_staff_profile WHERE admin_id=:id FOR " + ("SHARE" if shared else "UPDATE"),
                id=actor,
            )
            if not row:
                raise DomainError("STAFF_NOT_READY", "客服尚未设置接待资料")
            result[actor] = row
        return result

    async def _capacity(self, conn, staff):
        if presence(staff) != "ONLINE":
            raise DomainError("STAFF_NOT_ONLINE", "客服需先切换为在线状态")
        view = await staff_view(conn, staff)
        if view["active_count"] >= staff["capacity"]:
            raise DomainError("STAFF_CAPACITY_REACHED", "客服接待容量已满")

    async def _fence(self, conn, conv):
        events = []
        active = await rows(
            conn,
            f"SELECT * FROM ag_run WHERE conversation_id=:id AND state IN {ACTIVE} ORDER BY id FOR UPDATE",
            id=conv["id"],
        )
        for run in active:
            await execute(
                conn,
                "UPDATE ag_run SET state='STOPPED',finished_at=now(),lease_owner=NULL,lease_until=NULL,version=version+1,updated_at=now() WHERE id=:id",
                id=run["id"],
            )
        await execute(
            conn,
            "UPDATE ag_action SET state='STALE',version=version+1,updated_at=now() WHERE conversation_id=:id AND state='PENDING_CONFIRMATION'",
            id=conv["id"],
        )
        # Keep a provider cooldown so cancellation cannot overbook generation slots.
        for run in active:
            await execute(
                conn,
                """UPDATE ag_generation_slot SET run_id=NULL,
                lease_until=CASE WHEN :hold THEN GREATEST(lease_until,now()+make_interval(secs=>:seconds)) ELSE NULL END WHERE run_id=:id""",
                id=run["id"],
                hold=self.settings.provider != "fixture",
                seconds=self.settings.model_timeout_seconds + 5,
            )
            messages = await rows(
                conn,
                "UPDATE ag_message SET status='STOPPED',updated_at=now() WHERE run_id=:id AND status='GENERATING' RETURNING *",
                id=run["id"],
            )
            events.extend(("message.completed", {"message": message_view(msg)}) for msg in messages)
            events.append(("run.stopped", {"run_id": str(run["id"]), "reason": "HANDOFF"}))
        return events

    async def _publish(self, conn, conv, handoff, events):
        payload = {
            "handoff": handoff_view(handoff, conv),
            "conversation": await conversation_projection(conn, conv),
        }
        await append_events(conn, conv, events + [("handoff.updated", payload)])

    async def request(self, principal, cid, key, body, runtime_fence=None):
        from app.application.runtime_fence import assert_runtime_fence

        scope = f"requestHandoff:{cid}"
        async with self.db.transaction() as conn:
            cached = await dedupe(conn, scope, principal.id, key, body)
            conv = await owned(conn, cid, principal.id, lock=True)
            await assert_runtime_fence(conn, conv, runtime_fence)
            if cached:
                return cached
            check_version(conv, body["expected_conversation_version"])
            if conv["state"] != "AI_ACTIVE":
                raise DomainError("HANDOFF_STATE_CONFLICT", "当前会话不能重复申请人工接待")
            handoff = await one(
                conn,
                "INSERT INTO ag_handoff(id,conversation_id,member_id,reason) VALUES (:id,:cid,:member,:reason) RETURNING *",
                id=uuid4(),
                cid=cid,
                member=principal.id,
                reason=body.get("reason", ""),
            )
            conv = await one(
                conn,
                "UPDATE ag_conversation SET state='WAITING_HUMAN',waiting_ai_enabled=false,waiting_since=now(),control_epoch=control_epoch+1,version=version+1,updated_at=now() WHERE id=:id RETURNING *",
                id=cid,
            )
            events = await self._fence(conn, conv)
            result = handoff_view(handoff, conv)
            await save_command(conn, principal, scope, key, body, result, handoff["id"])
            await self._publish(conn, conv, handoff, events)
            return result

    async def waiting(self, principal, cid, key, body, withdraw=False):
        scope = f"{'withdrawHandoff' if withdraw else 'setWaitingAI'}:{cid}"
        async with self.db.transaction() as conn:
            cached = await dedupe(conn, scope, principal.id, key, body)
            conv = await owned(conn, cid, principal.id, lock=True)
            if cached:
                return cached
            handoff = await latest_handoff(conn, cid, lock=True)
            if conv["state"] != "WAITING_HUMAN" or not handoff or handoff["state"] != "WAITING":
                raise DomainError("HANDOFF_STATE_CONFLICT", "接待状态已变化，请刷新")
            check_version(handoff if withdraw else conv, body["expected_version"])
            if withdraw:
                handoff = await one(
                    conn,
                    "UPDATE ag_handoff SET state='WITHDRAWN',finished_at=now(),version=version+1,updated_at=now() WHERE id=:id RETURNING *",
                    id=handoff["id"],
                )
            conv = await one(
                conn,
                """UPDATE ag_conversation SET state=:state,waiting_ai_enabled=:enabled,
                waiting_since=CASE WHEN :withdraw THEN NULL ELSE waiting_since END,control_epoch=control_epoch+1,version=version+1,updated_at=now() WHERE id=:id RETURNING *""",
                id=cid,
                state="AI_ACTIVE" if withdraw else "WAITING_HUMAN",
                enabled=False if withdraw else body["enabled"],
                withdraw=withdraw,
            )
            events = await self._fence(conn, conv)
            result = handoff_view(handoff, conv) if withdraw else await conversation_projection(conn, conv)
            await save_command(conn, principal, scope, key, body, result, handoff["id"])
            await self._publish(conn, conv, handoff, events)
            return result

    async def command(self, principal, hid, key, body, command, token=""):
        require_permission(principal, f"agent:handoff:{command}")
        if command == "claim":
            for permission in ("agent:conversation:read", "agent:conversation:reply", "agent:presence:write"):
                require_permission(principal, permission)
        if command == "transfer":
            if not self.bridge:
                raise DomainError("FEATURE_NOT_ACTIVE", "员工转接资格校验尚未配置")
            await self.bridge.staff_target(token, body["target_staff_id"])
        scope = f"{command}Handoff:{hid}"
        async with self.db.transaction() as conn:
            cached = await staff_dedupe(conn, principal, scope, key, body)
            raw = await one(
                conn, "SELECT conversation_id,claimed_admin_id FROM ag_handoff WHERE id=:id", id=hid
            )
            if not raw:
                raise DomainError("NOT_FOUND", "接待不存在", 404)
            actors = [principal.id]
            if raw["claimed_admin_id"]:
                actors.append(raw["claimed_admin_id"])
            target = int(body["target_staff_id"]) if command == "transfer" else principal.id
            actors.append(target)
            staff = await self._staff_locks(conn, actors)
            conv = await one(
                conn,
                "SELECT * FROM ag_conversation WHERE id=:id AND deleted_at IS NULL FOR UPDATE",
                id=raw["conversation_id"],
            )
            handoff = await one(conn, "SELECT * FROM ag_handoff WHERE id=:id FOR UPDATE", id=hid)
            if not conv:
                raise DomainError("NOT_FOUND", "会话不存在", 404)
            # Revalidate ownership even for a cached response after reassignment.
            if command != "claim" and (
                handoff["claimed_admin_id"] != principal.id or conv["assigned_admin_id"] != principal.id
            ):
                raise DomainError("NOT_FOUND", "接待不属于当前员工", 404)
            if cached:
                return cached
            check_version(handoff, body["expected_version"])
            if command == "claim":
                if handoff["state"] != "WAITING" or conv["state"] != "WAITING_HUMAN":
                    raise DomainError("HANDOFF_ALREADY_CLAIMED", "该接待已被领取，请刷新队列")
                await self._capacity(conn, staff[target])
            elif handoff["state"] != "CLAIMED" or conv["state"] != "HUMAN_ACTIVE":
                raise DomainError("HANDOFF_STATE_CONFLICT", "接待已经结束或变更")
            elif command == "transfer":
                if target == principal.id:
                    raise DomainError("VALIDATION_ERROR", "请选择另一位客服", 422)
                await self._capacity(conn, staff[target])
            closed = command == "close"
            handoff = await one(
                conn,
                """UPDATE ag_handoff SET state=:state,claimed_admin_id=:target,
                claimed_at=COALESCE(claimed_at,now()),finished_at=CASE WHEN :closed THEN now() ELSE NULL END,
                version=version+1,updated_at=now() WHERE id=:id RETURNING *""",
                id=hid,
                state="CLOSED" if closed else "CLAIMED",
                target=target,
                closed=closed,
            )
            conv = await one(
                conn,
                """UPDATE ag_conversation SET state=:state,assigned_admin_id=:target,
                waiting_ai_enabled=false,waiting_since=NULL,closed_at=CASE WHEN :closed THEN now() ELSE NULL END,
                control_epoch=control_epoch+1,version=version+1,updated_at=now() WHERE id=:id RETURNING *""",
                id=conv["id"],
                state="CLOSED" if closed else "HUMAN_ACTIVE",
                target=target,
                closed=closed,
            )
            events = await self._fence(conn, conv)
            result = handoff_view(handoff, conv)
            await save_command(
                conn,
                principal,
                scope,
                key,
                body,
                result,
                hid,
                {
                    "reason": body.get("reason", ""),
                    "from_staff_id": str(raw["claimed_admin_id"]) if raw["claimed_admin_id"] else None,
                    "to_staff_id": str(target),
                },
            )
            await self._publish(conn, conv, handoff, events)
            return result

    async def context(self, principal, cid, token, cards=None):
        require_permission(principal, "agent:business:read")
        async with self.db.transaction(snapshot=True) as conn:
            before = await visible_conversation(conn, principal, cid)
        selection = (
            cards
            if cards is not None
            else (
                [{"kind": "PRODUCT", "id": before["entry_context"]["product_id"]}]
                if before["entry_context"]["kind"] == "PRODUCT"
                else []
            )
        )
        result = await self.bridge.staff_context(token, principal, before, selection)
        async with self.db.transaction() as conn:
            after = await visible_conversation(conn, principal, cid)
            if after["control_epoch"] != before["control_epoch"]:
                raise DomainError("VERSION_CONFLICT", "会话分配已变化，请刷新")
            await audit(conn, principal, "getStaffBusinessContext", uuid4(), cid)
        return result, before["control_epoch"]

    async def send(self, principal, cid, key, body, note=False, token=""):
        require_permission(principal, "agent:conversation:note" if note else "agent:conversation:reply")
        scope = f"{'createStaffNote' if note else 'sendStaffMessage'}:{cid}"
        business, epoch = None, None
        # Only business cards need a separate read phase before the Java call.
        # Text, images and notes authorize and deduplicate in the write transaction.
        if not note and body["cards"]:
            async with self.db.transaction(snapshot=True) as conn:
                await visible_conversation(conn, principal, cid, mutate=True)
                cached = await staff_dedupe(conn, principal, scope, key, body)
                if cached:
                    return cached
                existing = await one(
                    conn,
                    "SELECT id FROM ag_message WHERE conversation_id=:cid AND client_message_id=:client",
                    cid=cid,
                    client=body["client_message_id"],
                )
            if not existing:
                business, epoch = await self.context(principal, cid, token, body["cards"])
        async with self.db.transaction() as conn:
            cached = await staff_dedupe(conn, principal, scope, key, body)
            profiles = await self._staff_locks(conn, [principal.id], shared=True)
            conv = await visible_conversation(conn, principal, cid, lock=True, mutate=True)
            if cached:
                return cached
            if conv["state"] != "HUMAN_ACTIVE":
                raise DomainError("CONVERSATION_NOT_ACTIVE", "仅当前人工接待可以发送回复或备注")
            if epoch is not None and conv["control_epoch"] != epoch:
                raise DomainError("VERSION_CONFLICT", "会话分配已变化，请刷新")
            policy = (await read_policy(conn))["value"]
            if not note and len(body["text"]) > policy["max_input_characters"]:
                raise DomainError("INPUT_TOO_LONG", "消息超过当前设置的字数上限", 422)
            table = "ag_staff_note" if note else "ag_message"
            client_column = "client_note_id" if note else "client_message_id"
            client_id = body[client_column]
            previous = await one(
                conn,
                f"SELECT * FROM {table} WHERE conversation_id=:cid AND {client_column}=:client",
                cid=cid,
                client=client_id,
            )
            if previous:
                if note:
                    if not await one(
                        conn,
                        f"SELECT id FROM ag_staff_note n WHERE id=:id AND {deadline_sql('n')}>now()",
                        id=previous["id"],
                    ):
                        raise DomainError("MESSAGE_EXPIRED", "原备注已到期", 410)
                else:
                    from app.application.expiry import assert_message_visible

                    await assert_message_visible(conn, previous["id"])
                actor = previous["admin_id"] if note else previous["sender_id"]
                content = previous["body"] if note else previous["text_content"]
                if (
                    actor != principal.id
                    or content != body["body" if note else "text"]
                    or (not note and previous["sender_kind"] != "STAFF")
                    or (
                        not note
                        and (
                            [b["attachment"]["id"] for b in previous["blocks"] if b["type"] == "attachment"]
                            != body["attachment_ids"]
                            or [
                                {
                                    "kind": "PRODUCT" if b["type"] == "product" else "ORDER",
                                    "id": b["product" if b["type"] == "product" else "staff_order"]["id"],
                                }
                                for b in previous["blocks"]
                                if b["type"] in {"product", "staff_order"}
                            ]
                            != body["cards"]
                        )
                    )
                ):
                    raise DomainError("MESSAGE_ID_REUSED", "消息编号已用于不同内容")
                result = note_view(previous) if note else message_view(previous)
            elif note:
                row = await one(
                    conn,
                    "INSERT INTO ag_staff_note(id,conversation_id,admin_id,client_note_id,body,retention_until) VALUES (:id,:cid,:actor,:client,:body,now()+make_interval(days=>:days)) RETURNING *",
                    days=policy["message_retention_days"],
                    id=uuid4(),
                    cid=cid,
                    actor=principal.id,
                    client=client_id,
                    body=body["body"],
                )
                result = note_view(row)
            else:
                from app.application.attachments import attachment_view, bind_message_attachments

                if body["attachment_ids"]:
                    require_permission(principal, "agent:attachment:upload")
                    if not self.settings.attachments_enabled:
                        raise DomainError("FEATURE_NOT_ACTIVE", "图片咨询尚未开放")
                attachments = await bind_message_attachments(
                    conn, principal.id, body["attachment_ids"], "STAFF"
                )
                blocks = [{"type": "attachment", "attachment": attachment_view(a)} for a in attachments]
                for card in body["cards"]:
                    kind = "product" if card["kind"] == "PRODUCT" else "staff_order"
                    source = business["products" if kind == "product" else "orders"]
                    value = next((item for item in source if item["id"] == card["id"]), None)
                    if not value:
                        raise DomainError("NOT_FOUND", "卡片业务资料不存在或不属于当前会员", 404)
                    blocks.append({"type": kind, kind: value})
                row = await one(
                    conn,
                    """INSERT INTO ag_message(id,conversation_id,seq,sender_kind,sender_id,sender_display_name,client_message_id,text_content,blocks,status,retention_until)
                    VALUES (:id,:cid,:seq,'STAFF',:actor,:name,:client,:body,CAST(:blocks AS jsonb),'COMPLETED',now()+make_interval(days=>:days)) RETURNING *""",
                    days=policy["message_retention_days"],
                    id=uuid4(),
                    cid=cid,
                    seq=conv["next_message_seq"],
                    actor=principal.id,
                    name=profiles[principal.id]["display_name"],
                    client=client_id,
                    body=body["text"],
                    blocks=encoded(blocks),
                )
                for attachment in attachments:
                    await execute(
                        conn,
                        "INSERT INTO ag_attachment_ref(id,attachment_id,ref_kind,message_id) VALUES (:id,:aid,'MESSAGE',:mid)",
                        id=uuid4(),
                        aid=attachment["id"],
                        mid=row["id"],
                    )
                result = message_view(row)
                conv = await one(
                    conn,
                    "UPDATE ag_conversation SET next_message_seq=next_message_seq+1,version=version+1,updated_at=now() WHERE id=:id RETURNING *",
                    id=cid,
                )
            await save_command(conn, principal, scope, key, body, result, cid)
            if not note and not previous:
                await append_events(conn, conv, [("message.completed", {"message": result})])
            elif note and not previous:
                event_id = await append_events(
                    conn, conv, [("note.created", {"note": result | {"body": ""}})]
                )
                await execute(
                    conn,
                    "UPDATE ag_event_outbox SET visibility='STAFF_ONLY',required_permission='agent:conversation:note' WHERE event_id=:id",
                    id=int(event_id),
                )
            return result

    async def set_presence(self, principal, key, body, heartbeat=False):
        require_permission(principal, "agent:presence:write")
        scope = "presenceHeartbeat" if heartbeat else "setPresence"
        async with self.db.transaction() as conn:
            cached = await staff_dedupe(conn, principal, scope, key, body)
            staff = (await self._staff_locks(conn, [principal.id]))[principal.id]
            if cached:
                return cached
            if not heartbeat:
                check_version(staff, body["expected_version"])
            mode = presence(staff) if heartbeat else body["presence"]
            row = await one(
                conn,
                """UPDATE ag_staff_profile SET presence=CAST(:mode AS text),heartbeat_at=now(),
                presence_expires_at=CASE WHEN CAST(:mode AS text)='OFFLINE' THEN NULL ELSE now()+make_interval(secs=>:seconds) END,
                updated_at=now(),version=version+1 WHERE admin_id=:id RETURNING *""",
                id=principal.id,
                mode=mode,
                seconds=(await read_policy(conn))["value"]["presence_timeout_seconds"],
            )
            result = await staff_view(conn, row)
            await save_command(conn, principal, scope, key, body, result, principal.id)
            return result

    async def list_staff(self, principal, limit, cursor):
        require_permission(principal, "agent:staff:read")
        scope = f"staff:{principal.id}"
        before = self.cursors.decode(cursor, scope) if cursor else None
        async with self.db.transaction(snapshot=True) as conn:
            data = await rows(
                conn,
                "SELECT * FROM ag_staff_profile WHERE admin_id>:id ORDER BY admin_id LIMIT :limit",
                id=int(before[0]) if before else 0,
                limit=limit + 1,
            )
            page = data[:limit]
            return {
                "items": [await staff_view(conn, row) for row in page],
                "has_more": len(data) > limit,
                "next_cursor": self.cursors.encode([str(page[-1]["admin_id"])], scope)
                if len(data) > limit
                else None,
            }

    async def update_profile(self, principal, sid, key, body):
        require_permission(principal, "agent:staff:manage")
        scope = f"updateStaffProfile:{sid}"
        async with self.db.transaction() as conn:
            cached = await staff_dedupe(conn, principal, scope, key, body)
            row = (await self._staff_locks(conn, [sid]))[sid]
            if cached:
                return cached
            check_version(row, body["expected_version"])
            view = await staff_view(conn, row)
            if body["capacity"] < view["active_count"]:
                raise DomainError("STAFF_CAPACITY_REACHED", "容量不能低于当前接待数量")
            row = await one(
                conn,
                "UPDATE ag_staff_profile SET display_name=:name,capacity=:capacity,version=version+1,updated_at=now() WHERE admin_id=:id RETURNING *",
                id=sid,
                name=body["display_name"],
                capacity=body["capacity"],
            )
            result = await staff_view(conn, row)
            await save_command(conn, principal, scope, key, body, result, sid)
            return result

    async def list(self, principal, bucket, query, limit, cursor, queue=False):
        require_permission(principal, "agent:handoff:claim" if queue else "agent:conversation:read")
        if bucket == "WAITING":
            require_permission(principal, "agent:handoff:claim")
        scope = f"reception:{principal.id}:{bucket}:{query}:{queue}:" + ",".join(
            sorted(principal.permissions)
        )
        before = self.cursors.decode(cursor, scope) if cursor else None
        ascending = bucket == "WAITING"
        date = "c.waiting_since" if ascending else "c.updated_at"
        condition = {
            "WAITING": "c.state='WAITING_HUMAN'",
            "MINE": "c.state='HUMAN_ACTIVE' AND c.assigned_admin_id=:actor",
            "HISTORY": "c.state='CLOSED' AND (c.assigned_admin_id=:actor OR :broad)",
        }[bucket]
        async with self.db.transaction(snapshot=True) as conn:
            data = await rows(
                conn,
                f"""SELECT c.*,h.id AS handoff_id FROM ag_conversation c
                LEFT JOIN LATERAL (SELECT id FROM ag_handoff WHERE conversation_id=c.id ORDER BY created_at DESC,id DESC LIMIT 1) h ON true
                WHERE c.deleted_at IS NULL AND {condition} AND (:query='' OR c.title ILIKE :pattern)
                AND (CAST(:date AS timestamptz) IS NULL OR ({date},c.id){">" if ascending else "<"}(CAST(:date AS timestamptz),CAST(:id AS uuid)))
                ORDER BY {date} {"ASC" if ascending else "DESC"},c.id {"ASC" if ascending else "DESC"} LIMIT :limit""",
                actor=principal.id,
                broad="agent:conversation:history:all" in principal.permissions,
                query=query,
                pattern=f"%{query}%",
                date=before[0] if before else None,
                id=before[1] if before else None,
                limit=limit + 1,
            )
            page = data[:limit]
            items = []
            for row in page:
                if queue:
                    handoff = await one(conn, "SELECT * FROM ag_handoff WHERE id=:id", id=row["handoff_id"])
                    items.append(handoff_view(handoff, row))
                else:
                    items.append(await conversation_projection(conn, row, actor=principal))
            clock = await one(conn, "SELECT last_event_id FROM ag_event_clock WHERE singleton_id=1")
            return {
                "items": items,
                "has_more": len(data) > limit,
                "next_cursor": self.cursors.encode(
                    [
                        page[-1]["waiting_since" if ascending else "updated_at"].isoformat(),
                        str(page[-1]["id"]),
                    ],
                    scope,
                )
                if len(data) > limit
                else None,
                "event_cursor": str(clock["last_event_id"]),
            }

    async def snapshot(self, principal, cid):
        async with self.db.transaction(snapshot=True) as conn:
            conv = await visible_conversation(conn, principal, cid)
            return await self.conversations.snapshot_in(
                conn, conv, f"staff-messages:{principal.id}:{cid}", actor=principal
            )

    async def mark_read(self, principal, cid, key, body):
        from app.application.read_positions import advance_read

        scope = f"adminReadCursor:{cid}"
        async with self.db.transaction() as conn:
            cached = await staff_dedupe(conn, principal, scope, key, body)
            conv = await visible_conversation(conn, principal, cid, lock=True)
            if cached is not None:
                return cached
            result, changed = await advance_read(conn, cid, "STAFF", principal.id, int(body["last_seq"]))
            await save_command(conn, principal, scope, key, body, result, cid)
            if changed:
                event_id = await append_events(conn, conv, [("read.updated", result)])
                await execute(
                    conn,
                    "UPDATE ag_event_outbox SET visibility='STAFF_ONLY',required_permission='agent:conversation:read' WHERE event_id=:id",
                    id=int(event_id),
                )
            return result

    async def history(self, principal, cid, limit, cursor, notes=False):
        require_permission(principal, "agent:conversation:note" if notes else "agent:conversation:read")
        scope = f"{'notes' if notes else 'staff-messages'}:{principal.id}:{cid}"
        before = self.cursors.decode(cursor, scope) if cursor else None
        async with self.db.transaction(snapshot=True) as conn:
            await visible_conversation(conn, principal, cid)
            if notes:
                data = await rows(
                    conn,
                    f"SELECT * FROM ag_staff_note n WHERE conversation_id=:cid AND {deadline_sql('n')}>now() AND (CAST(:date AS timestamptz) IS NULL OR (created_at,id)<(CAST(:date AS timestamptz),CAST(:id AS uuid))) ORDER BY created_at DESC,id DESC LIMIT :limit",
                    cid=cid,
                    date=before[0] if before else None,
                    id=before[1] if before else None,
                    limit=limit + 1,
                )
                page = data[:limit]
                next_position = [page[-1]["created_at"].isoformat(), str(page[-1]["id"])] if page else []
                items = [note_view(row) for row in page]
            else:
                data = await rows(
                    conn,
                    f"SELECT * FROM ag_message m WHERE conversation_id=:cid AND {visible_sql()} AND seq<:seq ORDER BY seq DESC LIMIT :limit",
                    cid=cid,
                    seq=int(before[0]) if before else 9223372036854775807,
                    limit=limit + 1,
                )
                page = data[:limit]
                next_position = [page[-1]["seq"]] if page else []
                items = await messages_projection(conn, list(reversed(page)))
            result = {
                "items": items,
                "has_more": len(data) > limit,
                "next_cursor": self.cursors.encode(next_position, scope) if len(data) > limit else None,
            }
            if notes:
                row = await one(
                    conn,
                    "SELECT now()-make_interval(days=>:days) cutoff",
                    days=(await read_policy(conn))["value"]["message_retention_days"],
                )
                result["retention_cutoff"] = row["cutoff"]
            return result


def note_view(row):
    return {
        "id": str(row["id"]),
        "admin_id": str(row["admin_id"]),
        "body": row["body"],
        "created_at": row["created_at"],
        "retention_until": row["retention_until"],
    }
