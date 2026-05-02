"""Round-trip tests for has_gps / is_quarantined columns."""

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from benchlog.models import FileVersion, Project, ProjectFile


@pytest_asyncio.fixture
async def sample_user(db):
    from benchlog.models import User
    user = User(
        username="gpsuser",
        email="gps@test.com",
        display_name="GPS Tester",
        email_verified=True,
    )
    db.add(user)
    await db.commit()
    return user


@pytest_asyncio.fixture
async def sample_file_version(db, sample_user):
    project = Project(user_id=sample_user.id, title="P", slug="p")
    db.add(project)
    await db.flush()
    pf = ProjectFile(project_id=project.id, path="", filename="x.jpg")
    db.add(pf)
    await db.flush()
    fv = FileVersion(
        file_id=pf.id,
        version_number=1,
        storage_path="files/test/1",
        original_name="x.jpg",
        size_bytes=10,
        mime_type="image/jpeg",
        checksum="deadbeef",
    )
    db.add(fv)
    await db.commit()
    return fv


async def test_has_gps_defaults_to_null(db, sample_file_version):
    await db.refresh(sample_file_version)
    assert sample_file_version.has_gps is None


async def test_is_quarantined_defaults_to_false(db, sample_file_version):
    await db.refresh(sample_file_version)
    assert sample_file_version.is_quarantined is False


async def test_has_gps_round_trips(db, sample_file_version):
    sample_file_version.has_gps = True
    await db.commit()
    await db.refresh(sample_file_version)
    assert sample_file_version.has_gps is True


async def test_is_quarantined_round_trips(db, sample_file_version):
    sample_file_version.is_quarantined = True
    await db.commit()
    await db.refresh(sample_file_version)
    assert sample_file_version.is_quarantined is True
