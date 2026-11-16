"""Real read-only probes and fixed local negative cases; never calls paid models."""

import asyncio
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.application.observability import readiness
from app.infrastructure.dependency_probes import measured, private_objects, signed_bridge
from app.runtime import run
from app.settings import Settings

ROOT = Path(__file__).resolve().parents[2]


async def main():
    config = json.loads((ROOT / "deployment/.run/agent-env.json").read_text(encoding="utf-8"))
    vector = json.loads((ROOT / "deployment/.run/milvus-env.json").read_text(encoding="utf-8"))
    settings = Settings(
        database_url=config["databaseUrl"],
        bridge_secret=config["bridgeSecret"],
        broker_secret=config["brokerSecret"],
        cursor_secret=config["cursorSecret"],
        milvus_reader_user=vector["readerUser"],
        milvus_reader_password=vector["readerPassword"],
        milvus_database=vector["database"],
    )
    spec = importlib.util.spec_from_file_location("monitor_agent", ROOT / "deployment/monitor-agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = {"started_at": datetime.now(UTC).isoformat(), "paid_model_calls": 0, "business_commands": 0}
    async with httpx.AsyncClient(timeout=6, trust_env=False) as client:
        snapshot = await module.observation(settings, client)
        assert all(sample.state == "UP" for sample in snapshot.dependencies.values()), snapshot.model_dump(
            mode="json"
        )
        assert readiness(snapshot, settings)[0]
        result["healthy_snapshot"] = snapshot.model_dump(mode="json")
        invalid = settings.model_copy(
            update={"bridge_secret": __import__("pydantic").SecretStr("invalid-test-key")}
        )
        broken = await measured(lambda: signed_bridge(client, invalid))
        assert broken.state == "DOWN"
        refused = settings.model_copy(update={"objects_endpoint": "127.0.0.1:1"})
        missing = await measured(lambda: asyncio.to_thread(private_objects, refused, None))
        assert missing.state == "DOWN"
        result["bad_bridge_signature"] = broken.model_dump(mode="json")
        result["closed_private_object_endpoint"] = missing.model_dump(mode="json")
        restored = await measured(lambda: signed_bridge(client, settings))
        assert restored.state == "UP"
        result["original_bridge_after_negative_case"] = restored.model_dump(mode="json")
        response = await client.get("http://127.0.0.1:8099/readyz")
        assert response.status_code == 200
        result["ready"] = response.json()
        result["synthetic_failure_readiness"] = readiness(
            snapshot.model_copy(
                update={"dependencies": {**snapshot.dependencies, "private_objects": missing}}
            ),
            settings,
        )[0]
        assert result["synthetic_failure_readiness"] is False
    result["result"] = "REAL_READ_ONLY_PROBES_AND_BOUNDED_NEGATIVE_CASES_PASSED"
    result["finished_at"] = datetime.now(UTC).isoformat()
    (ROOT / "deployment/.run/completion-observability-live-20261113.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        "PASS authenticated dependency probes, cloud/PG/Java outbox snapshot, bad signature, closed local endpoint and healthy recovery"
    )


if __name__ == "__main__":
    run(main())
