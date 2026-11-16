"""Exercise the live knowledge loop with clearly marked synthetic evidence only."""

import asyncio
import hashlib
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator, RefResolver

from app.runtime import run


async def main(persistence=False):
    root = Path(__file__).resolve().parents[2]
    path = root / "deployment/.run/knowledge-smoke.json"
    contract = json.loads((root / "docs/api/openapi.json").read_text(encoding="utf-8"))
    resolver = RefResolver.from_schema(contract)
    evidence = {"time": datetime.now(UTC).isoformat(), "checks": []}
    async with (
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=60, trust_env=False) as staff,
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=45, trust_env=False) as member,
    ):
        for client, url, body, form in [
            (staff, "/mall-admin/admin/login", {"username": "admin", "password": "MallDemo2026!"}, False),
            (member, "/mall-portal/sso/login", {"username": "member", "password": "MallDemo2026!"}, True),
        ]:
            response = await client.post(url, **({"data": body} if form else {"json": body}))
            data = response.json()
            assert data.get("code") == 200, "Demo login rejected"
            client.headers["Authorization"] = data["data"]["tokenHead"] + data["data"]["token"]

        async def api(client, url, method="GET", body=None, key=None, schema=None, expected=200):
            r = await client.request(
                method,
                url,
                **({"json": body} if body is not None else {}),
                headers={"Idempotency-Key": str(key or uuid4())} if method != "GET" else {},
            )
            assert r.status_code == expected, (url, r.status_code, r.json().get("code"))
            result = r.json()["data"]
            if schema:
                Draft202012Validator(contract["components"]["schemas"][schema], resolver=resolver).validate(
                    result
                )
            return result

        admin = "/mall-agent/v1/admin"
        buyer = "/mall-agent/v1/member"

        def passed(name):
            evidence["checks"].append(name)
            print("PASS", name, flush=True)

        if persistence:
            evidence = json.loads(path.read_text(encoding="utf-8"))
            snapshot = await api(member, buyer + "/conversations/" + evidence["conversation_id"])
            assert any(evidence["second_code"] in m["text"] for m in snapshot["recent_messages"])
            ref = evidence["last_source"]
            r = await member.get(buyer + f"/citations/{ref['version_id']}/chunks/{ref['chunk_id']}")
            assert r.status_code == 410
            passed("knowledge_message_and_revoked_citation_survive_api_restart")
            path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            return
        me = await api(staff, admin + "/me")
        assert {"agent:kb:read", "agent:kb:edit", "agent:kb:publish"}.issubset(me["permissions"])
        passed("fresh_staff_knowledge_permissions")
        code = "KB-" + secrets.token_hex(4).upper()
        second = "KB-" + secrets.token_hex(4).upper()
        title = "[TEST ONLY] 配送演示方案说明资料 " + secrets.token_hex(3)
        doc = None

        async def task(v, kind):
            job = await api(
                staff,
                admin + f"/kb/versions/{v['id']}/{kind}",
                "POST",
                {"expected_version": v["version"]},
                schema="Job",
                expected=202,
            )
            for _ in range(150):
                j = await api(staff, admin + "/jobs/" + job["id"], schema="Job")
                if j["state"] == "FAILED":
                    raise AssertionError("Knowledge task failed: " + str(j["error_code"]))
                if j["state"] == "SUCCEEDED":
                    return await api(staff, admin + "/kb/versions/" + v["id"], schema="KBVersion")
                await asyncio.sleep(1)
            raise AssertionError("Knowledge task did not finish")

        async def ask(cid):
            await api(
                member,
                buyer + f"/conversations/{cid}/messages",
                "POST",
                {
                    "client_message_id": str(uuid4()),
                    "text": "配送演示方案说明资料中的公开演示方案名称是什么？请根据当前已发布知识资料回答。",
                    "attachment_ids": [],
                },
                expected=202,
            )
            for _ in range(150):
                snap = await api(member, buyer + "/conversations/" + cid)
                if not snap["active_run"]:
                    messages = [m for m in snap["recent_messages"] if m["sender_kind"] == "ASSISTANT"]
                    assert messages and messages[-1]["status"] == "COMPLETED", [
                        (m["status"]) for m in messages
                    ]
                    return messages[-1]
                await asyncio.sleep(1)
            raise AssertionError("Knowledge answer did not finish")

        try:
            body = {
                "title": title,
                "category": "TEST_ONLY",
                "visibility": "MEMBER",
                "scope": {"product_ids": [], "category_ids": []},
            }
            key = uuid4()
            doc = await api(
                staff, admin + "/kb/documents", "POST", body, key, schema="KBDocument", expected=201
            )
            assert (await api(staff, admin + "/kb/documents", "POST", body, key, expected=201))["id"] == doc[
                "id"
            ]
            raw = f"# 配送演示方案说明资料\n本资料仅用于自动验收，不是正式店铺政策。\n本版配送演示公开演示方案名称为 {code}。\n方案名称是公开的合成测试数据，只用于验证资料发布与引用链路。".encode()
            upload_key = uuid4()

            async def upload():
                r = await staff.post(
                    admin + "/attachments",
                    headers={"Idempotency-Key": str(upload_key)},
                    data={"purpose": "KNOWLEDGE"},
                    files={"file": ("synthetic-knowledge.md", raw, "text/markdown")},
                )
                assert r.status_code == 201, ("upload", r.status_code, r.json().get("code"))
                return r.json()["data"]

            attachment = await upload()
            assert (await upload())["id"] == attachment["id"]
            original = await staff.get(admin + f"/attachments/{attachment['id']}/content")
            assert (
                original.status_code == 200
                and hashlib.sha256(original.content).digest() == hashlib.sha256(raw).digest()
            )
            passed("private_original_upload_replay_and_authenticated_read")
            v = await api(
                staff,
                admin + f"/kb/documents/{doc['id']}/versions",
                "POST",
                {
                    "source_attachment_id": attachment["id"],
                    "change_note": "Synthetic acceptance, not business policy",
                },
                schema="KBVersion",
                expected=201,
            )
            blocked = await staff.post(
                admin + f"/kb/documents/{doc['id']}/publish",
                headers={"Idempotency-Key": str(uuid4())},
                json={"expected_document_version": doc["version"], "version_id": v["id"]},
            )
            assert blocked.status_code == 409
            v = await task(v, "parse")
            preview = await api(staff, admin + f"/kb/versions/{v['id']}/preview", schema="KBPreview")
            assert code in preview["parsed_text"]
            v = await api(
                staff,
                admin + f"/kb/versions/{v['id']}/preview",
                "PATCH",
                {
                    "expected_version": v["version"],
                    "parsed_text": preview["parsed_text"] + "\n验收人员已核对：仅为演示测试。",
                },
                schema="KBVersion",
            )
            v = await task(v, "build")
            assert v["quality_passed"] is True and v["chunk_count"] > 0
            assert not (await api(staff, admin + "/kb/documents/" + doc["id"]))["active_version_id"]
            passed("parse_preview_edit_real_embedding_milvus_build_without_auto_publish")
            doc = await api(
                staff,
                admin + f"/kb/documents/{doc['id']}/publish",
                "POST",
                {"expected_document_version": doc["version"], "version_id": v["id"]},
                schema="KBDocument",
            )
            conv = await api(
                member,
                buyer + "/conversations",
                "POST",
                {"title": "[TEST ONLY] 知识闭环验收", "entry_context": {"kind": "GENERAL"}},
                expected=201,
            )
            message = await ask(conv["id"])
            assert code in message["text"]
            sources = [b["source"] for b in message["blocks"] if b["type"] == "source"]
            assert sources and all(s["version_id"] == v["id"] for s in sources)
            source = sources[0]
            content = await api(
                member,
                buyer + f"/citations/{source['version_id']}/chunks/{source['chunk_id']}",
                schema="SourceContent",
            )
            assert code in content["excerpt"]
            passed("real_gpt_knowledge_answer_with_readable_authorized_citation")
            old = source
            v2 = await api(
                staff,
                admin + f"/kb/documents/{doc['id']}/versions",
                "POST",
                {
                    "source_text": f"配送演示方案说明资料，仅为测试，不是正式政策。本版唯一公开演示方案名称为 {second}。",
                    "change_note": "Synthetic replacement",
                },
                schema="KBVersion",
                expected=201,
            )
            v2 = await task(v2, "parse")
            v2 = await task(v2, "build")
            doc = await api(
                staff,
                admin + f"/kb/documents/{doc['id']}/publish",
                "POST",
                {"expected_document_version": doc["version"], "version_id": v2["id"]},
                schema="KBDocument",
            )
            stale = await member.get(buyer + f"/citations/{old['version_id']}/chunks/{old['chunk_id']}")
            assert stale.status_code == 410
            message2 = await ask(conv["id"])
            assert second in message2["text"] and code not in message2["text"]
            sources2 = [b["source"] for b in message2["blocks"] if b["type"] == "source"]
            assert sources2 and all(s["version_id"] == v2["id"] for s in sources2)
            passed("version_replacement_old_citation_410_and_new_answer_uses_current_source")
            doc = await api(
                staff,
                admin + f"/kb/documents/{doc['id']}/enabled",
                "PUT",
                {"expected_version": doc["version"], "enabled": False},
                schema="KBDocument",
            )
            source = sources2[0]
            r = await member.get(buyer + f"/citations/{source['version_id']}/chunks/{source['chunk_id']}")
            assert r.status_code == 410
            message3 = await ask(conv["id"])
            assert second not in message3["text"] and not any(
                b["type"] == "source" for b in message3["blocks"]
            )
            passed("disabled_document_stops_retrieval_and_source_reading")
            evidence.update(
                document_id=doc["id"],
                first_version_id=v["id"],
                second_version_id=v2["id"],
                conversation_id=conv["id"],
                second_code=second,
                last_source=source,
                answer_message_id=message2["id"],
            )
            path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            if doc:
                latest = await api(staff, admin + "/kb/documents/" + doc["id"])
                if latest["enabled"]:
                    await api(
                        staff,
                        admin + f"/kb/documents/{doc['id']}/enabled",
                        "PUT",
                        {"expected_version": latest["version"], "enabled": False},
                    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--persistence", action="store_true")
    run(main(parser.parse_args().persistence))
