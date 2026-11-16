import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import pytest


def setup_module_source():
    path = Path(__file__).resolve().parents[1] / "scripts/prepare_query_benchmark.py"
    spec = importlib.util.spec_from_file_location("query_fixture_setup", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_private_fixture_directory_rejects_paths_outside_ignored_run_directory():
    module = setup_module_source()
    for path in [module.ROOT, module.ROOT / "docs", module.ROOT / "deployment/.run/../../docs"]:
        with pytest.raises(ValueError):
            module.location(path)
    result = module.location(module.ROOT / "deployment/.run/query-baseline/example")
    assert result.is_relative_to((module.ROOT / "deployment/.run").resolve())


@pytest.mark.anyio
async def test_cleanup_refuses_an_account_when_server_identity_does_not_match_owned_fixture(
    tmp_path, monkeypatch
):
    module = setup_module_source()
    experiment = str(uuid4())
    prefix = "query_" + experiment[:8]
    state = {
        "id": experiment,
        "prefix": prefix,
        "buyers": [],
        "staff": [{"id": "99", "username": prefix + "_0"}],
    }
    (tmp_path / "fixtures.json").write_text(json.dumps(state), encoding="utf-8")
    calls = []

    class Client:
        async def get(self, path):
            return type(
                "Reply", (), {"json": lambda self: {"code": 200, "data": {"username": "someone-else"}}}
            )()

        async def post(self, *args, **kwargs):
            calls.append("unexpected mutation")
            raise AssertionError("Foreign identity must never be disabled")

        async def aclose(self):
            pass

    async def authenticated(*args, **kwargs):
        pass

    monkeypatch.setattr(module, "client", Client)
    monkeypatch.setattr(module, "login", authenticated)
    with pytest.raises(RuntimeError, match="foreign account"):
        await module.cleanup(tmp_path)
    assert calls == []


@pytest.mark.anyio
async def test_prepared_fixture_artifacts_are_consumable_by_the_real_benchmark_contract(
    tmp_path, monkeypatch
):
    module = setup_module_source()
    from benchmark_queries import load_authorizations, validate_plan

    accounts = {}

    class Reply:
        def json(self):
            return {"code": 200, "data": {"tokenHead": "Bearer ", "token": "private-test-only-token"}}

    class Client:
        def __init__(self):
            self.headers = {}
            self.username = None

        async def post(self, path, json=None):
            assert path == "/mall-admin/admin/login"
            self.username = json["username"]
            return Reply()

        async def aclose(self):
            pass

    async def authenticated(client, staff=False, username=None):
        client.username = username or "operator"
        client.headers["Authorization"] = "Bearer private-test-only-token"

    async def legacy(client, path, body=None, data=None):
        if path.endswith("/register"):
            value = {"id": 200 + len(accounts), "username": body["username"]}
            accounts[body["username"]] = value
            return value
        assert path.endswith("/role/update")
        return 1

    async def api(client, path, method="GET", body=None, **kwargs):
        if path == module.MEMBER + "/me":
            return {"id": str(100 + module.BUYERS.index(client.username))}
        if path == module.ADMIN + "/me":
            return accounts[client.username]
        if path == module.ADMIN + "/staff?limit=100":
            return {"items": [{"id": str(a["id"]), "version": 1} for a in accounts.values()]}
        if path == module.MEMBER + "/conversations":
            return {"id": str(uuid4()), "version": 1, "title": body["title"]}
        if path.endswith("/handoff"):
            return {"id": str(uuid4()), "version": 1}
        if path.endswith("/claim"):
            return {"id": path.split("/")[-2], "version": 2}
        if path.endswith("/messages"):
            return {"id": str(uuid4()), "run": None}
        assert path.endswith(("/presence", "/close"))
        return {}

    monkeypatch.setattr(module, "client", Client)
    monkeypatch.setattr(module, "login", authenticated)
    monkeypatch.setattr(module, "legacy", legacy)
    monkeypatch.setattr(module, "api", api)
    monkeypatch.setattr(module, "fingerprint", lambda state: {"sha256": "a" * 64})
    await module.prepare(tmp_path)
    routes = json.loads((tmp_path / "routes.json").read_text(encoding="utf-8"))
    streams = json.loads((tmp_path / "streams.json").read_text(encoding="utf-8"))
    actors = {r["actor_key"] for r in routes + streams}
    assert len(actors) == 13
    auth = load_authorizations(tmp_path / "auth.private.json", actors)
    assert set(auth) == actors
    validate_plan(
        {
            "schema_version": 1,
            "base_url": "http://127.0.0.1:8201",
            "rate_per_second": 1.5,
            "samples_per_route": 1000,
            "warmup_seconds": 120,
            "max_in_flight": 32,
            "timeout_seconds": 30,
            "max_start_lag_seconds": 1,
            "routes": routes,
            "streams": streams,
        }
    )
