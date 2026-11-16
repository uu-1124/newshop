"""Provision the incremental quote field and enable the existing Java checkout adapter."""

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    subprocess.run(
        ["D:/develop/nodejs/node.exe", str(ROOT / "deployment/prepare-order-schema.cjs")], check=True
    )
    portal_path = ROOT / "deployment/config/portal.yaml"
    portal = json.loads(portal_path.read_text(encoding="utf-8"))
    if not portal.get("MALL_AGENT_ACTIONS_ENABLED"):
        raise RuntimeError("Prepare existing confirmation and reconciliation infrastructure first")
    portal["MALL_AGENT_ORDERS_ENABLED"] = True
    portal_path.write_text(json.dumps(portal, ensure_ascii=False, indent=2), encoding="utf-8")
    env_path = ROOT / "mall-agent/.env"
    env = env_path.read_text(encoding="utf-8")
    for name, value in {"AGENT_ORDERS_ENABLED": "true", "AGENT_BUILD": "order-0.9"}.items():
        pattern = rf"(?m)^{name}=.*$"
        env = (
            re.sub(pattern, name + "=" + value, env)
            if re.search(pattern, env)
            else env + "\n" + name + "=" + value + "\n"
        )
    env_path.write_text(env, encoding="utf-8")
    print(
        "PASS incremental order schema and private checkout flags prepared; restart portal/API/workers after installing the adapter"
    )


if __name__ == "__main__":
    main()
