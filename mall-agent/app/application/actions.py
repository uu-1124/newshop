"""A buyer click commits intent once; recovery can only reconcile the same operation."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from app.application.conversations import dedupe, owned, save_dedupe
from app.application.expiry import deadline_sql
from app.application.governance import read_policy
from app.application.orders import validate_cancellation_quote
from app.application.runtime_fence import assert_runtime_fence
from app.domain.identity import DomainError
from app.infrastructure.action_security import ActionSecurity, parameter_hash
from app.infrastructure.postgres.database import append_events, encoded, execute, one, rows


def action_view(row):
    result = {
        key: row[key]
        for key in (
            "action_type",
            "state",
            "version",
            "conversation_version",
            "parameters",
            "expires_at",
            "result",
            "error_code",
            "control_epoch",
        )
    } | {"id": str(row["id"]), "conversation_id": str(row["conversation_id"]), "quote": row["quote_snapshot"]}
    if result["state"] == "PENDING_CONFIRMATION" and result["expires_at"] <= datetime.now(UTC):
        result["state"] = "EXPIRED"
    return result


async def audit(conn, principal, operation, resource, request_id, audit_id=None, details=None):
    identifier = audit_id or uuid4()
    await execute(
        conn,
        """INSERT INTO ag_audit_log(id,actor_kind,actor_id,operation,resource_type,resource_id,request_id,outcome,details)
        VALUES (:id,'MEMBER',:actor,:operation,'ACTION',:resource,:request,'SUCCEEDED',CAST(:details AS jsonb))""",
        id=identifier,
        actor=principal.id,
        operation=operation,
        resource=str(resource),
        request=request_id,
        details=encoded(details or {}),
    )
    return identifier


def ai_state(conv):
    return conv["state"] == "AI_ACTIVE" or (conv["state"] == "WAITING_HUMAN" and conv["waiting_ai_enabled"])


class Actions:
    def __init__(self, db, bridge, settings):
        self.db, self.bridge, self.settings = db, bridge, settings
        self.security = ActionSecurity(settings)

    async def _load(self, conn, principal, aid, lock=False):
        raw = await one(
            conn,
            "SELECT conversation_id FROM ag_action WHERE id=:id AND member_id=:member",
            id=aid,
            member=principal.id,
        )
        if not raw:
            raise DomainError("NOT_FOUND", "操作不存在", 404)
        conv = await owned(conn, raw["conversation_id"], principal.id, lock)
        action = await one(
            conn,
            "SELECT * FROM ag_action WHERE id=:id" + (" FOR UPDATE" if lock else ""),
            id=aid,
        )
        return conv, action

    async def get(self, principal, aid):
        async with self.db.transaction() as conn:
            conv, action = await self._load(conn, principal, aid, True)
            return await self._current_view(conn, conv, action)

    async def _current_view(self, conn, conv, action):
        # Read the clock after both row locks have been acquired. A SELECT
        # expression (even clock_timestamp()) can be evaluated before its wait.
        expired = action["expires_at"] <= datetime.now(UTC)
        if action["state"] == "PENDING_CONFIRMATION" and (
            expired or action["control_epoch"] != conv["control_epoch"] or not ai_state(conv)
        ):
            state = "EXPIRED" if expired else "STALE"
            action = await one(
                conn,
                "UPDATE ag_action SET state=:state,version=version+1,updated_at=now() WHERE id=:id RETURNING *",
                id=action["id"],
                state=state,
            )
            await append_events(conn, conv, [("action.updated", {"action": action_view(action)})])
        return action_view(action)

    async def _prepared_replay(self, conn, principal, cached, runtime_fence):
        # Dedupe identifies the original preparation; current state is authoritative.
        conv, action = await self._load(conn, principal, UUID(cached["id"]), True)
        await assert_runtime_fence(conn, conv, runtime_fence)
        return await self._current_view(conn, conv, action)

    async def checkout_options(self, principal, token, body, key):
        if not self.settings.orders_enabled:
            raise DomainError("FEATURE_NOT_ACTIVE", "订单准备尚未开放", 409)
        return await self.bridge.checkout_options(token, body, key)

    async def prepare(
        self, principal, token, cid, key, body, runtime_fence=None, validate_member_choice=None
    ):
        action_type = body["parameters"]["action_type"]
        if (
            action_type in {"CREATE_ORDER", "CANCEL_ORDER", "CREATE_RETURN"}
            and not self.settings.orders_enabled
        ):
            raise DomainError("FEATURE_NOT_ACTIVE", "订单准备尚未开放", 409)
        scope = f"prepareAction:{cid}"
        # Auth and state preflight precede Java IO. Recheck under lock after IO.
        async with self.db.transaction() as conn:
            cached = await dedupe(conn, scope, principal.id, key, body)
            conv = await owned(conn, cid, principal.id, lock=runtime_fence is not None)
            await assert_runtime_fence(conn, conv, runtime_fence)
            if cached:
                return await self._prepared_replay(conn, principal, cached, runtime_fence)
            self._prepare_state(conv, body)
            policy = (await read_policy(conn))["value"]
            prepared_at = datetime.now(UTC)
        quote = await self.bridge.quote(token, body["parameters"], key)
        validate_cancellation_quote(quote, body["parameters"])
        if quote.get("parameters") != body["parameters"] or quote.get("parameters_hash") != parameter_hash(
            principal.id, body["parameters"]
        ):
            raise DomainError("JAVA_PROTOCOL_ERROR", "商城准备结果不匹配", 503)
        try:
            expiry = datetime.fromisoformat(quote["expires_at"].replace("Z", "+00:00"))
            expiry = min(expiry, prepared_at + timedelta(seconds=policy["action_ttl_seconds"]))
            expected_lines = (
                1
                if action_type == "CREATE_RETURN"
                else 0
                if action_type == "CANCEL_ORDER"
                else len(body["parameters"].get("items", body["parameters"].get("cart_item_ids", [])))
            )
            if expiry <= datetime.now(UTC) or len(quote["lines"]) != expected_lines:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise DomainError("QUOTE_EXPIRED", "商品准备结果已失效，请重新准备") from None
        async with self.db.transaction() as conn:
            await execute(conn, "SELECT pg_advisory_xact_lock(:member)", member=principal.id)
            cached = await dedupe(conn, scope, principal.id, key, body)
            conv = await owned(conn, cid, principal.id, True)
            await assert_runtime_fence(conn, conv, runtime_fence)
            if cached:
                return await self._prepared_replay(conn, principal, cached, runtime_fence)
            self._prepare_state(conv, body)
            if validate_member_choice is not None:
                # Internal, process-local validation after quote IO and row-lock
                # waits. Never persist callbacks or accept them from HTTP input.
                await validate_member_choice(conn, conv)
            if await one(
                conn,
                "SELECT id FROM ag_action WHERE member_id=:member AND state IN ('EXECUTING','UNKNOWN')",
                member=principal.id,
            ):
                raise DomainError("ACTION_UNRESOLVED", "已有操作正在核对，请先等待原操作结果")
            old = await rows(
                conn,
                "UPDATE ag_action SET state='STALE',version=version+1,updated_at=now() WHERE conversation_id=:id AND state='PENDING_CONFIRMATION' RETURNING *",
                id=cid,
            )
            aid = uuid4()
            action = await one(
                conn,
                """INSERT INTO ag_action(id,conversation_id,member_id,action_type,conversation_version,parameters,parameters_hash,quote_id,operation_no,expires_at,control_epoch,quote_snapshot)
                VALUES (:id,:cid,:member,:type,:version,CAST(:parameters AS jsonb),:hash,:quote,:id,:expiry,:epoch,CAST(:snapshot AS jsonb)) RETURNING *""",
                id=aid,
                cid=cid,
                member=principal.id,
                type=action_type,
                version=conv["version"],
                parameters=encoded(body["parameters"]),
                hash=quote["parameters_hash"],
                quote=quote["quote_id"],
                expiry=expiry,
                epoch=conv["control_epoch"],
                snapshot=encoded(quote),
            )
            if action_type == "CREATE_RETURN":
                requested = sorted(body["parameters"]["attachment_ids"])
                locked = (
                    await rows(
                        conn,
                        "SELECT id FROM ag_attachment WHERE id=ANY(CAST(:ids AS uuid[])) AND owner_kind='MEMBER' AND owner_id=:member AND purpose='CHAT_IMAGE' AND state='READY' ORDER BY id FOR UPDATE",
                        ids=requested,
                        member=principal.id,
                    )
                    if requested
                    else []
                )
                # A policy may shorten while this command waits on attachment
                # locks. Use a fresh statement after all locks for that policy.
                eligible = (
                    await rows(
                        conn,
                        f"SELECT a.id FROM ag_attachment a WHERE id=ANY(CAST(:ids AS uuid[])) AND {deadline_sql('a', 'image_retention_days')}>now()",
                        ids=[item["id"] for item in locked],
                    )
                    if locked
                    else []
                )
                if len(eligible) != len(requested):
                    raise DomainError("ATTACHMENT_UNAVAILABLE", "退货凭证不可用，请重新上传", 404)
                for attachment_id in requested:
                    await execute(
                        conn,
                        "INSERT INTO ag_attachment_ref(id,attachment_id,ref_kind,action_id,protected) VALUES(:id,CAST(:attachment AS uuid),'RETURN_PROOF',:action,true)",
                        id=uuid4(),
                        attachment=attachment_id,
                        action=aid,
                    )
            # Member/row/proof locks may outlast the quote. Roll back this whole
            # preparation, including replacement invalidation and proof refs,
            # rather than publishing a card which was already expired.
            conv = await one(
                conn,
                "UPDATE ag_conversation SET next_message_seq=next_message_seq+1,version=version+1,updated_at=now() WHERE id=:id AND :expiry>clock_timestamp() RETURNING *",
                id=cid,
                expiry=expiry,
            )
            if not conv:
                raise DomainError("QUOTE_EXPIRED", "确认内容已过期，请重新准备")
            result = action_view(action)
            message_id = uuid4()
            await execute(
                conn,
                """INSERT INTO ag_message(id,conversation_id,seq,sender_kind,sender_id,sender_display_name,status,text_content,blocks,retention_until)
                VALUES (:id,:cid,:seq,'SYSTEM',0,'操作确认','COMPLETED',:text,CAST(:blocks AS jsonb),now()+make_interval(days=>:days))""",
                days=policy["message_retention_days"],
                id=message_id,
                cid=cid,
                seq=conv["next_message_seq"] - 1,
                text={
                    "CREATE_ORDER": "请核对商品、地址、优惠和应付金额，再确认创建待付款订单。支付仅为演示。",
                    "CANCEL_ORDER": "请核对订单编号，再确认取消未付款订单。",
                    "CREATE_RETURN": "请核对订单明细、数量、原因与申请金额，再确认提交退货申请。退款仅为演示，须员工另行审核。",
                }.get(action_type, "请核对商品、规格和数量，再确认加入购物车。"),
                blocks=encoded([{"type": "action", "action": result}]),
            )
            await execute(
                conn, "UPDATE ag_action SET message_id=:message WHERE id=:id", id=aid, message=message_id
            )
            await save_dedupe(conn, scope, principal.id, key, body, result)
            await audit(conn, principal, "prepareAction", aid, key)
            await append_events(
                conn,
                conv,
                [("action.updated", {"action": action_view(row)}) for row in old]
                + [("action.updated", {"action": result})],
            )
            return result

    def _prepare_state(self, conv, body):
        if conv["version"] != body["expected_conversation_version"]:
            raise DomainError("VERSION_CONFLICT", "会话状态已变化，请刷新")
        if not ai_state(conv):
            raise DomainError("ACTION_STATE_CONFLICT", "当前会话不能准备 AI 操作")

    async def cancel(self, principal, aid, key, body):
        scope = f"cancelAction:{aid}"
        async with self.db.transaction() as conn:
            cached = await dedupe(conn, scope, principal.id, key, body)
            conv, action = await self._load(conn, principal, aid, True)
            if cached:
                return cached
            if action["version"] != body["expected_version"]:
                raise DomainError("VERSION_CONFLICT", "操作状态已变化，请刷新")
            if action["state"] != "PENDING_CONFIRMATION":
                raise DomainError("ACTION_STATE_CONFLICT", "此操作不能取消")
            action = await one(
                conn,
                "UPDATE ag_action SET state='CANCELLED',version=version+1,updated_at=now() WHERE id=:id RETURNING *",
                id=aid,
            )
            await execute(
                conn,
                "UPDATE ag_attachment_ref SET protected=false,released_at=now(),version=version+1 WHERE action_id=:id AND ref_kind='RETURN_PROOF'",
                id=aid,
            )
            result = action_view(action)
            await save_dedupe(conn, scope, principal.id, key, body, result)
            await audit(conn, principal, "cancelAction", aid, key)
            await append_events(conn, conv, [("action.updated", {"action": result})])
            return result

    async def confirm(self, principal, token, aid, key, body):
        scope = f"confirmAction:{aid}"
        owner = "confirm-" + str(uuid4())
        async with self.db.transaction() as conn:
            cached = await dedupe(conn, scope, principal.id, key, body)
            conv, action = await self._load(conn, principal, aid, True)
            if cached:
                return action_view(action)
            if action["version"] != body["expected_version"]:
                raise DomainError("VERSION_CONFLICT", "操作状态已变化，请刷新")
            if (
                action["state"] != "PENDING_CONFIRMATION"
                or action["expires_at"] <= datetime.now(UTC)
                or not ai_state(conv)
                or action["control_epoch"] != conv["control_epoch"]
            ):
                raise DomainError("ACTION_STATE_CONFLICT", "确认卡已失效或操作已提交，请刷新")
            try:
                validate_cancellation_quote(action["quote_snapshot"], action["parameters"])
            except DomainError:
                raise DomainError(
                    "ACTION_STATE_CONFLICT", "此旧确认卡缺少可信订单号或金额，请重新读取订单并准备取消卡"
                ) from None
            proof_rows = await rows(
                conn,
                "SELECT a.*,r.id AS ref_id,r.attachment_id FROM ag_attachment_ref r JOIN ag_attachment a ON a.id=r.attachment_id WHERE r.action_id=:id AND r.ref_kind='RETURN_PROOF' AND r.protected AND r.released_at IS NULL AND a.state='READY'",
                id=aid,
            )
            if action["action_type"] == "CREATE_RETURN" and len(proof_rows) != len(
                action["parameters"]["attachment_ids"]
            ):
                raise DomainError("ATTACHMENT_UNAVAILABLE", "退货凭证已失效，请重新准备", 409)
            proofs = [self.security.proof(action, item) for item in proof_rows]
            audit_id = uuid4()
            action = await one(
                conn,
                "UPDATE ag_action SET state='EXECUTING',confirmed_at=clock_timestamp(),version=version+1,updated_at=now() WHERE id=:id AND expires_at>clock_timestamp() RETURNING *",
                id=aid,
            )
            if not action:
                raise DomainError("ACTION_STATE_CONFLICT", "确认卡已过期，请重新准备")
            confirmed_at = action["confirmed_at"]
            receipt, encrypted, expiry = self.security.issue(action, audit_id, confirmed_at)
            # The audit FK and grant are committed with the single-use dispatch fact.
            await audit(
                conn,
                principal,
                "confirmAction",
                aid,
                key,
                audit_id,
                {
                    "confirmed_at": confirmed_at.isoformat(),
                    "parameters_hash": action["parameters_hash"],
                    "control_epoch": action["control_epoch"],
                },
            )
            dispatch = await one(
                conn,
                """INSERT INTO ag_action_dispatch(action_id,state,confirmation_audit_id,reconcile_grant_ciphertext,grant_key_version,grant_expires_at,dispatch_started_at,next_attempt_at,lease_owner,lease_until)
                VALUES (:id,'DISPATCHING',:audit,:grant,'v1',:expiry,now(),now()+interval '30 seconds',:owner,now()+interval '30 seconds') RETURNING *""",
                id=aid,
                audit=audit_id,
                grant=encrypted,
                expiry=expiry,
                owner=owner,
            )
            result = action_view(action)
            await save_dedupe(conn, scope, principal.id, key, body, result)
            await append_events(conn, conv, [("action.updated", {"action": result})])
        # Exactly one invocation by this first sender, outside all SQL locks.
        # Client cancellation / process death leaves durable evidence for recovery.
        try:
            business = await self.bridge.execute_action(
                token,
                {
                    "operation_no": str(action["operation_no"]),
                    "quote_id": str(action["quote_id"]),
                    "parameters_hash": action["parameters_hash"],
                    "parameters": action["parameters"],
                    "confirmation_receipt": receipt,
                    "proofs": proofs,
                },
            )
        except DomainError:
            business = None
        await self._record(action, dispatch, business)
        return await self.get(principal, aid)

    async def _record(self, action, claimed, business, reconciliation_blocked=None):
        async with self.db.transaction() as conn:
            conv = await one(
                conn, "SELECT * FROM ag_conversation WHERE id=:id FOR UPDATE", id=action["conversation_id"]
            )
            current = await one(conn, "SELECT * FROM ag_action WHERE id=:id FOR UPDATE", id=action["id"])
            dispatch = await one(
                conn,
                "SELECT *,lease_until>now() AS lease_valid FROM ag_action_dispatch WHERE action_id=:id FOR UPDATE",
                id=action["id"],
            )
            if (
                current["state"] not in {"EXECUTING", "UNKNOWN"}
                or dispatch["fencing_token"] != claimed["fencing_token"]
                or dispatch["lease_owner"] != claimed["lease_owner"]
                or not dispatch["lease_valid"]
            ):
                return
            valid = (
                business
                and business.get("operation_no") == str(action["operation_no"])
                and business.get("state") in {"SUCCEEDED", "REJECTED"}
            )
            state = ("SUCCEEDED" if business["state"] == "SUCCEEDED" else "FAILED") if valid else "UNKNOWN"
            if valid and action["action_type"] == "CREATE_RETURN":
                if state == "SUCCEEDED":
                    await execute(
                        conn,
                        "UPDATE ag_attachment_ref SET return_apply_id=:return_id,version=version+1 WHERE action_id=:id AND ref_kind='RETURN_PROOF'",
                        id=action["id"],
                        return_id=int(business["business_id"]),
                    )
                else:
                    await execute(
                        conn,
                        "UPDATE ag_attachment_ref SET protected=false,released_at=now(),version=version+1 WHERE action_id=:id AND ref_kind='RETURN_PROOF'",
                        id=action["id"],
                    )
            error = (
                business.get("error_code") if valid else reconciliation_blocked or "OPERATION_RESULT_PENDING"
            )
            current = await one(
                conn,
                "UPDATE ag_action SET state=:state,result=CAST(:result AS jsonb),error_code=:error,reconcile_after=CASE WHEN :terminal THEN NULL ELSE now()+interval '2 seconds' END,updated_at=now(),version=version+1 WHERE id=:id RETURNING *",
                id=action["id"],
                state=state,
                result=encoded(business) if valid else None,
                error=error,
                terminal=bool(valid),
            )
            delay = min(300, 2 ** min(dispatch["attempts"] + 1, 8))
            await execute(
                conn,
                "UPDATE ag_action_dispatch SET state=:state,lease_owner=NULL,lease_until=NULL,next_attempt_at=now()+make_interval(secs=>:delay),last_error_code=:error,version=version+1,updated_at=now() WHERE action_id=:id",
                id=action["id"],
                state="TERMINAL" if valid else "MANUAL_REQUIRED" if reconciliation_blocked else "RECONCILING",
                error=error,
                delay=delay,
            )
            await append_events(conn, conv, [("action.updated", {"action": action_view(current)})])

    async def reconcile_due(self):
        async with self.db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_attachment_ref r SET protected=false,released_at=now(),version=r.version+1 FROM ag_action a WHERE a.id=r.action_id AND r.ref_kind='RETURN_PROOF' AND r.return_apply_id IS NULL AND r.released_at IS NULL AND (a.state IN ('FAILED','CANCELLED','EXPIRED','STALE') OR (a.state='PENDING_CONFIRMATION' AND a.expires_at<=now()))",
            )
            candidates = await rows(
                conn,
                """SELECT a.id,a.conversation_id FROM ag_action a JOIN ag_action_dispatch d ON d.action_id=a.id
                WHERE a.state IN ('EXECUTING','UNKNOWN') AND d.state IN ('READY','DISPATCHING','RECONCILING')
                AND d.next_attempt_at<=now() AND (d.lease_until IS NULL OR d.lease_until<=now()) ORDER BY d.next_attempt_at,a.id LIMIT 5""",
            )
        for candidate in candidates:
            async with self.db.transaction() as conn:
                conv = await one(
                    conn,
                    "SELECT * FROM ag_conversation WHERE id=:id FOR UPDATE SKIP LOCKED",
                    id=candidate["conversation_id"],
                )
                if not conv:
                    continue
                action = await one(
                    conn, "SELECT * FROM ag_action WHERE id=:id FOR UPDATE", id=candidate["id"]
                )
                dispatch = await one(
                    conn,
                    "SELECT *,next_attempt_at<=now() AND (lease_until IS NULL OR lease_until<=now()) AS due,grant_expires_at<=now() AS expired FROM ag_action_dispatch WHERE action_id=:id FOR UPDATE",
                    id=candidate["id"],
                )
                if (
                    not dispatch["due"]
                    or action["state"] not in {"EXECUTING", "UNKNOWN"}
                    or dispatch["state"] not in {"READY", "DISPATCHING", "RECONCILING"}
                ):
                    continue
                if dispatch["expired"]:
                    await execute(
                        conn,
                        "UPDATE ag_action_dispatch SET state='MANUAL_REQUIRED',last_error_code='RECONCILE_GRANT_EXPIRED',version=version+1 WHERE action_id=:id",
                        id=action["id"],
                    )
                    continue
                try:
                    grant = self.security.decrypt(dispatch["reconcile_grant_ciphertext"])
                except DomainError:
                    await execute(
                        conn,
                        "UPDATE ag_action_dispatch SET state='MANUAL_REQUIRED',last_error_code='RECONCILE_GRANT_UNAVAILABLE',version=version+1 WHERE action_id=:id",
                        id=action["id"],
                    )
                    continue
                claimed = await one(
                    conn,
                    "UPDATE ag_action_dispatch SET state='RECONCILING',lease_owner=:owner,lease_until=now()+interval '30 seconds',fencing_token=fencing_token+1,attempts=attempts+1,version=version+1,updated_at=now() WHERE action_id=:id RETURNING *",
                    id=action["id"],
                    owner="reconcile-" + str(uuid4()),
                )
            blocked = None
            try:
                business = await self.bridge.reconcile_action(
                    str(action["operation_no"]), action["parameters_hash"], grant
                )
            except DomainError as error:
                business = None
                if error.code in {
                    "CONFIRMATION_INVALID",
                    "RECONCILE_GRANT_EXPIRED",
                    "RECONCILE_GRANT_UNAVAILABLE",
                }:
                    blocked = "RECONCILE_GRANT_REJECTED"
            await self._record(action, claimed, business, blocked)
        return len(candidates)
