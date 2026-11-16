from uuid import UUID

from fastapi import Header, Request
from fastapi.encoders import jsonable_encoder

from app.domain.identity import DomainError


def services(request: Request):
    return request.app.state.services


def ok(request, data):
    return {
        "code": "OK",
        "message": "成功",
        "data": jsonable_encoder(data),
        "request_id": request.state.request_id,
    }


async def member(request: Request, authorization: str = Header(default="")):
    if not authorization.startswith("Bearer "):
        raise DomainError("AUTH_REQUIRED", "请先登录商城", 401)
    return await services(request).bridge.identity(authorization)


async def idempotency(idempotency_key: UUID = Header()):
    return idempotency_key
