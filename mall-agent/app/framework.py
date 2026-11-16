"""Executable module composition and reviewed-contract coverage.

Scaffold coverage and business activation are independent. This manifest contains
public architecture metadata only; it never serializes service instances/settings.
"""

import importlib
import json
from pathlib import Path

from app.module_catalog import MODULES

ROOT = Path(__file__).resolve().parents[2]

# requires describes construction dependencies, not every collaboration in a UoW.
SCAFFOLDS = {
    "M01": ("bridge", "app.domain.capabilities:StaffIdentity", (), ("mall-agent/app/api/dependencies.py",)),
    "M02": ("conversations", "app.application.conversations:Conversations", ("M01",), ()),
    "M03": ("events", "app.infrastructure.postgres.database:append_events", ("M02",), ()),
    "M04": ("runs", "app.agents.workflow:build_workflow_graph", ("M03", "M11"), ()),
    "M05": ("tools", "app.agents.dialog:TOOLS", ("M17",), ()),
    "M06": ("actions", "app.domain.capabilities:Actions", ("M02", "M18"), ()),
    "M07": ("handoffs", "app.domain.capabilities:Handoffs", ("M02", "M03"), ()),
    "M08": ("knowledge", "app.domain.capabilities:KnowledgeManagement", ("M10", "M14"), ()),
    "M09": ("retrieval", "app.domain.capabilities:AuthorizedKnowledge", ("M08",), ()),
    "M10": ("attachments", "app.application.attachments:Attachments", ("M01",), ()),
    "M11": ("models", "app.infrastructure.model_ledger:ModelLedger", (), ()),
    "M12": ("retention", "app.domain.capabilities:Retention", ("M02", "M10", "M14"), ()),
    "M13": ("observability", "app.domain.capabilities:Observability", ("M01",), ()),
    "M14": ("jobs", "app.application.jobs:JobHandlers", (), ("mall-agent/app/workers/jobs.py",)),
    "M15": (
        "buyer_ui",
        "app.framework:ExternalBoundary",
        ("M02",),
        ("mall-app-web/src/services/agent-contracts.ts",),
    ),
    "M16": (
        "staff_ui",
        "app.framework:ExternalBoundary",
        ("M01",),
        ("mall-admin-web/src/router/agent.ts", "mall-admin-web/src/views/agent/Workspace.vue"),
    ),
    "M17": (
        "bridge",
        "app.infrastructure.java_bridge:JavaBridge",
        (),
        (
            "mall-swarm/mall-portal/src/main/java/com/macro/mall/portal/controller/AgentMemberBridgeController.java",
        ),
    ),
    "M18": (
        "transactions",
        "app.domain.capabilities:JavaTransactions",
        (),
        ("mall-swarm/mall-common/src/main/java/com/macro/mall/common/agent/AgentTransactionPort.java",),
    ),
    "M19": (
        "after_sale",
        "app.domain.capabilities:JavaAfterSale",
        ("M18",),
        ("mall-swarm/mall-common/src/main/java/com/macro/mall/common/agent/AgentAfterSalePort.java",),
    ),
    "M20": (
        "runtime",
        "app.settings:Settings",
        (),
        ("mall-agent/scripts/check_framework.py", "mall-agent/app/workers/outbox.py"),
    ),
}


class ExternalBoundary:
    """A separately built frontend/Java component, not a Python business handler."""


class Framework:
    def __init__(self, bindings):
        self.bindings = bindings
        self.coverage = json.loads((ROOT / "docs/architecture/coverage.json").read_text(encoding="utf-8"))
        self.contract = json.loads((ROOT / "docs/api/openapi.json").read_text(encoding="utf-8"))
        self.validate()

    def validate(self):
        expected = set(self.coverage["modules"])
        if set(MODULES) != expected or set(SCAFFOLDS) != expected:
            raise ValueError("Module registry does not cover the reviewed architecture")
        for module_id, (binding, reference, dependencies, paths) in SCAFFOLDS.items():
            if binding not in self.bindings or self.bindings[binding] is None:
                raise ValueError(f"Missing composition slot: {module_id}")
            package, name = reference.split(":")
            if not hasattr(importlib.import_module(package), name):
                raise ValueError(f"Missing code boundary: {reference}")
            if not set(dependencies) <= expected or module_id in dependencies:
                raise ValueError(f"Invalid construction dependencies: {module_id}")
            for relative in paths:
                if not (ROOT / relative).is_file():
                    raise ValueError(f"Missing framework artifact: {relative}")
        visited, visiting = set(), set()

        def visit(module_id):
            if module_id in visiting:
                raise ValueError("Construction dependency cycle")
            if module_id in visited:
                return
            visiting.add(module_id)
            for dependency in SCAFFOLDS[module_id][2]:
                visit(dependency)
            visiting.remove(module_id)
            visited.add(module_id)

        for module_id in expected:
            visit(module_id)
        contract = {
            operation["operationId"]: (method.upper(), path)
            for path, methods in self.contract["paths"].items()
            for method, operation in methods.items()
            if isinstance(operation, dict) and "operationId" in operation
        }
        operations = self.coverage["operations"]
        if len({operation["id"] for operation in operations}) != len(operations):
            raise ValueError("Duplicate operation ownership")
        if {operation["id"] for operation in operations} != set(contract):
            raise ValueError("Unowned reviewed HTTP operation")
        for operation in operations:
            if operation["owner"] not in expected:
                raise ValueError("Unknown HTTP operation owner")
            if contract[operation["id"]] != (operation["method"], operation["path"]):
                raise ValueError("HTTP contract/ownership mismatch")

    def describe(self, active_operations=()):
        active = set(active_operations)
        return {
            "schema": "framework-v1",
            "scaffold": "BOUNDARIES_REGISTERED",
            "full_business_runtime": "NOT_READY",
            "business": "PARTIAL",
            "module_count": len(SCAFFOLDS),
            "contract_operation_count": len(self.coverage["operations"]),
            "active_python_operation_count": len(active),
            "modules": [
                {
                    "id": module_id,
                    "name": MODULES[module_id]["name"],
                    "scaffold": "BOUND",
                    "business": MODULES[module_id]["state"],
                    "binding": specification[0],
                    "contract": specification[1],
                    "requires": list(specification[2]),
                    "artifacts": list(specification[3]),
                }
                for module_id, specification in SCAFFOLDS.items()
            ],
            "operations": [
                {
                    "id": operation["id"],
                    "method": operation["method"],
                    "path": operation["path"],
                    "owner": operation["owner"],
                    "phase": operation["phase"],
                    "permission": operation["permission"],
                    "python_route_active": operation["id"] in active,
                }
                for operation in self.coverage["operations"]
            ],
        }
