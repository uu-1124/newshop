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
