"""Add a dedicated knowledge role/menu while preserving all existing allocations."""

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
            assert data.get("code") == 200, f"Knowledge role preparation rejected: {path}"
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
        category = next((c for c in categories if c["name"] == "Agent knowledge"), None)
        if not category:
            await call("POST", "/resourceCategory/create", json={"name": "Agent knowledge", "sort": 101})
            category = next(
                c for c in await call("GET", "/resourceCategory/listAll") if c["name"] == "Agent knowledge"
            )
        definitions = {
            "agent:kb:read": "/mall-agent/v1/admin/kb/documents",
            "agent:kb:edit": "/mall-agent/v1/admin/kb/versions/**",
            "agent:kb:publish": "/mall-agent/v1/admin/kb/documents/*/publish",
            "agent:attachment:upload": "/mall-agent/v1/admin/attachments",
            "agent:attachment:read": "/mall-agent/v1/admin/attachments/*/content",
            "agent:job:read": "/mall-agent/v1/admin/jobs/**",
            "agent:job:retry": "/mall-agent/v1/admin/jobs/*/retry",
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
                        "description": "Explicit Agent knowledge permission",
                    },
                )
        resources = [r for r in await call("GET", "/resource/listAll") if r["name"] in definitions]
        roles = await call("GET", "/role/listAll")
        role = next((r for r in roles if r["name"] == "Agent knowledge editor"), None)
        if not role:
            await call(
                "POST",
                "/role/create",
                json={
                    "name": "Agent knowledge editor",
                    "description": "Knowledge edit/publish and own task operations",
                    "status": 1,
                    "sort": 101,
                },
            )
            role = next(
                r for r in await call("GET", "/role/listAll") if r["name"] == "Agent knowledge editor"
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
        child = next((m for m in parent.get("children", []) if m["name"] == "agentKnowledge"), None)
        if not child:
            await call(
                "POST",
                "/menu/create",
                json={
                    "parentId": parent["id"],
                    "title": "知识管理",
                    "name": "agentKnowledge",
                    "icon": "ums-resource",
                    "hidden": 0,
                    "sort": 100,
                },
            )
            tree = await call("GET", "/menu/treeList")
            parent = next(m for m in tree if m["name"] == "agent")
            child = next(m for m in parent["children"] if m["name"] == "agentKnowledge")
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
        (Path(__file__).resolve().parents[2] / "deployment/.run/knowledge-role.json").write_text(
            json.dumps(evidence, indent=2), encoding="utf-8"
        )
        print("PASS dedicated knowledge role and menu; prior permissions and roles preserved")


if __name__ == "__main__":
    asyncio.run(main())
