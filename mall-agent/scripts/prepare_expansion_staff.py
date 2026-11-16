"""Provision explicit expansion roles in the local demo; preserve all prior allocations."""

import asyncio
import json
import os
from pathlib import Path

import httpx

DEFINITIONS = {
    "agent:return:read": "/mall-agent/v1/admin/returns/**",
    "agent:return:review": "/mall-agent/v1/admin/returns/*/review",
    "agent:return:receive": "/mall-agent/v1/admin/returns/*/receive",
    "agent:return:refund": "/mall-agent/v1/admin/returns/*/simulate-refund",
    "agent:logistics:read": "/mall-agent/v1/admin/orders/*/logistics",
    "agent:logistics:write": "/mall-agent/v1/admin/shipments/**",
    "agent:business:read": "/mall-agent/v1/admin/conversations/*/context",
    "agent:attachment:upload": "/mall-agent/v1/admin/attachments",
    "agent:attachment:read": "/mall-agent/v1/admin/attachments/*/content",
    "agent:events:subscribe": "/mall-agent/v1/admin/events",
    "agent:audit:read": "/mall-agent/v1/admin/audit-logs",
    "agent:conversation:history:all": "/mall-agent/v1/admin/conversations/**",
    "agent:conversation:read": "/mall-agent/v1/admin/conversations/**",
    "agent:model:read": "/mall-agent/v1/admin/model-profiles/**",
    "agent:model:write": "/mall-agent/v1/admin/model-profiles/**",
    "agent:model:activate": "/mall-agent/v1/admin/model-profiles/*/activate",
    "agent:prompt:read": "/mall-agent/v1/admin/prompt-versions/**",
    "agent:prompt:write": "/mall-agent/v1/admin/prompt-versions",
    "agent:prompt:publish": "/mall-agent/v1/admin/prompt-versions/*/publish",
    "agent:job:read": "/mall-agent/v1/admin/jobs/**",
    "agent:job:retry": "/mall-agent/v1/admin/jobs/*/retry",
}


