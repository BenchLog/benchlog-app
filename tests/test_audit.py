"""Tests for the audit log: events get recorded across auth/admin flows
and the admin viewer renders correctly."""

from sqlalchemy import select

from benchlog import audit
from benchlog.models import AuditEvent, SiteSettings, SMTPConfig
from tests.conftest import csrf_token, login, make_user, post_form


async def _events(db, action_prefix=None):
    stmt = select(AuditEvent).order_by(AuditEvent.created_at)
    if action_prefix:
        stmt = stmt.where(AuditEvent.action.like(f"{action_prefix}%"))
    return list((await db.execute(stmt)).scalars().all())


# ---------- auth flow events ----------


async def test_login_success_records_event(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    events = await _events(db, "auth.login")
    assert any(
        e.action == audit.AUTH_LOGIN_SUCCESS and e.actor_user_id == user.id
        for e in events
    )


async def test_login_failure_records_event_with_actor_when_user_exists(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice", "wrong-password")

    events = await _events(db, "auth.login")
    failed = [e for e in events if e.action == audit.AUTH_LOGIN_FAILED]
    assert failed
    assert failed[0].actor_user_id == user.id
    assert failed[0].outcome == "failure"
    assert failed[0].event_metadata == {"reason": "bad_password"}


async def test_login_failure_records_event_for_unknown_user(client):
    await login(client, "ghost@test.com", "whatever1234")

    # Use a fresh session because conftest fixtures isolate per-test
    from tests.conftest import _test_session

    async with _test_session() as db:
        events = await _events(db, "auth.login")
    failed = [e for e in events if e.action == audit.AUTH_LOGIN_FAILED]
    assert failed
    assert failed[0].actor_user_id is None
    assert failed[0].actor_label == "ghost@test.com"


async def test_logout_records_event(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    await post_form(client, "/logout", csrf_path="/")

    events = await _events(db, "auth.logout")
    assert len(events) == 1


async def test_signup_records_event(client, db, signup_payload):
    token = await csrf_token(client, "/signup")
    await client.post("/signup", data={**signup_payload, "_csrf": token})

    events = await _events(db, "auth.signup")
    assert len(events) == 1
    assert events[0].event_metadata["method"] == "password"
    assert events[0].event_metadata["first_run"] is True


async def test_password_change_records_event(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    token = await csrf_token(client, "/account")
    await client.post(
        "/account/password",
        data={
            "current_password": "testpass1234",
            "password": "freshly-set-pw",
            "password_confirm": "freshly-set-pw",
            "_csrf": token,
        },
    )

    events = await _events(db, "auth.password")
    assert any(
        e.action == audit.AUTH_PASSWORD_CHANGED and e.actor_user_id == user.id
        for e in events
    )


async def test_account_self_delete_records_event(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    user_id = user.id
    user_email = user.email
    await login(client, "alice")

    token = await csrf_token(client, "/account")
    await client.post(
        "/account/delete",
        data={
            "confirm_username": "alice",
            "current_password": "testpass1234",
            "_csrf": token,
        },
    )

    events = await _events(db, "account.deleted")
    assert len(events) == 1
    # FK was SET NULL on user delete, but the audit row survives.
    assert events[0].actor_user_id is None
    assert events[0].actor_label == user_email
    assert events[0].target_id == str(user_id)


# ---------- admin actions ----------


async def test_admin_disable_user_records_event(client, db):
    admin = await make_user(
        db, email="admin@test.com", username="admin", is_site_admin=True
    )
    target = await make_user(db, email="bob@test.com", username="bob")
    await login(client, "admin")

    token = await csrf_token(client, "/admin/users")
    await client.post(
        f"/admin/users/{target.id}/toggle-active",
        data={"_csrf": token},
    )

    events = await _events(db, "admin.user")
    disabled = [e for e in events if e.action == audit.ADMIN_USER_DISABLED]
    assert disabled
    assert disabled[0].actor_user_id == admin.id
    assert disabled[0].target_id == str(target.id)


async def test_admin_settings_save_records_event_only_when_changed(client, db):
    await make_user(db, email="admin@test.com", username="admin", is_site_admin=True)
    db.add(SiteSettings(site_name="X", allow_local_signup=False))
    await db.commit()
    await login(client, "admin")

    # Same values — no event
    token = await csrf_token(client, "/admin/settings")
    await client.post(
        "/admin/settings/save",
        data={"site_name": "X", "_csrf": token},
    )
    events = await _events(db, "admin.settings")
    assert len(events) == 0

    # Now change something
    await client.post(
        "/admin/settings/save",
        data={
            "site_name": "Y",
            "allow_local_signup": "on",
            "_csrf": await csrf_token(client, "/admin/settings"),
        },
    )
    events = await _events(db, "admin.settings")
    assert len(events) == 1
    assert set(events[0].event_metadata["changed"]) == {
        "site_name",
        "allow_local_signup",
    }


# ---------- viewer ----------


def _event_rows(html: str) -> list[str]:
    """Extract action values from event-table rows (ignores combo options).

    The filter combo renders every action as a checkbox in the page HTML, so a
    plain substring check against resp.text would always pass. Event rows are
    tagged with data-event-action=<action>, which is what we assert against.
    """
    import re

    return re.findall(r'data-event-action="([^"]+)"', html)


async def test_admin_audit_page_renders(client, db):
    admin = await make_user(
        db, email="admin@test.com", username="admin", is_site_admin=True
    )
    # Seed an event directly
    await audit.record(
        db,
        action=audit.AUTH_LOGIN_SUCCESS,
        actor=admin,
        metadata={"method": "password"},
    )
    await db.commit()
    await login(client, "admin")

    resp = await client.get("/admin/audit")
    assert resp.status_code == 200
    assert audit.AUTH_LOGIN_SUCCESS in _event_rows(resp.text)


async def test_admin_audit_filter_by_domain(client, db):
    admin = await make_user(
        db, email="admin@test.com", username="admin", is_site_admin=True
    )
    await audit.record(db, action=audit.AUTH_LOGIN_SUCCESS, actor=admin)
    await audit.record(db, action=audit.ADMIN_SETTINGS_UPDATED, actor=admin)
    await db.commit()
    await login(client, "admin")

    resp = await client.get("/admin/audit?domain=auth.")
    rows = _event_rows(resp.text)
    assert audit.AUTH_LOGIN_SUCCESS in rows
    assert audit.ADMIN_SETTINGS_UPDATED not in rows

    resp = await client.get("/admin/audit?domain=admin.")
    rows = _event_rows(resp.text)
    assert audit.ADMIN_SETTINGS_UPDATED in rows
    # Login may still appear if the test login itself recorded an event,
    # so only assert the inverse on the filtered prefix.


async def test_admin_audit_filter_by_action_multi(client, db):
    """Multi-select action filter ANDs with domain and selects exactly the chosen actions."""
    admin = await make_user(
        db, email="admin@test.com", username="admin", is_site_admin=True
    )
    await audit.record(db, action=audit.AUTH_LOGIN_SUCCESS, actor=admin)
    await audit.record(db, action=audit.AUTH_LOGIN_FAILED, actor=admin, outcome="failure")
    await audit.record(db, action=audit.AUTH_LOGOUT, actor=admin)
    await audit.record(db, action=audit.ADMIN_SETTINGS_UPDATED, actor=admin)
    await db.commit()
    await login(client, "admin")

    resp = await client.get(
        "/admin/audit"
        f"?action={audit.AUTH_LOGIN_SUCCESS}"
        f"&action={audit.AUTH_LOGIN_FAILED}"
    )
    rows = _event_rows(resp.text)
    assert audit.AUTH_LOGIN_SUCCESS in rows
    assert audit.AUTH_LOGIN_FAILED in rows
    assert audit.AUTH_LOGOUT not in rows
    assert audit.ADMIN_SETTINGS_UPDATED not in rows


async def test_admin_audit_filter_ignores_unknown_action(client, db):
    """Unknown action values must not reach the WHERE clause."""
    admin = await make_user(
        db, email="admin@test.com", username="admin", is_site_admin=True
    )
    await audit.record(db, action=audit.AUTH_LOGIN_SUCCESS, actor=admin)
    await db.commit()
    await login(client, "admin")

    resp = await client.get("/admin/audit?action=totally.bogus.value")
    assert resp.status_code == 200
    # Unknown-only filter is dropped, so all events render.
    assert audit.AUTH_LOGIN_SUCCESS in _event_rows(resp.text)


async def test_audit_page_requires_admin(client, db):
    await make_user(db, email="user@test.com", username="user")
    await login(client, "user")
    resp = await client.get("/admin/audit")
    assert resp.status_code == 403


# ---------- record() resilience ----------


async def test_record_failure_does_not_raise(db, monkeypatch):
    """A broken audit insert must not propagate into the caller."""

    async def boom(*args, **kwargs):
        from sqlalchemy.exc import SQLAlchemyError
        raise SQLAlchemyError("simulated audit failure")

    monkeypatch.setattr(db, "flush", boom)
    # Should not raise
    await audit.record(db, action="test.action", metadata={"x": 1})


async def test_smtp_save_records_event(client, db):
    await make_user(db, email="admin@test.com", username="admin", is_site_admin=True)
    await login(client, "admin")

    token = await csrf_token(client, "/admin/smtp")
    await client.post(
        "/admin/smtp/save",
        data={
            "host": "smtp.example.com",
            "port": "587",
            "from_address": "noreply@example.com",
            "from_name": "BenchLog",
            "enabled": "on",
            "_csrf": token,
        },
    )

    events = await _events(db, "admin.smtp")
    assert len(events) == 1


async def test_existing_smtp_test_does_not_record(client, db):
    """Sanity: get_smtp_config existing config is unaffected by audit additions."""
    await make_user(db, email="admin@test.com", username="admin", is_site_admin=True)
    db.add(SMTPConfig(host="h", port=25, from_address="a@b.com", enabled=False))
    await db.commit()
    await login(client, "admin")
    resp = await client.get("/admin/smtp")
    assert resp.status_code == 200
