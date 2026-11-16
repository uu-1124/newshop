"""Create bounded, labelled query fixtures through existing APIs; never call a model."""
# ruff: noqa: E402

import argparse
import hashlib
import json
import secrets
import sys
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pymysql

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "deployment"), str(Path(__file__).parent)]
from backup_data import pg_connection
from expansion_client import ADMIN, MEMBER, api, login

from app.runtime import run

BUYERS = [
    "agent-order-af9d323d10",
    "agent-order-d6695cb5e6",
    "agent-order-00ca27bf08",
    "agent-order-ab2d711b5c",
    "agent-order-826885e839",
    "agent-pricing-68455e784b",
    "agent-conversation-bd63b203",
    "agent-lifecycle-c1eeefdd",
    "agent-lifecycle-6c72a0f5",
    "member",
]


def location(path):
    value = Path(path).resolve()
    root = (ROOT / "deployment/.run").resolve()
    if not value.is_relative_to(root) or value == root:
        raise ValueError("Private benchmark files must stay in deployment/.run")
    return value


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def fingerprint(state):
    ids = [b["conversation_id"] for b in state["buyers"]]
    with pg_connection(ROOT, True) as conn:
        conversations = conn.execute(
            "SELECT id,member_id,title,state,assigned_admin_id,entry_context,version,control_epoch,updated_at,deleted_at FROM ag_conversation WHERE id=ANY(%s::uuid[]) ORDER BY id",
            (ids,),
        ).fetchall()
        messages = conn.execute(
            "SELECT id,conversation_id,seq,sender_kind,sender_id,status,text_content,blocks,updated_at,deleted_at FROM ag_message WHERE conversation_id=ANY(%s::uuid[]) ORDER BY conversation_id,seq",
            (ids,),
        ).fetchall()
        settings = conn.execute(
            "SELECT key,value,version FROM ag_setting WHERE key IN ('agent-runtime-v1','agent-return-rule-catalog-v1') ORDER BY key"
        ).fetchall()
        models = conn.execute(
            "SELECT id,capability,model_name,is_active,enabled,version FROM ag_model_profile ORDER BY id"
        ).fetchall()
        counters = dict(
            conn.execute(
                "SELECT (SELECT count(*) FROM ag_model_call) model_calls,(SELECT count(*) FROM ag_action) actions,(SELECT count(*) FROM ag_run WHERE state IN ('QUEUED','RUNNING','STOP_REQUESTED')) active_runs"
            ).fetchone()
        )
    config = json.loads((ROOT / "deployment/config/portal.yaml").read_text(encoding="utf-8"))["spring"][
        "datasource"
    ]
    mysql = pymysql.connect(
        host="127.0.0.1",
        port=13306,
        user=config["username"],
        password=config["password"],
        database="mall",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=8,
        read_timeout=15,
    )
    try:
        with mysql.cursor() as cursor:
            cursor.execute("START TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT id,member_id,status,pay_amount,delete_status,modify_time FROM oms_order ORDER BY id"
            )
            orders = cursor.fetchall()
            cursor.execute(
                "SELECT id,order_id,product_id,product_sku_id,product_quantity,real_amount FROM oms_order_item ORDER BY id"
            )
            items = cursor.fetchall()
    finally:
        mysql.rollback()
        mysql.close()
    values = {
        "conversations": conversations,
        "messages": messages,
        "settings": settings,
        "profiles": models,
        "orders": orders,
        "order_items": items,
    }
    return {
        "sha256": digest(values),
        "parts": {k: {"sha256": digest(v), "count": len(v)} for k, v in values.items()},
        "counters": counters,
    }


async def legacy(client, path, body=None, data=None):
    response = await client.post(path, json=body, data=data)
    value = response.json()
    if response.status_code != 200 or value.get("code") != 200:
        raise RuntimeError("Fixture management API rejected the operation")
    return value["data"]


def client():
    return httpx.AsyncClient(base_url="http://127.0.0.1:8201", trust_env=False, timeout=45)


