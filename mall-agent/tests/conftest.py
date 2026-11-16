import asyncio
import json
import os
import selectors
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest


class PrivateIntegrationConfig(dict):
    def __repr__(self):
        return "<isolated database configuration; credentials redacted>"


@pytest.fixture(autouse=True)
def no_paid_provider_in_tests(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "fixture")
    monkeypatch.setenv("AGENT_ATTACHMENTS_ENABLED", "false")
    monkeypatch.setenv("AGENT_VISION_ENABLED", "false")
    monkeypatch.setenv("AGENT_NOTIFICATIONS_ENABLED", "false")
    monkeypatch.setenv("AGENT_STAFF_BRIDGE_SECRET", "")
    monkeypatch.setenv("AGENT_KNOWLEDGE_ENABLED", "false")
    monkeypatch.setenv("AGENT_HANDOFFS_ENABLED", "false")
    monkeypatch.setenv("AGENT_ACTIONS_ENABLED", "false")
    monkeypatch.setenv("AGENT_ORDERS_ENABLED", "false")
    monkeypatch.setenv("AGENT_GOVERNANCE_ENABLED", "false")
    monkeypatch.setenv("AGENT_RETENTION_ENABLED", "false")
    monkeypatch.setenv("AGENT_BACKUPS_ENABLED", "false")
    monkeypatch.setenv("AGENT_OBSERVABILITY_ENABLED", "false")


@pytest.fixture(scope="session")
def integration_config():
    path = Path(__file__).resolve().parents[2] / "deployment/.run/agent-env.json"
    if not path.exists():
        pytest.skip("Dedicated agent environment has not been prepared")
    config = json.loads(path.read_text(encoding="utf-8"))
    owner = config["migrationUrl"].replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(owner.rsplit("/", 1)[0] + "/postgres", autocommit=True) as conn:
        if not conn.execute(
            "SELECT 1 FROM pg_database WHERE datname='mall_agent_architecture_test'"
        ).fetchone():
            conn.execute("CREATE DATABASE mall_agent_architecture_test OWNER mall_agent_owner")
    config = dict(config)
    config["databaseUrl"] = config["databaseUrl"].rsplit("/", 1)[0] + "/mall_agent_architecture_test"
    config["migrationUrl"] = config["migrationUrl"].rsplit("/", 1)[0] + "/mall_agent_architecture_test"
    agent_root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, "scripts/migrate.py"],
        cwd=agent_root,
        env={**os.environ, "AGENT_MIGRATION_URL": config["migrationUrl"]},
        check=True,
    )
    # Dedicated test DB only: make repeated runs deterministic after a previously interrupted test.
    test_owner = config["migrationUrl"].replace("postgresql+psycopg://", "postgresql://")
    if test_owner.rsplit("/", 1)[-1] != "mall_agent_architecture_test":
        raise RuntimeError("Refusing to reset a database outside the isolated test target")
    with psycopg.connect(test_owner) as conn:
        conn.execute(
            "UPDATE ag_run SET state='INTERRUPTED',error_code='TEST_SESSION_RESET',lease_owner=NULL,lease_until=NULL,finished_at=now() WHERE state IN ('QUEUED','RUNNING','STOP_REQUESTED')"
        )
        conn.execute("UPDATE ag_message SET status='INTERRUPTED' WHERE status='GENERATING'")
        conn.execute("UPDATE ag_generation_slot SET run_id=NULL,lease_until=NULL")
        conn.execute("DELETE FROM ag_job_wakeup")
        conn.execute("DELETE FROM ag_kb_citation")
        conn.execute("UPDATE ag_kb_document SET active_version_id=NULL")
        conn.execute("DELETE FROM ag_attachment_ref WHERE kb_version_id IS NOT NULL")
        conn.execute("DELETE FROM ag_kb_chunk")
        conn.execute("DELETE FROM ag_kb_version")
        conn.execute("DELETE FROM ag_kb_document")
        conn.execute("DELETE FROM ag_retention_plan")
        # Job-linked model calls now use the existing FK. Clear only their test
        # accounting before resetting jobs in this dedicated database.
        conn.execute(
            "DELETE FROM ag_quota_reservation WHERE call_id IN (SELECT id FROM ag_model_call WHERE job_id IS NOT NULL)"
        )
        conn.execute(
            "DELETE FROM ag_model_meter WHERE call_id IN (SELECT id FROM ag_model_call WHERE job_id IS NOT NULL)"
        )
        conn.execute("DELETE FROM ag_model_call WHERE job_id IS NOT NULL")
        conn.execute("DELETE FROM ag_job")
    return PrivateIntegrationConfig(config)


@pytest.fixture
def anyio_backend():
    return "asyncio", {"loop_factory": lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())}
