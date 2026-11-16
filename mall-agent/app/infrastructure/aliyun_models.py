import asyncio
import base64
import math
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.domain.identity import DomainError
from app.infrastructure.model_transport import json_request


class AliyunModels:
    """Separate Aliyun routes per capability; no credentials or signed URLs leave the adapter."""

    def __init__(self, settings, ledger, transport=None):
        self.settings, self.ledger = settings, ledger
        options = {
            "headers": {"Authorization": "Bearer " + settings.ali_api_key.get_secret_value()},
            "timeout": httpx.Timeout(settings.model_timeout_seconds, connect=10),
            "trust_env": False,
            "transport": transport,
        }
        self.compatible = httpx.AsyncClient(base_url=settings.ali_compatible_url + "/", **options)
        self.native = httpx.AsyncClient(base_url=settings.ali_native_url + "/", **options)

    async def close(self):
        await self.compatible.aclose()
        await self.native.aclose()

    async def _call(self, capability, client, path, body, reservation_units=None, validate=None):
        record = self.ledger.recorder(capability)
        call_id = await record({"stage": "reserve", "reservation_units": reservation_units or {"REQUEST": 1}})
        start, status, result, error = time.monotonic(), "UNKNOWN", {}, None
        try:
            result = await json_request(client, path, body)
            output = validate(result) if validate else result
            status = "SUCCEEDED"
            return output
        except asyncio.CancelledError:
            status, error = "CANCELLED", "MODEL_CANCELLED_BILLING_UNKNOWN"
            raise
        except DomainError as exc:
            status, error = "FAILED", exc.code
            raise
        finally:
            await record(
                {
                    "call_id": call_id,
                    "status": status,
                    "usage": result.get("usage"),
                    "provider_request_id": result.get("request_id", result.get("id")),
                    "latency_ms": int((time.monotonic() - start) * 1000),
                    "error_code": error,
                }
            )

    async def embed(self, texts):
        if not texts or len(texts) > 10 or any(not t.strip() or len(t) > 8000 for t in texts):
            raise DomainError("VALIDATION_ERROR", "向量文本批次不符合限制", 422)

        def validate(result):
            try:
                items = sorted(result.get("data", []), key=lambda x: x["index"])
                if (
                    len(items) != len(texts)
                    or any(type(x["index"]) is not int for x in items)
                    or [x["index"] for x in items] != list(range(len(texts)))
                ):
                    raise DomainError("MODEL_PROTOCOL_ERROR", "向量结果条目不匹配", 503)
                vectors = [item.get("embedding", []) for item in items]
                if any(
                    len(v) != self.settings.embedding_dimension
                    or any(
                        isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
                        for x in v
                    )
                    for v in vectors
                ):
                    raise DomainError("MODEL_DIMENSION_MISMATCH", "向量维度或数值不符合配置", 503)
                return vectors
            except (AttributeError, KeyError, TypeError, ValueError):
                raise DomainError("MODEL_PROTOCOL_ERROR", "向量结果结构不符合约定", 503) from None

        return await self._call(
            "EMBEDDING",
            self.compatible,
            "embeddings",
            {
                "model": self.settings.embedding_model,
                "input": texts,
                "dimensions": self.settings.embedding_dimension,
                "encoding_format": "float",
            },
            {"REQUEST": 1, "TOKEN": sum(len(text.encode()) for text in texts)},
            validate=validate,
        )

    async def rerank(self, query, documents, top_n=5):
        if not query.strip() or not documents or len(documents) > 20 or not 1 <= top_n <= len(documents):
            raise DomainError("VALIDATION_ERROR", "重排输入不符合限制", 422)

        def validate(result):
            try:
                items = result.get("output", {}).get("results", [])
                if (
                    not isinstance(items, list)
                    or len(items) > top_n
                    or len({x["index"] for x in items}) != len(items)
                    or any(
                        type(x["index"]) is not int
                        or not 0 <= x["index"] < len(documents)
                        or isinstance(x["relevance_score"], bool)
                        or not isinstance(x["relevance_score"], (int, float))
                        or not math.isfinite(x["relevance_score"])
                        for x in items
                    )
                ):
                    raise DomainError("MODEL_PROTOCOL_ERROR", "重排返回了范围外的候选", 503)
                return items
            except (AttributeError, KeyError, TypeError, ValueError):
                raise DomainError("MODEL_PROTOCOL_ERROR", "重排结果结构不符合约定", 503) from None

        return await self._call(
            "RERANK",
            self.native,
            "services/rerank/text-rerank/text-rerank",
            {
                "model": self.settings.rerank_model,
                "input": {"query": query, "documents": documents},
                "parameters": {"top_n": top_n, "return_documents": False},
            },
            {"REQUEST": 1, "TOKEN": len(query.encode()) + sum(len(text.encode()) for text in documents)},
            validate=validate,
        )

    async def transcribe(self, audio, mime="audio/wav"):
        if mime not in {"audio/wav", "audio/mp3", "audio/mpeg"} or not audio or len(audio) > 5_000_000:
            raise DomainError("VALIDATION_ERROR", "音频格式或大小不符合限制", 422)

        def validate(result):
            try:
                text = result["choices"][0]["message"]["content"]
                if not isinstance(text, str) or not text.strip() or len(text) > 10000:
                    raise ValueError()
                return text
            except (KeyError, TypeError, IndexError, ValueError):
                raise DomainError("MODEL_PROTOCOL_ERROR", "转写结果格式异常，请重录或使用文字", 503) from None

        return await self._call(
            "ASR",
            self.compatible,
            "chat/completions",
            {
                "model": self.settings.asr_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": f"data:{mime};base64," + base64.b64encode(audio).decode(),
                                    "format": "wav" if mime == "audio/wav" else "mp3",
                                },
                            },
                        ],
                    }
                ],
                "asr_options": {"language": "zh", "enable_itn": False},
            },
            {"REQUEST": 1, "SECOND": 60},
            validate=validate,
        )

    async def synthesize(self, text):
        if not text.strip() or len(text) > 400:
            raise DomainError("VALIDATION_ERROR", "播报文本须为1至400字", 422)
        result = await self._call(
            "TTS",
            self.native,
            "services/aigc/multimodal-generation/generation",
            {
                "model": self.settings.tts_model,
                "input": {"text": text, "voice": "Cherry", "language_type": "Chinese"},
            },
            {"REQUEST": 1, "CHARACTER": len(text)},
        )
        audio = result.get("output", {}).get("audio", {})
        if audio.get("data"):
            decoded = base64.b64decode(audio["data"], validate=True)
            if len(decoded) > 10_000_000:
                raise DomainError("MODEL_PROTOCOL_ERROR", "合成音频过大", 503)
            return decoded
        url = urlsplit(audio.get("url", ""))
        # Provider's known OSS bucket only; request without the model Authorization header.
        if url.hostname != "dashscope-result-bj.oss-cn-beijing.aliyuncs.com" or url.port or url.username:
            raise DomainError("MODEL_PROTOCOL_ERROR", "合成音频地址未注册", 503)
        target = urlunsplit(("https", url.netloc, url.path, url.query, ""))
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False) as client:
                async with client.stream("GET", target) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 10_000_000:
                            raise DomainError("MODEL_PROTOCOL_ERROR", "合成音频过大", 503)
                        chunks.append(chunk)
                    return b"".join(chunks)
        except httpx.HTTPError:
            raise DomainError("MODEL_AUDIO_UNAVAILABLE", "合成音频下载失败", 503) from None
