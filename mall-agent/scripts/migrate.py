"""Run reviewed migrations with owner credentials, then restrict runtime access."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import sql

root = Path(__file__).resolve().parents[2]
config = json.loads((root / "deployment/.run/agent-env.json").read_text())
config["migrationUrl"] = os.environ.get("AGENT_MIGRATION_URL", config["migrationUrl"])
os.environ["AGENT_MIGRATION_URL"] = config["migrationUrl"]
subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=root / "mall-agent", check=True)


async def setup():
    uri = config["migrationUrl"].replace("postgresql+psycopg://", "postgresql://")
    async with AsyncPostgresSaver.from_conn_string(
        uri + "?options=-csearch_path%3Dlanggraph_checkpoint"
    ) as saver:
        await saver.setup()


if sys.platform == "win32":
    import selectors

    asyncio.run(setup(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
else:
    asyncio.run(setup())
with psycopg.connect(config["migrationUrl"].replace("postgresql+psycopg://", "postgresql://")) as conn:
    conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    conn.execute("GRANT USAGE ON SCHEMA public,langgraph_checkpoint TO mall_agent")
    conn.execute(
        "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public,langgraph_checkpoint TO mall_agent"
    )
    conn.execute("REVOKE ALL ON TABLE alembic_version FROM mall_agent")
    conn.execute("GRANT SELECT ON TABLE alembic_version TO mall_agent")
    conn.execute("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public,langgraph_checkpoint TO mall_agent")
    conn.execute(sql.SQL("ALTER ROLE mall_agent SET search_path TO public"))
print("Migrations at head and library checkpoint setup complete; runtime cannot create tables")
