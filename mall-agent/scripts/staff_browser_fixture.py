"""Prepare/clean labelled UI acceptance data; never starts a model or trade operation."""

import io
import json
from pathlib import Path
from uuid import uuid4

import httpx
from expansion_client import ADMIN, MEMBER, api, login
from PIL import Image

from app.runtime import run

STATE = Path(__file__).resolve().parents[2] / "deployment/.run/staff-browser-fixture.json"


async def main(cleanup=False):
    async with (
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=40, trust_env=False) as staff,
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=40, trust_env=False) as member,
    ):
        await login(staff, True)
        await login(member, username="agent-conversation-bd63b203")
        me = await api(staff, ADMIN + "/me")

        async def profile():
            return next(
                s for s in (await api(staff, ADMIN + "/staff?limit=100"))["items"] if s["id"] == me["id"]
            )

        if cleanup:
            state = json.loads(STATE.read_text(encoding="utf-8"))
            response = await member.get(MEMBER + "/conversations/" + state["conversation_id"])
            if response.status_code == 200:
                snap = response.json()["data"]
                await api(
                    member,
                    MEMBER + "/conversations/" + state["conversation_id"],
                    "DELETE",
                    {"expected_version": snap["conversation"]["version"]},
                )
            current = await profile()
            await api(
                staff,
                ADMIN + "/presence",
                "PUT",
                {"expected_version": current["version"], "presence": state["initial_presence"]},
            )
            state["deleted"] = True
            STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            print("PASS staff browser fixture deleted and presence restored")
            return
        current = await profile()
        state = {"initial_presence": current["presence"]}
        await api(
            staff, ADMIN + "/presence", "PUT", {"expected_version": current["version"], "presence": "ONLINE"}
        )
        conv = await api(
            member,
            MEMBER + "/conversations",
            "POST",
            {"title": "[TEST ONLY] 员工页面图片卡片验收", "entry_context": {"kind": "GENERAL"}},
            expected=201,
        )
        cid = conv["id"]
        state["conversation_id"] = cid
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        h = await api(
            member,
            MEMBER + f"/conversations/{cid}/handoff",
            "POST",
            {"expected_conversation_version": conv["version"], "reason": "[TEST ONLY] browser acceptance"},
            expected=201,
        )
        await api(staff, ADMIN + f"/handoffs/{h['id']}/claim", "POST", {"expected_version": h["version"]})
        key = str(uuid4())
        await api(
            member,
            MEMBER + f"/conversations/{cid}/messages",
            "POST",
            {"client_message_id": key, "text": "[TEST ONLY] 请协助核对商品参数。", "attachment_ids": []},
            expected=202,
        )
        raw = io.BytesIO()
        Image.new("RGB", (120, 80), "blue").save(raw, format="PNG")
        response = await staff.post(
            ADMIN + "/attachments",
            headers={"Idempotency-Key": str(uuid4())},
            data={"purpose": "CHAT_IMAGE"},
            files={"file": ("synthetic-blue.png", raw.getvalue(), "image/png")},
        )
        assert response.status_code == 201
        aid = response.json()["data"]["id"]
        key = str(uuid4())
        await api(
            staff,
            ADMIN + f"/conversations/{cid}/messages",
            "POST",
            {
                "client_message_id": key,
                "text": "[TEST ONLY] 图片及商品卡片已附上，请核对当前商城详情。",
                "attachment_ids": [aid],
                "cards": [{"kind": "PRODUCT", "id": "45"}],
            },
            key,
            201,
        )
        await api(
            staff,
            ADMIN + f"/conversations/{cid}/notes",
            "POST",
            {"client_note_id": str(uuid4()), "body": "[TEST ONLY] 内部备注不会发送给会员或模型。"},
            expected=201,
        )
        print("PASS labelled staff browser fixture prepared")


if __name__ == "__main__":
    import sys

    run(main("--cleanup" in sys.argv))
