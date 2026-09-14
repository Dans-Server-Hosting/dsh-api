from dsh_api.config import Limits, Settings


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
        }
    )
    assert s.hostname("alpha") == "alpha.play.example.com"
    assert s.sslip_hostname("alpha") == "alpha.203-0-113-10.sslip.io"
    assert s.max_servers_per_tenant == 2
    assert s.jwt_secret == "s"
