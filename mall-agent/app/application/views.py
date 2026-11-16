from app.application.expiry import deadline_sql, visible_sql


def conversation_view(row, preview=""):
    return {
        "id": str(row["id"]),
        "title": row["title"],
        "state": row["state"],
        "waiting_ai_enabled": row["waiting_ai_enabled"],
        "assigned_staff": row.get("assigned_staff"),
        "entry_context": row["entry_context"],
        "last_message_preview": preview[:200],
        "unread_count": row.get("unread_count", 0),
        "version": row["version"],
        "control_epoch": row["control_epoch"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "closed_at": row["closed_at"],
    }


def message_view(row, run=None):
    from app.application.reply_failures import reply_failure

    return {
        "id": str(row["id"]),
        "conversation_id": str(row["conversation_id"]),
        "seq": str(row["seq"]),
        "sender_kind": row["sender_kind"],
        "sender_display_name": row.get("sender_display_name")
        or (
            "我"
            if row["sender_kind"] == "MEMBER"
            else "人工客服"
            if row["sender_kind"] == "STAFF"
            else "商城咨询助手"
        ),
        "client_message_id": str(row["client_message_id"]) if row["client_message_id"] else None,
        "status": row["status"],
        "text": row["text_content"],
        "blocks": row["blocks"],
        "run_id": str(row["run_id"]) if row["run_id"] else None,
        "reply_failure": reply_failure(row, run),
        "created_at": row["created_at"],
    }


async def conversation_projection(conn, row, preview=None, actor=None, position=None):
    from app.application.handoffs import presence
    from app.application.read_positions import read_position
    from app.infrastructure.postgres.database import one

    assigned = None
    if row["assigned_admin_id"]:
        staff = await one(
            conn, "SELECT * FROM ag_staff_profile WHERE admin_id=:id", id=row["assigned_admin_id"]
        )
        if staff:
            assigned = {
                "id": str(staff["admin_id"]),
                "display_name": staff["display_name"],
                "presence": presence(staff),
            }
    position = position or (
        {"unread_count": row["projected_unread"]}
        if actor is None and "projected_unread" in row
        else await read_position(
            conn, row["id"], actor.kind if actor else "MEMBER", actor.id if actor else row["member_id"]
        )
    )
    if preview is None:
        if "projected_preview" in row:
            preview = row["projected_preview"] or ""
        else:
            message = await one(
                conn,
                f"""SELECT text_content FROM ag_message m WHERE conversation_id=:cid
                AND {visible_sql()} AND text_content<>'' ORDER BY seq DESC LIMIT 1""",
                cid=row["id"],
            )
            preview = message["text_content"] if message else ""
    return conversation_view(
        dict(row) | {"assigned_staff": assigned, "unread_count": position["unread_count"]}, preview
    )


def run_view(row):
    if not row:
        return None
    return {
        key: row[key]
        for key in [
            "state",
            "attempt",
            "version",
            "error_code",
            "created_at",
            "started_at",
            "finished_at",
            "control_epoch",
        ]
    } | {
        "id": str(row["id"]),
        "conversation_id": str(row["conversation_id"]),
        "trigger_message_id": str(row["trigger_message_id"]),
        "queue_position": None,
    }


async def message_projection(conn, row):
    return (await messages_projection(conn, [row]))[0]


async def messages_projection(conn, messages):
    """Project one bounded page with the same current facts, without per-block queries."""
    from app.application.actions import action_view
    from app.infrastructure.postgres.database import encoded, rows

    failed_run_ids = {
        message["run_id"]
        for message in messages
        if message["sender_kind"] == "ASSISTANT"
        and message["status"] in {"FAILED", "INTERRUPTED"}
        and message["run_id"] is not None
    }
    failed_runs = {
        (str(run["id"]), str(run["conversation_id"])): run
        for run in (
            await rows(
                conn,
                "SELECT id,conversation_id,state,error_code,finished_at FROM ag_run WHERE id=ANY(CAST(:ids AS uuid[]))",
                ids=list(failed_run_ids),
            )
            if failed_run_ids
            else []
        )
    }
    sources, attachments, actions = {}, set(), {}
    for message in messages:
        for block in message["blocks"]:
            if block["type"] == "source":
                source = block["source"]
                key = (
                    str(message["id"]),
                    source["document_id"],
                    source["version_id"],
                    source["chunk_id"],
                )
                sources[key] = dict(zip(("message_id", "document_id", "version_id", "chunk_id"), key))
            elif block["type"] == "attachment":
                attachments.add(block["attachment"]["id"])
            elif block["type"] == "action":
                key = (block["action"]["id"], str(message["conversation_id"]))
                actions[key] = {"id": key[0], "conversation_id": key[1]}

    current_sources, available_attachments, current_actions = {}, set(), {}
    if sources:
        from app.application.return_knowledge import RULE_CURRENT_SQL

        current_sources = {
            tuple(str(row[name]) for name in ("message_id", "document_id", "version_id", "chunk_id")): bool(
                row["available"]
            )
            for row in await rows(
                conn,
                f"""SELECT r.*, EXISTS(SELECT 1 FROM ag_kb_citation k
                    JOIN ag_kb_document d ON d.id=k.document_id
                    JOIN ag_kb_version v ON v.id=k.version_id JOIN ag_kb_chunk c ON c.id=k.chunk_id
                    WHERE k.message_id=r.message_id AND k.document_id=r.document_id
                    AND k.version_id=r.version_id AND k.chunk_id=r.chunk_id
                    AND d.enabled AND d.visibility='MEMBER' AND d.active_version_id=v.id
                    AND v.state='READY' AND v.published_at IS NOT NULL
                    AND {RULE_CURRENT_SQL}
                    AND encode(sha256(convert_to(c.content,'UTF8')),'hex')=k.content_sha256) AS available
                FROM jsonb_to_recordset(CAST(:refs AS jsonb))
                    AS r(message_id uuid,document_id uuid,version_id uuid,chunk_id uuid)""",
                refs=encoded(list(sources.values())),
            )
        }
    if attachments:
        available_attachments = {
            str(row["id"])
            for row in await rows(
                conn,
                f"""SELECT id FROM ag_attachment a WHERE id=ANY(CAST(:ids AS uuid[])) AND state='READY'
                AND ({deadline_sql("a", "image_retention_days")}>now() OR EXISTS(SELECT 1 FROM ag_attachment_ref r
                    WHERE r.attachment_id=a.id AND r.protected AND r.released_at IS NULL))""",
                ids=list(attachments),
            )
        }
    if actions:
        current_actions = {
            (str(row["id"]), str(row["conversation_id"])): row
            for row in await rows(
                conn,
                """SELECT a.* FROM ag_action a JOIN jsonb_to_recordset(CAST(:refs AS jsonb))
                AS r(id uuid,conversation_id uuid) ON a.id=r.id AND a.conversation_id=r.conversation_id""",
                refs=encoded(list(actions.values())),
            )
        }

    result = []
    for message in messages:
        blocks = []
        for block in message["blocks"]:
            if block["type"] == "source":
                source = block["source"]
                key = (
                    str(message["id"]),
                    source["document_id"],
                    source["version_id"],
                    source["chunk_id"],
                )
                block = {"type": "source", "source": source | {"available": current_sources.get(key, False)}}
            elif block["type"] == "attachment":
                if block["attachment"]["id"] not in available_attachments:
                    continue
            elif block["type"] == "action":
                action = current_actions.get((block["action"]["id"], str(message["conversation_id"])))
                if not action:
                    continue
                block = {"type": "action", "action": action_view(action)}
            blocks.append(block)
        result.append(
            message_view(message, failed_runs.get((str(message["run_id"]), str(message["conversation_id"]))))
            | {"blocks": blocks}
        )
    return result
