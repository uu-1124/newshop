from uuid import UUID

from fastapi import Depends, Query, Request
from pydantic import Field

from app.api.admin import staff
from app.api.dependencies import idempotency, ok, services
from app.api.dto import Input, VersionInput


class ProfileCreate(Input):
    name: str = Field(min_length=1, max_length=100)
    capability: str = Field(max_length=20)
    provider: str = Field(max_length=80)
    model_name: str = Field(min_length=1, max_length=160)
    endpoint_ref: str = Field(max_length=160)
    secret_ref: str = Field(max_length=200)
    public_options: dict


class ProfileUpdate(VersionInput):
    name: str = Field(min_length=1, max_length=100)
    model_name: str = Field(min_length=1, max_length=160)
    endpoint_ref: str = Field(max_length=160)
    secret_ref: str | None = Field(default=None, max_length=200)
    public_options: dict
    enabled: bool


class ActivationInput(VersionInput):
    compatibility_job_id: UUID


class PromptInput(Input):
    prompt_key: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=30000)
    change_note: str = Field(max_length=500)


def register_model_routes(app):
    prefix = "/mall-agent/v1/admin"

    def service(request):
        return services(request).extensions.model_admin.require()

    @app.get(prefix + "/model-registry", operation_id="getModelRegistry")
    async def registry(request: Request, principal=Depends(staff)):
        return ok(request, await service(request).registry(principal))

    @app.get(prefix + "/model-profiles", operation_id="listModelProfiles")
    async def listing(
        request: Request,
        limit: int = Query(20, ge=1, le=100),
        cursor: str | None = Query(None, max_length=2048),
        principal=Depends(staff),
    ):
        return ok(request, await service(request).listing(principal, limit, cursor))

    @app.get(prefix + "/model-profiles/{profile_id}/versions", operation_id="listModelProfileVersions")
    async def versions(
        profile_id: UUID,
        request: Request,
        limit: int = Query(20, ge=1, le=100),
        cursor: str | None = Query(None, max_length=2048),
        principal=Depends(staff),
    ):
        return ok(request, await service(request).listing(principal, limit, cursor, profile_id))

    @app.post(prefix + "/model-profiles", status_code=201, operation_id="createModelProfile")
    async def create(
        body: ProfileCreate, request: Request, principal=Depends(staff), key=Depends(idempotency)
    ):
        return ok(request, await service(request).create(principal, key, body.model_dump(mode="json")))

    @app.patch(prefix + "/model-profiles/{profile_id}", operation_id="updateModelProfile")
    async def update(
        profile_id: UUID,
        body: ProfileUpdate,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return ok(
            request, await service(request).update(principal, profile_id, key, body.model_dump(mode="json"))
        )

    @app.post(prefix + "/model-profiles/{profile_id}/test", status_code=202, operation_id="testModelProfile")
    async def test(
        profile_id: UUID,
        body: VersionInput,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return ok(
            request, await service(request).test(principal, profile_id, key, body.model_dump(mode="json"))
        )

    @app.post(prefix + "/model-profiles/{profile_id}/activate", operation_id="activateModelProfile")
    async def activate(
        profile_id: UUID,
        body: ActivationInput,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return ok(
            request, await service(request).activate(principal, profile_id, key, body.model_dump(mode="json"))
        )

    @app.get(prefix + "/prompt-versions", operation_id="listPrompts")
    async def prompts(
        request: Request,
        prompt_key: str = Query("", max_length=80),
        limit: int = Query(20, ge=1, le=100),
        cursor: str | None = Query(None, max_length=2048),
        principal=Depends(staff),
    ):
        return ok(request, await service(request).prompts(principal, limit, cursor, prompt_key))

    @app.post(prefix + "/prompt-versions", status_code=201, operation_id="createPrompt")
    async def create_prompt(
        body: PromptInput, request: Request, principal=Depends(staff), key=Depends(idempotency)
    ):
        return ok(request, await service(request).create_prompt(principal, key, body.model_dump(mode="json")))

    @app.post(prefix + "/prompt-versions/{prompt_id}/publish", operation_id="publishPrompt")
    async def publish_prompt(
        prompt_id: UUID,
        body: VersionInput,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await service(request).publish_prompt(principal, prompt_id, key, body.model_dump(mode="json")),
        )
