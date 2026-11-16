"""Register governance capabilities and a dedicated role, preserving prior grants."""

import asyncio
import json
import os
from pathlib import Path

import httpx

DEFINITIONS = {
    "agent:backup:verify": "/mall-agent/v1/admin/backups/**",
    "agent:action:read": "/mall-agent/v1/admin/actions/unresolved",
    "agent:action:reconcile": "/mall-agent/v1/admin/actions/*/reconcile",
    "agent:settings:read": "/mall-agent/v1/admin/settings",
    "agent:settings:write": "/mall-agent/v1/admin/settings",
    "agent:statistics:read": "/mall-agent/v1/admin/statistics",
    "agent:usage:read": "/mall-agent/v1/admin/model-usage/unknown",
    "agent:usage:write": "/mall-agent/v1/admin/model-usage/*/reconcile",
    "agent:retention:preview": "/mall-agent/v1/admin/retention/preview",
    "agent:retention:execute": "/mall-agent/v1/admin/retention/execute",
    "agent:job:read": "/mall-agent/v1/admin/jobs/**",
    "agent:job:retry": "/mall-agent/v1/admin/jobs/*/retry",
    "agent:audit:read": "/mall-agent/v1/admin/audit-logs",
}


async def main():
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8201/mall-admin", timeout=30, trust_env=False
    ) as client:

        async def call(method, path, **kwargs):
            response = await client.request(method, path, **kwargs)
            result = response.json()
            if result.get("code") != 200:
                raise RuntimeError(f"Governance provisioning rejected: {path}")
            return result.get("data")

        username = os.environ.get("MALL_DEMO_ADMIN", "admin")
        auth = await call(
            "POST",
            "/admin/login",
            json={
                "username": username,
                "password": os.environ.get("MALL_DEMO_ADMIN_PASSWORD", "MallDemo2026!"),
            },
        )
        client.headers["Authorization"] = auth["tokenHead"] + auth["token"]
        categories = await call("GET", "/resourceCategory/listAll")
        category = next((c for c in categories if c["name"] == "Agent governance"), None)
        if not category:
            await call("POST", "/resourceCategory/create", json={"name": "Agent governance", "sort": 105})
            category = next(
                c for c in await call("GET", "/resourceCategory/listAll") if c["name"] == "Agent governance"
            )
        existing = await call("GET", "/resource/listAll")
        for name, url in DEFINITIONS.items():
            prior = next((r for r in existing if r["name"] == name), None)
            if prior:
                assert prior["url"] == url, "Registered capability URL differs"
            else:
                await call(
                    "POST",
                    "/resource/create",
                    json={
                        "name": name,
                        "url": url,
                        "categoryId": category["id"],
                        "description": "Explicit governance capability",
                    },
                )
        resources = [r for r in await call("GET", "/resource/listAll") if r["name"] in DEFINITIONS]
        roles = await call("GET", "/role/listAll")
        role = next((r for r in roles if r["name"] == "Agent governance managers"), None)
        if not role:
            await call(
                "POST",
                "/role/create",
                json={
                    "name": "Agent governance managers",
                    "description": "Local demo governance managers",
                    "status": 1,
                    "sort": 105,
                },
            )
            role = next(
                r for r in await call("GET", "/role/listAll") if r["name"] == "Agent governance managers"
            )
        prior = await call("GET", f"/role/listResource/{role['id']}")
        await call(
            "POST",
            "/role/allocResource",
            data={
                "roleId": role["id"],
                "resourceIds": ",".join(map(str, sorted({r["id"] for r in prior + resources}))),
            },
        )
        parent = next(m for m in await call("GET", "/menu/treeList") if m["name"] == "agent")
        menu = next((m for m in parent.get("children", []) if m["name"] == "agentGovernance"), None)
        if not menu:
            await call(
                "POST",
                "/menu/create",
                json={
                    "parentId": parent["id"],
                    "title": "治理与任务",
                    "name": "agentGovernance",
                    "icon": "ums-resource",
                    "hidden": 0,
                    "sort": 105,
                },
            )
            parent = next(m for m in await call("GET", "/menu/treeList") if m["name"] == "agent")
            menu = next(m for m in parent["children"] if m["name"] == "agentGovernance")
        oldmenus = await call("GET", f"/role/listMenu/{role['id']}")
        await call(
            "POST",
            "/role/allocMenu",
            data={
                "roleId": role["id"],
                "menuIds": ",".join(map(str, sorted({m["id"] for m in oldmenus + [parent, menu]}))),
            },
        )
        accounts = await call("GET", "/admin/list", params={"keyword": username, "pageSize": 100})
        admin = next(a for a in accounts["list"] if a["username"] == username)
        oldroles = await call("GET", f"/admin/role/{admin['id']}")
        await call(
            "POST",
            "/admin/role/update",
            data={
                "adminId": admin["id"],
                "roleIds": ",".join(map(str, sorted({r["id"] for r in oldroles + [role]}))),
            },
        )
        path = Path(__file__).resolve().parents[2] / "deployment/.run/governance-role-20261113.json"
        path.write_text(
            json.dumps(
                {
                    "admin_id": admin["id"],
                    "role_id": role["id"],
                    "permissions": sorted(DEFINITIONS),
                    "menu_id": menu["id"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print("PASS dedicated governance role and menu; previous grants preserved")


if __name__ == "__main__":
    asyncio.run(main())
