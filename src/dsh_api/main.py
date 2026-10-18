"""FastAPI application. Build it with ``create_app`` (uvicorn: ``--factory``)."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

from dsh_api import __version__
from dsh_api.auth import Hs256Validator, InvalidToken, TokenValidator, bearer_token
from dsh_api.catalog import CatalogUnavailable, DpcCatalog, PluginCatalog
from dsh_api.cluster import ClusterBackend, ClusterError, KubectlHelmBackend
from dsh_api.config import Settings
from dsh_api.db import Database
from dsh_api.export_service import (
    ExportConflict,
    ExportDisabled,
    ExportLimit,
    ExportNotFound,
    ExportService,
    InvalidArea,
)
from dsh_api.feedback import FeedbackNotFound, FeedbackRateLimited, FeedbackService
from dsh_api.mojang import MojangResolver, UnknownUsername, UuidResolver
from dsh_api.plugin_service import (
    PluginNotSelected,
    PluginRequired,
    PluginSelectionDisabled,
    PluginService,
    ServerBusy,
    UnknownPlugin,
)
from dsh_api.plugins import describe_default_plugins
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


class ApplyPlugins(BaseModel):
    confirm_players: bool = False


class ExportArea(BaseModel):
    """Two opposite X/Z corners, inclusive block coordinates; every height."""

    x1: int
    z1: int
    x2: int
    z2: int


class CreateExport(BaseModel):
    area: ExportArea | None = None


def create_app(
    settings: Settings | None = None,
    *,
    db: Database | None = None,
    cluster: ClusterBackend | None = None,
    validator: TokenValidator | None = None,
    uuids: UuidResolver | None = None,
    jobs: JobRunner | None = None,
    catalog: PluginCatalog | None = None,
    export_jobs: JobRunner | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    if not settings.node_ip:
        # Every server's fallback hostname is <name>.<ip-dashed>.sslip.io.
        raise ValueError("DSH_NODE_IP is required")
    db = db or Database(settings.db_path)
    cluster = cluster or KubectlHelmBackend(
        settings.omcsi_dir,
        settings.backup_dir,
        settings.rollout_timeout,
        service_account=(settings.service_account_namespace, settings.service_account_name),
    )
    validator = validator or Hs256Validator(settings.jwt_secret)
    uuids = uuids or MojangResolver()
    # Provisioning and deletes run off the request thread; two at a time is
    # plenty for a node that hosts a dozen servers, and keeps kubectl/helm
    # from piling up.
    jobs = jobs or ThreadPoolExecutor(max_workers=2, thread_name_prefix="dsh-provision")
    service = ServerService(settings, db, cluster, uuids, jobs)
    service.recover_interrupted()
    feedback = FeedbackService(db)
    catalog = catalog or DpcCatalog(settings.default_plugins, api=settings.dpc_api)
    plugins = PluginService(settings, db, cluster, service, catalog)
    # One export at a time on the whole node (RFC 0021 §5); the rest wait queued.
    export_jobs = export_jobs or ThreadPoolExecutor(max_workers=1, thread_name_prefix="dsh-export")
    exports = ExportService(settings, db, cluster, service, export_jobs)
    if settings.world_export:
        exports.recover_interrupted()
        exports.sweep()

    app = FastAPI(title="dsh-api", version=__version__)
    app.state.settings = settings
    app.state.service = service
    app.state.feedback = feedback
    app.state.jobs = jobs
    app.state.exports = exports

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

    @app.exception_handler(PluginSelectionDisabled)
    def _plugins_disabled(_: Request, exc: PluginSelectionDisabled) -> JSONResponse:
        return JSONResponse(
            {"detail": "choosing plugins is not switched on for this service yet"},
            status_code=503,
        )

    @app.exception_handler(CatalogUnavailable)
    def _catalog_unavailable(_: Request, exc: CatalogUnavailable) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(ExportDisabled)
    def _exports_disabled(_: Request, exc: ExportDisabled) -> JSONResponse:
        return JSONResponse({"detail": "world export is not enabled"}, status_code=503)

    @app.exception_handler(ExportNotFound)
    def _export_not_found(_: Request, exc: ExportNotFound) -> JSONResponse:
        return JSONResponse({"detail": "no such export"}, status_code=404)

    @app.exception_handler(ExportConflict)
    def _export_conflict(_: Request, exc: ExportConflict) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ExportLimit)
    def _export_limit(_: Request, exc: ExportLimit) -> JSONResponse:
        return JSONResponse(
            {
                "detail": f"at most {exc.limit} exports a day; try again later",
                "retry_after": exc.retry_after,
            },
            status_code=429,
            headers={"Retry-After": str(exc.retry_after)},
        )

    @app.exception_handler(InvalidArea)
    def _invalid_area(_: Request, exc: InvalidArea) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(ServerNotFound)
    def _server_not_found(_: Request, exc: ServerNotFound) -> JSONResponse:
        return JSONResponse({"detail": "no such server"}, status_code=404)

    @app.exception_handler(ServerBusy)
    def _server_busy(_: Request, exc: ServerBusy) -> JSONResponse:
        return JSONResponse(
            {"detail": f"the server is {exc.state}; try again once it is up"},
            status_code=409,
        )

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/limits")
    def limits() -> dict:
        return {
            "servers_per_tenant": settings.max_servers_per_tenant,
            **asdict(settings.limits),
            **exports.limits(),
        }

    @app.get("/api/v1/default-plugins")
    def default_plugins() -> list[dict]:
        """The plugins every new server is installed with, in install order.
        Public, like ``/limits``: the portal shows it before sign-in."""
        return describe_default_plugins(settings.default_plugins)

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

    @app.delete("/api/v1/servers/{name}", status_code=202)
    def delete_server(tenant: Tenant, name: str, force: bool = False) -> dict:
        """Accept the delete and answer at once; ``GET`` reports ``deleting``
        while the backup, uninstall and namespace removal run, then 404. Works
        for a ``failed`` create too (its backup is skipped when there is
        nothing to read)."""
        try:
            return asdict(service.delete(tenant, name, force=force))
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

    @app.get("/api/v1/catalog")
    def plugin_catalog() -> list[dict]:
        """Every plugin a tenant can install, by title. Public, like
        ``/default-plugins``: the portal can show it before sign-in."""
        return plugins.list_catalog()

    @app.get("/api/v1/servers/{name}/plugins")
    def server_plugins(tenant: Tenant, name: str) -> dict:
        """The server's selection, each plugin's status from the last boot, and
        ``pending`` while a change waits for the next start."""
        return plugins.view(tenant, name)

    @app.put("/api/v1/servers/{name}/plugins/{slug}")
    def add_plugin(tenant: Tenant, name: str, slug: str) -> dict:
        """Select a plugin and anything it requires; idempotent. Takes effect at
        the next start (``POST .../plugins/apply`` to restart now)."""
        try:
            return plugins.add(tenant, name, slug)
        except UnknownPlugin as exc:
            raise HTTPException(404, f"no plugin '{exc.slug}' in the catalog") from None

    @app.delete("/api/v1/servers/{name}/plugins/{slug}")
    def remove_plugin(tenant: Tenant, name: str, slug: str) -> dict:
        """Deselect a plugin. Its data folder is kept. ``unused_dependencies``
        names what was only there for it, for the portal to offer removing."""
        try:
            return plugins.remove(tenant, name, slug)
        except PluginNotSelected:
            raise HTTPException(404, f"'{slug}' is not installed on this server") from None
        except PluginRequired as exc:
            return JSONResponse(
                {
                    "detail": f"{slug} is needed by {', '.join(exc.dependents)}",
                    "required_by": exc.dependents,
                },
                status_code=409,
            )

    @app.post("/api/v1/servers/{name}/plugins/apply", status_code=202)
    def apply_plugins(tenant: Tenant, name: str, body: ApplyPlugins | None = None) -> dict:
        """Restart an awake server so its selection takes effect; an asleep one
        applies it at its next wake. With players online, 409 unless
        ``confirm_players``."""
        try:
            return plugins.apply(tenant, name, (body or ApplyPlugins()).confirm_players)
        except PlayersOnline as exc:
            return JSONResponse(
                {
                    "detail": f"{exc.count} player(s) online; confirm to restart anyway",
                    "players_online": exc.count,
                },
                status_code=409,
            )

    # --- world export (RFC 0021); 503 while DSH_WORLD_EXPORT is off ------------

    @app.get("/api/v1/servers/{name}/exports")
    def list_exports(tenant: Tenant, name: str) -> dict:
        """The server's unexpired exports, newest first."""
        return {"exports": exports.list(tenant, name)}

    @app.post("/api/v1/servers/{name}/exports", status_code=202)
    def create_export(tenant: Tenant, name: str, body: CreateExport | None = None) -> dict:
        """Start an export: by default 256 × 256 blocks around spawn at every
        height, or ``area``. Answers at once with the export ``queued``; poll it
        until ``ready`` or ``failed``. Never writes the world or pauses the game."""
        area = (body or CreateExport()).area
        corners = (area.x1, area.z1, area.x2, area.z2) if area else None
        return exports.create(tenant, name, corners)

    @app.get("/api/v1/servers/{name}/exports/{export_id}")
    def get_export(tenant: Tenant, name: str, export_id: str) -> dict:
        return exports.get(tenant, name, export_id)

    @app.delete("/api/v1/servers/{name}/exports/{export_id}", status_code=204)
    def delete_export(tenant: Tenant, name: str, export_id: str) -> Response:
        exports.delete(tenant, name, export_id)
        return Response(status_code=204)

    @app.get("/api/v1/servers/{name}/exports/{export_id}/preview/{file}")
    def export_preview(tenant: Tenant, name: str, export_id: str, file: str) -> FileResponse:
        """A usd-web preview file: ``index.json`` or a ``.uwt`` tile (gzip
        bytes the viewer unpacks itself)."""
        path = exports.preview_file(tenant, name, export_id, file)
        media = "application/json" if file == "index.json" else "application/octet-stream"
        return FileResponse(path, media_type=media, headers={"Cache-Control": "private"})

    @app.post("/api/v1/servers/{name}/exports/{export_id}/link")
    def export_link(tenant: Tenant, name: str, export_id: str) -> dict:
        """A signed link to the zip, valid for 15 minutes, that needs no token."""
        return exports.link(tenant, name, export_id)

    @app.get("/api/v1/export-downloads/{token}")
    def export_download(token: str) -> FileResponse:
        """No Bearer token: the signed link is the credential."""
        path, filename = exports.download(token)
        return FileResponse(
            path,
            media_type="application/zip",
            filename=filename,
            headers={"Cache-Control": "private, no-store"},
        )

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
