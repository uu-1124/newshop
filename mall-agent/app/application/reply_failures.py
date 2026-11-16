"""Public recovery guidance, never supplier or driver exception text."""

from datetime import UTC, timedelta

MESSAGES = {
    "MODEL_QUOTA_EXCEEDED": "本轮请求超过当日模型可用额度，原问题和已显示内容已保留。可在额度窗口更新后重试，或转人工继续咨询。",
    "AUTH_CONTEXT_LOST": "本轮登录授权已中断，原问题和已显示内容已保留。请重新登录后重试，或转人工继续咨询。",
    "MODEL_STREAM_INTERRUPTED": "回复连接已中断，原问题和已显示内容已保留。连接恢复后可重试，或转人工继续咨询。",
    "MODEL_OUTPUT_INCOMPLETE": "回复未完整生成，原问题和已显示内容已保留。可重试，或转人工继续咨询。",
    "MODEL_TIMEOUT": "本轮回复超时，原问题和已显示内容已保留。可稍后重试，或转人工继续咨询。",
    "WORKER_LEASE_EXPIRED": "回复处理已中断，原问题和已显示内容已保留。可重试，或转人工继续咨询。",
    "SOURCE_UNAVAILABLE": "本轮依据已失效，原问题和已显示内容已保留。重试时会重新核验来源，也可转人工确认。",
    "WORKFLOW_REQUERY_EXHAUSTED": "本轮未能稳定核实依据，原问题和已显示内容已保留。请补充问题后重问，或转人工确认。",
    "ATTACHMENT_UNAVAILABLE": "本轮图片已不可用，原问题和已显示内容已保留。请重新选择图片后发送，也可转人工咨询。",
    "REPLY_UNAVAILABLE": "本轮回复暂不可用，原问题和已显示内容已保留。可稍后重试，或转人工继续咨询。",
}


def reply_failure(message, run):
    if (
        not run
        or message["sender_kind"] != "ASSISTANT"
        or message["status"] not in {"FAILED", "INTERRUPTED"}
        or run["state"] not in {"FAILED", "INTERRUPTED"}
        or str(message["run_id"]) != str(run["id"])
        or str(message["conversation_id"]) != str(run["conversation_id"])
    ):
        return None
    code = run["error_code"]
    if code not in MESSAGES:
        code = "REPLY_UNAVAILABLE"
    retry_after = None
    if code == "MODEL_QUOTA_EXCEEDED" and run["finished_at"] is not None:
        finished = run["finished_at"].astimezone(UTC)
        retry_after = (
            finished.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        ).isoformat()
    return {"code": code, "message": MESSAGES[code], "retry_after": retry_after}
