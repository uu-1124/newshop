"""Failure recovery is projected from the owned run in one bounded batch."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event
from test_runtime import settings_for

from app.bootstrap import Services
from app.domain.identity import DomainError, Principal
from app.infrastructure.postgres.database import execute, one


def test_quota_guidance_uses_the_failed_turn_day_without_claiming_current_day_usage():
    from app.application.reply_failures import reply_failure

    cid, rid = uuid4(), uuid4()
    message = {"sender_kind": "ASSISTANT", "status": "FAILED", "run_id": rid, "conversation_id": cid}
    failed = {
        "id": rid,
        "conversation_id": cid,
        "state": "FAILED",
        "error_code": "MODEL_QUOTA_EXCEEDED",
        "finished_at": datetime(2026, 11, 13, 23, 59, 59, tzinfo=UTC),
    }
    recovery = reply_failure(message, failed)
    assert datetime.fromisoformat(recovery["retry_after"]) == datetime(2026, 11, 14, tzinfo=UTC)
    assert "今日" not in recovery["message"] and "今天" not in recovery["message"]


async def running(svc, buyer):
    conv = await svc.conversations.create(
        buyer, str(uuid4()), {"title": "[TEST ONLY] reply recovery", "entry_context": {"kind": "GENERAL"}}
    )
    sent = await svc.conversations.send(
        buyer,
        "synthetic-only",
        conv["id"],
        str(uuid4()),
        {"client_message_id": str(uuid4()), "text": "[TEST ONLY] 保留原问题", "attachment_ids": []},
    )
    rid, mid, cid = UUID(sent["run"]["id"]), uuid4(), UUID(conv["id"])
    async with svc.db.transaction() as conn:
        r = await one(
            conn,
            "UPDATE ag_run SET state='RUNNING',lease_owner='recovery-fixture',lease_until=now()+interval '5 minutes' WHERE id=:id RETURNING *",
            id=rid,
        )
        await execute(
            conn,
            "INSERT INTO ag_message(id,conversation_id,seq,sender_kind,sender_id,status,text_content,run_id,retention_until) VALUES(:id,:cid,2,'ASSISTANT',0,'GENERATING','已核验的部分😀',:run,now()+interval '90 days')",
            id=mid,
            cid=cid,
            run=rid,
        )
        await execute(
            conn, "UPDATE ag_conversation SET next_message_seq=3,version=version+1 WHERE id=:id", id=cid
        )
    return (
        cid,
        rid,
        mid,
        {
            "id": rid,
            "conversation_id": cid,
            "assistant_id": mid,
            "fencing_token": r["fencing_token"],
            "lease_owner": r["lease_owner"],
        },
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "code",
    [
        "MODEL_QUOTA_EXCEEDED",
        "AUTH_CONTEXT_LOST",
        "MODEL_STREAM_INTERRUPTED",
        "SOURCE_UNAVAILABLE",
        "PRIVATE_DRIVER_MESSAGE_MUST_NOT_LEAK",
    ],
)
async def test_persisted_failure_has_safe_consistent_event_snapshot_and_history_recovery(
    integration_config, code
):
    svc = Services(settings_for(integration_config))
    buyer = Principal(1400000000 + uuid4().int % 100000000, "recovery buyer")
    try:
        cid, rid, mid, claimed = await running(svc, buyer)
        await svc.runs.finish(claimed, code)
        snapshot = await svc.conversations.snapshot(buyer, cid)
        message = next(m for m in snapshot["recent_messages"] if m["id"] == str(mid))
        failure = message.get("reply_failure")
        assert failure and message["text"] == "已核验的部分😀"
        assert "PRIVATE_DRIVER_MESSAGE" not in str(message)
        assert failure["message"] and "原问题" in failure["message"]
        if code == "MODEL_QUOTA_EXCEEDED":
            assert failure["code"] == code and failure["retry_after"] is not None
            next_day = datetime.fromisoformat(str(failure["retry_after"]).replace("Z", "+00:00")).astimezone(
                UTC
            )
            assert next_day.hour == next_day.minute == next_day.second == 0
        else:
            assert failure["retry_after"] is None
        async with svc.db.transaction() as conn:
            record = await one(
                conn,
                "SELECT * FROM ag_event_outbox WHERE conversation_id=:cid AND event_type='message.completed' ORDER BY event_id DESC LIMIT 1",
                cid=cid,
            )
            assert record["payload"]["message"]["reply_failure"] == failure
            from app.application.expiry import filter_events

            replayed = await filter_events(conn, [record])
            assert replayed[0]["payload"]["message"]["reply_failure"] == failure
        history = await svc.conversations.messages(buyer, cid, 20, 9223372036854775807)
        assert next(m for m in history["items"] if m["id"] == str(mid))["reply_failure"] == failure
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_authorization_context_recovery_emits_the_same_safe_interruption_as_history(integration_config):
    svc = Services(settings_for(integration_config))
    buyer = Principal(1420000000 + uuid4().int % 10000000, "authorization recovery")
    try:
        cid, _, mid, _ = await running(svc, buyer)
        async with svc.db.transaction() as conn:
            pending = (
                await one(
                    conn, "SELECT count(*) n FROM ag_run WHERE state IN ('QUEUED','RUNNING','STOP_REQUESTED')"
                )
            )["n"]
        # Recovery deliberately commits one run at a time with the event clock last.
        # Other isolated tests may leave older runs; a single call need not select ours.
        for _ in range(pending):
            assert await svc.runs.recover_expired("new-auth-context")
            snapshot = await svc.conversations.snapshot(buyer, cid)
            assistant = next(m for m in snapshot["recent_messages"] if m["id"] == str(mid))
            if assistant["status"] == "INTERRUPTED":
                break
        assert assistant["status"] == "INTERRUPTED"
        assert assistant["reply_failure"]["code"] == "AUTH_CONTEXT_LOST"
        async with svc.db.transaction() as conn:
            event = await one(
                conn,
                "SELECT payload FROM ag_event_outbox WHERE conversation_id=:cid AND event_type='message.completed' ORDER BY event_id DESC LIMIT 1",
                cid=cid,
            )
            assert event["payload"]["message"]["reply_failure"] == assistant["reply_failure"]
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_failure_projection_batches_runs_and_rejects_a_forged_cross_conversation_run_reference(
    integration_config,
):
    from app.application.views import messages_projection

    svc = Services(settings_for(integration_config))
    buyer = Principal(1450000000 + uuid4().int % 10000000, "failure projection")
    counter = [0]

    def count(*args):
        counter[0] += 1

    event.listen(svc.db.engine.sync_engine, "before_cursor_execute", count)
    try:
        cid, rid, mid, claimed = await running(svc, buyer)
        await svc.runs.finish(claimed, "MODEL_QUOTA_EXCEEDED")
        async with svc.db.transaction() as conn:
            message = await one(conn, "SELECT * FROM ag_message WHERE id=:id", id=mid)
            values = [dict(message) | {"id": uuid4(), "seq": i + 1} for i in range(80)]
            other = uuid4()
            values.append(dict(message) | {"id": uuid4(), "conversation_id": other})
            counter[0] = 0
            projected = await messages_projection(conn, values)
            assert counter[0] == 1, "Failure projection must issue one run batch, never per message"
            assert all(
                m.get("reply_failure", {}).get("code") == "MODEL_QUOTA_EXCEEDED" for m in projected[:-1]
            )
            assert projected[-1].get("reply_failure") is None
            await execute(conn, "UPDATE ag_run SET finished_at='2026-11-13T23:59:59Z' WHERE id=:id", id=rid)
            next_day = (await messages_projection(conn, [message]))[0]["reply_failure"]["retry_after"]
            assert datetime.fromisoformat(str(next_day).replace("Z", "+00:00")) == datetime(
                2026, 11, 14, tzinfo=UTC
            )
    finally:
        event.remove(svc.db.engine.sync_engine, "before_cursor_execute", count)
        await svc.close()


@pytest.mark.anyio
async def test_retry_cannot_reintroduce_an_old_question_after_a_new_member_turn(integration_config):
    svc = Services(settings_for(integration_config))
    buyer = Principal(1470000000 + uuid4().int % 10000000, "old retry context")
    try:
        cid, rid, _, claimed = await running(svc, buyer)
        await svc.runs.finish(claimed, "MODEL_STREAM_INTERRUPTED")
        old = await svc.runs.get(buyer, rid)
        sent = await svc.conversations.send(
            buyer,
            "synthetic-only",
            cid,
            str(uuid4()),
            {
                "client_message_id": str(uuid4()),
                "text": "改成新的规格和数量，旧问题不再沿用",
                "attachment_ids": [],
            },
        )
        latest = sent["run"]
        await svc.runs.stop(buyer, UUID(latest["id"]), str(uuid4()), {"expected_version": latest["version"]})
        with pytest.raises(DomainError, match="RUN_NOT_RETRYABLE"):
            await svc.conversations.retry_run(
                buyer, "synthetic-only", rid, str(uuid4()), {"expected_version": old["version"]}
            )
        async with svc.db.transaction() as conn:
            count = await one(
                conn,
                "SELECT count(*) n FROM ag_run WHERE trigger_message_id=(SELECT trigger_message_id FROM ag_run WHERE id=:id)",
                id=rid,
            )
            assert count["n"] == 1
    finally:
        await svc.close()
