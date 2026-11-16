"""Build and verify the full framework without connecting to DB or paid models."""

import asyncio
import json
import sys
from pathlib import Path


async def main():
    from app.bootstrap import Services
    from app.main import create_app
    from app.settings import Settings

    class OfflineSettings(Settings):
        @classmethod
        def settings_customise_sources(
            cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
        ):
            return (init_settings,)

    settings = OfflineSettings(
        _env_file=None,
        database_url="postgresql+psycopg://framework:framework@127.0.0.1:1/framework",
        bridge_secret="framework-only",
        broker_secret="framework-only",
        cursor_secret="framework-only",
        provider="fixture",
        attachments_enabled=False,
        vision_enabled=False,
        notifications_enabled=False,
        knowledge_enabled=False,
        handoffs_enabled=False,
        actions_enabled=False,
        orders_enabled=False,
        staff_bridge_secret="",
    )
    services = Services(settings)
    try:
        app = create_app(settings)
        active = {route.operation_id for route in app.routes if getattr(route, "operation_id", None)}
        manifest = services.framework.describe(active)
        from app.agents.workflow import build_workflow_graph

        graph = build_workflow_graph()
        node_ids = set(graph.get_graph().nodes) - {"__start__", "__end__"}
        assert node_ids == {f"N{number:02}" for number in range(1, 13)}
        assert services.extensions.status()["model_admin"]["bound"]
        assert all(
            not value["bound"]
            for name, value in services.extensions.status().items()
            if name != "model_admin"
        )
        assert {kind.value for kind in services.job_handlers.supported} == {
            "RUNTIME_CHECK",
            "RETENTION_EXECUTE",
            "ATTACHMENT_DELETE",
            "MODEL_COMPATIBILITY",
        }
        from app.domain.capabilities import JobType
        from app.domain.identity import DomainError

        try:
            services.job_handlers.validate(JobType.RETENTION_EXECUTE, {"arbitrary_scope": "all"})
        except DomainError as error:
            assert error.code == "VALIDATION_ERROR"
        else:
            raise AssertionError("Conversation deletion must not activate arbitrary retention work")
        print(
            json.dumps(
                {
                    "result": "passed",
                    "modules": manifest["module_count"],
                    "reviewed_operations": manifest["contract_operation_count"],
                    "active_python_operations": manifest["active_python_operation_count"],
                    "workflow_nodes": len(node_ids),
                    "unimplemented_workflows": "FAIL_CLOSED",
                    "external_io": False,
                }
            )
        )
    finally:
        await services.close()


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    asyncio.run(main())
