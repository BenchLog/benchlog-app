"""Tests for the public user profile at /u/{username}.

Covers route-level visibility (guest + case-insensitive + inactive),
rendering (bio markdown, social link icons, pinned ordering, empty
states), owner-only UI (Edit profile button), and the account-page edit
surface (bio + social links CRUD + URL normalization).
"""

from sqlalchemy import select

from benchlog.models import (
    Project,
    ProjectStatus,
    UserSocialLink,
    UserSocialLinkType,
)
from tests.conftest import csrf_token, login, make_user, post_form


# ---------- view: basic rendering ----------


async def test_profile_page_renders_for_existing_user(client, db):
    await make_user(
        db, email="alice@test.com", username="alice", display_name="Alice Maker"
    )

    resp = await client.get("/u/alice")
    assert resp.status_code == 200
    assert "Alice Maker" in resp.text
    assert "@alice" in resp.text


async def test_profile_page_404_for_unknown_user(client):
    resp = await client.get("/u/nope")
    assert resp.status_code == 404


async def test_profile_page_case_insensitive_username(client, db):
    await make_user(db, email="alice@test.com", username="alice")

    resp = await client.get("/u/ALICE")
    assert resp.status_code == 200
    assert "@alice" in resp.text


async def test_profile_page_404_for_inactive_user(client, db):
    await make_user(
        db, email="ghost@test.com", username="ghost", is_active=False
    )
    resp = await client.get("/u/ghost")
    assert resp.status_code == 404


async def test_profile_page_guest_can_view(client, db):
    await make_user(db, email="alice@test.com", username="alice")

    # No login — relying on middleware whitelist for 2-segment /u/{username}.
    resp = await client.get("/u/alice")
    assert resp.status_code == 200


# ---------- view: projects list ----------


