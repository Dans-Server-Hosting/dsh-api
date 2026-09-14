"""FastAPI application. Build it with ``create_app`` (uvicorn: ``--factory``)."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from dsh_api import __version__
from dsh_api.auth import Hs256Validator, InvalidToken, TokenValidator, bearer_token
from dsh_api.cluster import ClusterBackend, ClusterError, KubectlHelmBackend
from dsh_api.config import Settings
from dsh_api.db import Database
from dsh_api.feedback import FeedbackNotFound, FeedbackRateLimited, FeedbackService
from dsh_api.mojang import MojangResolver, UnknownUsername, UuidResolver
from dsh_api.service import (
    CreateInProgress,
    JobRunner,
    NameTaken,
    PlayersOnline,
    ServerNotFound,
    ServerService,
    TenantAtCap,
    validate_name,
)


class CreateServer(BaseModel):
    name: str = Field(examples=["myserver"])
    motd: str | None = Field(default=None, max_length=120)
    operator_username: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_]{3,16}$")

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return validate_name(value)

    @field_validator("motd")
    @classmethod
    def _motd(cls, value: str | None) -> str | None:
        # The MOTD travels through ``helm --set``, where these characters have meaning.
        if value is not None and (set(value) & set(",=[]{}\\") or not value.isprintable()):
            raise ValueError("motd must be printable and may not contain , = [ ] { } or \\")
        return value


class SubmitFeedback(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    page: str | None = Field(default=None, max_length=200, examples=["/servers"])


class UpdateFeedback(BaseModel):
    status: Literal["read", "new"]


def create_app(
    settings: Settings | None = None,
    *,
    db: Database | None = None,
    cluster: ClusterBackend | None = None,
    validator: TokenValidator | None = None,
    uuids: UuidResolver | None = None,
    jobs: JobRunner | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    db = db or Database(settings.db_path)
    cluster = cluster or KubectlHelmBackend(
        settings.omcsi_dir, settings.backup_dir, settings.rollout_timeout
    )
    validator = validator or Hs256Validator(settings.jwt_secret)
    uuids = uuids or MojangResolver()
    # Provisioning runs off the request thread; two at a time is plenty for a
    # node that hosts a dozen servers, and keeps kubectl/helm from piling up.
    jobs = jobs or ThreadPoolExecutor(max_workers=2, thread_name_prefix="dsh-provision")
    service = ServerService(settings, db, cluster, uuids, jobs)
    service.recover_interrupted()
    feedback = FeedbackService(db)

    app = FastAPI(title="dsh-api", version=__version__)
    app.state.settings = settings
    app.state.service = service
    app.state.feedback = feedback
    app.state.jobs = jobs

    def current_tenant(authorization: Annotated[str | None, Header()] = None) -> str:
        try:
            tenant_id = validator.validate(bearer_token(authorization))
        except InvalidToken as exc:
            raise HTTPException(401, str(exc), headers={"WWW-Authenticate": "Bearer"}) from None
        db.ensure_tenant(tenant_id)
        return tenant_id

    Tenant = Annotated[str, Depends(current_tenant)]

    def current_admin(tenant: Tenant) -> str:
        if not settings.is_admin(tenant):
            raise HTTPException(403, "admin only")
        return tenant

    Admin = Annotated[str, Depends(current_admin)]

    @app.exception_handler(ClusterError)
    def _cluster_error(_: Request, exc: ClusterError) -> JSONResponse:
        return JSONResponse({"detail": f"cluster operation failed: {exc}"}, status_code=502)

    @app.exception_handler(CreateInProgress)
    def _create_in_progress(_: Request, exc: CreateInProgress) -> JSONResponse:
        # Distinct from the cap 403: the earlier request is succeeding, not refused.
        return JSONResponse(
            {"detail": "a server is already being created for this account", "server": exc.name},
            status_code=409,
        )

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/limits")
    def limits() -> dict:
        return {"servers_per_tenant": settings.max_servers_per_tenant, **asdict(settings.limits)}

    @app.get("/api/v1/servers")
    def list_servers(tenant: Tenant) -> list[dict]:
        return [asdict(s) for s in service.list(tenant)]

    @app.post("/api/v1/servers", status_code=202)
    def create_server(tenant: Tenant, body: CreateServer) -> dict:
        """Reserve the server and answer at once; ``GET`` reports ``provisioning``
        until the background steps have finished. The admin password is here only."""
        try:
            view, admin_password = service.create(
                tenant, body.name, body.motd, body.operator_username
            )
        except TenantAtCap:
            raise HTTPException(
                403, f"tenant is at its cap of {settings.max_servers_per_tenant} server(s)"
            ) from None
        except NameTaken:
            raise HTTPException(409, f"the name '{body.name}' is taken") from None
        except UnknownUsername as exc:
            raise HTTPException(422, f"operator username could not be resolved: {exc}") from None
        return {**asdict(view), "admin_username": "admin", "admin_password": admin_password}

    @app.get("/api/v1/servers/{name}")
    def get_server(tenant: Tenant, name: str) -> dict:
        try:
            return asdict(service.get(tenant, name))
        except ServerNotFound:
            raise HTTPException(404, "no such server") from None

    @app.post("/api/v1/servers/{name}/wake", status_code=202)
    def wake_server(tenant: Tenant, name: str) -> dict:
        try:
            return asdict(service.wake(tenant, name))
        except ServerNotFound:
            raise HTTPException(404, "no such server") from None

    @app.delete("/api/v1/servers/{name}")
    def delete_server(tenant: Tenant, name: str, force: bool = False) -> dict:
        """Back up, uninstall and remove the namespace. Works for a ``failed``
        create too (its backup is skipped when there is nothing to read)."""
        try:
            backup = service.delete(tenant, name, force=force)
        except ServerNotFound:
            raise HTTPException(404, "no such server") from None
        except CreateInProgress:
            raise HTTPException(
                409, "the server is still being created; wait for it to finish"
            ) from None
        except PlayersOnline as exc:
            raise HTTPException(
                409, f"{exc.count} player(s) online; pass ?force=true to disconnect them"
            ) from None
        return {"name": name, "deleted": True, "backup": backup}

    @app.get("/api/v1/me")
    def me(tenant: Tenant) -> dict:
        return {"username": tenant, "is_admin": settings.is_admin(tenant)}

    @app.post("/api/v1/feedback", status_code=201)
    def submit_feedback(tenant: Tenant, body: SubmitFeedback) -> dict:
        try:
            return asdict(feedback.submit(tenant, body.message, body.page))
        except FeedbackRateLimited as exc:
            raise HTTPException(
                429, f"at most {exc.limit} feedback messages per hour; try again later"
            ) from None

    @app.get("/api/v1/feedback")
    def list_feedback(_: Admin, status: Literal["new", "read", "all"] = "new") -> list[dict]:
        return [asdict(row) for row in feedback.list(status)]

    @app.patch("/api/v1/feedback/{feedback_id}")
    def update_feedback(_: Admin, feedback_id: int, body: UpdateFeedback) -> dict:
        try:
            return asdict(feedback.set_status(feedback_id, body.status))
        except FeedbackNotFound:
            raise HTTPException(404, "no such feedback") from None

    return app
