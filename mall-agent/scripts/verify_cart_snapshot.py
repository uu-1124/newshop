"""Verify deployed cart reads against existing member carts without changing business data."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
from expansion_client import login

from app.application.cart import CartRead, validated_cart_page
from app.infrastructure.java_bridge import JavaBridge
from app.runtime import run
from app.settings import Settings

ROOT = Path(__file__).resolve().parents[2]


async def main():
    config = json.loads((ROOT / "deployment/.run/agent-env.json").read_text(encoding="utf-8"))
    settings = Settings(
        _env_file=None,
        database_url=config["databaseUrl"],
        bridge_secret=config["bridgeSecret"],
        broker_secret=config["brokerSecret"],
        cursor_secret=config["cursorSecret"],
    )
    evidence = {
        "checked_at": datetime.now(UTC).isoformat(),
        "business_writes": False,
        "paid_models": False,
        "checks": [],
    }
    bridge = JavaBridge(settings)
    try:
        async with httpx.AsyncClient(base_url=settings.gateway_url, trust_env=False, timeout=20) as client:
            for _ in range(30):
                try:
                    healthy = await client.get("/mall-portal/actuator/health")
                    if healthy.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(2)
            else:
                raise RuntimeError("Portal did not become ready")
            memberships = []
            for username in ("agent-lifecycle-6c72a0f5", "member"):
                await login(client, username=username)
                token = client.headers["Authorization"]
                legacy = await client.get("/mall-portal/cart/list")
                legacy.raise_for_status()
                raw = legacy.json()
                assert raw.get("code") == 200
                expected = {str(item["id"]): item for item in raw["data"]}
                after, seen, pages = None, {}, 0
                while True:
                    query = CartRead(limit=1, after=after)
                    page = validated_cart_page(await bridge.cart(token, query), query)
                    pages += 1
                    for item in page["items"]:
                        assert item["id"] in expected and item["id"] not in seen
                        original = expected[item["id"]]
                        assert item["quantity"] == original["quantity"]
                        assert item["product_id"] == str(original["productId"])
                        assert item["sku_id"] == str(original["productSkuId"])
                        assert "member_id" not in item and "memberId" not in item
                        seen[item["id"]] = item
                    if not page["has_more"]:
                        break
                    after = page["next_cursor"]
                    assert pages < 1000, "Cart continuation did not terminate"
                assert set(seen) == set(expected)
                memberships.append(set(seen))
                evidence["checks"].append(
                    {
                        "case": "current_member_cart_matches_existing_cart",
                        "member_fixture": username,
                        "items": len(seen),
                        "pages": pages,
                    }
                )
            assert not memberships[0] & memberships[1]
            evidence["checks"].append({"case": "two_member_snapshots_do_not_share_cart_row_ids"})
            unsigned = await client.get("/mall-portal/agent-bridge/v1/cart?limit=20")
            assert unsigned.status_code == 403
            evidence["checks"].append({"case": "logged_in_but_unsigned_bridge_read_denied"})
            evidence["result"] = "PASSED"
            (ROOT / "deployment/.run/completion-cart-snapshot-live-20261113.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps(evidence, ensure_ascii=False), flush=True)
    finally:
        await bridge.client.aclose()


if __name__ == "__main__":
    run(main())
