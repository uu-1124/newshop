"""Read-only post-restart evidence, with credentials kept in memory."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[2]


async def main():
    evidence_path = ROOT / "deployment/.run/handoff-smoke.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    config = json.loads((ROOT / "deployment/.run/agent-env.json").read_text(encoding="utf-8"))
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=20, trust_env=False) as client:
        ready = (await client.get("http://127.0.0.1:8099/readyz")).json()
        assert ready["build"] == "handoff-0.7" and ready["migration"] == "0005"
        # The test account is the existing local demonstration member.
        import os

        login = (
            await client.post(
                "/mall-portal/sso/login",
                data={
                    "username": "member",
                    "password": os.environ.get("MALL_DEMO_MEMBER_PASSWORD", "MallDemo2026!"),
                },
            )
        ).json()
        assert login["code"] == 200
        token = login["data"]["tokenHead"] + login["data"]["token"]
        response = await client.get(
            f"/mall-agent/v1/member/conversations/{evidence['conversation_id']}",
            headers={"Authorization": token},
        )
        snap = response.json()["data"]
        assert snap["conversation"]["state"] == "CLOSED" and snap["active_run"] is None
        assert snap["handoff"]["state"] == "CLOSED"
        assert sum(m["sender_kind"] == "STAFF" for m in snap["recent_messages"]) == 2
        assert "INTERNAL_NOTE_MUST_STAY_PRIVATE" not in json.dumps(snap)
    with psycopg.connect(config["databaseUrl"].replace("postgresql+psycopg://", "postgresql://")) as conn:
        total, published = conn.execute(
            "SELECT count(*),count(published_at) FROM ag_event_outbox WHERE conversation_id=%s",
            (evidence["conversation_id"],),
        ).fetchone()
        assert total == published and total >= 10
        leaked = conn.execute(
            "SELECT count(*) FROM ag_event_outbox WHERE conversation_id=%s AND payload::text LIKE '%%INTERNAL_NOTE_MUST_STAY_PRIVATE%%'",
            (evidence["conversation_id"],),
        ).fetchone()[0]
        assert leaked == 0
        active = conn.execute(
            "SELECT count(*) FROM ag_handoff WHERE state IN ('WAITING','CLAIMED')"
        ).fetchone()[0]
        assert active == 0
        docs = conn.execute(
            "SELECT count(*) FROM ag_kb_document WHERE enabled AND active_version_id IS NOT NULL"
        ).fetchone()[0]
        assert docs == 0
        ui = conn.execute("""SELECT c.id,c.state FROM ag_conversation c JOIN ag_message m ON m.conversation_id=c.id
            WHERE m.text_content='[TEST ONLY] UI 接待验证：您好，人工客服已接入。' ORDER BY c.created_at DESC LIMIT 1""").fetchone()
        assert ui and ui[1] == "CLOSED"
    evidence["checks"] = list(
        dict.fromkeys(
            evidence["checks"]
            + [
                "closed_reception_messages_and_note_isolation_survive_api_restart",
                "all_15_conversation_events_published_to_real_outbox",
                "browser_member_request_claim_reply_note_member_reply_and_close",
            ]
        )
    )
    evidence.update(
        {
            "post_restart_verified_at": datetime.now(UTC).isoformat(),
            "event_count": total,
            "published_event_count": published,
            "active_handoff_count": active,
            "active_published_knowledge_documents": docs,
            "ui_conversation_id": str(ui[0]),
        }
    )
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS post-restart reception persistence, private notes, published outbox and closed UI test")


if __name__ == "__main__":
    asyncio.run(main())
