from dsh_api.config import DEFAULT_PLUGINS, Limits, Settings, split_csv


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_limits_are_the_free_tier_defaults(client):
    resp = client.get("/api/v1/limits")
    assert resp.status_code == 200
    assert resp.json() == {
        "servers_per_tenant": 1,
        "heap_gb": 3,
        "memory_limit_gib": 3.5,
        "world_quota_gib": 5,
        "idle_minutes": 20,
        "max_awake_servers": 12,
        "max_registered_servers": 40,
        "archive_after_days": 60,
        "backup_retention_days": 14,
        "minecraft_version": "26.2",
    }


def test_limits_need_no_token(client):
    assert client.get("/api/v1/limits").status_code == 200


def test_limits_come_from_configuration():
    limits = Limits.from_env({"DSH_LIMIT_IDLE_MINUTES": "45", "DSH_LIMIT_HEAP_GB": "2"})
    assert limits.idle_minutes == 45
    assert limits.heap_gb == 2
    assert limits.max_registered_servers == 40


def test_settings_from_env():
    s = Settings.from_env(
        {
            "DSH_BASE_DOMAIN": "example.com",
            "DSH_NODE_IP": "203.0.113.10",
            "DSH_MAX_SERVERS_PER_TENANT": "2",
            "USERAUTH_JWT_SECRET": "s",
            "DSH_SERVICE_ACCOUNT_NAMESPACE": "platform",
            "DSH_SERVICE_ACCOUNT_NAME": "api-sa",
        }
    )
    assert s.hostname("alpha") == "alpha.play.example.com"
    assert s.sslip_hostname("alpha") == "alpha.203-0-113-10.sslip.io"
    assert s.max_servers_per_tenant == 2
    assert s.jwt_secret == "s"
    assert (s.service_account_namespace, s.service_account_name) == ("platform", "api-sa")
    defaults = Settings.from_env({})
    assert (defaults.service_account_namespace, defaults.service_account_name) == (
        "dsh-api",
        "dsh-api",
    )  # what deploy/rbac.yaml creates


def test_settings_defaults_for_admins_and_plugins():
    s = Settings.from_env({"USERAUTH_JWT_SECRET": "s"})
    assert s.admin_users == frozenset({"dmccoystephenson"})
    assert s.is_admin("dmccoystephenson") and not s.is_admin("alice")
    assert s.default_plugins == split_csv(DEFAULT_PLUGINS)
    assert len(s.default_plugins) == 3
    dpm, viaversion, viabackwards = s.default_plugins
    assert dpm.startswith("https://github.com/Dans-Plugins/Dans-Plugin-Manager/")
    # ViaVersion + ViaBackwards ship together: ViaBackwards `depend`s on ViaVersion.
    assert viaversion.startswith("https://github.com/ViaVersion/ViaVersion/releases/download/")
    assert viabackwards.startswith("https://github.com/ViaVersion/ViaBackwards/releases/download/")
    assert viaversion.rsplit("/", 2)[1] == viabackwards.rsplit("/", 2)[1]  # same release


def test_settings_split_comma_separated_lists():
    s = Settings.from_env(
        {
            "DSH_ADMIN_USERS": "alice, bob,",
            "DSH_DEFAULT_PLUGINS": "https://a/x.jar,https://b/y.jar",
        }
    )
    assert s.admin_users == frozenset({"alice", "bob"})
    assert s.default_plugins == ("https://a/x.jar", "https://b/y.jar")
    assert Settings.from_env({"DSH_DEFAULT_PLUGINS": ""}).default_plugins == ()
