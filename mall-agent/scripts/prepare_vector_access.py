"""Provision isolated Milvus accounts. Run explicitly; no model requests or business data writes."""

import argparse
import json
import logging
import secrets
from pathlib import Path

from pymilvus import MilvusClient


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--initialize-root", action="store_true")
    args = parser.parse_args()
    path = Path(__file__).resolve().parents[2] / "deployment/.run/milvus-env.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    logging.getLogger("pymilvus").setLevel(logging.CRITICAL)
    options = {"uri": "http://127.0.0.1:19530", "timeout": 15, "grpc_options": {"grpc.enable_http_proxy": 0}}
    if not config.get("rootRotated"):
        if not args.initialize_root:
            raise SystemExit("Fresh installation requires explicit --initialize-root")
        client = MilvusClient(**options, user="root", password="Milvus")
        try:
            client.update_password("root", "Milvus", config["rootPassword"], timeout=15)
        finally:
            client.close()
        config["rootRotated"] = True
    config.setdefault("database", "mall_agent")
    for name in ("reader", "builder"):
        config.setdefault(name + "User", "mall_agent_" + name)
        config.setdefault(name + "Password", secrets.token_hex(24))
    # Save generated credentials before making changes so a lost response remains recoverable.
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    client = MilvusClient(**options, user="root", password=config["rootPassword"])
    try:
        if config["database"] not in client.list_databases():
            client.create_database(config["database"])
        users, roles = client.list_users(), client.list_roles()
        for name in ("reader", "builder"):
            user, role = config[name + "User"], "mall_agent_" + name
            if user not in users:
                client.create_user(user, config[name + "Password"])
            if role not in roles:
                client.create_role(role)
            groups = (
                ("DatabaseReadOnly", "CollectionReadOnly")
                if name == "reader"
                else ("DatabaseAdmin", "CollectionReadWrite")
            )
            for group in groups:
                client.grant_privilege_v2(role, group, "*", db_name=config["database"])
            client.grant_role(user, role)
        print("PASS isolated database and reader/builder roles provisioned; no cluster admin granted")
    finally:
        client.close()


if __name__ == "__main__":
    main()