async def prepare(folder):
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / "fixtures.json").exists():
        raise RuntimeError("Fixtures already exist; use fingerprint or cleanup without duplicating data")
    experiment = str(uuid4())
    prefix = "query_" + experiment[:8]
    state = {
        "schema_version": 1,
        "id": experiment,
        "prefix": prefix,
        "buyers": [],
        "staff": [],
        "result": "PREPARING",
    }
    private = {"schema_version": 1, "actors": {}}
    save(folder / "fixtures.json", state)
    save(folder / "auth.private.json", private)
    clients = []
    operator = client()
    try:
        await login(operator, True)
        private["actors"]["operator"] = {"authorization": operator.headers["Authorization"]}
        for i in range(3):
            username = prefix + "_" + str(i)
            password = secrets.token_urlsafe(24) + "aA1!"
            account = await legacy(
                operator,
                "/mall-admin/admin/register",
                {
                    "username": username,
                    "password": password,
                    "nickName": "[TEST ONLY] query " + str(i),
                    "email": username + "@example.invalid",
                    "note": "Fixed query baseline fixture; disable after cleanup",
                },
            )
            state["staff"].append(
                {"username": username, "id": str(account["id"]), "actor_key": "staff" + str(i)}
            )
            save(folder / "fixtures.json", state)
            await legacy(
                operator, "/mall-admin/admin/role/update", data={"adminId": account["id"], "roleIds": "10"}
            )
            staff = client()
            clients.append(staff)
            response = await staff.post(
                "/mall-admin/admin/login", json={"username": username, "password": password}
            )
            data = response.json()
            if data.get("code") != 200:
                raise RuntimeError("Dedicated staff login rejected")
            staff.headers["Authorization"] = data["data"]["tokenHead"] + data["data"]["token"]
            private["actors"]["staff" + str(i)] = {"authorization": staff.headers["Authorization"]}
            await api(staff, ADMIN + "/me")
        for i, username in enumerate(BUYERS):
            buyer = client()
            clients.append(buyer)
            await login(buyer, username=username)
            actor = "buyer" + str(i)
            private["actors"][actor] = {"authorization": buyer.headers["Authorization"]}
            identity = await api(buyer, MEMBER + "/me")
            title = "[TEST ONLY] " + prefix + " buyer " + str(i)
            conv = await api(
                buyer,
                MEMBER + "/conversations",
                "POST",
                {"title": title, "entry_context": {"kind": "GENERAL"}},
                expected=201,
            )
            cid = conv["id"]
            state["buyers"].append(
                {
                    "username": username,
                    "member_id": identity["id"],
                    "actor_key": actor,
                    "conversation_id": cid,
                    "title": title,
                    "staff_index": i % 3,
                }
            )
            save(folder / "fixtures.json", state)
            save(folder / "auth.private.json", private)
            staff = clients[i % 3]
            profile = next(
                p
                for p in (await api(staff, ADMIN + "/staff?limit=100"))["items"]
                if p["id"] == state["staff"][i % 3]["id"]
            )
            await api(
                staff,
                ADMIN + "/presence",
                "PUT",
                {"expected_version": profile["version"], "presence": "ONLINE"},
            )
            handoff = await api(
                buyer,
                MEMBER + f"/conversations/{cid}/handoff",
                "POST",
                {
                    "expected_conversation_version": conv["version"],
                    "reason": "[TEST ONLY] fixed query dataset",
                },
                expected=201,
            )
            claimed = await api(
                staff,
                ADMIN + f"/handoffs/{handoff['id']}/claim",
                "POST",
                {"expected_version": handoff["version"]},
            )
            for n in range(3):
                sent = await api(
                    buyer,
                    MEMBER + f"/conversations/{cid}/messages",
                    "POST",
                    {
                        "client_message_id": str(uuid4()),
                        "text": f"[TEST ONLY] fixed buyer record {n}",
                        "attachment_ids": [],
                    },
                    expected=202,
                )
                if sent["run"] is not None:
                    raise RuntimeError("Fixture unexpectedly created a model run")
                await api(
                    staff,
                    ADMIN + f"/conversations/{cid}/messages",
                    "POST",
                    {
                        "client_message_id": str(uuid4()),
                        "text": f"[TEST ONLY] fixed staff record {n}",
                        "attachment_ids": [],
                        "cards": [],
                    },
                    expected=201,
                )
            await api(
                staff,
                ADMIN + f"/handoffs/{claimed['id']}/close",
                "POST",
                {
                    "expected_version": claimed["version"],
                    "reason": "[TEST ONLY] freeze query fixture without presence heartbeats",
                },
            )
            state["buyers"][-1]["closed"] = True
            save(folder / "fixtures.json", state)
        if len({b["member_id"] for b in state["buyers"]}) != 10:
            raise RuntimeError("Fixture buyers are not distinct")
        focus = state["buyers"][9]
        cid = focus["conversation_id"]
        watermark = (await api(clients[-1], MEMBER + f"/conversations/{cid}"))["event_cursor"]
        routes = [
            {
                "name": "member_list",
                "actor_key": "buyer9",
                "path": MEMBER + "/conversations?limit=20",
                "expected_operation": "listConversations",
                "weight": 1,
            },
            {
                "name": "member_snapshot",
                "actor_key": "buyer9",
                "path": MEMBER + f"/conversations/{cid}",
                "expected_operation": "getConversation",
                "weight": 1,
            },
            {
                "name": "member_history",
                "actor_key": "buyer9",
                "path": MEMBER + f"/conversations/{cid}/messages?limit=20",
                "expected_operation": "listMessages",
                "weight": 1,
            },
            {
                "name": "member_orders",
                "actor_key": "buyer9",
                "path": MEMBER + "/orders?limit=5",
                "expected_operation": "listMemberOrders",
                "weight": 1,
            },
            {
                "name": "staff_history",
                "actor_key": "staff0",
                "path": ADMIN + f"/conversations/{cid}/messages?limit=20",
                "expected_operation": "adminListMessages",
                "weight": 1,
            },
        ]
        streams = [
            {
                "name": b["actor_key"],
                "actor_key": b["actor_key"],
                "path": MEMBER + f"/conversations/{b['conversation_id']}/events?after={watermark}",
                "expected_owner_cid": b["conversation_id"],
            }
            for b in state["buyers"]
        ]
        streams += [
            {
                "name": s["actor_key"],
                "actor_key": s["actor_key"],
                "path": ADMIN + f"/events?after={watermark}",
            }
            for s in state["staff"]
        ]
        save(folder / "routes.json", routes)
        save(folder / "streams.json", streams)
        state["fingerprint"] = fingerprint(state)
        state["result"] = "READY"
        save(folder / "fixtures.json", state)
        print("PASS fixed 10-buyer/3-staff fixtures, closed conversations and 60 labelled messages")
    finally:
        await operator.aclose()
        for c in clients:
            await c.aclose()


