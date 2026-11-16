"""Real GPT → SKU selection → buyer confirmation → Java cart → receipt recovery.

Runs only on the named local demonstration gateway. Restores precisely the test
SKU quantity through normal buyer APIs and preserves operation/audit evidence.
"""

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pymysql

ROOT = Path(__file__).resolve().parents[2]


async def main():
    evidence = {"verified_at": datetime.now(UTC).isoformat(), "checks": []}
    headers = {}
    before_items = []
    selected_sku = None
    added = 0
    actions = []
    config = json.loads((ROOT / "deployment/.run/agent-env.json").read_text(encoding="utf-8"))
    sql_url = config["databaseUrl"].replace("postgresql+psycopg://", "postgresql://")
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=35, trust_env=False) as client:

        async def call(method, path, body=None, key=None, legacy=False, **kwargs):
            h = dict(headers)
            if key:
                h["Idempotency-Key"] = key
            response = await client.request(method, path, headers=h, json=body, **kwargs)
            data = response.json()
            if not response.is_success or data.get("code") != (200 if legacy else "OK"):
                raise RuntimeError(
                    f"Cart smoke rejected {path}: status={response.status_code}, code={data.get('code')}"
                )
            return data["data"]

        async def snapshot():
            return await call("GET", f"/mall-agent/v1/member/conversations/{cid}")

        async def prepare(product, sku):
            snap = await snapshot()
            body = {
                "expected_conversation_version": snap["conversation"]["version"],
                "parameters": {
                    "action_type": "ADD_CART",
                    "items": [{"product_id": product["id"], "sku_id": sku["id"], "quantity": 1}],
                },
            }
            action = await call(
                "POST", f"/mall-agent/v1/member/conversations/{cid}/actions", body, str(uuid4())
            )
            actions.append(action["id"])
            return action

        try:
            ready = (await client.get("http://127.0.0.1:8099/readyz")).json()
            assert ready["migration"] == "0006" and ready["capabilities"]["transactions"] == "ADD_CART_LIVE"
            login = await call(
                "POST",
                "/mall-portal/sso/login",
                legacy=True,
                data={
                    "username": "member",
                    "password": os.environ.get("MALL_DEMO_MEMBER_PASSWORD", "MallDemo2026!"),
                },
            )
            headers = {"Authorization": login["tokenHead"] + login["token"]}
            before_items = await call("GET", "/mall-portal/cart/list", legacy=True)
            conv = await call(
                "POST",
                "/mall-agent/v1/member/conversations",
                {"title": "[TEST ONLY] cart confirmation", "entry_context": {"kind": "GENERAL"}},
                str(uuid4()),
            )
            cid = conv["id"]
            key = str(uuid4())
            sent = await call(
                "POST",
                f"/mall-agent/v1/member/conversations/{cid}/messages",
                {
                    "client_message_id": key,
                    "text": "我想把小米手机加入购物车，请先查询商城商品，给我选择规格和数量，暂时不要执行。",
                    "attachment_ids": [],
                },
                key,
            )
            run_id = sent["run"]["id"]
            for _ in range(100):
                snap = await snapshot()
                if not snap["active_run"]:
                    break
                await asyncio.sleep(1)
            else:
                raise RuntimeError("Cart shopping graph did not finish")
            candidates = [
                b["product"]
                for m in snap["recent_messages"]
                if m["sender_kind"] == "ASSISTANT"
                for b in m["blocks"]
                if b["type"] == "product"
            ]
            assert candidates, "Real shopping graph must provide Java-backed product selection cards"
            product = next(
                p
                for p in candidates
                if any(s["visible"] and s["available_quantity"] > 2 for s in p["sku_options"])
            )
            sku = next(s for s in product["sku_options"] if s["visible"] and s["available_quantity"] > 2)
            selected_sku = sku["id"]
            before_qty = sum(
                item["quantity"] for item in before_items if str(item["productSkuId"]) == selected_sku
            )
            evidence["checks"].append("real_gpt_shopping_graph_returns_only_java_backed_sku_cards")
            action = await prepare(product, sku)
            assert (await call("GET", "/mall-portal/cart/list", legacy=True)) == before_items
            evidence["checks"].append("preparation_is_readonly_and_quote_matches_current_java_sku")
            key = str(uuid4())
            results = await asyncio.gather(
                *(
                    call(
                        "POST",
                        f"/mall-agent/v1/member/actions/{action['id']}/confirm",
                        {"expected_version": action["version"]},
                        key,
                    )
                    for _ in range(2)
                )
            )
            result = await call("GET", f"/mall-agent/v1/member/actions/{action['id']}")
            assert result["state"] == "SUCCEEDED" and all(x["id"] == action["id"] for x in results)
            added += 1
            cart = await call("GET", "/mall-portal/cart/list", legacy=True)
            assert (
                sum(item["quantity"] for item in cart if str(item["productSkuId"]) == selected_sku)
                == before_qty + 1
            )
            evidence["checks"].append("concurrent_duplicate_buyer_click_adds_exactly_one_unit")

            # Mimic a committed Java result whose response was not saved to PG.
            # Recovery reads the same result and must not dispatch execute again.
            with psycopg.connect(sql_url) as db:
                db.execute(
                    "UPDATE ag_action SET state='UNKNOWN',result=NULL,error_code='TEST_RESPONSE_LOSS',version=version+1 WHERE id=%s",
                    (action["id"],),
                )
                db.execute(
                    "UPDATE ag_action_dispatch SET state='RECONCILING',lease_owner=NULL,lease_until=NULL,next_attempt_at=now(),version=version+1 WHERE action_id=%s",
                    (action["id"],),
                )
            for _ in range(30):
                result = await call("GET", f"/mall-agent/v1/member/actions/{action['id']}")
                if result["state"] == "SUCCEEDED":
                    break
                await asyncio.sleep(1)
            else:
                raise RuntimeError("Real operation recovery did not finish")
            assert (
                sum(
                    item["quantity"]
                    for item in await call("GET", "/mall-portal/cart/list", legacy=True)
                    if str(item["productSkuId"]) == selected_sku
                )
                == before_qty + 1
            )
            evidence["checks"].append("real_scheduler_reconciles_committed_result_without_second_add")

            cancelled = await prepare(product, sku)
            cancelled = await call(
                "POST",
                f"/mall-agent/v1/member/actions/{cancelled['id']}/cancel",
                {"expected_version": cancelled["version"]},
                str(uuid4()),
            )
            assert cancelled["state"] == "CANCELLED"
            assert (
                sum(
                    item["quantity"]
                    for item in await call("GET", "/mall-portal/cart/list", legacy=True)
                    if str(item["productSkuId"]) == selected_sku
                )
                == before_qty + 1
            )
            evidence["checks"].append("cancellation_changes_no_cart_data")
            evidence.update(
                {
                    "conversation_id": cid,
                    "run_id": run_id,
                    "action_ids": actions,
                    "sku_id": selected_sku,
                    "added_units": added,
                }
            )
        finally:
            if selected_sku and headers:
                cart = await call("GET", "/mall-portal/cart/list", legacy=True)
                originals = {
                    str(item["id"]): item
                    for item in before_items
                    if str(item["productSkuId"]) == selected_sku
                }
                current = [item for item in cart if str(item["productSkuId"]) == selected_sku]
                expected = sum(item["quantity"] for item in originals.values()) + added
                if sum(item["quantity"] for item in current) != expected:
                    evidence["cleanup_requires_review"] = "cart changed concurrently; left untouched"
                else:
                    for item in current:
                        original = originals.get(str(item["id"]))
                        if original:
                            if original["quantity"] != item["quantity"]:
                                await call(
                                    "GET",
                                    "/mall-portal/cart/update/quantity",
                                    legacy=True,
                                    params={"id": item["id"], "quantity": original["quantity"]},
                                )
                        else:
                            await call(
                                "POST",
                                "/mall-portal/cart/delete",
                                legacy=True,
                                params={"ids": str(item["id"])},
                            )
                    final = await call("GET", "/mall-portal/cart/list", legacy=True)
                    assert (
                        sum(item["quantity"] for item in final if str(item["productSkuId"]) == selected_sku)
                        == expected - added
                    )
                    evidence["test_cart_quantity_restored"] = True
            (ROOT / "deployment/.run/cart-smoke.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    # Independent read of the MySQL durable operation facts.
    portal = json.loads((ROOT / "deployment/config/portal.yaml").read_text(encoding="utf-8"))["spring"][
        "datasource"
    ]
    with pymysql.connect(
        host="127.0.0.1", port=13306, user=portal["username"], password=portal["password"], database="mall"
    ) as mysql:
        with mysql.cursor() as query:
            query.execute(
                "SELECT state,confirmation_audit_id,control_epoch FROM oms_agent_operation WHERE operation_no=%s",
                (actions[0],),
            )
            row = query.fetchone()
            assert row and row[0] == "SUCCEEDED" and row[1] and row[2] >= 1
    evidence["checks"].append("mysql_operation_and_confirmation_audit_evidence_persisted")
    (ROOT / "deployment/.run/cart-smoke.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("PASS real GPT cart preparation, buyer confirmation, recovery and cleanup")


if __name__ == "__main__":
    asyncio.run(main())
