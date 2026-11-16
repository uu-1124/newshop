"""Add explicit agent runtime resources and a dedicated role to the demo admin."""

import asyncio
import os

import httpx


async def prepare(client):
    async def call(method, path, **kwargs):
        response = await client.request(method, "/mall-admin" + path, **kwargs)
        body = response.json()
        assert body.get("code") == 200, f"Staff resource preparation rejected: {path}"
        return body.get("data")

    login = await call(
        "POST",
        "/admin/login",
        json={
            "username": os.environ.get("MALL_DEMO_ADMIN", "admin"),
            "password": os.environ.get("MALL_DEMO_ADMIN_PASSWORD", "MallDemo2026!"),
        },
    )
    client.headers["Authorization"] = login["tokenHead"] + login["token"]
    categories = await call("GET", "/resourceCategory/listAll")
    category = next((c for c in categories if c["name"] == "Agent runtime"), None)
    if not category:
        await call("POST", "/resourceCategory/create", json={"name": "Agent runtime", "sort": 100})
        category = next(
            c for c in await call("GET", "/resourceCategory/listAll") if c["name"] == "Agent runtime"
        )
    resources = await call("GET", "/resource/listAll")
    definitions = {
        "agent:job:read": "/mall-agent/v1/admin/jobs/**",
        "agent:job:retry": "/mall-agent/v1/admin/jobs/*/retry",
    }
    for name, url in definitions.items():
        existing = next((r for r in resources if r["name"] == name), None)
        if existing:
            assert existing["url"] == url, "Existing agent resource differs; left untouched"
        else:
            await call(
                "POST",
                "/resource/create",
                json={
                    "name": name,
                    "url": url,
                    "categoryId": category["id"],
                    "description": "Agent runtime explicit permission",
                },
            )
    resources = [r for r in await call("GET", "/resource/listAll") if r["name"] in definitions]
    roles = await call("GET", "/role/listAll")
    role = next((r for r in roles if r["name"] == "Agent runtime operator"), None)
    if not role:
        await call(
            "POST",
            "/role/create",
            json={
                "name": "Agent runtime operator",
                "description": "Agent runtime job read and retry only",
                "status": 1,
                "sort": 100,
            },
        )
        role = next(r for r in await call("GET", "/role/listAll") if r["name"] == "Agent runtime operator")
    prior = await call("GET", f"/role/listResource/{role['id']}")
    resource_ids = sorted({r["id"] for r in prior + resources})
    await call(
        "POST",
        "/role/allocResource",
        data={"roleId": role["id"], "resourceIds": ",".join(map(str, resource_ids))},
    )
    admins = await call(
        "GET", "/admin/list", params={"keyword": os.environ.get("MALL_DEMO_ADMIN", "admin"), "pageSize": 100}
    )
    admin = next(a for a in admins["list"] if a["username"] == os.environ.get("MALL_DEMO_ADMIN", "admin"))
    current = await call("GET", f"/admin/role/{admin['id']}")
    role_ids = sorted({r["id"] for r in current + [role]})
    await call(
        "POST", "/admin/role/update", data={"adminId": admin["id"], "roleIds": ",".join(map(str, role_ids))}
    )
    print("PASS explicit job permissions and dedicated runtime role; prior roles preserved")
    return {"resources": {r["name"]: r["id"] for r in resources}, "role_id": role["id"]}


async def main():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=20, trust_env=False) as client:
        await prepare(client)


if __name__ == "__main__":
    asyncio.run(main())
