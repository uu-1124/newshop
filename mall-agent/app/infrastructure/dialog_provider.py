import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from app.domain.identity import DomainError
from app.infrastructure.model_transport import sse_events, upstream_error


@dataclass
class DialogEvent:
    kind: str
    data: object


class ResponsesDialogProvider:
    """Responses protocol; no fallback model, hidden reasoning, or automatic paid retry."""

    def __init__(self, settings, transport=None, model=None):
        self.settings = settings
        self.model = model or settings.dialog_model
        self.client = httpx.AsyncClient(
            base_url=settings.dialog_base_url + "/",
            headers={"Authorization": "Bearer " + settings.dialog_api_key.get_secret_value()},
            timeout=httpx.Timeout(settings.model_timeout_seconds, connect=10),
            trust_env=False,
            transport=transport,
        )

    async def close(self):
        await self.client.aclose()

    async def stream_turn(
        self, inputs, instructions, tools, record, output_schema=None
    ) -> AsyncIterator[DialogEvent]:
        # UTF-8 bytes are a conservative token estimate; include tool schema and image
        # payload representation rather than undercounting them as a short prompt.
        input_bound = len(
            json.dumps(
                {"inputs": inputs, "instructions": instructions, "tools": tools}, ensure_ascii=False
            ).encode()
        )
        call_id = await record(
            {
                "stage": "reserve",
                "reservation_units": {
                    "REQUEST": 1,
                    "TOKEN": input_bound + self.settings.dialog_max_output_tokens,
                },
            }
        )
        started, response_id, usage, state, error = time.monotonic(), None, None, "UNKNOWN", None
        body = {
            "model": self.model,
            "instructions": instructions,
            "input": inputs,
            "stream": True,
            "store": False,
            "reasoning": {"effort": "low"},
            "max_output_tokens": self.settings.dialog_max_output_tokens,
            "tools": tools,
            "parallel_tool_calls": False,
        }
        if output_schema is not None:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "validated_result",
                    "strict": True,
                    "schema": output_schema,
                }
            }
        items, finished = {}, False
        try:
            async with self.client.stream("POST", "responses", json=body) as response:
                if not response.is_success:
                    state = "FAILED"
                    raise upstream_error(response.status_code)
                async for event in sse_events(response):
                    kind = event.get("type")
                    if kind == "response.created":
                        response_id = event.get("response", {}).get("id")
                    elif kind == "response.output_item.added":
                        items[event.get("item", {}).get("id")] = event.get("item", {})
                    elif kind == "response.output_text.delta":
                        item = items.get(event.get("item_id"), {})
                        if item.get("type") == "message" and item.get("phase") != "commentary":
                            yield DialogEvent("text", event.get("delta", ""))
                    elif kind == "response.completed":
                        result = event["response"]
                        response_id, usage = result.get("id", response_id), result.get("usage")
                        if result.get("status", "completed") != "completed":
                            raise DomainError("MODEL_OUTPUT_INCOMPLETE", "模型回复未完整结束", 503)
                        state, finished = "SUCCEEDED", True
                        # Tool arguments are acted on only after the complete response terminal event.
                        yield DialogEvent("completed", result.get("output", []))
                        break
                    elif kind in {"response.failed", "response.incomplete", "error"}:
                        usage = event.get("response", {}).get("usage")
                        state = "FAILED"
                        raise DomainError("MODEL_OUTPUT_INCOMPLETE", "模型回复未完整结束", 503)
                if not finished:
                    raise DomainError("MODEL_STREAM_INTERRUPTED", "模型连接中断，已收到的内容会保留", 503)
        except asyncio.CancelledError:
            state, error = "CANCELLED", "MODEL_CANCELLED_BILLING_UNKNOWN"
            raise
        except DomainError as exc:
            error = exc.code
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            error = "MODEL_UNAVAILABLE"
            raise DomainError(error, "模型连接或响应异常", 503) from None
        finally:
            await record(
                {
                    "call_id": call_id,
                    "status": state,
                    "provider_request_id": response_id,
                    "usage": usage,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    "error_code": error,
                }
            )
