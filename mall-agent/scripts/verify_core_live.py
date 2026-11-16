"""Real gateway/private objects/ASR/TTS acceptance using labelled disposable chat data."""

import argparse
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from expansion_client import ADMIN, MEMBER, api, login
from PIL import Image
from smoke_stream_ui import baseline

from app.infrastructure.audio import wav_info
from app.infrastructure.postgres.database import Database, append_events, execute, one, rows
from app.runtime import run

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "deployment/.run/core-functions-live-20261114.json"


def save(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


async def main(mode):
    state = (
        json.loads(STATE.read_text(encoding="utf-8"))
        if STATE.exists()
        else {
            "created_at": datetime.now(UTC).isoformat(),
            "checks": [],
            "keys": {},
            "buyer": "agent-lifecycle-6c72a0f5",
        }
    )
    cfg = json.loads((ROOT / "deployment/.run/agent-env.json").read_text())
    db = Database(cfg["databaseUrl"])

    def key(name):
        state["keys"].setdefault(name, str(uuid4()))
        save(state)
        return state["keys"][name]

    def passed(name):
        if name not in state["checks"]:
            state["checks"].append(name)
        save(state)
        print("PASS", name, flush=True)

    async with (
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=100, trust_env=False) as buyer,
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=100, trust_env=False) as staff,
    ):
        await login(buyer, username=state["buyer"])
        await login(staff, True)

        async def profile():
            me = await api(staff, ADMIN + "/me")
            return next(
                p for p in (await api(staff, ADMIN + "/staff?limit=100"))["items"] if p["id"] == me["id"]
            )

        try:
            if mode == "prepare":
                if "baseline" not in state:
                    state["baseline"] = baseline()
                    save(state)
                if not state.get("conversation_id"):
                    c = await api(
                        buyer,
                        MEMBER + "/conversations",
                        "POST",
                        {"title": "[TEST ONLY] 核心功能收尾验收", "entry_context": {"kind": "GENERAL"}},
                        key("conversation"),
                        201,
                    )
                    state["conversation_id"] = c["id"]
                    save(state)
                cid = state["conversation_id"]
                if not state.get("handoff"):
                    snap = await api(buyer, MEMBER + "/conversations/" + cid)
                    state["handoff"] = await api(
                        buyer,
                        MEMBER + f"/conversations/{cid}/handoff",
                        "POST",
                        {
                            "expected_conversation_version": snap["conversation"]["version"],
                            "reason": "[TEST ONLY] core feature acceptance",
                        },
                        key("handoff"),
                        201,
                    )
                    save(state)
                if not state.get("image"):
                    image = io.BytesIO()
                    Image.new("RGB", (180, 100), "#4b80bc").save(image, format="PNG")
                    response = await buyer.post(
                        MEMBER + "/attachments",
                        headers={"Idempotency-Key": key("image-upload")},
                        data={"purpose": "CHAT_IMAGE"},
                        files={"file": ("synthetic.png", image.getvalue(), "image/png")},
                    )
                    assert response.status_code == 201, (response.status_code, response.json().get("code"))
                    state["image"] = response.json()["data"]
                    save(state)
                if not state.get("image_message"):
                    state["image_message"] = await api(
                        buyer,
                        MEMBER + f"/conversations/{cid}/messages",
                        "POST",
                        {
                            "client_message_id": key("image-message"),
                            "text": "[TEST ONLY] 人工接待图片验收，图片为合成色块。",
                            "attachment_ids": [state["image"]["id"]],
                        },
                        key("image-message"),
                        202,
                    )
                    save(state)
                assert state["image_message"]["run"] is None
                passed("waiting_member_image_does_not_start_ai")
                current = await profile()
                if "initial_presence" not in state:
                    state["initial_presence"] = current["presence"]
                    save(state)
                await api(
                    staff,
                    ADMIN + "/presence",
                    "PUT",
                    {"expected_version": current["version"], "presence": "ONLINE"},
                )
                if not state.get("claimed"):
                    state["claimed"] = await api(
                        staff,
                        ADMIN + f"/handoffs/{state['handoff']['id']}/claim",
                        "POST",
                        {"expected_version": state["handoff"]["version"]},
                        key("claim"),
                    )
                    save(state)
                response = await staff.get(ADMIN + f"/attachments/{state['image']['id']}/content")
                assert response.status_code == 200 and response.content.startswith(b"\xff\xd8\xff")
                passed("assigned_staff_reads_private_member_image")
                if not state.get("message_id"):
                    async with db.transaction() as conn:
                        conv = await one(
                            conn, "SELECT * FROM ag_conversation WHERE id=:id FOR UPDATE", id=cid
                        )
                        mid = uuid4()
                        await execute(
                            conn,
                            """INSERT INTO ag_message(id,conversation_id,seq,sender_kind,sender_id,text_content,status,retention_until)
                            VALUES(:id,:cid,:seq,'ASSISTANT',0,:text,'COMPLETED',now()+interval '1 day')""",
                            id=mid,
                            cid=cid,
                            seq=conv["next_message_seq"],
                            text="【验收合成文本】商城语音功能测试。转写结果可以编辑，请核对内容后手动发送。",
                        )
                        updated = await one(
                            conn,
                            "UPDATE ag_conversation SET next_message_seq=next_message_seq+1,version=version+1 WHERE id=:id RETURNING *",
                            id=cid,
                        )
                        from app.application.views import message_view

                        msg = await one(conn, "SELECT * FROM ag_message WHERE id=:id", id=mid)
                        await append_events(
                            conn, updated, [("message.completed", {"message": message_view(msg)})]
                        )
                    state["message_id"] = str(mid)
                    state["answer_source"] = "EXPLICIT_SYNTHETIC_FIXTURE_NOT_MODEL_GENERATED"
                    save(state)
                mid = state["message_id"]
                if not state.get("feedback"):
                    first = await api(
                        buyer,
                        MEMBER + f"/messages/{mid}/feedback",
                        "POST",
                        {"vote": -1, "reason": "[TEST ONLY] 评价保存验收"},
                        key("feedback-negative"),
                    )
                    second = await api(
                        buyer,
                        MEMBER + f"/messages/{mid}/feedback",
                        "POST",
                        {"vote": 1, "reason": "[TEST ONLY] 更新评价验收"},
                        key("feedback-positive"),
                    )
                    assert first["id"] == second["id"]
                    state["feedback"] = second
                    save(state)
                current_feedback = await api(buyer, MEMBER + f"/messages/{mid}/feedback")
                assert current_feedback["id"] == state["feedback"]["id"] and current_feedback["vote"] in (
                    -1,
                    1,
                )
                state["current_feedback"] = current_feedback
                passed("feedback_saved_replaced_and_read_back")
                if not state.get("speech"):
                    state["speech"] = await api(
                        buyer, MEMBER + f"/messages/{mid}/speech", "POST", {"part": 0}, key("speech")
                    )
                    save(state)
                content = await buyer.get(
                    MEMBER + f"/attachments/{state['speech']['attachment']['id']}/content"
                )
                assert content.status_code == 200 and content.headers["content-type"].startswith("audio/wav")
                state["speech_duration_ms"] = wav_info(content.content)
                save(state)
                cached = await api(
                    buyer, MEMBER + f"/messages/{mid}/speech", "POST", {"part": 0}, key("speech-cache")
                )
                assert (
                    cached["cache_hit"] and cached["attachment"]["id"] == state["speech"]["attachment"]["id"]
                )
                passed("real_aliyun_tts_private_wav_and_cache")
                if not state.get("recording"):
                    response = await buyer.post(
                        MEMBER + "/attachments",
                        headers={"Idempotency-Key": key("recording-upload")},
                        data={"purpose": "RECORDING"},
                        files={"file": ("synthetic-spoken.wav", content.content, "audio/wav")},
                    )
                    assert response.status_code == 201, (response.status_code, response.json().get("code"))
                    state["recording"] = response.json()["data"]
                    save(state)
                if not state.get("transcription"):
                    state["transcription"] = await api(
                        buyer,
                        MEMBER + "/audio/transcriptions",
                        "POST",
                        {"attachment_id": state["recording"]["id"]},
                        key("transcription"),
                    )
                    save(state)
                assert state["transcription"]["editable"] and "商城" in state["transcription"]["text"]
                passed("real_aliyun_asr_returns_editable_text_without_sending")
                async with db.transaction(snapshot=True) as conn:
                    calls = await rows(
                        conn,
                        """SELECT c.id,c.capability,c.status,c.media_request_id,m.actor_kind,m.actor_id
                        FROM ag_model_call c JOIN ag_media_request r ON r.id=c.media_request_id JOIN ag_model_meter m ON m.call_id=c.id
                        WHERE r.member_id=(SELECT member_id FROM ag_conversation WHERE id=:cid) AND r.created_at>=:since""",
                        cid=cid,
                        since=datetime.fromisoformat(state["created_at"]),
                    )
                    assert len(calls) >= 2 and all(
                        r["status"] == "SUCCEEDED" and r["actor_kind"] == "MEMBER" for r in calls
                    )
                    state["voice_calls"] = [dict(r) for r in calls]
                    assert (
                        await one(conn, "SELECT count(*) n FROM ag_run WHERE conversation_id=:cid", cid=cid)
                    )["n"] == 0
                passed("all_real_voice_calls_including_failed_attempts_accounted_to_member_zero_ai_runs")
                save(state)
            elif mode == "rules":
                page = await api(staff, ADMIN + "/return-rules")
                assert page["items"] and page["active_rule_version"]
                state["rule_read"] = page
                passed("live_rule_versions_and_active_baseline_visible")
            else:
                cid = state["conversation_id"]
                snap = await buyer.get(MEMBER + "/conversations/" + cid)
                if snap.status_code == 200:
                    await api(
                        buyer,
                        MEMBER + "/conversations/" + cid,
                        "DELETE",
                        {"expected_version": snap.json()["data"]["conversation"]["version"]},
                    )
                for id in [state["image"]["id"], state["speech"]["attachment"]["id"]]:
                    assert (await buyer.get(MEMBER + f"/attachments/{id}/content")).status_code == 404
                assert (
                    await buyer.get(MEMBER + f"/messages/{state['message_id']}/feedback")
                ).status_code == 404
                current = await profile()
                await api(
                    staff,
                    ADMIN + "/presence",
                    "PUT",
                    {"expected_version": current["version"], "presence": state["initial_presence"]},
                )
                # Only the disposable recording created by this acceptance is
                # expired here. Ordinary recordings keep the configured policy.
                if state.get("recording"):
                    from app.application.jobs import wake_job

                    aid = state["recording"]["id"]
                    async with db.transaction() as conn:
                        item = await one(
                            conn,
                            "SELECT * FROM ag_attachment WHERE id=:id AND purpose='RECORDING' AND owner_kind='MEMBER' FOR UPDATE",
                            id=aid,
                        )
                        assert item and str(item["id"]) == aid
                        await execute(
                            conn,
                            "UPDATE ag_attachment SET state='DELETING',retention_until=now()-interval '1 second',updated_at=now(),version=version+1 WHERE id=:id",
                            id=aid,
                        )
                        await execute(
                            conn, "UPDATE ag_media_request SET result=NULL WHERE attachment_id=:id", id=aid
                        )
                        await execute(
                            conn,
                            "INSERT INTO ag_erasure_tombstone(resource_type,resource_id) VALUES('ATTACHMENT',:id) ON CONFLICT DO NOTHING",
                            id=aid,
                        )
                        job = await one(
                            conn,
                            """INSERT INTO ag_job(id,job_type,dedupe_key,parameters)
                            VALUES(:id,'ATTACHMENT_DELETE',:key,CAST(:parameters AS jsonb)) ON CONFLICT DO NOTHING RETURNING *""",
                            id=uuid4(),
                            key="attachment-delete:" + aid,
                            parameters=json.dumps({"attachment_id": aid}),
                        )
                        if job:
                            await wake_job(conn, job)
                    assert (await buyer.get(MEMBER + f"/attachments/{aid}/content")).status_code == 404
                original = state["baseline"]
                after = baseline()
                assert original["business"] == after["business"] and original["policies"] == after["policies"]
                state["cleaned"] = True
                passed("test_chat_deleted_private_images_speech_feedback_revoked_original_business_unchanged")
        finally:
            await db.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["prepare", "rules", "cleanup"])
    args = parser.parse_args()
    try:
        run(main(args.mode))
    except Exception as exc:
        print("ACCEPTANCE_FAILED", type(exc).__name__, str(exc)[:300])
        raise