async def cleanup(folder):
    state = json.loads((folder / "fixtures.json").read_text(encoding="utf-8"))
    UUID(state["id"])
    if state["prefix"] != "query_" + state["id"][:8]:
        raise RuntimeError("Fixture ownership prefix mismatch")
    operator = client()
    try:
        await login(operator, True)
        for b in state["buyers"]:
            if b.get("deleted"):
                continue
            async with client() as buyer:
                await login(buyer, username=b["username"])
                snap = await api(buyer, MEMBER + "/conversations/" + b["conversation_id"])
                if snap["conversation"]["title"] != b["title"] or not b["title"].startswith(
                    "[TEST ONLY] " + state["prefix"]
                ):
                    raise RuntimeError("Refusing to delete a foreign conversation")
                await api(
                    buyer,
                    MEMBER + "/conversations/" + b["conversation_id"],
                    "DELETE",
                    {"expected_version": snap["conversation"]["version"]},
                )
                b["deleted"] = True
                save(folder / "fixtures.json", state)
        for s in state["staff"]:
            if s.get("disabled"):
                continue
            response = await operator.get("/mall-admin/admin/" + s["id"])
            data = response.json()
            if (
                data.get("code") != 200
                or data["data"]["username"] != s["username"]
                or not s["username"].startswith(state["prefix"] + "_")
            ):
                raise RuntimeError("Refusing to disable a foreign account")
            await legacy(operator, "/mall-admin/admin/updateStatus/" + s["id"], data={"status": 0})
            s["disabled"] = True
            save(folder / "fixtures.json", state)
        state["cleanup"] = "passed"
        save(folder / "fixtures.json", state)
        print("PASS only owned fixtures cleaned and dedicated staff disabled")
    finally:
        await operator.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "fingerprint", "cleanup"])
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    folder = location(args.directory)
    if args.action == "fingerprint":
        state = json.loads((folder / "fixtures.json").read_text(encoding="utf-8"))
        print(json.dumps(fingerprint(state), default=str))
    else:
        run(prepare(folder) if args.action == "prepare" else cleanup(folder))


if __name__ == "__main__":
    main()
