"""Read-only cart persistence and cleanup evidence after an API restart."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg
import pymysql

ROOT = Path(__file__).resolve().parents[2]


def main():
    run = ROOT / "deployment/.run"
    evidence = json.loads((run / "cart-smoke.json").read_text(encoding="utf-8"))
    ui = json.loads((run / "cart-ui.json").read_text(encoding="utf-8"))
    config = json.loads((run / "agent-env.json").read_text(encoding="utf-8"))
    with httpx.Client(timeout=10, trust_env=False) as client:
        ready = client.get("http://127.0.0.1:8099/readyz").json()
        assert ready["build"] == "cart-0.8" and ready["migration"] == "0006"
    with psycopg.connect(config["databaseUrl"].replace("postgresql+psycopg://", "postgresql://")) as db:
        for aid in [evidence["action_ids"][0], ui["action_id"]]:
            row = db.execute(
                "SELECT a.state,a.quote_snapshot,a.message_id,d.state,d.reconcile_grant_ciphertext FROM ag_action a JOIN ag_action_dispatch d ON d.action_id=a.id WHERE a.id=%s",
                (aid,),
            ).fetchone()
            assert row and row[0] == "SUCCEEDED" and row[1] and row[2] and row[3] == "TERMINAL"
            assert b"reconcile_only" not in bytes(row[4])
        unknown = db.execute(
            "SELECT count(*) FROM ag_action WHERE state IN ('EXECUTING','UNKNOWN')"
        ).fetchone()[0]
        assert unknown == 0
        published = db.execute(
            "SELECT count(*),count(published_at) FROM ag_event_outbox WHERE conversation_id=%s",
            (evidence["conversation_id"],),
        ).fetchone()
        assert published[0] == published[1]
        checkpoint = db.execute(
            "SELECT state,error_code,config_snapshot->>'graph_version' FROM ag_run WHERE id=%s",
            (evidence["run_id"],),
        ).fetchone()
        assert checkpoint == ("COMPLETED", None, "cart-0.8")
    portal = json.loads((ROOT / "deployment/config/portal.yaml").read_text(encoding="utf-8"))["spring"][
        "datasource"
    ]
    with pymysql.connect(
        host="127.0.0.1", port=13306, user=portal["username"], password=portal["password"], database="mall"
    ) as db:
        with db.cursor() as q:
            q.execute(
                "SELECT state,confirmation_audit_id,control_epoch FROM oms_agent_operation WHERE operation_no=%s",
                (ui["action_id"],),
            )
            row = q.fetchone()
            assert row and row[0] == "SUCCEEDED" and row[1] and row[2] >= 1
            q.execute(
                "SELECT COALESCE(SUM(quantity),0) FROM oms_cart_item WHERE member_id=%s AND product_sku_id=%s AND delete_status=0",
                (ui["member_id"], ui["sku_id"]),
            )
            assert q.fetchone()[0] == ui["before_quantity"]
    evidence.update(
        {
            "post_restart_verified_at": datetime.now(UTC).isoformat(),
            "published_event_count": published[1],
            "unresolved_action_count": unknown,
            "ui_verified": True,
            "ui_test_cart_quantity_restored": ui["test_cart_quantity_restored"],
            "screenshot": "deployment/.run/cart-ui.jpg",
        }
    )
    evidence["checks"] = list(
        dict.fromkeys(
            evidence["checks"]
            + [
                "browser_sku_selection_prepare_explicit_confirm_and_success_card",
                "pg_mysql_quotes_actions_dispatch_and_confirmation_audit_survive_api_restart",
                "all_cart_events_published_and_no_unresolved_actions",
            ]
        )
    )
    (run / "cart-smoke.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS post-restart cart facts, encrypted recovery grants, event publication and restored demo cart")


if __name__ == "__main__":
    main()
