"""Explicit paid compatibility smoke. Uses synthetic text/media only; writes non-secret evidence."""

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont

from app.infrastructure.aliyun_models import AliyunModels
from app.infrastructure.dialog_provider import ResponsesDialogProvider
from app.infrastructure.gpt_vision import GptVision
from app.infrastructure.model_ledger import ModelLedger
from app.infrastructure.postgres.database import Database
from app.runtime import run
from app.settings import Settings


async def main():
    root = Path(__file__).resolve().parents[2]
    config = json.loads((root / "deployment/.run/agent-env.json").read_text(encoding="utf-8"))
    settings = Settings(
        database_url=config["databaseUrl"],
        bridge_secret=config["bridgeSecret"],
        broker_secret=config["brokerSecret"],
        cursor_secret=config["cursorSecret"],
    )
    db = Database(settings.database_url.get_secret_value())
    ledger = ModelLedger(db, settings)
    await ledger.setup()
    ali, dialog = AliyunModels(settings, ledger), ResponsesDialogProvider(settings)
    vision = GptVision(settings, ledger)
    evidence = {
        "time": datetime.now(UTC).isoformat(),
        "dialog_model": settings.dialog_model,
        "profiles": ledger.profiles,
        "checks": [],
    }

    def passed(name, **details):
        evidence["checks"].append({"name": name, "result": "passed", **details})
        print("PASS", name)

    try:
        vectors = await ali.embed(["演示规则：商品签收后七日内可申请退货。", "手机商品说明。"])
        passed("aliyun_embedding", count=len(vectors), dimension=len(vectors[0]))
        reranked = await ali.rerank("退货时间", ["商品签收后七日内可申请退货。", "手机商品说明。"], 2)
        assert reranked[0]["index"] == 0
        passed("aliyun_rerank", first_index=reranked[0]["index"])
        audio = await ali.synthesize("商城连接测试。")
        assert audio.startswith(b"RIFF")
        passed("aliyun_tts", bytes=len(audio), model=settings.tts_model)
        transcript = await ali.transcribe(audio)
        assert "商城" in transcript and "测试" in transcript
        passed("aliyun_asr", text=transcript, model=settings.asr_model)
        chunks = []
        async for event in dialog.stream_turn(
            [{"role": "user", "content": "只输出结构化结果，connected设为true。"}],
            "遵守用户请求。",
            [],
            ledger.recorder("DIALOG"),
            {
                "type": "object",
                "properties": {"connected": {"type": "boolean"}},
                "required": ["connected"],
                "additionalProperties": False,
            },
        ):
            if event.kind == "text":
                chunks.append(event.data)
        assert json.loads("".join(chunks)) == {"connected": True}
        passed("gpt_responses_stream_and_structured", chunks=len(chunks))
        if settings.vision_enabled:
            label = "ITEM-" + uuid4().hex[:6].upper()
            image = Image.new("RGB", (650, 220), "white")
            ImageDraw.Draw(image).text(
                (30, 60), label, fill="black", font=ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 44)
            )
            data = io.BytesIO()
            image.save(data, format="JPEG")
            observation = await vision.understand_image(
                data.getvalue(), "请读出这张测试商品标签图上的ITEM开头文字。"
            )
            assert label in observation
            passed("gpt_vision", model=settings.vision_model, expected_label=label)
        else:
            evidence["checks"].append({"name": "gpt_vision", "result": "disabled_not_retested"})
        evidence["result"] = "passed" if settings.vision_enabled else "passed_with_vision_disabled"
        (root / "deployment/.run/model-compatibility.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    finally:
        await ali.close()
        await dialog.close()
        await vision.close()
        await db.engine.dispose()


if __name__ == "__main__":
    run(main())
