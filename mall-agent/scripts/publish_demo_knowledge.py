"""Publish versioned demo policies, with resumable API keys and source hashes."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import httpx
from expansion_client import ADMIN, api, login, wait_job

from app.runtime import run

ROOT = Path(__file__).resolve().parents[2]
SOURCES = {
    "demo_service": "docs/demo-knowledge/演示支付退款与物流说明.md",
    "demo_return": "docs/demo-knowledge/演示明细退货规则.md",
    "demo_confirmation": "docs/demo-knowledge/确认操作与异常核对说明.md",
}


async def main():
    output = ROOT / "deployment/.run/completion-demo-policy-publication-20261113.json"
    state = (
        json.loads(output.read_text(encoding="utf-8"))
        if output.exists()
        else {"started_at": datetime.now(UTC).isoformat(), "documents": {}}
    )

    def save():
        output.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", trust_env=False, timeout=60) as client:
        await login(client, True)
        for name, relative in SOURCES.items():
            source = (ROOT / relative).read_text(encoding="utf-8")
            sha = hashlib.sha256(source.encode()).hexdigest()
            record = state["documents"].setdefault(name, {"path": relative, "source_sha256": sha})
            if record["source_sha256"] != sha:
                raise RuntimeError("Published source changed; create a new reviewed version")

            def key(kind):
                return str(uuid5(NAMESPACE_URL, f"newshop/demo-knowledge/{name}/{sha}/{kind}"))

            title = "[演示规则] " + source.splitlines()[0].lstrip("# ")
            if not record.get("document_id"):
                doc = await api(
                    client,
                    ADMIN + "/kb/documents",
                    "POST",
                    {
                        "title": title,
                        "category": "DEMO_SERVICE_20261113",
                        "visibility": "MEMBER",
                        "scope": {"product_ids": [], "category_ids": []},
                    },
                    key=key("document"),
                    expected=201,
                )
                record["document_id"] = doc["id"]
                save()
            if not record.get("version_id"):
                version = await api(
                    client,
                    ADMIN + f"/kb/documents/{record['document_id']}/versions",
                    "POST",
                    {
                        "source_text": source,
                        "change_note": "发布经当前演示实现核对的规则；仅演示，不接入真实支付退款物流。",
                    },
                    key=key("version"),
                    expected=201,
                )
                record["version_id"] = version["id"]
                save()
            for operation, ready in [
                ("parse", {"PREVIEW_READY", "INDEXING", "READY", "PUBLISHED"}),
                ("build", {"READY", "PUBLISHED"}),
            ]:
                version = await api(client, ADMIN + f"/kb/versions/{record['version_id']}")
                if version["state"] in ready:
                    continue
                field = operation + "_job_id"
                if not record.get(field):
                    job = await api(
                        client,
                        ADMIN + f"/kb/versions/{version['id']}/{operation}",
                        "POST",
                        {"expected_version": version["version"]},
                        key=key(operation),
                        expected=202,
                    )
                    record[field] = job["id"]
                    save()
                await wait_job(client, record[field], timeout=300)
            doc = await api(client, ADMIN + f"/kb/documents/{record['document_id']}")
            if doc["active_version_id"] != record["version_id"]:
                doc = await api(
                    client,
                    ADMIN + f"/kb/documents/{doc['id']}/publish",
                    "POST",
                    {"expected_document_version": doc["version"], "version_id": record["version_id"]},
                    key=key("publish"),
                )
            if not doc["enabled"]:
                doc = await api(
                    client,
                    ADMIN + f"/kb/documents/{doc['id']}/enabled",
                    "PUT",
                    {"expected_version": doc["version"], "enabled": True},
                    key=key("enable"),
                )
            record.update(
                published=True,
                enabled=doc["enabled"],
                document_version=doc["version"],
                published_at=datetime.now(UTC).isoformat(),
            )
            save()
            print("PASS published demo policy:", name, flush=True)
    state["result"] = "passed"
    save()


if __name__ == "__main__":
    run(main())
