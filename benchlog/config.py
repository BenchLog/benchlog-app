from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Bootstrap configuration — loaded from environment at startup.

    Runtime-configurable things (SMTP, OIDC providers, site settings) live
    in the database and are managed from the admin UI. The `initial_*`
    fields below are only consulted on startup by services/bootstrap.py —
    they seed SMTP and a single OIDC provider when no rows exist yet.
    Changing them after first boot has no effect; edit via the admin UI.
    """

    model_config = SettingsConfigDict(env_prefix="BENCHLOG_", env_file=".env", extra="ignore")

    secret_key: str = "change-me"
    database_url: str = "postgresql+asyncpg://benchlog:benchlog@localhost/benchlog"
    base_url: str = "http://localhost:8000"

    # Trust X-Forwarded-For for client IP (rate limiting). Only enable when
    # deployed behind a trusted reverse proxy — the header is spoofable.
    trust_proxy_headers: bool = False

    # ---- initial SMTP (seeded once when no config row exists) ----
    initial_smtp_host: str = ""
    initial_smtp_port: int = 587
    initial_smtp_username: str = ""
    initial_smtp_password: str = ""
    initial_smtp_from_address: str = ""
    initial_smtp_from_name: str = "BenchLog"
    initial_smtp_use_tls: bool = False
    initial_smtp_use_starttls: bool = True
    initial_smtp_enabled: bool = False

    # ---- initial OIDC provider (seeded once when no providers exist) ----
    # Requires slug + discovery_url + client_id to activate; other fields
    # fall back to sensible defaults.
    initial_oidc_slug: str = ""
    initial_oidc_display_name: str = ""
    initial_oidc_discovery_url: str = ""
    initial_oidc_client_id: str = ""
    initial_oidc_client_secret: str = ""
    initial_oidc_scopes: str = "openid email profile"
    initial_oidc_enabled: bool = True
    initial_oidc_auto_create_users: bool = False
    initial_oidc_auto_link_verified_email: bool = False
    initial_oidc_allow_private_network: bool = False

    # ---- link metadata fetcher ----
    # Server-side OG metadata fetcher: when False, requests resolving to
    # private/loopback/link-local addresses are blocked (cloud-metadata
    # IPs always blocked regardless). When True, allows previews of dev
    # servers + LAN URLs — appropriate for single-user self-hosting.
    # Saved links never depend on this; only the preview fetch does.
    metadata_fetch_allow_private: bool = False

    # ---- storage ----
    # Pluggable backend; only "local" is wired up right now. The s3_* fields
    # are placeholders for a future S3/MinIO backend.
    storage_backend: str = "local"
    storage_local_path: str = "./data/files"
    storage_s3_bucket: str = ""
    storage_s3_endpoint: str = ""
    storage_s3_access_key: str = ""
    storage_s3_secret_key: str = ""
    storage_s3_region: str = ""

    # Upload limits (bytes). Enforced at the file-upload route.
    max_upload_size: int = 500 * 1024 * 1024  # 500 MB
    # "*" means no extension whitelist; otherwise comma-separated list.
    allowed_extensions: str = "*"

    @property
    def storage_path(self) -> Path:
        return Path(self.storage_local_path)


settings = Settings()
