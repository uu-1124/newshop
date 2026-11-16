"""Fail closed for version-bound return guidance while Java rules are changing."""

from datetime import datetime
from uuid import uuid4

from app.domain.identity import DomainError
from app.infrastructure.postgres.database import encoded, execute, one

CATALOG_KEY = "agent-return-rule-catalog-v1"

# Rules are a Java projection, not editable knowledge or an alternative transaction authority.
RULE_CURRENT_SQL = """NOT EXISTS (
    SELECT 1 FROM regexp_matches(v.parsed_text, 'ITEM_NET_168H_DEMO_V1|ITEM_NET_DEMO_V[1-9][0-9]*', 'g') AS markers(marker)
    WHERE marker[1] IS DISTINCT FROM (
        SELECT item->>'rule_version' FROM ag_setting rc,
            LATERAL jsonb_array_elements(rc.value->'items') item
        WHERE rc.key='agent-return-rule-catalog-v1' AND rc.value->>'state'='READY'
            AND (item->>'effective_at')::timestamptz<=clock_timestamp()
        ORDER BY (item->>'effective_at')::timestamptz DESC,(item->>'version_no')::int DESC LIMIT 1
    ))"""


def validate_rules(page):
    try:
        items = page["items"]
        if not isinstance(items, list) or not items or len(items) > 100:
            raise ValueError()
        versions = set()
        for item in items:
            version = item["version_no"]
            expected = "ITEM_NET_168H_DEMO_V1" if version == 1 else f"ITEM_NET_DEMO_V{version}"
            if (
                type(version) is not int
                or version < 1
                or version in versions
                or item["rule_version"] != expected
                or datetime.fromisoformat(item["effective_at"].replace("Z", "+00:00")).tzinfo is None
            ):
                raise ValueError()
            versions.add(version)
        return [{k: item[k] for k in ("rule_version", "version_no", "effective_at")} for item in items]
    except (KeyError, TypeError, ValueError, AttributeError):
        raise DomainError("JAVA_PROTOCOL_ERROR", "售后规则版本目录不完整，旧资料继续停用", 503) from None


async def begin_rule_sync(db, actor=0, recover=False):
    token = str(uuid4())
    async with db.transaction() as conn:
        await execute(conn, "SELECT pg_advisory_xact_lock(hashtextextended('return-rule-sync',0))")
        old = await one(
            conn,
            "SELECT value,updated_at>clock_timestamp()-interval '2 minutes' fresh FROM ag_setting WHERE key=:key FOR UPDATE",
            key=CATALOG_KEY,
        )
        if old and old["value"].get("state") == "SYNCING" and (not recover or old["fresh"]):
            raise DomainError("RULE_SYNC_IN_PROGRESS", "售后规则正在同步，请稍后刷新核对", 409)
        await execute(
            conn,
            """INSERT INTO ag_setting(key,value,updated_by) VALUES(:key,CAST(:value AS jsonb),:actor)
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_by=EXCLUDED.updated_by,version=ag_setting.version+1,updated_at=now()""",
            key=CATALOG_KEY,
            actor=actor,
            value=encoded({"state": "SYNCING", "token": token, "items": []}),
        )
    return token


async def finish_rule_sync(db, token, page=None):
    items = validate_rules(page) if page is not None else []
    async with db.transaction() as conn:
        await execute(
            conn,
            """UPDATE ag_setting SET value=CAST(:value AS jsonb),version=version+1,updated_at=now()
            WHERE key=:key AND value->>'token'=:token""",
            key=CATALOG_KEY,
            token=token,
            value=encoded({"state": "READY" if page is not None else "BLOCKED", "items": items}),
        )


async def refresh_rule_sync(db, bridge, token, actor):
    # A read of an unchanged catalog must not temporarily revoke live citations.
    # Serialize with publication, and keep the row locked until the Java read is
    # validated so a late refresh cannot overwrite a newer publication.
    async with db.transaction() as conn:
        await execute(conn, "SELECT pg_advisory_xact_lock(hashtextextended('return-rule-sync',0))")
        old = await one(
            conn,
            "SELECT value,updated_at>clock_timestamp()-interval '2 minutes' fresh FROM ag_setting WHERE key=:key FOR UPDATE",
            key=CATALOG_KEY,
        )
        if old and old["value"].get("state") == "SYNCING" and old["fresh"]:
            raise DomainError("RULE_SYNC_IN_PROGRESS", "售后规则正在同步，请稍后刷新核对", 409)
        page = await bridge.get("/mall-admin/agent-bridge/v1/return-rules", token, staff=True)
        value = {"state": "READY", "items": validate_rules(page)}
        if not old or old["value"] != value:
            await execute(
                conn,
                """INSERT INTO ag_setting(key,value,updated_by) VALUES(:key,CAST(:value AS jsonb),:actor)
                ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_by=EXCLUDED.updated_by,
                version=ag_setting.version+1,updated_at=now()""",
                key=CATALOG_KEY,
                actor=actor,
                value=encoded(value),
            )
        return page