async def test_profile_page_shows_public_projects_only(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    db.add_all(
        [
            Project(
                user_id=alice.id,
                title="Public Bench",
                slug="public-bench",
                status=ProjectStatus.in_progress,
                is_public=True,
            ),
            Project(
                user_id=alice.id,
                title="Secret Drawer",
                slug="secret-drawer",
                status=ProjectStatus.in_progress,
                is_public=False,
            ),
        ]
    )
    await db.commit()

    resp = await client.get("/u/alice")
    assert resp.status_code == 200
    assert "Public Bench" in resp.text
    assert "Secret Drawer" not in resp.text


async def test_profile_page_pinned_projects_first(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    db.add_all(
        [
            Project(
                user_id=alice.id,
                title="Unpinned First",
                slug="unpinned-first",
                status=ProjectStatus.in_progress,
                is_public=True,
                pinned=False,
            ),
            Project(
                user_id=alice.id,
                title="Pinned Thing",
                slug="pinned-thing",
                status=ProjectStatus.in_progress,
                is_public=True,
                pinned=True,
            ),
        ]
    )
    await db.commit()

    resp = await client.get("/u/alice")
    assert resp.status_code == 200
    body = resp.text
    # Pinned must appear before unpinned regardless of updated_at ordering.
    assert body.index("Pinned Thing") < body.index("Unpinned First")


async def test_profile_page_empty_projects_shows_empty_state(client, db):
    await make_user(db, email="alice@test.com", username="alice")

    resp = await client.get("/u/alice")
    assert resp.status_code == 200
    assert "No public projects yet." in resp.text


# ---------- view: bio + social links ----------


async def test_profile_page_bio_renders_markdown(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    user.bio = "Hello I am **bold**."
    await db.commit()

    resp = await client.get("/u/alice")
    assert resp.status_code == 200
    assert "<strong>bold</strong>" in resp.text


async def test_profile_page_social_links_render_with_icons(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add_all(
        [
            UserSocialLink(
                user_id=user.id,
                link_type=UserSocialLinkType.github,
                url="https://github.com/alice",
                sort_order=0,
            ),
            UserSocialLink(
                user_id=user.id,
                link_type=UserSocialLinkType.website,
                url="https://alice.example",
                sort_order=1,
            ),
        ]
    )
    await db.commit()

    resp = await client.get("/u/alice")
    assert resp.status_code == 200
    assert 'data-lucide="github"' in resp.text
    assert 'data-lucide="globe"' in resp.text
    assert "https://github.com/alice" in resp.text
    assert "https://alice.example" in resp.text


# ---------- view: owner UI ----------


async def test_profile_page_edit_button_only_visible_to_owner(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")

    # Alice — owner — sees the Edit button.
    await login(client, "alice")
    resp = await client.get("/u/alice")
    assert resp.status_code == 200
    assert "Edit profile" in resp.text

    # Bob — different user — does not.
    await login(client, "bob")
    resp = await client.get("/u/alice")
    assert resp.status_code == 200
    assert "Edit profile" not in resp.text


# ---------- edit surface: bio ----------


async def test_account_bio_update_persists(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    resp = await post_form(
        client,
        "/account/bio",
        {"bio": "Tinkerer, tinkerer, tinkerer."},
        csrf_path="/account",
    )
    assert resp.status_code == 302

    await db.refresh(user)
    assert user.bio == "Tinkerer, tinkerer, tinkerer."


# ---------- edit surface: social links ----------


async def test_account_social_link_add_and_delete(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    # Add a GitHub link.
    resp = await post_form(
        client,
        "/account/social-links",
        {"link_type": "github", "url": "https://github.com/alice"},
        csrf_path="/account",
    )
    assert resp.status_code == 302

    links = (
        await db.execute(
            select(UserSocialLink).where(UserSocialLink.user_id == user.id)
        )
    ).scalars().all()
    assert len(links) == 1
    assert links[0].link_type == UserSocialLinkType.github
    assert links[0].url == "https://github.com/alice"

    # It appears on the public profile.
    resp = await client.get("/u/alice")
    assert "https://github.com/alice" in resp.text

    # Delete it.
    resp = await post_form(
        client,
        f"/account/social-links/{links[0].id}/delete",
        {},
        csrf_path="/account",
    )
    assert resp.status_code == 302

    remaining = (
        await db.execute(
            select(UserSocialLink).where(UserSocialLink.user_id == user.id)
        )
    ).scalars().all()
    assert remaining == []

    resp = await client.get("/u/alice")
    assert "https://github.com/alice" not in resp.text


async def test_social_link_url_normalized(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    resp = await post_form(
        client,
        "/account/social-links",
        {"link_type": "github", "url": "github.com/alice"},
        csrf_path="/account",
    )
    assert resp.status_code == 302

    link = (
        await db.execute(
            select(UserSocialLink).where(UserSocialLink.user_id == user.id)
        )
    ).scalar_one()
    # normalize_url should have prepended https://
    assert link.url == "https://github.com/alice"


async def test_social_link_invalid_url_rejected(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    # javascript: URLs are blocked by normalize_url and must not persist.
    resp = await post_form(
        client,
        "/account/social-links",
        {"link_type": "other", "url": "javascript:alert(1)"},
        csrf_path="/account",
    )
    assert resp.status_code == 302

    links = (
        await db.execute(
            select(UserSocialLink).where(UserSocialLink.user_id == user.id)
        )
    ).scalars().all()
    assert links == []


async def test_cannot_delete_other_users_social_link(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    link = UserSocialLink(
        user_id=alice.id,
        link_type=UserSocialLinkType.github,
        url="https://github.com/alice",
        sort_order=0,
    )
    db.add(link)
    await db.commit()
    await db.refresh(link)
    link_id = link.id

    await login(client, "bob")
    token = await csrf_token(client, "/account")
    resp = await client.post(
        f"/account/social-links/{link_id}/delete",
        data={"_csrf": token},
    )
    assert resp.status_code == 302

    still = (
        await db.execute(
            select(UserSocialLink).where(UserSocialLink.id == link_id)
        )
    ).scalar_one_or_none()
    assert still is not None
