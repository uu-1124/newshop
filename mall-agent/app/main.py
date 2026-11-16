import hmac
import logging
import time
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from app.api.actions import register_action_routes
from app.api.admin import register_admin_routes
from app.api.backups import register_backup_routes
from app.api.catalog import register_catalog_routes
from app.api.contracts import install_reviewed_contract
from app.api.dependencies import ok, services
from app.api.governance import register_governance
from app.api.handoffs import register_handoff_routes
from app.api.knowledge import register_knowledge_routes
from app.api.member import register_member_routes
from app.api.model_admin import register_model_routes
from app.api.orders import register_order_routes
from app.api.staff_events import register_staff_events
from app.application.catalog import CatalogDetail, CatalogQuery
from app.application.orders import OrderRead, query_orders
from app.bootstrap import Services
from app.domain.identity import DomainError
from app.infrastructure.performance_timing import HEADER, RUN_HEADER, finish_request, phase, start_request
from app.infrastructure.postgres.database import one
from app.infrastructure.request_metrics import record_request
from app.settings import get_settings

log = logging.getLogger(__name__)


def create_app(settings=None):
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        app.state.services = Services(settings)
        await app.state.services.notifications.start()
        if settings.provider != "fixture":
            await app.state.services.models.setup()
        while await app.state.services.runs.recover_expired(app.state.services.auth.context_id):
            pass
        yield
        await app.state.services.close()

    app = FastAPI(
        title="Mall Agent architecture slice",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/internal/docs",
        openapi_url="/internal/openapi.json",
    )

    @app.middleware("http")
    async def tracing(request, call_next):
        started = time.perf_counter()
        try:
            request.state.request_id = str(UUID(request.headers.get("X-Request-ID", str(uuid4()))))
        except ValueError:
            request.state.request_id = str(uuid4())
        timing, token = start_request(
            settings.performance_timing_enabled, request.headers.get(RUN_HEADER), request.state.request_id
        )
        try:
            with phase("handler_ms"):
                response = await call_next(request)
            await record_request(request, response.status_code, started)
            response.headers["X-Request-ID"] = request.state.request_id
            if request.url.path.startswith("/mall-agent/v1/"):
                response.headers["Cache-Control"] = "private, no-store"
            if timing is not None:
                route = request.scope.get("route")
                try:
                    header = timing.header(
                        getattr(route, "operation_id", None),
                        response.status_code,
                        response.headers.get("content-type", "").startswith("text/event-stream"),
                    )
                except Exception:
                    log.warning("performance timing header unavailable")
                    header = None
                if header is not None:
                    response.headers[HEADER] = header
            return response
        finally:
            finish_request(timing, token)

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse(
            {"code": exc.code, "message": exc.message, "data": None, "request_id": request.state.request_id},
            status_code=exc.status,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return await domain_error(request, DomainError("VALIDATION_ERROR", "请求参数不符合接口要求", 422))

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        log.error("database failure request=%s type=%s", request.state.request_id, type(exc).__name__)
        return await domain_error(request, DomainError("DATABASE_UNAVAILABLE", "数据服务暂不可用", 503))

    @app.get("/livez")
    async def live():
        return {"status": "UP", "build": settings.build}

    @app.get("/readyz")
    async def ready(request: Request):
        async with services(request).db.transaction() as conn:
            version = await one(conn, "SELECT version_num FROM alembic_version")
            await one(conn, "SELECT last_event_id FROM ag_event_clock WHERE singleton_id=1")
            slots = await one(conn, "SELECT count(*) AS n FROM ag_generation_slot")
            dialog = await one(
                conn,
                "SELECT model_name FROM ag_model_profile WHERE capability='DIALOG' AND is_active AND enabled",
            )
            vision = await one(
                conn,
                "SELECT model_name FROM ag_model_profile WHERE capability='VISION' AND is_active AND enabled",
            )
        if version["version_num"] != "0011" or slots["n"] != 3:
            raise DomainError("MIGRATION_REQUIRED", "迁移版本不匹配", 503)
        dependency_states = {}
        if settings.observability_enabled:
            from app.application.observability import readiness, runtime_snapshot

            async with services(request).db.transaction() as conn:
                snapshot = await runtime_snapshot(conn)
            ready, dependency_states = readiness(snapshot, settings)
            if not ready:
                raise DomainError("DEPENDENCY_NOT_READY", "关键依赖未就绪或探针已过期", 503)
        return {
            "status": "UP",
            "build": settings.build,
            "migration": "0011",
            "dependencies": dependency_states,
            "framework": {
                "modules": len(services(request).framework.coverage["modules"]),
                "scaffold": "BOUNDARIES_REGISTERED",
                "business": "PARTIAL",
            },
            "capabilities": {
                "dialog": "LIVE" if settings.provider == "openai_responses" else "FIXTURE_ONLY",
                "dialog_model": dialog["model_name"]
                if dialog and settings.provider == "openai_responses"
                else None,
                "knowledge": "TRUSTED_PRODUCT_CATEGORY_LIVE" if settings.knowledge_enabled else "NOT_ACTIVE",
                "media": "PRIVATE_IMAGES_ASR_TTS"
                if settings.attachments_enabled and settings.vision_enabled
                else "NOT_ACTIVE",
                "vision_model": vision["model_name"] if vision and settings.vision_enabled else None,
                "transactions": (
                    "ADD_CART_DIRECT_CART_CANCEL_LIVE" if settings.orders_enabled else "ADD_CART_LIVE"
                )
                if settings.actions_enabled
                else "NOT_ACTIVE",
                "handoffs": "IMAGES_CARDS_SSE_AUDIT_LIVE" if settings.handoffs_enabled else "NOT_ACTIVE",
                "model_administration": "VERSIONED_LIVE",
                "governance": "VERSIONED_QUOTAS_USAGE_METRICS"
                if settings.governance_enabled
                else "NOT_ACTIVE",
                "retention": "POLICY_PREVIEW_BATCH_ERASURE" if settings.retention_enabled else "NOT_ACTIVE",
                "after_sale": "ITEM_RETURN_DEMO_LIVE"
                if settings.orders_enabled and settings.actions_enabled
                else "NOT_ACTIVE",
            },
        }

    @app.get("/internal/framework", include_in_schema=False)
    async def framework(request: Request):
        svc = services(request)
        active = {route.operation_id for route in app.routes if getattr(route, "operation_id", None)}
        return {
            **svc.framework.describe(active),
            "extensions": svc.extensions.status(),
            "job_handlers": svc.job_handlers.describe(),
            "notifications": svc.notifications.describe(),
        }

    @app.get("/internal/pool", include_in_schema=False)
    async def pool_status(request: Request):
        return services(request).db.pool_metrics()

    register_member_routes(app, settings)
    register_catalog_routes(app)
    register_admin_routes(app)
    register_model_routes(app)
    register_knowledge_routes(app)
    register_handoff_routes(app)
    register_staff_events(app)
    register_action_routes(app)
    register_order_routes(app)
    register_governance(app)
    register_backup_routes(app, settings)

    class Borrow(BaseModel):
        model_config = ConfigDict(extra="forbid")
        lease_owner: str
        fencing_token: int
        query: str | None = None
        filters: CatalogQuery | None = None
        kind: Literal["SEARCH", "DETAIL", "ORDERS"] = "SEARCH"
        product_id: str | None = None
        order_query: OrderRead | None = None

    @app.post("/internal/runtime-auth/{rid}/products", include_in_schema=False)
    async def borrow(rid: UUID, request: Request, body: Borrow, x_broker_key: str = Header(default="")):
        if not hmac.compare_digest(x_broker_key, settings.broker_secret.get_secret_value()):
            raise DomainError("NOT_FOUND", "接口不存在", 404)
        svc = services(request)
        async with svc.db.transaction() as conn:
            run = await one(
                conn,
                """SELECT r.*,c.member_id,c.control_epoch AS current_epoch,c.deleted_at
                FROM ag_run r JOIN ag_conversation c ON c.id=r.conversation_id
                WHERE r.id=:id AND r.state='RUNNING' AND r.lease_until>now()""",
                id=rid,
            )
        if (
            not run
            or run["deleted_at"]
            or run["current_epoch"] != run["control_epoch"]
            or run["lease_owner"] != body.lease_owner
            or run["fencing_token"] != body.fencing_token
        ):
            raise DomainError("LEASE_LOST", "运行租约已失效", 409)
        token = svc.auth.get(str(rid))
        identity = await svc.bridge.identity(token)
        if identity.id != run["member_id"]:
            raise DomainError("AUTH_CONTEXT_LOST", "身份上下文不匹配", 409)
        if body.kind == "ORDERS":
            if (
                not settings.orders_enabled
                or body.order_query is None
                or body.query is not None
                or body.filters is not None
                or body.product_id is not None
            ):
                raise DomainError("FEATURE_NOT_ACTIVE", "订单查询参数无效或未开放", 409)
            result = await query_orders(svc.bridge, token, body.order_query)
            async with svc.db.transaction() as conn:
                current = await one(
                    conn,
                    "SELECT r.id FROM ag_run r JOIN ag_conversation c ON c.id=r.conversation_id WHERE r.id=:id AND r.state='RUNNING' AND r.lease_owner=:owner AND r.fencing_token=:fence AND r.lease_until>now() AND c.deleted_at IS NULL AND c.control_epoch=r.control_epoch",
                    id=rid,
                    owner=body.lease_owner,
                    fence=body.fencing_token,
                )
                if not current:
                    raise DomainError("LEASE_LOST", "订单查询运行已失效", 409)
            return ok(request, result)
        if body.order_query is not None:
            raise DomainError("VALIDATION_ERROR", "商品查询不接收订单参数", 422)
        if body.query is not None and len(body.query) > 200:
            raise DomainError("VALIDATION_ERROR", "商品关键词过长", 422)
        if body.kind == "DETAIL":
            if body.query is not None or body.filters is not None:
                raise DomainError("VALIDATION_ERROR", "商品详情参数不正确", 422)
            try:
                selection = CatalogDetail(product_id=body.product_id)
            except ValueError:
                raise DomainError("VALIDATION_ERROR", "商品编号不正确", 422) from None
            return ok(request, await svc.bridge.product(token, selection.product_id))
        if body.product_id is not None or (body.query is not None and body.filters is not None):
            raise DomainError("VALIDATION_ERROR", "商品查询参数不正确", 422)
        if body.filters is not None:
            return ok(request, await svc.bridge.catalog(token, body.filters))
        return ok(request, await svc.bridge.products(token, body.query))

    from app.api.runtime_tools import register_runtime_tools

    register_runtime_tools(app, settings)
    install_reviewed_contract(app)
    return app
