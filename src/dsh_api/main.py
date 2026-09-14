"""FastAPI application. Build it with ``create_app`` (uvicorn: ``--factory``)."""

from dataclasses import asdict
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from dsh_api import __version__
from dsh_api.auth import Hs256Validator, InvalidToken, TokenValidator, bearer_token
from dsh_api.cluster import ClusterBackend, ClusterError, KubectlHelmBackend
from dsh_api.config import Settings
from dsh_api.db import Database
from dsh_api.mojang import MojangResolver, UnknownUsername, UuidResolver
from dsh_api.service import (
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


def create_app(
    settings: Settings | None = None,
    *,
    db: Database | None = None,
    cluster: ClusterBackend | None = None,
    validator: TokenValidator | None = None,
    uuids: UuidResolver | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    db = db or Database(settings.db_path)
    cluster = cluster or KubectlHelmBackend(
        settings.omcsi_dir, settings.backup_dir, settings.helm_timeout
    )
    validator = validator or Hs256Validator(settings.jwt_secret)
    uuids = uuids or MojangResolver()
    service = ServerService(settings, db, cluster, uuids)

    app = FastAPI(title="dsh-api", version=__version__)
    app.state.settings = settings
    app.state.service = service

    def current_tenant(authorization: Annotated[str | None, Header()] = None) -> str:
        try:
            tenant_id = validator.validate(bearer_token(authorization))
        except InvalidToken as exc:
            raise HTTPException(401, str(exc), headers={"WWW-Authenticate": "Bearer"}) from None
        db.ensure_tenant(tenant_id)
        return tenant_id

    Tenant = Annotated[str, Depends(current_tenant)]

    @app.exception_handler(ClusterError)
    def _cluster_error(_: Request, exc: ClusterError) -> JSONResponse:
        return JSONResponse({"detail": f"cluster operation failed: {exc}"}, status_code=502)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/limits")
    def limits() -> dict:
        return {"servers_per_tenant": settings.max_servers_per_tenant, **asdict(settings.limits)}

    @app.get("/api/v1/servers")
    def list_servers(tenant: Tenant) -> list[dict]:
        return [asdict(s) for s in service.list(tenant)]

    @app.post("/api/v1/servers", status_code=201)
    def create_server(tenant: Tenant, body: CreateServer) -> dict:
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

    @app.post("/api/v1/servers/{name}/wake")
    def wake_server(tenant: Tenant, name: str) -> dict:
        try:
            return asdict(service.wake(tenant, name))
        except ServerNotFound:
            raise HTTPException(404, "no such server") from None

    @app.delete("/api/v1/servers/{name}")
    def delete_server(tenant: Tenant, name: str, force: bool = False) -> dict:
        try:
            backup = service.delete(tenant, name, force=force)
        except ServerNotFound:
            raise HTTPException(404, "no such server") from None
        except PlayersOnline as exc:
            raise HTTPException(
                409, f"{exc.count} player(s) online; pass ?force=true to disconnect them"
            ) from None
        return {"name": name, "deleted": True, "backup": backup}

    return app
