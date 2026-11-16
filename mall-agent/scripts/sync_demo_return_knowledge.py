"""Publish the reviewed V2 return document as a new version, preserving V1."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import httpx
from expansion_client import ADMIN, api, login, wait_job

from app.runtime import run

ROOT = Path(__file__).resolve().parents[2]


async def main():
    source = (ROOT / "docs/demo-knowledge/演示明细退货规则.md").read_text(encoding="utf-8")
    sha = hashlib.sha256(source.encode()).hexdigest()
    original = json.loads(
        (ROOT / "deployment/.run/completion-demo-policy-publication-20261113.json").read_text(
            encoding="utf-8"
        )
    )["documents"]["demo_return"]
    output = ROOT / "deployment/.run/conservative-return-publication-20261116.json"
    state = (
        json.loads(output.read_text(encoding="utf-8"))
        if output.exists()
        else {
            "document_id": original["document_id"],
            "source_sha256": sha,
            "previous_version_id": original["version_id"],
            "started_at": datetime.now(UTC).isoformat(),
        }
    )
    if state["source_sha256"] != sha:
        raise RuntimeError("Reviewed source changed after publication began")

    def save():
        output.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def key(step):
        return str(uuid5(NAMESPACE_URL, "newshop/return-knowledge/" + sha + "/" + step))

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", trust_env=False, timeout=60) as client:
        await login(client, True)
        page = await api(client, ADMIN + "/return-rules")
        active = next(r for r in page["items"] if r["rule_version"] == page["active_rule_version"])
        if (
            active["rule_version"] != "ITEM_NET_DEMO_V2"
            or active["return_window_hours"] != 168
            or active["allow_shipped"] is not True
        ):
            raise RuntimeError("Reviewed document differs from active Java rules; review before publishing")
        save()
        if not state.get("version_id"):
            version = await api(
                client,
                ADMIN + f"/kb/documents/{state['document_id']}/versions",
                "POST",
                {
                    "source_text": source,
                    "change_note": "核对业务规则V2（168小时、允许已发货申请），同步资料版本；保留旧版本与历史引用。",
                },
                key=key("version"),
                expected=201,
            )
            state["version_id"] = version["id"]
            save()
        for operation, ready in [
            ("parse", {"PREVIEW_READY", "INDEXING", "READY", "PUBLISHED"}),
            ("build", {"READY", "PUBLISHED"}),
        ]:
            version = await api(client, ADMIN + f"/kb/versions/{state['version_id']}")
            if version["state"] in ready:
                continue
            field = operation + "_job_id"
            if not state.get(field):
                job = await api(
                    client,
                    ADMIN + f"/kb/versions/{state['version_id']}/{operation}",
                    "POST",
                    {"expected_version": version["version"]},
                    key=key(operation),
                    expected=202,
                )
                state[field] = job["id"]
                save()
            await wait_job(client, state[field], timeout=300)
        # Recheck the rule after indexing; a concurrent business publication wins.
        latest = await api(client, ADMIN + "/return-rules")
        if latest["active_rule_version"] != active["rule_version"]:
            raise RuntimeError("Rule changed during indexing; reviewed knowledge was not published")
        doc = await api(client, ADMIN + f"/kb/documents/{state['document_id']}")
        if doc["active_version_id"] != state["version_id"]:
            if doc["active_version_id"] != state["previous_version_id"]:
                raise RuntimeError("An operator published another document version; refusing to replace it")
            doc = await api(
                client,
                ADMIN + f"/kb/documents/{doc['id']}/publish",
                "POST",
                {"expected_document_version": doc["version"], "version_id": state["version_id"]},
                key=key("publish"),
            )
        state.update(
            result="passed",
            active_rule_version=active["rule_version"],
            document_version=doc["version"],
            finished_at=datetime.now(UTC).isoformat(),
        )
        save()
        print("PASS reviewed V2 knowledge published; previous version retained")


if __name__ == "__main__":
    run(main())
