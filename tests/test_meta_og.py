"""OpenGraph / Twitter meta tags on public URLs.

Public project pages and all profile pages render social-preview tags.
Private project pages omit them (don't leak metadata if a URL escapes).
"""

from benchlog.models import Project, ProjectStatus
from tests.conftest import make_user


async def test_profile_page_has_og_tags(client, db):
    await make_user(
        db,
        email="alice@test.com",
        username="alice",
        display_name="Alice Maker",
    )
    # Bio used as description — markdown stripped.
    from sqlalchemy import select
    from benchlog.models import User

    alice = (await db.execute(select(User))).scalar_one()
    alice.bio = "I build **tiny** CNC routers."
    await db.commit()

    resp = await client.get("/u/alice")
    body = resp.text
    assert 'property="og:title" content="Alice Maker (@alice)"' in body
    assert 'property="og:type" content="profile"' in body
    assert 'property="og:url" content="http://testserver/u/alice"' in body
    # Markdown stripped from description.
    assert 'property="og:description" content="I build tiny CNC routers."' in body
    # No image → summary card.
    assert 'name="twitter:card" content="summary"' in body
    assert 'rel="canonical" href="http://testserver/u/alice"' in body


async def test_public_project_page_has_og_tags(client, db):
    user = await make_user(db, email="a@t.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="Bench",
            slug="bench",
            description="A small woodworking bench.",
            status=ProjectStatus.in_progress,
            is_public=True,
        )
    )
    await db.commit()

    resp = await client.get("/u/alice/bench")
    body = resp.text
    assert 'property="og:title" content="Bench"' in body
    assert 'property="og:type" content="article"' in body
    assert 'property="og:url" content="http://testserver/u/alice/bench"' in body
    assert (
        'property="og:description" content="A small woodworking bench."' in body
    )


async def test_private_project_page_omits_og_tags(client, db):
    user = await make_user(db, email="a@t.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="Secret",
            slug="secret",
            description="Hush hush.",
            status=ProjectStatus.idea,
            is_public=False,
        )
    )
    await db.commit()
    # Owner needs to be logged in to view a private project.
    from tests.conftest import login

    await login(client, "alice")

    resp = await client.get("/u/alice/secret")
    assert resp.status_code == 200
    body = resp.text
    assert 'property="og:title"' not in body
    assert 'property="og:description"' not in body


async def test_excerpt_truncates_long_bio(client, db):
    # Exercise the plain_excerpt helper via the profile page. Long bio gets
    # cut near the 200-char default with a trailing ellipsis.
    await make_user(db, email="a@t.com", username="alice", display_name="Alice")
    from sqlalchemy import select
    from benchlog.models import User

    alice = (await db.execute(select(User))).scalar_one()
    alice.bio = "A" * 300
    await db.commit()

    resp = await client.get("/u/alice")
    body = resp.text
    # 200 'A's (approximately; word-boundary trim may shave a few) + ellipsis.
    assert "A" * 150 in body  # plenty of A's made it in
    assert "…" in body  # truncation marker present
