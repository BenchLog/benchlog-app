"""redesign links: drop flat ProjectLink, add LinkSection + new ProjectLink

Revision ID: 55a77b7c702f
Revises: e7b2f4a91c3d
Create Date: 2026-04-24 12:00:00.000000

The flat `project_links` row format (with the `link_type` enum) is
replaced by two tables: `link_sections` (user-named accordion buckets,
case-insensitive uniqueness per project) and a rebuilt `project_links`
without a direct `project_id` (owner-scoping joins through the section).
The `link_type` Postgres enum is dropped — there is no replacement.

Both new tables use TimestampMixin (created_at + updated_at) so renames
and note edits get an audit timestamp.

No data preservation: pre-launch project, no production rows.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "55a77b7c702f"
down_revision: Union[str, None] = "e7b2f4a91c3d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the existing project_links table and its enum.
    op.drop_index(op.f("ix_project_links_project_id"), table_name="project_links")
    op.drop_table("project_links")
    sa.Enum(name="link_type").drop(op.get_bind(), checkfirst=False)

    # New: link_sections — owner of the project owns the section.
    op.create_table(
        "link_sections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("name_key", sa.String(length=120), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "name_key", name="uq_link_sections_project_name"
        ),
    )
    op.create_index(
        op.f("ix_link_sections_project_id"),
        "link_sections",
        ["project_id"],
        unique=False,
    )

    # Rebuilt project_links — only section_id ties it to a project.
    op.create_table(
        "project_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("note", sa.String(length=280), nullable=True),
        sa.Column("og_title", sa.String(length=512), nullable=True),
        sa.Column("og_description", sa.Text(), nullable=True),
        sa.Column("og_image_url", sa.String(length=2048), nullable=True),
        sa.Column("og_site_name", sa.String(length=256), nullable=True),
        sa.Column("favicon_url", sa.String(length=2048), nullable=True),
        sa.Column("metadata_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["section_id"], ["link_sections.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_project_links_section_id"),
        "project_links",
        ["section_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_project_links_section_id"), table_name="project_links")
    op.drop_table("project_links")
    op.drop_index(op.f("ix_link_sections_project_id"), table_name="link_sections")
    op.drop_table("link_sections")

    # Recreate the original `project_links` shape so a downgrade leaves
    # the schema useable. Old enum values restored verbatim. The
    # `sa.Enum(..., name="link_type")` in the column definition triggers
    # type creation as part of `op.create_table` — no explicit
    # `.create()` needed, matching the pattern in `047e59d6fd67`.
    op.create_table(
        "project_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column(
            "link_type",
            sa.Enum(
                "github",
                "website",
                "video",
                "cloud_storage",
                "store",
                "documentation",
                "other",
                name="link_type",
            ),
            nullable=False,
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_project_links_project_id"),
        "project_links",
        ["project_id"],
        unique=False,
    )
