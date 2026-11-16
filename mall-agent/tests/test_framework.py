import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.agents.workflow import WorkflowContext, build_workflow_graph
from app.application.extensions import Extensions
from app.application.jobs import JobHandlers
from app.bootstrap import Services
from app.domain.capabilities import JobLease, JobType
from app.domain.identity import DomainError
from app.framework import SCAFFOLDS
from app.main import create_app
from app.settings import Settings


def test_framework_cli_ignores_live_environment_and_runs_outside_project(monkeypatch, tmp_path):
    for name in ("ACTIONS", "ORDERS", "KNOWLEDGE", "HANDOFFS", "ATTACHMENTS", "VISION", "NOTIFICATIONS"):
        monkeypatch.setenv(f"AGENT_{name}_ENABLED", "true")
    monkeypatch.setenv("AGENT_PROVIDER", "invalid-live-provider")
    monkeypatch.setenv("AGENT_DATABASE_URL", "invalid-live-database")
    monkeypatch.setenv("AGENT_DIALOG_BASE_URL", "http://untrusted.invalid")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    script = Path(__file__).resolve().parents[1] / "scripts/check_framework.py"
    result = subprocess.run(
        [sys.executable, str(script)], cwd=tmp_path, capture_output=True, text=True, timeout=30, check=True
    )
    report = json.loads(result.stdout)
    assert report["result"] == "passed" and report["external_io"] is False
    assert report["modules"] == 20 and report["active_python_operations"] == 103


def framework_settings():
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://framework:framework@127.0.0.1:1/framework",
        bridge_secret="unit-bridge",
        broker_secret="unit-broker",
        cursor_secret="unit-cursor",
        provider="fixture",
        attachments_enabled=False,
        vision_enabled=False,
    )


@pytest.mark.anyio
async def test_framework_owns_all_contracts_without_opening_reserved_apis(monkeypatch):
    settings = framework_settings()
    svc = Services(settings)
    app = create_app(settings)
    app.state.services = svc
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            manifest = (await client.get("/internal/framework")).json()
            assert manifest["module_count"] == 20
            assert manifest["contract_operation_count"] == 135
            assert manifest["active_python_operation_count"] == 103
            assert manifest["business"] == "PARTIAL"
            assert all(module["scaffold"] == "BOUND" for module in manifest["modules"])
            assert manifest["extensions"]["model_admin"]["bound"]
            assert all(
                not slot["bound"] for name, slot in manifest["extensions"].items() if name != "model_admin"
            )
            assert "unit-broker" not in str(manifest)
            assert (await client.get("/mall-agent/v1/admin/knowledge/documents")).status_code == 404
            active = app.openapi()["paths"]
            assert "/mall-agent/v1/admin/jobs" in active
            assert "/mall-agent/v1/admin/knowledge/documents" not in active
        original = SCAFFOLDS["M01"]
        monkeypatch.setitem(SCAFFOLDS, "M01", (original[0], original[1], ("M04",), original[3]))
        with pytest.raises(ValueError, match="cycle"):
            svc.framework.validate()
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_deferred_modules_and_jobs_never_acknowledge_unimplemented_work():
    extensions = Extensions()
    for slot in vars(extensions).values():
        with pytest.raises(DomainError) as unavailable:
            slot.require()
        assert unavailable.value.code == "FEATURE_NOT_ACTIVE"
    jobs = JobHandlers()
    lease = JobLease(uuid4(), JobType.KB_PARSE, "unit-owner", 1, {"version_id": str(uuid4())})
    with pytest.raises(DomainError) as missing:
        await jobs.dispatch(lease)
    assert missing.value.code == "JOB_HANDLER_NOT_ACTIVE"
    called = []

    async def handler(job):
        called.append(job.id)
        return {"parsed": True}

    jobs.register(JobType.KB_PARSE, handler)
    with pytest.raises(ValueError, match="Duplicate"):
        jobs.register(JobType.KB_PARSE, handler)
    assert await jobs.dispatch(lease) == {"parsed": True}
    with pytest.raises(DomainError):
        await jobs.dispatch(replace(lease, kind=JobType.RETENTION_EXECUTE))
    assert called == [lease.id]


@pytest.mark.anyio
async def test_full_graph_routes_through_guards_and_cannot_execute_transactions():
    graph = build_workflow_graph()
    assert set(graph.get_graph().nodes) - {"__start__", "__end__"} == {
        f"N{number:02}" for number in range(1, 13)
    }
    called, guarded = [], []

    async def guard(state):
        guarded.append(state.get("steps_used", 0))
        return {}

    def handler(node_id):
        async def invoke(state):
            called.append(node_id)
            if node_id == "N03":
                return {"intent": "ORDER"}
            return {}

        return invoke

    context = WorkflowContext(guard, {f"N{n:02}": handler(f"N{n:02}") for n in range(1, 13)})
    result = await graph.ainvoke(
        {"schema_version": "agent-state-v1", "question": "订单咨询"}, context=context
    )
    assert called == ["N01", "N02", "N03", "N07", "N10", "N11", "N12"]
    assert guarded == list(range(7)) and result["steps_used"] == 7
    assert not any("execute" in name or "confirm" in name for name in graph.get_graph().nodes)

    async def stopped(state):
        raise DomainError("LEASE_LOST", "已停止")

    called.clear()
    with pytest.raises(DomainError) as denied:
        await graph.ainvoke({"question": "测试"}, context=WorkflowContext(stopped, context.nodes))
    assert denied.value.code == "LEASE_LOST" and not called

    with pytest.raises(DomainError) as inactive:
        await graph.ainvoke({"question": "测试"}, context=WorkflowContext(guard))
    assert inactive.value.code == "WORKFLOW_NODE_NOT_ACTIVE"
    with pytest.raises(DomainError) as budget:
        await graph.ainvoke({"question": "测试"}, context=WorkflowContext(guard, context.nodes, max_steps=2))
    assert budget.value.code == "WORKFLOW_BUDGET_EXCEEDED"
