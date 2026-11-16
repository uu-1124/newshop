"""Synthetic child for an isolated test: terminate after durable confirmation A/B.

Connection configuration arrives over stdin and is never logged. The ledger models
the external business service; real MySQL/outbox behavior is tested separately.
"""

import json
import os
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main(payload):
    from pydantic import SecretStr
    from test_actions import Bridge, settings

    from app.bootstrap import Services
    from app.domain.identity import Principal

    config = payload["config"]
    if config["databaseUrl"].rsplit("/", 1)[-1] != "mall_agent_architecture_test":
        raise RuntimeError("Isolated recovery database required")
    cfg = settings(config).model_copy(update={"action_encryption_key": SecretStr(payload["envelope"])})
    svc = Services(cfg)
    principal = Principal(payload["member"], "synthetic process buyer")

    class CrashBridge(Bridge):
        async def execute_action(self, token, body):
            record = {"phase": payload["phase"], "execute_attempts": 1}
            if payload["phase"] == "after_commit":
                record["business"] = self.result(body["operation_no"])
            path = Path(payload["ledger"])
            with path.open("w", encoding="utf-8") as stream:
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os._exit(73)

    svc.extensions.actions.require().bridge = CrashBridge(principal)
    await svc.extensions.actions.require().confirm(
        principal, "synthetic-only", payload["action"], str(uuid4()), {"expected_version": 1}
    )


if __name__ == "__main__":
    try:
        from app.runtime import run

        run(main(json.load(sys.stdin)))
    except Exception as error:
        print("CHILD_FAILED type=" + type(error).__name__)
        raise SystemExit(1)
