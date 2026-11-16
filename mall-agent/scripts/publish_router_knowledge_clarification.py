"""Append a narrow routing fix through immutable audited prompt administration."""

import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import httpx
from expansion_client import ADMIN, api, login

from app.runtime import run

ROOT = Path(__file__).resolve().parents[2]
SUPPLEMENT = "用户询问资料、文档、服务方案中写了什么或规则含义时选择KNOWLEDGE；即使尚未检索，也应先进入知识检索，而不是因当前没有原文就归GENERAL。"


async def main():
    output = ROOT / "deployment/.run/completion-router-knowledge-prompt-20261113.json"
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", trust_env=False, timeout=60) as client:
        await login(client, True)
        prompts = await api(client, ADMIN + "/prompt-versions?prompt_key=workflow-router&limit=100")
        current = next(p for p in prompts["items"] if p["state"] == "PUBLISHED")
        if SUPPLEMENT in current["content"]:
            print("PASS knowledge-document routing supplement already published")
            return
        candidate = await api(
            client,
            ADMIN + "/prompt-versions",
            "POST",
            {
                "prompt_key": "workflow-router",
                "content": current["content"] + "\n" + SUPPLEMENT,
                "change_note": "修复知识文档问题被归一般聊天而未检索；保留原路由内容与版本，权限和执行边界不变。",
            },
            key=str(uuid5(NAMESPACE_URL, "newshop/router-knowledge-20261113/" + current["id"])),
            expected=201,
        )
        published = await api(
            client,
            ADMIN + f"/prompt-versions/{candidate['id']}/publish",
            "POST",
            {"expected_version": candidate["version"]},
            key=str(uuid5(NAMESPACE_URL, "newshop/router-knowledge-publish/" + candidate["id"])),
        )
        output.write_text(
            json.dumps(
                {
                    "previous_prompt_id": current["id"],
                    "published_prompt_id": published["id"],
                    "prompt_version_no": published["version_no"],
                    "reason": "Knowledge-document questions must retrieve before answering",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print("PASS immutable router clarification published with audit and previous version retained")


if __name__ == "__main__":
    run(main())
