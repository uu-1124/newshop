"""Apply reviewed, profile-bound public prices without changing past call snapshots."""

import json
from pathlib import Path
from uuid import uuid4

import httpx
from expansion_client import ADMIN, api, login

from app.runtime import run

ROOT = Path(__file__).resolve().parents[2]


async def main():
    catalogue = json.loads((ROOT / "deployment/model-prices-20261116.json").read_text(encoding="utf-8"))
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", trust_env=False, timeout=60) as client:
        await login(client, True)
        policy = await api(client, ADMIN + "/settings")
        profiles = (await api(client, ADMIN + "/model-profiles?limit=100"))["items"]
        prices = list(policy["value"]["model_prices"])
        original_prices = list(prices)
        added = []
        for rule in catalogue["rules"]:
            matching = [
                p
                for p in profiles
                if p["capability"] == rule["capability"]
                and p["model_name"] == rule["model_name"]
                and p["is_active"]
                and p["enabled"]
                and p["provider"] == rule.get("provider", "aliyun")
            ]
            if len(matching) != 1:
                raise RuntimeError("Reviewed model is not the unique matching active provider profile")
            profile = matching[0]
            if any(
                p["capability"] == rule["capability"] and p.get("profile_id") in (None, profile["id"])
                for p in original_prices
            ):
                continue  # Existing operator prices always win; never silently overwrite a contract.
            prices.append(
                {k: v for k, v in rule.items() if k not in {"provider", "source", "currency"}}
                | {
                    "profile_id": profile["id"],
                    "currency": rule.get("currency", catalogue["currency"]),
                    "source_ref": catalogue["verified_on"]
                    + " public list price; group multiplier=1; "
                    + rule.get("source", catalogue["source"]),
                }
            )
            added.append(rule["capability"] + ":" + rule["unit"])
        if added:
            await api(
                client,
                ADMIN + "/settings",
                "PUT",
                {"expected_version": policy["version"], "value": policy["value"] | {"model_prices": prices}},
                key=str(uuid4()),
            )
        print(
            json.dumps(
                {"added": added, "historical_meters_rewritten": False, "unknown_usage_reconciled": False}
            )
        )


if __name__ == "__main__":
    run(main())
