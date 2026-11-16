"""Read-only verification of persisted order evidence after restarting the API."""

import json
from pathlib import Path

import httpx
import psycopg
import pymysql

ROOT = Path(__file__).resolve().parents[2]


def main():
    run = ROOT / "deployment/.run"
    evidence = json.loads((run / "order-smoke.json").read_text(encoding="utf-8"))
    config = json.loads((run / "agent-env.json").read_text(encoding="utf-8"))
    with httpx.Client(timeout=10, trust_env=False) as client:
        ready = client.get("http://127.0.0.1:8099/readyz").json()
        assert ready["build"] == "order-0.9" and ready["migration"] == "0006"
    with psycopg.connect(config["databaseUrl"].replace("postgresql+psycopg://", "postgresql://")) as db:
        row = db.execute(
            "SELECT a.state,a.action_type,a.quote_snapshot,a.result,a.message_id,d.state,d.reconcile_grant_ciphertext "
            "FROM ag_action a JOIN ag_action_dispatch d ON d.action_id=a.id WHERE a.id=%s",
            (evidence["action_id"],),
        ).fetchone()
        assert (
            row and row[0:2] == ("SUCCEEDED", "CREATE_ORDER") and row[2] and row[4] and row[5] == "TERMINAL"
        )
        assert row[3]["business_id"] == evidence["order_id"]
        assert row[3]["order"]["pay_amount"] == row[2]["pricing"]["payable"]
        assert b"reconcile_only" not in bytes(row[6])
        assert (
            db.execute(
                "SELECT count(*) FROM ag_action WHERE member_id=%s AND state IN ('EXECUTING','UNKNOWN')",
                (int(evidence["member_id"]),),
            ).fetchone()[0]
            == 0
        )
        published = db.execute(
            "SELECT count(*),count(published_at) FROM ag_event_outbox WHERE conversation_id=%s",
            (evidence["conversation_id"],),
        ).fetchone()
        assert published[0] == published[1]
    portal = json.loads((ROOT / "deployment/config/portal.yaml").read_text(encoding="utf-8"))["spring"][
        "datasource"
    ]
    with pymysql.connect(
        host="127.0.0.1", port=13306, user=portal["username"], password=portal["password"], database="mall"
    ) as db:
        with db.cursor() as query:
            query.execute(
                "SELECT state,action_type,business_id,confirmation_audit_id FROM oms_agent_operation WHERE operation_no=%s",
                (evidence["action_id"],),
            )
            result = query.fetchone()
            assert (
                result[0:2] == ("SUCCEEDED", "CREATE_ORDER")
                and str(result[2]) == evidence["order_id"]
                and result[3]
            )
            query.execute(
                "SELECT status FROM oms_order WHERE id=%s AND member_id=%s",
                (evidence["order_id"], evidence["member_id"]),
            )
            assert query.fetchone()[0] == 4
    evidence["post_restart_verified"] = True
    evidence["conversation_events_published"] = published[0]
    (run / "order-smoke.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "PASS restarted API, persistent Java order/action/audit, encrypted reconciliation and published events"
    )


if __name__ == "__main__":
    main()
