"""Secret-safe HTTP errors and bounded SSE decoding for provider adapters."""

import json

import httpx

from app.domain.identity import DomainError


def upstream_error(status):
    code = {
        401: "MODEL_AUTH_FAILED",
        403: "MODEL_ACCESS_DENIED",
        429: "MODEL_RATE_LIMITED",
    }.get(status, "MODEL_REQUEST_REJECTED" if status < 500 else "MODEL_UNAVAILABLE")
    return DomainError(code, "模型服务暂不可用，请稍后重试", 503)


async def json_request(client, path, body):
    try:
        response = await client.post(path, json=body)
        if not response.is_success:
            raise upstream_error(response.status_code)
        return response.json()
    except (httpx.HTTPError, ValueError):
        raise DomainError("MODEL_UNAVAILABLE", "模型连接或响应异常", 503) from None


async def sse_events(response):
    data, size = [], 0
    async for line in response.aiter_lines():
        if not line:
            if data:
                payload = "\n".join(data)
                if payload != "[DONE]":
                    try:
                        yield json.loads(payload)
                    except ValueError:
                        raise DomainError("MODEL_PROTOCOL_ERROR", "模型事件格式异常", 503) from None
                data, size = [], 0
            continue
        if line.startswith("data:"):
            part = line[5:].lstrip(" ")
            size += len(part)
            if size > 2_000_000:
                raise DomainError("MODEL_PROTOCOL_ERROR", "模型事件超过大小限制", 503)
            data.append(part)
    if data and "\n".join(data) != "[DONE]":
        # A truncated event is never interpreted as a successful completion.
        raise DomainError("MODEL_STREAM_INTERRUPTED", "模型连接中断，已收到的内容会保留", 503)
