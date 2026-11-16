import json
from pathlib import Path


def install_reviewed_contract(app):
    """Expose only activated operations using the reviewed wire contract, never inactive success stubs."""
    path = Path(__file__).resolve().parents[3] / "docs/api/openapi.json"
    contract = json.loads(path.read_text(encoding="utf-8"))
    active = {getattr(route, "operation_id", None) for route in app.routes}
    paths = {}
    for url, methods in contract["paths"].items():
        selected = {
            method: operation
            for method, operation in methods.items()
            if isinstance(operation, dict) and operation.get("operationId") in active
        }
        if selected:
            paths[url] = selected
    app.openapi = lambda: {
        **contract,
        "paths": paths,
        "info": {"title": "Mall Agent activated architecture slice", "version": "0.1.0"},
    }
