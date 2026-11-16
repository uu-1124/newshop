"""Isolated test subprocess: reserve synthetic work, then actually exit."""

import json
import os
import sys
from uuid import UUID

from app.bootstrap import Services
from app.infrastructure.model_call_context import ModelCallContext, model_call_scope
from app.runtime import run
from app.settings import Settings


async def main():
    url = os.environ["ATTRIBUTION_TEST_DATABASE"]
    if url.rsplit("/", 1)[-1] != "mall_agent_architecture_test":
        raise RuntimeError("Dedicated test database required")
    svc = Services(
        Settings(
            _env_file=None,
            database_url=url,
            bridge_secret="test-only",
            broker_secret="test-only",
            cursor_secret="test-only",
            provider="fixture",
        )
    )
    await svc.models.setup()
    context = ModelCallContext(job_id=UUID(sys.argv[1]), lease_owner="attribution-job", fencing_token=1)
    with model_call_scope(context):
        call = await svc.models.recorder("EMBEDDING")(
            {"stage": "reserve", "reservation_units": {"TOKEN": 10}}
        )
    print(json.dumps({"call_id": call}), flush=True)
    os._exit(73)


if __name__ == "__main__":
    try:
        run(main())
    except Exception as error:
        print(type(error).__name__, flush=True)
        os._exit(74)
