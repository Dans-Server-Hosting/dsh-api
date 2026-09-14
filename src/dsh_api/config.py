"""Configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_ADMIN_USERS = "dmccoystephenson"
DEFAULT_PLUGINS = (
    "https://github.com/Dans-Plugins/Dans-Plugin-Manager/releases/download/"
    "v0.7.0-SNAPSHOT-8-8-2026/DansPluginManager-0.7.0-SNAPSHOT-8-8-2026.jar"
)


def split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Limits:
    """The free-tier profile, as numbers the portal can display.

    These describe what the cluster enforces; they are configuration, not
    constants, so the two can be kept in step without a code change.
    """

    heap_gb: float = 3
    memory_limit_gib: float = 3.5
    world_quota_gib: float = 5
    idle_minutes: int = 20
    max_awake_servers: int = 12
    max_registered_servers: int = 40
    archive_after_days: int = 60
    backup_retention_days: int = 14
    minecraft_version: str = "26.2"

    @classmethod
    def from_env(cls, env: dict[str, str]) -> Limits:
        return cls(
            heap_gb=float(env.get("DSH_LIMIT_HEAP_GB", cls.heap_gb)),
            memory_limit_gib=float(env.get("DSH_LIMIT_MEMORY_LIMIT_GIB", cls.memory_limit_gib)),
            world_quota_gib=float(env.get("DSH_LIMIT_WORLD_QUOTA_GIB", cls.world_quota_gib)),
            idle_minutes=int(env.get("DSH_LIMIT_IDLE_MINUTES", cls.idle_minutes)),
            max_awake_servers=int(env.get("DSH_LIMIT_MAX_AWAKE", cls.max_awake_servers)),
            max_registered_servers=int(
                env.get("DSH_LIMIT_MAX_REGISTERED", cls.max_registered_servers)
            ),
            archive_after_days=int(env.get("DSH_LIMIT_ARCHIVE_AFTER_DAYS", cls.archive_after_days)),
            backup_retention_days=int(
                env.get("DSH_LIMIT_BACKUP_RETENTION_DAYS", cls.backup_retention_days)
            ),
            minecraft_version=env.get("DSH_LIMIT_MINECRAFT_VERSION", cls.minecraft_version),
        )


@dataclass(frozen=True)
class Settings:
    db_path: str = "dsh-api.db"
    jwt_secret: str = ""
    base_domain: str = "dansserverhosting.com"
    node_ip: str = ""
    omcsi_dir: str = "/opt/omcsi"
    backup_dir: str = "/backups"
    rollout_timeout: str = "5m"
    service_account_namespace: str = "dsh-api"
    service_account_name: str = "dsh-api"
    """The ServiceAccount the API runs as; each tenant namespace gets a
    RoleBinding granting it the dsh-api-tenant ClusterRole there."""
    max_servers_per_tenant: int = 1
    admin_users: frozenset[str] = frozenset(split_csv(DEFAULT_ADMIN_USERS))
    default_plugins: tuple[str, ...] = split_csv(DEFAULT_PLUGINS)
    limits: Limits = field(default_factory=Limits)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        return cls(
            db_path=env.get("DSH_DB_PATH", cls.db_path),
            jwt_secret=env.get("USERAUTH_JWT_SECRET", cls.jwt_secret),
            base_domain=env.get("DSH_BASE_DOMAIN", cls.base_domain),
            node_ip=env.get("DSH_NODE_IP", cls.node_ip),
            omcsi_dir=env.get("OMCSI_CHART_DIR", cls.omcsi_dir),
            backup_dir=env.get("DSH_BACKUP_DIR", cls.backup_dir),
            rollout_timeout=env.get("DSH_ROLLOUT_TIMEOUT", cls.rollout_timeout),
            service_account_namespace=env.get(
                "DSH_SERVICE_ACCOUNT_NAMESPACE", cls.service_account_namespace
            ),
            service_account_name=env.get("DSH_SERVICE_ACCOUNT_NAME", cls.service_account_name),
            max_servers_per_tenant=int(
                env.get("DSH_MAX_SERVERS_PER_TENANT", cls.max_servers_per_tenant)
            ),
            admin_users=frozenset(split_csv(env.get("DSH_ADMIN_USERS", DEFAULT_ADMIN_USERS))),
            default_plugins=split_csv(env.get("DSH_DEFAULT_PLUGINS", DEFAULT_PLUGINS)),
            limits=Limits.from_env(env),
        )

    def is_admin(self, username: str) -> bool:
        return username in self.admin_users

    def hostname(self, name: str) -> str:
        return f"{name}.play.{self.base_domain}"

    def sslip_hostname(self, name: str) -> str:
        return f"{name}.{self.node_ip.replace('.', '-')}.sslip.io"
