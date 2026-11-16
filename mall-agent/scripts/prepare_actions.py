"""Apply the scoped cart schema and prepare private runtime keys without printing them."""

import json
import re
import secrets
from pathlib import Path

import pymysql
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[2]


def main():
    import subprocess

    subprocess.run(
        [r"D:\develop\nodejs\node.exe", str(ROOT / "deployment/prepare-cart-schema.cjs")],
        check=True,
        cwd=ROOT,
    )
    config = json.loads((ROOT / "deployment/.run/cart-test-env.json").read_text(encoding="utf-8"))
    with pymysql.connect(
        host="127.0.0.1",
        port=13306,
        user=config["user"],
        password=config["password"],
        database=config["database"],
        autocommit=True,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id,product_id FROM pms_sku_stock ORDER BY id LIMIT 2")
            fixtures = cursor.fetchall()
            assert len(fixtures) == 2, "Two published SKU fixtures are needed"
        (ROOT / "deployment/.run/cart-test-fixtures.json").write_text(
            json.dumps([{"sku_id": str(s), "product_id": str(p)} for s, p in fixtures]), encoding="utf-8"
        )
    path = ROOT / "deployment/.run/action-env.json"
    keys = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {
            "confirmationSecret": secrets.token_hex(32),
            "reconciliationSecret": secrets.token_hex(32),
            "encryptionKey": Fernet.generate_key().decode(),
        }
    )
    path.write_text(json.dumps(keys, indent=2), encoding="utf-8")
    portal_path = ROOT / "deployment/config/portal.yaml"
    portal = json.loads(portal_path.read_text(encoding="utf-8"))
    portal.update(
        {
            "MALL_AGENT_ACTIONS_ENABLED": True,
            "MALL_AGENT_CONFIRM_SECRET": keys["confirmationSecret"],
            "MALL_AGENT_RECONCILE_SECRET": keys["reconciliationSecret"],
        }
    )
    portal_path.write_text(json.dumps(portal, ensure_ascii=False, indent=2), encoding="utf-8")
    env_path = ROOT / "mall-agent/.env"
    env = env_path.read_text(encoding="utf-8")
    values = {
        "AGENT_BUILD": "cart-0.8",
        "AGENT_ACTIONS_ENABLED": "true",
        "AGENT_CONFIRMATION_SECRET": keys["confirmationSecret"],
        "AGENT_RECONCILIATION_SECRET": keys["reconciliationSecret"],
        "AGENT_ACTION_ENCRYPTION_KEY": keys["encryptionKey"],
    }
    for name, value in values.items():
        pattern = rf"(?m)^{name}=.*$"
        env = (
            re.sub(pattern, name + "=" + value, env)
            if re.search(pattern, env)
            else env + "\n" + name + "=" + value + "\n"
        )
    env_path.write_text(env, encoding="utf-8")
    print(
        "PASS cart quote/operation/mutex schema, isolated MySQL test target and separate private keys prepared"
    )


if __name__ == "__main__":
    main()
