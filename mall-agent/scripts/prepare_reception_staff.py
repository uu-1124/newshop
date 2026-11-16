"""Add a dedicated reception role/menu while preserving all existing allocations."""

import asyncio
import json
import os
from pathlib import Path

import httpx


async def main():
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8201/mall-admin", timeout=20, trust_env=False
    ) as client:

        async def call(method, path, **kwargs):
            r = await client.request(method, path, **kwargs)
            data = r.json()
            assert data.get("code") == 200, f"Reception role preparation rejected: {path}"
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
        category = next((c for c in categories if c["name"] == "Agent reception"), None)
        if not category:
            await call("POST", "/resourceCategory/create", json={"name": "Agent reception", "sort": 101})
            category = next(
                c for c in await call("GET", "/resourceCategory/listAll") if c["name"] == "Agent reception"
            )
        definitions = {
            "agent:conversation:read": "/mall-agent/v1/admin/conversations/**",
            "agent:conversation:reply": "/mall-agent/v1/admin/conversations/*/messages",
            "agent:conversation:note": "/mall-agent/v1/admin/conversations/*/notes",
            "agent:handoff:claim": "/mall-agent/v1/admin/handoffs/**",
            "agent:handoff:transfer": "/mall-agent/v1/admin/handoffs/*/transfer",
            "agent:handoff:close": "/mall-agent/v1/admin/handoffs/*/close",
            "agent:presence:write": "/mall-agent/v1/admin/presence/**",
            "agent:staff:read": "/mall-agent/v1/admin/staff",
            "agent:staff:manage": "/mall-agent/v1/admin/staff/*",
        }
        existing = await call("GET", "/resource/listAll")
        for name, url in definitions.items():
            row = next((r for r in existing if r["name"] == name), None)
            if row:
                assert row["url"] == url, "Existing permission definition differs; left unchanged"
            else:
                await call(
                    "POST",
                    "/resource/create",
                    json={
                        "name": name,
                        "url": url,
                        "categoryId": category["id"],
                        "description": "Explicit Agent reception permission",
                    },
                )
        resources = [r for r in await call("GET", "/resource/listAll") if r["name"] in definitions]
        roles = await call("GET", "/role/listAll")
        role = next((r for r in roles if r["name"] == "Agent reception staff"), None)
        if not role:
            await call(
                "POST",
                "/role/create",
                json={
                    "name": "Agent reception staff",
                    "description": "Human reception, own conversation scope and presence",
                    "status": 1,
                    "sort": 101,
                },
            )
            role = next(r for r in await call("GET", "/role/listAll") if r["name"] == "Agent reception staff")
        prior = await call("GET", f"/role/listResource/{role['id']}")
        await call(
            "POST",
            "/role/allocResource",
            data={
                "roleId": role["id"],
                "resourceIds": ",".join(map(str, sorted({r["id"] for r in prior + resources}))),
            },
        )
        tree = await call("GET", "/menu/treeList")
        parent = next((m for m in tree if m["name"] == "agent"), None)
        if not parent:
            await call(
                "POST",
                "/menu/create",
                json={
                    "parentId": 0,
                    "title": "智能客服",
                    "name": "agent",
                    "icon": "ums-admin",
                    "hidden": 0,
                    "sort": 100,
                },
            )
            tree = await call("GET", "/menu/treeList")
            parent = next(m for m in tree if m["name"] == "agent")
        child = next((m for m in parent.get("children", []) if m["name"] == "agentService"), None)
        if not child:
            await call(
                "POST",
                "/menu/create",
                json={
                    "parentId": parent["id"],
                    "title": "客服工作台",
                    "name": "agentService",
                    "icon": "ums-resource",
                    "hidden": 0,
                    "sort": 100,
                },
            )
            tree = await call("GET", "/menu/treeList")
            parent = next(m for m in tree if m["name"] == "agent")
            child = next(m for m in parent["children"] if m["name"] == "agentService")
        menus = await call("GET", f"/role/listMenu/{role['id']}")
        await call(
            "POST",
            "/role/allocMenu",
            data={
                "roleId": role["id"],
                "menuIds": ",".join(map(str, sorted({m["id"] for m in menus + [parent, child]}))),
            },
        )
        admins = await call("GET", "/admin/list", params={"keyword": username, "pageSize": 100})
        admin = next(a for a in admins["list"] if a["username"] == username)
        allocated = await call("GET", f"/admin/role/{admin['id']}")
        await call(
            "POST",
            "/admin/role/update",
            data={
                "adminId": admin["id"],
                "roleIds": ",".join(map(str, sorted({r["id"] for r in allocated + [role]}))),
            },
        )
        evidence = {
            "admin_id": admin["id"],
            "role_id": role["id"],
            "permissions": sorted(definitions),
            "menu_ids": [parent["id"], child["id"]],
        }
        (Path(__file__).resolve().parents[2] / "deployment/.run/reception-role.json").write_text(
            json.dumps(evidence, indent=2), encoding="utf-8"
        )
        print("PASS dedicated reception role and menu; prior permissions and roles preserved")


if __name__ == "__main__":
    asyncio.run(main())
