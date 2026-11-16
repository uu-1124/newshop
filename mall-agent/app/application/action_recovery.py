"""Staff may resume original-operation reconciliation; no new execution authority."""

from datetime import datetime

from app.application.commands import staff_dedupe
from app.application.governance import governance_audit
from app.domain.identity import DomainError
from app.infrastructure.action_security import ActionSecurity, parameter_hash
from app.infrastructure.postgres.database import digest, encoded, execute, one, rows
from app.infrastructure.staff_auth import require_permission


def recovery_view(row):
    return {
        "action_id": str(row["id"]),
        "operation_no": str(row["operation_no"]),
        "action_type": row["action_type"],
        "action_state": row["state"],
        "action_version": row["version"],
        "dispatch_state": row["dispatch_state"],
        "dispatch_version": row["dispatch_version"],
        "last_error_code": row["last_error_code"],
        "confirmed_at": row["confirmed_at"],
        "updated_at": row["updated_at"],
    }


class ActionRecovery:
    def __init__(self, db, settings):
        self.db, self.security = db, ActionSecurity(settings)

    async def listing(self, principal, before=None, limit=20):
        require_permission(principal, "agent:action:read")
        async with self.db.transaction(snapshot=True) as conn:
            data = await rows(
                conn,
                """SELECT a.*,d.state dispatch_state,d.version dispatch_version,d.last_error_code
                FROM ag_action a JOIN ag_action_dispatch d ON d.action_id=a.id
                WHERE a.state IN ('UNKNOWN','EXECUTING') AND (CAST(:before AS uuid) IS NULL OR (a.created_at,a.id)<
                    (SELECT created_at,id FROM ag_action WHERE id=CAST(:before AS uuid)))
                ORDER BY a.created_at DESC,a.id DESC LIMIT :limit""",
                before=before,
                limit=limit + 1,
            )
        return {
            "items": [recovery_view(r) for r in data[:limit]],
            "has_more": len(data) > limit,
            "next_before": str(data[limit - 1]["id"]) if len(data) > limit else None,
        }

    async def resume(self, principal, aid, key, body):
        require_permission(principal, "agent:action:reconcile")
        scope = f"resumeActionReconciliation:{aid}"
        async with self.db.transaction() as conn:
            cached = await staff_dedupe(conn, principal, scope, key, body)
            if cached:
                return cached
            ref = await one(conn, "SELECT conversation_id FROM ag_action WHERE id=:id", id=aid)
            if not ref:
                raise DomainError("NOT_FOUND", "操作不存在", 404)
            await one(
                conn, "SELECT id FROM ag_conversation WHERE id=:id FOR UPDATE", id=ref["conversation_id"]
            )
            action = await one(conn, "SELECT * FROM ag_action WHERE id=:id FOR UPDATE", id=aid)
            dispatch = await one(
                conn,
                "SELECT *,lease_until>now() leased FROM ag_action_dispatch WHERE action_id=:id FOR UPDATE",
                id=aid,
            )
            if not dispatch or action["state"] not in {"UNKNOWN", "EXECUTING"}:
                raise DomainError("INVALID_STATE", "该操作无需人工恢复核对", 409)
            if (
                action["version"] != body["expected_version"]
                or dispatch["version"] != body["expected_dispatch_version"]
            ):
                raise DomainError("VERSION_CONFLICT", "操作核对状态已变化，请刷新", 409)
            if dispatch["leased"]:
                raise DomainError("ACTION_RECONCILING", "操作仍在执行或核对中，请等待租约结束", 409)
            audit = await one(
                conn,
                """SELECT * FROM ag_audit_log WHERE id=:id AND operation='confirmAction'
                AND resource_id=:resource AND actor_kind='MEMBER' AND actor_id=:member AND outcome='SUCCEEDED'""",
                id=dispatch["confirmation_audit_id"],
                resource=str(aid),
                member=action["member_id"],
            )
            try:
                audited_time = datetime.fromisoformat(audit["details"]["confirmed_at"]) if audit else None
            except (KeyError, TypeError, ValueError):
                audited_time = None
            if (
                not audit
                or action["confirmed_at"] is None
                or audited_time != action["confirmed_at"]
                or str(action["operation_no"]) != str(action["id"])
                or action["parameters_hash"] != parameter_hash(action["member_id"], action["parameters"])
                or audit["details"].get("parameters_hash") != action["parameters_hash"]
                or audit["details"].get("control_epoch") != action["control_epoch"]
            ):
                raise DomainError(
                    "CONFIRMATION_EVIDENCE_INVALID", "原始确认依据不完整，保留人工状态并核实账本", 409
                )
            encrypted, expiry = self.security.renew_reconciliation(
                action, audit["id"], action["confirmed_at"]
            )
            dispatch = await one(
                conn,
                """UPDATE ag_action_dispatch SET state='READY',reconcile_grant_ciphertext=:grant,
                grant_expires_at=:expiry,grant_key_version='v1',lease_owner=NULL,lease_until=NULL,
                fencing_token=fencing_token+1,next_attempt_at=now(),last_error_code=NULL,version=version+1,updated_at=now()
                WHERE action_id=:id RETURNING *""",
                id=aid,
                grant=encrypted,
                expiry=expiry,
            )
            await governance_audit(
                conn,
                principal,
                "resumeActionReconciliation",
                str(aid),
                key,
                {
                    "reason": body["reason"],
                    "original_confirmation_audit_id": str(audit["id"]),
                    "operation_no": str(action["operation_no"]),
                    "authority": "RECONCILE_ONLY",
                    "grant_expires_at": expiry.isoformat(),
                },
            )
            result = {
                "action_id": str(aid),
                "action_state": action["state"],
                "dispatch_state": "READY",
                "dispatch_version": dispatch["version"],
                "reconciliation_only": True,
            }
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
            return result
