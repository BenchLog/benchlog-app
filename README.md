# BenchLog

A self-hosted project journal for makers. Document, archive, and organize your maker projects — 3D printing, electronics, CNC, woodworking, and everything in between.

## Tech Stack

- **Backend:** Python 3.14, FastAPI, SQLAlchemy 2.0 (async), Alembic
- **Database:** PostgreSQL 18
- **Frontend:** Jinja2 templates, Tailwind CSS v4 (standalone CLI)
- **Auth:** local password, OIDC (multi-provider via Authlib), WebAuthn passkeys
- **Email:** SMTP via aiosmtplib (host configured in admin UI)
- **Package manager:** uv

## Quick Start

### Prerequisites

- [uv](https://docs.astral.sh/uv/) for Python package management
- [Docker](https://www.docker.com/) for PostgreSQL (or a local Postgres instance)

### Development

```bash
# Start Postgres
docker compose up db -d

# Install dependencies
uv sync --extra dev

# Run database migrations
uv run alembic upgrade head

# Build CSS (one-shot or --watch)
./tailwindcss -i benchlog/static/css/input.css -o benchlog/static/css/output.css

# Start the dev server
uv run uvicorn benchlog.main:app --reload
```

Or use the bundled `just dev` target which runs Postgres, migrations, the Tailwind watcher, and uvicorn together.

App is at `http://localhost:8000`. The first signup is always a local account and is auto-promoted to site admin — sign up to bootstrap.

### Docker Compose (full stack)

```bash
docker compose up
```

## Configuration

Bootstrap settings come from environment variables (prefixed `BENCHLOG_`); everything else (SMTP, OIDC providers, site toggles) lives in the database and is configured from the admin UI.

| Variable | Default | Description |
| --- | --- | --- |
| `BENCHLOG_SECRET_KEY` | `change-me` | Session signing key (set this in production; must be ≥32 chars when not localhost) |
| `BENCHLOG_DATABASE_URL` | `postgresql+asyncpg://benchlog:benchlog@localhost/benchlog` | Async SQLAlchemy URL |
| `BENCHLOG_BASE_URL` | `http://localhost:8000` | Public origin — used for OIDC redirects, email links, and WebAuthn RP ID |
| `BENCHLOG_TRUST_PROXY_HEADERS` | `false` | Trust `X-Forwarded-For` for rate-limit client IP. Only enable behind a trusted reverse proxy. |
| `BENCHLOG_METADATA_FETCH_ALLOW_PRIVATE` | `false` | When the link modal previews a URL it fetches OG metadata server-side. By default, requests resolving to loopback / RFC1918 / link-local addresses are blocked (cloud-metadata IPs are blocked unconditionally regardless). Flip this to `true` for single-user self-hosting if you want previews of dev-server / LAN / Docker links. The link itself is always saved either way — only the server-side preview fetch is gated. |

### Optional: seed SMTP / OIDC from env on first boot

The admin UI is the normal way to configure SMTP and OIDC providers. If you're deploying in a container and want those rows seeded from env vars on first startup, set any of the variables below. Seeding is **one-shot** — if a row already exists, changing the env vars has no effect (edit via the admin UI instead).

| Variable | Default | Notes |
| --- | --- | --- |
| `BENCHLOG_INITIAL_SMTP_HOST` | (unset) | Setting this triggers SMTP seeding; leave empty to skip. |
| `BENCHLOG_INITIAL_SMTP_PORT` | `587` | |
| `BENCHLOG_INITIAL_SMTP_USERNAME` / `_PASSWORD` | empty | |
| `BENCHLOG_INITIAL_SMTP_FROM_ADDRESS` / `_FROM_NAME` | empty / `BenchLog` | |
| `BENCHLOG_INITIAL_SMTP_USE_TLS` / `_USE_STARTTLS` | `false` / `true` | Mutually exclusive at the transport layer. |
| `BENCHLOG_INITIAL_SMTP_ENABLED` | `false` | Must be `true` for BenchLog to actually send mail. |
| `BENCHLOG_INITIAL_OIDC_SLUG` | (unset) | Slug + discovery URL + client ID are all required to seed a provider. |
| `BENCHLOG_INITIAL_OIDC_DISPLAY_NAME` | slug | |
| `BENCHLOG_INITIAL_OIDC_DISCOVERY_URL` | (unset) | Must be HTTPS outside localhost dev. |
| `BENCHLOG_INITIAL_OIDC_CLIENT_ID` / `_CLIENT_SECRET` | empty | |
| `BENCHLOG_INITIAL_OIDC_SCOPES` | `openid email profile` | |
| `BENCHLOG_INITIAL_OIDC_ENABLED` | `true` | |
| `BENCHLOG_INITIAL_OIDC_AUTO_CREATE_USERS` / `_AUTO_LINK_VERIFIED_EMAIL` | `false` | |
| `BENCHLOG_INITIAL_OIDC_ALLOW_PRIVATE_NETWORK` | `false` | Permit outbound OIDC requests to private/loopback IPs. Enable for self-hosted IdPs on a LAN. |

## Auth model

- **First user** signs up locally and is auto-promoted to site admin.
- **Subsequent local signups** are gated by the `allow_local_signup` site setting.
- **Email verification** is optional per site setting; requires SMTP configured.
- **OIDC providers** are configured per-instance via `/admin/oidc`. Discovery URL based — works with any conformant IdP.
- **Account linking** defaults to manual: matching emails do not auto-link. Each provider has an `auto_link_verified_email` toggle for trusted IdPs (requires both sides to assert verified email).
- **Passkeys (WebAuthn)** are managed from `/account` and used at `/login` for password-less sign-in. Cannot remove your last sign-in method (password / passkey / OIDC).

## Tests

Pytest suite under `tests/` covers signup/login/logout, password reset, email verification, account settings, admin user management self-protections, OIDC error paths, passkey routes, and the auth middleware.

Tests run against a separate Postgres database (`benchlog_test`) on the same Docker container as dev. The conftest creates and resets the DB on demand — no manual setup needed.

```bash
docker compose up db -d        # Postgres must be reachable
uv sync --extra dev
uv run pytest
```

## License

TBD
