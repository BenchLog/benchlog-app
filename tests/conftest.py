"""Pytest fixtures for the BenchLog test suite.

Strategy:
- Run against a separate Postgres database (`benchlog_test`) created on demand.
- Replace the app's engine with a NullPool variant so connections aren't
  pinned to a specific event loop across tests.
- Truncate every table after each test for isolation.
"""

import asyncio
import os
from collections.abc import AsyncIterator

# These MUST be set before any benchlog import so pydantic-settings picks them up.
os.environ.setdefault(
    "BENCHLOG_DATABASE_URL",
    "postgresql+asyncpg://benchlog:benchlog@localhost/benchlog_test",
)
os.environ.setdefault("BENCHLOG_SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("BENCHLOG_BASE_URL", "http://testserver")

import asyncpg  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

import benchlog.database as db_module  # noqa: E402
from benchlog.config import settings  # noqa: E402
from benchlog.models import Base, User  # noqa: E402
from benchlog.auth.passwords import hash_password  # noqa: E402
from benchlog.rate_limit import limiter  # noqa: E402

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session = async_sessionmaker(_test_engine, class_=AsyncSession, expire_on_commit=False)
db_module.engine = _test_engine
db_module.async_session = _test_session

from benchlog.main import app  # noqa: E402  (imported after engine swap)


def _admin_dsn() -> dict:
    return {
        "host": "localhost",
        "user": "benchlog",
        "password": "benchlog",
        "database": "postgres",
    }


async def _create_test_db_if_missing() -> None:
    conn = await asyncpg.connect(**_admin_dsn())
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname='benchlog_test'"
        )
        if not exists:
            await conn.execute("CREATE DATABASE benchlog_test")
    finally:
        await conn.close()


async def _reset_schema() -> None:
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


def _bootstrap() -> None:
    asyncio.run(_create_test_db_if_missing())
    asyncio.run(_reset_schema())


_bootstrap()


@pytest_asyncio.fixture(autouse=True)
async def _truncate_tables() -> AsyncIterator[None]:
    yield
    async with _test_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())
    # Rate limiter is in-process and would otherwise leak state across tests,
    # eventually tripping 429s in later tests that issue many logins.
    limiter._hits.clear()


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with _test_session() as session:
        yield session


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as c:
        yield c


# ---------- helpers ----------


async def make_user(
    db: AsyncSession,
    *,
    email: str = "user@test.com",
    username: str = "user",
    display_name: str | None = None,
    password: str | None = "testpass1234",
    is_site_admin: bool = False,
    email_verified: bool = True,
    is_active: bool = True,
) -> User:
    user = User(
        email=email,
        username=username,
        display_name=display_name or username,
        password_hash=hash_password(password) if password else None,
        is_site_admin=is_site_admin,
        email_verified=email_verified,
        is_active=is_active,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def csrf_token(client: AsyncClient, path: str = "/login") -> str:
    """GET a page that renders the csrf meta tag, extract and return the token.

    Follows redirects so `/signup` (which may bounce to `/login` when signup is
    disabled) still yields a token.
    """
    import re

    r = await client.get(path, follow_redirects=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.text)
    if m is None:
        raise RuntimeError(f"No csrf-token meta tag in {path} response")
    return m.group(1)


async def post_form(
    client: AsyncClient,
    url: str,
    data: dict | None = None,
    *,
    csrf_path: str = "/login",
    **kwargs,
):
    """POST a form with a fresh CSRF token auto-injected."""
    payload = dict(data or {})
    if "_csrf" not in payload:
        payload["_csrf"] = await csrf_token(client, csrf_path)
    return await client.post(url, data=payload, **kwargs)


async def login(client: AsyncClient, identifier: str, password: str = "testpass1234"):
    token = await csrf_token(client, "/login")
    return await client.post(
        "/login",
        data={"identifier": identifier, "password": password, "_csrf": token},
    )


@pytest.fixture
def signup_payload():
    return {
        "email": "first@test.com",
        "username": "first",
        "display_name": "First",
        "password": "testpass1234",
        "password_confirm": "testpass1234",
    }
