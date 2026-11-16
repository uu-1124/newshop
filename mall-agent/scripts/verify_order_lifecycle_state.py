"""Read-only audit of the dedicated order/return/direct-purchase acceptance fixture.

Requires the recorded local smoke/browser evidence and completed fixture cleanup.
Never calls a model, changes business records, or prints credentials.
"""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
from expansion_client import ADMIN, MEMBER, api, login
from smoke_order_lifecycle import mysql

from app.infrastructure.postgres.database import Database, one, rows
from app.runtime import run

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "deployment/.run"


def read(name):
    return json.loads((EVIDENCE / name).read_text(encoding="utf-8"))


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


async def main():
    result = {"verified_at": datetime.now(UTC).isoformat(), "checks": []}

    def passed(name):
        result["checks"].append(name)
        print("PASS", name, flush=True)

    smoke = read("order-lifecycle-smoke.json")
    browser = read("order-lifecycle-browser.json")
    assert len(smoke["checks"]) >= 6 and browser["result"] == "passed"
    assert smoke["model_queries_completed"] and smoke["demo_stock_restored"]
    assert smoke["test_conversation_deleted"]
    for name, marker in (
        ("order-lifecycle-python-full-final.log", "94 passed in"),
        ("order-return-direct-java-package-final.log", "Tests run: 31, Failures: 0, Errors: 0, Skipped: 0"),
        ("order-return-direct-timezone-regression.log", "Tests run: 4, Failures: 0, Errors: 0, Skipped: 0"),
        ("order-return-direct-admin-build.log", "built in"),
        ("order-lifecycle-buyer-build-final.log", "Build complete"),
    ):
        assert marker in (EVIDENCE / name).read_text(encoding="utf-8-sig", errors="replace"), name
    for name in ("order-return-direct-admin-types.log", "order-lifecycle-buyer-types-final.log"):
        assert "error TS" not in (EVIDENCE / name).read_text(encoding="utf-8-sig", errors="replace"), name
    for name in browser["screenshots"]:
        assert (EVIDENCE / name).stat().st_size > 10000
    passed("three_groups_have_gateway_model_browser_and_regression_evidence")

    async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
        ready = await client.get("http://127.0.0.1:8099/readyz")
        assert ready.status_code == 200
        ready = ready.json()
        assert ready["migration"] == "0007"
        assert ready["capabilities"]["transactions"] == "ADD_CART_DIRECT_CART_CANCEL_LIVE"
        assert ready["capabilities"]["after_sale"] == "ITEM_RETURN_DEMO_LIVE"
        response = await client.get("http://127.0.0.1:8099/internal/framework")
        response.raise_for_status()
        framework = response.json()
        assert (
            framework["module_count"],
            framework["contract_operation_count"],
            framework["active_python_operation_count"],
        ) == (20, 122, 88)
        assert framework["extensions"]["after_sale"]["bound"]
        assert framework["full_business_runtime"] == "NOT_READY"
        result["runtime"] = ready
        result["framework"] = {
            key: framework[key]
            for key in (
                "module_count",
                "contract_operation_count",
                "active_python_operation_count",
                "full_business_runtime",
                "extensions",
            )
        }
        for port in (8080, 8085):
            response = await client.get(f"http://127.0.0.1:{port}/actuator/health")
            assert response.status_code == 200 and response.json()["status"] == "UP"
    passed("live_services_migration_0007_and_88_active_operations")

    oid = smoke["fulfilled_order_id"]
    return_ids = (smoke["return_id"], browser["browser_return_id"])
    with mysql() as db, db.cursor() as q:
        q.execute(
            "SELECT id,status,pay_amount,note FROM oms_order WHERE member_id=%s ORDER BY id",
            (smoke["member_id"],),
        )
        orders = q.fetchall()
        states = {str(row["id"]): row["status"] for row in orders}
        assert states == {smoke["cancelled_order_id"]: 4, oid: 3, browser["direct_order_id"]: 4}
        assert next(row for row in orders if str(row["id"]) == oid)["note"] == (
            "[TEST ONLY] lifecycle resources restored"
        )
        q.execute(
            "SELECT return_id,order_item_id,quantity,requested_amount,approved_amount,stage,version,"
            "simulated_refund_no FROM oms_agent_return WHERE order_id=%s ORDER BY return_id",
            (oid,),
        )
        returns = q.fetchall()
        assert {str(row["return_id"]) for row in returns} == set(return_ids)
        assert all(row["stage"] == "SIMULATED_REFUNDED" and row["version"] == 4 for row in returns)
        assert all(row["requested_amount"] == row["approved_amount"] for row in returns)
        assert len({row["simulated_refund_no"] for row in returns}) == 2
        assert all(row["simulated_refund_no"] for row in returns)
        q.execute(
            "SELECT SUM(product_quantity) quantity,SUM(real_amount*product_quantity) amount "
            "FROM oms_order_item WHERE order_id=%s",
            (oid,),
        )
        purchased = q.fetchone()
        assert sum(row["quantity"] for row in returns) == purchased["quantity"] == 2
        assert sum(row["approved_amount"] for row in returns) == purchased["amount"] == Decimal("4498.00")
        q.execute(
            "SELECT resource_id,operation,COUNT(*) n FROM oms_agent_business_audit "
            "WHERE actor_kind='STAFF' AND resource_id IN (%s,%s) "
            "AND operation IN ('REVIEW','RECEIVE','REFUND') GROUP BY resource_id,operation",
            return_ids,
        )
        audits = q.fetchall()
        assert len(audits) == 6 and all(row["n"] == 1 for row in audits)
        q.execute("SELECT stock,lock_stock FROM pms_sku_stock WHERE id=%s", (smoke["sku_id"],))
        stock = q.fetchone()
        assert stock == smoke["stock_before"]
        q.execute(
            "SELECT COUNT(*) n FROM oms_cart_item WHERE member_id=%s AND delete_status=0",
            (smoke["member_id"],),
        )
        assert q.fetchone()["n"] == 0
        q.execute("SELECT integration FROM ums_member WHERE id=%s", (smoke["member_id"],))
        assert q.fetchone()["integration"] == 0
        result["orders"] = {key: value for key, value in states.items()}
        result["returns"] = returns
        result["stock"] = stock
        result["staff_transition_audit_rows"] = len(audits)
        item_id = str(returns[0]["order_item_id"])
    passed("returns_match_original_quantity_amount_and_have_six_distinct_stage_audits")
    passed("browser_and_api_unpaid_orders_cancelled_cart_empty_stock_and_points_restored")

    cfg = read("agent-env.json")
    database = Database(cfg["databaseUrl"])
    try:
        async with database.transaction() as conn:
            cid = smoke["conversation_id"]
            conv = await one(
                conn, "SELECT deleted_at FROM ag_conversation WHERE id=CAST(:id AS uuid)", id=cid
            )
            assert conv and conv["deleted_at"]
            job = await one(
                conn,
                "SELECT state FROM ag_job WHERE job_type='RETENTION_EXECUTE' "
                "AND parameters->>'conversation_id'=:id ORDER BY created_at DESC LIMIT 1",
                id=cid,
            )
            assert job and job["state"] == "SUCCEEDED"
            for name, sql in (
                (
                    "remaining_event_copies",
                    "SELECT count(*) n FROM ag_event_outbox WHERE conversation_id=CAST(:id AS uuid)",
                ),
                (
                    "remaining_message_content",
                    "SELECT count(*) n FROM ag_message WHERE conversation_id=CAST(:id AS uuid) AND (text_content<>'' OR blocks<>'[]'::jsonb OR deleted_at IS NULL)",
                ),
                (
                    "active_runs",
                    "SELECT count(*) n FROM ag_run WHERE conversation_id=CAST(:id AS uuid) AND state IN ('QUEUED','RUNNING','STOP_REQUESTED')",
                ),
                (
                    "pending_or_unknown_actions",
                    "SELECT count(*) n FROM ag_action WHERE conversation_id=CAST(:id AS uuid) AND state IN ('PENDING_CONFIRMATION','EXECUTING','UNKNOWN')",
                ),
            ):
                value = await one(conn, sql, id=cid)
                result[name] = value["n"]
                assert value["n"] == 0, name
            proof = await one(
                conn,
                "SELECT a.state,a.sha256,r.return_apply_id,r.protected,r.released_at "
                "FROM ag_attachment a JOIN ag_attachment_ref r ON r.attachment_id=a.id "
                "WHERE a.id=CAST(:id AS uuid) AND r.ref_kind='RETURN_PROOF'",
                id=smoke["attachment_id"],
            )
            assert proof and proof["state"] == "READY" and proof["protected"]
            assert not proof["released_at"] and str(proof["return_apply_id"]) == smoke["return_id"]
            model_runs = await rows(
                conn,
                "SELECT state,count(*) n FROM ag_run WHERE conversation_id=CAST(:id AS uuid) GROUP BY state",
                id=cid,
            )
            result["model_run_outcomes"] = {row["state"]: row["n"] for row in model_runs}
            assert result["model_run_outcomes"].get("COMPLETED", 0) >= 2
            workers = await rows(
                conn,
                "SELECT DISTINCT process_kind FROM ag_worker_heartbeat WHERE last_seen_at>now()-interval '45 seconds'",
            )
            result["workers"] = sorted(row["process_kind"] for row in workers)
            assert {"generation", "scheduler", "jobs", "outbox"} <= set(result["workers"])
    finally:
        await database.engine.dispose()
    passed("test_conversation_erasure_finished_no_pending_actions_and_return_proof_protected")

    async with (
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", trust_env=False, timeout=45) as buyer,
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", trust_env=False, timeout=45) as staff,
    ):
        await login(buyer, username=smoke["buyer"])
        await login(staff, True)
        eligibility = await api(buyer, MEMBER + f"/orders/{oid}/items/{item_id}/return-eligibility")
        assert not eligibility["eligible"] and eligibility["remaining_quantity"] == 0
        assert Decimal(eligibility["remaining_amount"]) == 0
        for client, prefix in ((buyer, MEMBER), (staff, ADMIN)):
            response = await client.get(prefix + f"/attachments/{smoke['attachment_id']}/content")
            assert response.status_code == 200
            assert hashlib.sha256(response.content).hexdigest() == proof["sha256"]
        response = await buyer.get(MEMBER + f"/conversations/{smoke['conversation_id']}")
        assert response.status_code == 404
    passed("remaining_eligibility_zero_and_owner_staff_can_read_proof_after_conversation_deletion")

    result["jars"] = {}
    for service, expected in (
        ("admin", "867f9185c2d2651c83f27703aa5073e951e235893fd7ba6f10a4cf890cf39a81"),
        ("portal", "a67c787db7a18620f4700b5a07630353177776c754c13ecb65bd874a6b71470d"),
    ):
        active = EVIDENCE / f"agent-jars/mall-{service}.jar"
        built = ROOT / f"mall-swarm/mall-{service}/target/mall-{service}-1.0-SNAPSHOT.jar"
        assert sha(active) == sha(built) == expected
        result["jars"][service] = expected
    passed("active_java_packages_match_built_and_verified_artifacts")
    result.update(result="passed", paid_model_calls=False, real_financial_channels_called=False)
    (EVIDENCE / "order-lifecycle-final-state.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("PASS read-only order lifecycle final acceptance audit", flush=True)


if __name__ == "__main__":
    run(main())
