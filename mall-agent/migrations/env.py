import os

from alembic import context
from sqlalchemy import create_engine

# Owner credentials are only supplied to this migration process, never the API/worker.
url = os.environ["AGENT_MIGRATION_URL"]
engine = create_engine(url, hide_parameters=True)
with engine.connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
