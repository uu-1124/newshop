"""Live scoped retention: fresh uploads, existing demo return quote, no submission.

Only newly uploaded test attachment deadlines are aged in PG. Full preview is
inspected before execution, so unrelated expired resources cannot be swept by
this acceptance. No policy change, model call, payment or refund is performed.
"""

import asyncio
import hashlib
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from expansion_client import ADMIN, MEMBER, api, login, wait_job
from PIL import Image
from smoke_order_lifecycle import mysql

from app.runtime import run

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "deployment/.run/completion-retention-protection-live-20261114.json"
sys.path.insert(0, str(ROOT / "deployment"))


def save(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def business_snapshot():
    with mysql() as db, db.cursor() as q:
        q.execute("START TRANSACTION READ ONLY")
        result = {}
        for name, query in [
            ("order", "SELECT * FROM oms_order WHERE id=116"),
            ("return", "SELECT * FROM oms_agent_return WHERE return_id=38"),
            ("stock", "SELECT * FROM pms_sku_stock WHERE id=239"),
            ("shipment", "SELECT * FROM oms_agent_shipment WHERE order_id=116"),
            (
                "nodes",
                "SELECT e.* FROM oms_agent_shipment_event e JOIN oms_agent_shipment s ON s.id=e.shipment_id WHERE s.order_id=116 ORDER BY e.id",
            ),
        ]:
            q.execute(query)
            result[name] = hashlib.sha256(
                json.dumps(q.fetchall(), sort_keys=True, default=str).encode()
            ).hexdigest()
        db.rollback()
        return result


def evidence(state, name):
    from backup_data import pg_connection

    with pg_connection(ROOT, True) as conn:
        rows = list(
            conn.execute(
                "SELECT id,state,storage_deleted_at,version,object_key FROM ag_attachment WHERE id=ANY(%s) ORDER BY id",
                ([state["proof_id"], state["ordinary_id"]],),
            )
        )
        refs = list(
            conn.execute(
                "SELECT attachment_id,protected,released_at,return_apply_id FROM ag_attachment_ref WHERE attachment_id=ANY(%s) ORDER BY attachment_id",
                ([state["proof_id"], state["ordinary_id"]],),
            )
        )
    state.setdefault("observations", {})[name] = {
        "attachments": rows,
        "refs": refs,
        "checked_at": datetime.now(UTC).isoformat(),
    }
    save(state)
    return {str(row["id"]): row for row in rows}


async def main():
    from backup_data import object_client, pg_connection

    state = (
        json.loads(STATE.read_text(encoding="utf-8"))
        if STATE.exists()
        else {"created_at": datetime.now(UTC).isoformat(), "keys": {}}
    )

    def key(name):
        if name not in state["keys"]:
            state["keys"][name] = str(uuid4())
            save(state)
        return state["keys"][name]

    async with (
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=60, trust_env=False) as buyer,
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=60, trust_env=False) as staff,
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=60, trust_env=False) as other,
    ):
        await login(buyer, username="agent-lifecycle-6c72a0f5")
        await login(staff, True)
        await login(other, username="member")
        policy = await api(staff, ADMIN + "/settings")
        state.setdefault("original_policy", policy)
        assert state["original_policy"] == policy
        state.setdefault("original_business", business_snapshot())
        save(state)
        if not state.get("conversation_id"):
            conv = await api(
                buyer,
                MEMBER + "/conversations",
                "POST",
                {"title": "[TEST ONLY] 附件留存与售后保护", "entry_context": {"kind": "GENERAL"}},
                key("conversation"),
                201,
            )
            state["conversation_id"] = conv["id"]
            save(state)
        cid = state["conversation_id"]
        for role, color in [("proof", "green"), ("ordinary", "orange")]:
            if state.get(role + "_id"):
                continue
            picture = io.BytesIO()
            Image.new("RGB", (32, 32), color).save(picture, format="PNG")
            response = await buyer.post(
                MEMBER + "/attachments",
                headers={"Idempotency-Key": key("upload:" + role)},
                data={"purpose": "CHAT_IMAGE"},
                files={"file": ("test-retention-" + role + ".png", picture.getvalue(), "image/png")},
            )
            assert response.status_code == 201
            state[role + "_id"] = response.json()["data"]["id"]
            save(state)
        if not state.get("action_id"):
            eligibility = await api(buyer, MEMBER + "/orders/116/items/156/return-eligibility")
            assert eligibility["eligible"] and eligibility["remaining_quantity"] == 1
            snap = await api(buyer, MEMBER + "/conversations/" + cid)
            action = await api(
                buyer,
                MEMBER + f"/conversations/{cid}/actions",
                "POST",
                {
                    "expected_conversation_version": snap["conversation"]["version"],
                    "parameters": {
                        "action_type": "CREATE_RETURN",
                        "order_id": "116",
                        "order_item_id": "156",
                        "quantity": 1,
                        "reason_code": eligibility["reason_codes"][0]["code"],
                        "description": "[TEST ONLY] 仅准备验证保护，不提交申请",
                        "attachment_ids": [state["proof_id"]],
                    },
                },
                key("prepare"),
                201,
            )
            state["action_id"] = action["id"]
            save(state)
        if not state.get("aged_at"):
            with pg_connection(ROOT) as conn:
                ref = conn.execute(
                    "SELECT a.id,c.title,a.member_id FROM ag_action a JOIN ag_conversation c ON c.id=a.conversation_id WHERE a.id=%s AND a.conversation_id=%s AND a.member_id=22",
                    (state["action_id"], cid),
                ).fetchone()
                assert ref and ref["title"] == "[TEST ONLY] 附件留存与售后保护"
                for aid in (state["proof_id"], state["ordinary_id"]):
                    rows = conn.execute(
                        "UPDATE ag_attachment SET retention_until=now()-interval '1 second' WHERE id=%s AND owner_kind='MEMBER' AND owner_id=22 AND original_name LIKE 'test-retention-%%' RETURNING id",
                        (aid,),
                    ).fetchall()
                    assert len(rows) == 1
            state["aged_at"] = datetime.now(UTC).isoformat()
            save(state)

        async def cycle(label, expected):
            if not state.get(label + "_preview"):
                preview = await api(
                    staff,
                    ADMIN + "/retention/preview",
                    "POST",
                    {"proposed_settings": policy["value"]},
                    key(label + ":preview"),
                    202,
                )
                state[label + "_preview"] = preview["id"]
                save(state)
            preview = await wait_job(staff, state[label + "_preview"])
            summary = preview["result"]
            assert (
                summary["messages"] == 0
                and summary["notes"] == 0
                and summary["attachments"] == (2 if label == "ordinary" else 1)
                and summary["protected_attachments"] == (1 if label == "ordinary" else 0)
            ), "Only one dedicated attachment may be scheduled"
            # Require the unprotected expired candidate set to equal the exact fixture.
            with pg_connection(ROOT, True) as conn:
                candidates = conn.execute(
                    "SELECT a.id FROM ag_attachment a WHERE a.state IN ('UPLOADING','FAILED','READY') AND a.created_at<=now() AND (a.state<>'UPLOADING' OR a.updated_at<now()-interval '10 minutes') AND NOT EXISTS(SELECT 1 FROM ag_attachment_ref r WHERE r.attachment_id=a.id AND (r.protected OR r.released_at IS NULL)) AND (LEAST(a.retention_until,a.created_at+make_interval(days=>CASE a.purpose WHEN 'RECORDING' THEN %s WHEN 'SPEECH' THEN %s ELSE %s END))<=now() OR (a.created_at<now()-interval '1 day' AND (a.state IN ('UPLOADING','FAILED') OR a.purpose='KNOWLEDGE'))) ORDER BY a.id",
                    (
                        policy["value"]["audio_retention_days"],
                        policy["value"]["tts_retention_days"],
                        policy["value"]["image_retention_days"],
                    ),
                ).fetchall()
            if not state.get(label + "_execute"):
                assert [str(r["id"]) for r in candidates] == [expected]
            if not state.get(label + "_execute"):
                execution = await api(
                    staff,
                    ADMIN + "/retention/execute",
                    "POST",
                    {"preview_job_id": preview["id"], "expected_settings_version": policy["version"]},
                    key(label + ":execute"),
                    202,
                )
                state[label + "_execute"] = execution["id"]
                save(state)
            execution = await wait_job(staff, state[label + "_execute"])
            assert execution["result"]["attachments_scheduled"] == 1
            for _ in range(90):
                rows = evidence(state, label)
                if rows[expected]["state"] == "DELETED":
                    break
                await asyncio.sleep(1)
            assert rows[expected]["state"] == "DELETED" and rows[expected]["storage_deleted_at"]
            client, bucket = object_client(ROOT)
            try:
                try:
                    client.stat_object(bucket, rows[expected]["object_key"])
                    raise AssertionError("Physical private object must be gone")
                except Exception as error:
                    assert getattr(error, "code", "") in {"NoSuchKey", "NoSuchObject"}
            finally:
                client._http.clear()
            state[label + "_physical_missing"] = True
            save(state)

        if not state.get("ordinary_physical_missing"):
            assert (
                await buyer.get(MEMBER + f"/attachments/{state['ordinary_id']}/content")
            ).status_code == 404
            assert (await buyer.get(MEMBER + f"/attachments/{state['proof_id']}/content")).status_code == 200
            assert (await other.get(MEMBER + f"/attachments/{state['proof_id']}/content")).status_code == 404
            await cycle("ordinary", state["ordinary_id"])
            rows = evidence(state, "after_ordinary_cleanup")
            assert rows[state["proof_id"]]["state"] == "READY"
            assert (await buyer.get(MEMBER + f"/attachments/{state['proof_id']}/content")).status_code == 200
        action = await api(buyer, MEMBER + "/actions/" + state["action_id"])
        if action["state"] == "PENDING_CONFIRMATION":
            await api(
                buyer,
                MEMBER + f"/actions/{action['id']}/cancel",
                "POST",
                {"expected_version": action["version"]},
                key("cancel"),
            )
        else:
            assert action["state"] in {"CANCELLED", "EXPIRED"}
        if not state.get("proof_physical_missing"):
            await cycle("proof", state["proof_id"])
        assert business_snapshot() == state["original_business"]
        assert await api(staff, ADMIN + "/settings") == state["original_policy"]
        snap = await buyer.get(MEMBER + "/conversations/" + cid)
        if snap.status_code == 200:
            await api(
                buyer,
                MEMBER + "/conversations/" + cid,
                "DELETE",
                {"expected_version": snap.json()["data"]["conversation"]["version"]},
                key("delete"),
            )
        assert (await buyer.get(MEMBER + "/conversations/" + cid)).status_code == 404
        state["result"] = "PASS"
        state["cleanup"] = {
            "both_objects_physically_deleted": True,
            "conversation_deleted": True,
            "return_submitted": False,
            "business_fingerprints_unchanged": True,
            "policy_unchanged": True,
            "paid_model_calls": 0,
        }
        save(state)
        print(json.dumps(state["cleanup"]))


if __name__ == "__main__":
    try:
        run(main())
    except Exception as error:
        print("RETENTION_ACCEPTANCE_FAILED type=" + type(error).__name__)
        raise SystemExit(1)