async def main():
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8201/mall-admin", timeout=30, trust_env=False
    ) as client:

        async def call(method, path, **kwargs):
            response = await client.request(method, path, **kwargs)
            data = response.json()
            assert data.get("code") == 200, f"Expansion role preparation rejected: {path}"
            return data.get("data")

        username = os.environ.get("MALL_DEMO_ADMIN", "admin")
        login = await call(
            "POST",
            "/admin/login",
            json={
                "username": username,
                "password": os.environ.get("MALL_DEMO_ADMIN_PASSWORD", "MallDemo2026!"),
            },
        )
        client.headers["Authorization"] = login["tokenHead"] + login["token"]
        categories = await call("GET", "/resourceCategory/listAll")
        category = next((c for c in categories if c["name"] == "Agent expansion"), None)
        if not category:
            await call("POST", "/resourceCategory/create", json={"name": "Agent expansion", "sort": 103})
            category = next(
                c for c in await call("GET", "/resourceCategory/listAll") if c["name"] == "Agent expansion"
            )
        existing = await call("GET", "/resource/listAll")
        for name, url in DEFINITIONS.items():
            previous = next((r for r in existing if r["name"] == name), None)
            if previous:
                assert previous["url"] == url, "Existing resource definition differs"
            else:
                await call(
                    "POST",
                    "/resource/create",
                    json={
                        "name": name,
                        "url": url,
                        "categoryId": category["id"],
                        "description": "Explicit expansion capability",
                    },
                )
        resources = {r["name"]: r for r in await call("GET", "/resource/listAll")}
        roles = await call("GET", "/role/listAll")
        tree = await call("GET", "/menu/treeList")
        parent = next(m for m in tree if m["name"] == "agent")
        menu = next((m for m in parent.get("children", []) if m["name"] == "agentModels"), None)
        if not menu:
            await call(
                "POST",
                "/menu/create",
                json={
                    "parentId": parent["id"],
                    "title": "模型与提示词",
                    "name": "agentModels",
                    "icon": "ums-resource",
                    "hidden": 0,
                    "sort": 102,
                },
            )
            tree = await call("GET", "/menu/treeList")
            parent = next(m for m in tree if m["name"] == "agent")
            menu = next(m for m in parent["children"] if m["name"] == "agentModels")
        operation_menu = next((m for m in parent.get("children", []) if m["name"] == "agentOperations"), None)
        if not operation_menu:
            await call(
                "POST",
                "/menu/create",
                json={
                    "parentId": parent["id"],
                    "title": "订单售后与物流",
                    "name": "agentOperations",
                    "icon": "oms-order",
                    "hidden": 0,
                    "sort": 104,
                },
            )
            parent = next(m for m in await call("GET", "/menu/treeList") if m["name"] == "agent")
            operation_menu = next(m for m in parent["children"] if m["name"] == "agentOperations")
        groups = {
            "Agent demo lifecycle operators": {
                p for p in DEFINITIONS if p.startswith(("agent:return:", "agent:logistics:"))
            }
            | {"agent:attachment:read", "agent:audit:read"},
            "Agent reception staff": {
                "agent:business:read",
                "agent:attachment:upload",
                "agent:attachment:read",
                "agent:events:subscribe",
            },
            "Agent model managers": {
                p for p in DEFINITIONS if p.startswith(("agent:model:", "agent:prompt:", "agent:job:"))
            },
            "Agent conversation supervisors": {
                "agent:audit:read",
                "agent:conversation:history:all",
                "agent:conversation:read",
                "agent:events:subscribe",
            },
        }
        granted = []
        evidence = {}
        for name, permissions in groups.items():
            role = next((r for r in roles if r["name"] == name), None)
            if not role:
                await call(
                    "POST",
                    "/role/create",
                    json={
                        "name": name,
                        "description": "Explicit local demo expansion role",
                        "status": 1,
                        "sort": 103,
                    },
                )
                roles = await call("GET", "/role/listAll")
                role = next(r for r in roles if r["name"] == name)
            prior = await call("GET", f"/role/listResource/{role['id']}")
            await call(
                "POST",
                "/role/allocResource",
                data={
                    "roleId": role["id"],
                    "resourceIds": ",".join(
                        map(str, sorted({r["id"] for r in prior} | {resources[p]["id"] for p in permissions}))
                    ),
                },
            )
            menus = await call("GET", f"/role/listMenu/{role['id']}")
            desired = (
                [parent, operation_menu]
                if name == "Agent demo lifecycle operators"
                else [parent, menu]
                if name in {"Agent model managers", "Agent demo lifecycle operators"}
                else [parent] + [m for m in parent["children"] if m["name"] == "agentService"]
            )
            await call(
                "POST",
                "/role/allocMenu",
                data={
                    "roleId": role["id"],
                    "menuIds": ",".join(map(str, sorted({m["id"] for m in menus + desired}))),
                },
            )
            granted.append(role)
            evidence[name] = {"id": role["id"], "permissions_added": sorted(permissions)}
        users = await call("GET", "/admin/list", params={"keyword": username, "pageSize": 100})
        admin = next(a for a in users["list"] if a["username"] == username)
        previous = await call("GET", f"/admin/role/{admin['id']}")
        await call(
            "POST",
            "/admin/role/update",
            data={
                "adminId": admin["id"],
                "roleIds": ",".join(map(str, sorted({r["id"] for r in previous + granted}))),
            },
        )
        output = Path(__file__).resolve().parents[2] / "deployment/.run/expansion-staff-roles.json"
        output.write_text(
            json.dumps({"admin_id": admin["id"], "roles": evidence}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print("PASS explicit expansion roles and model menu; previous allocations preserved")


if __name__ == "__main__":
    asyncio.run(main())
