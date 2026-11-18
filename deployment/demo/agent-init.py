"""Create the demo runtime role and initialize PostgreSQL as its separate owner."""

import asyncio
import os
import subprocess
import sys

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import sql

uri = os.environ["AGENT_MIGRATION_URL"].replace("postgresql+psycopg://", "postgresql://", 1)


async def checkpoint():
    async with AsyncPostgresSaver.from_conn_string(
        uri + "?options=-csearch_path%3Dlanggraph_checkpoint"
    ) as saver:
        await saver.setup()


with psycopg.connect(uri) as conn:
    if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'mall_agent'").fetchone() is None:
        conn.execute("CREATE ROLE mall_agent LOGIN")
    conn.execute(sql.SQL("ALTER ROLE mall_agent PASSWORD {}").format(sql.Literal(os.environ["DEMO_POSTGRES_PASSWORD"])))

subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
asyncio.run(checkpoint())
with psycopg.connect(uri) as conn:
    conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    conn.execute("GRANT USAGE ON SCHEMA public, langgraph_checkpoint TO mall_agent")
    conn.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public, langgraph_checkpoint TO mall_agent")
    conn.execute("REVOKE ALL ON TABLE alembic_version FROM mall_agent")
    conn.execute("GRANT SELECT ON TABLE alembic_version TO mall_agent")
    conn.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public, langgraph_checkpoint TO mall_agent")
print("Agent database and checkpoint schema ready")
