"""add categories and project_categories

Revision ID: e4f5a6b7c8d9
Revises: c2f715a3d84e
Create Date: 2026-04-19 12:00:00.000000

Curated, admin-managed category taxonomy with n-level nesting plus a
project<->category association table. Seeds the initial tree that matches
the product decision (3D Printing, Electronics, Woodworking, Metalworking,
Software, Crafts, plus standalone Photography/Writing/Other).

Slug uniqueness is scoped to the parent (see uq_category_parent_slug) so
"Other" can legitimately appear under multiple branches without collision.
The self-FK uses ON DELETE RESTRICT so an admin can't accidentally strand
children; projects_categories ON DELETE CASCADE cleanly detaches category
assignments when a category is removed.
"""
import uuid as _uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "e4f5a6b7c8d9"
down_revision: Union[str, None] = "c2f715a3d84e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    categories = op.create_table(
        "categories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "sort_order", sa.Integer(), nullable=False, server_default="0"
        ),
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
            ["parent_id"], ["categories.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "parent_id", "slug", name="uq_category_parent_slug"
        ),
    )
    op.create_index(
        op.f("ix_categories_parent_id"),
        "categories",
        ["parent_id"],
        unique=False,
    )

    op.create_table(
        "project_categories",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["category_id"], ["categories.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("project_id", "category_id"),
    )

    # --- seed the starter taxonomy ---
    # Generate parent UUIDs in Python so the children rows can reference
    # them in the same migration. bulk_insert can't reference rows it's
    # inserting alongside, so we do two passes.
    def _pid() -> _uuid.UUID:
        return _uuid.uuid4()

    roots = [
        ("3d-printing", "3D Printing", 10),
        ("electronics", "Electronics", 20),
        ("woodworking", "Woodworking", 30),
        ("metalworking", "Metalworking", 40),
        ("software", "Software", 50),
        ("crafts", "Crafts", 60),
        ("photography", "Photography", 70),
        ("writing", "Writing", 80),
        ("other", "Other", 90),
    ]
    root_ids: dict[str, _uuid.UUID] = {slug: _pid() for slug, _, _ in roots}

    op.bulk_insert(
        categories,
        [
            {
                "id": root_ids[slug],
                "parent_id": None,
                "slug": slug,
                "name": name,
                "sort_order": sort_order,
            }
            for slug, name, sort_order in roots
        ],
    )

    children_spec: list[tuple[str, str, str, int]] = [
        # (parent_slug, slug, name, sort_order)
        ("3d-printing", "fdm", "FDM", 10),
        ("3d-printing", "resin", "Resin", 20),
        ("electronics", "arduino", "Arduino", 10),
        ("electronics", "raspberry-pi", "Raspberry Pi", 20),
        ("electronics", "pcb", "PCB", 30),
        ("woodworking", "joinery", "Joinery", 10),
        ("woodworking", "turning", "Turning", 20),
        ("metalworking", "welding", "Welding", 10),
        ("metalworking", "machining", "Machining", 20),
        ("software", "web", "Web", 10),
        ("software", "mobile", "Mobile", 20),
        ("software", "cli", "CLI", 30),
        ("software", "embedded", "Embedded", 40),
        ("crafts", "sewing", "Sewing", 10),
        ("crafts", "leather", "Leather", 20),
        ("crafts", "paper", "Paper", 30),
    ]
    op.bulk_insert(
        categories,
        [
            {
                "id": _pid(),
                "parent_id": root_ids[parent_slug],
                "slug": slug,
                "name": name,
                "sort_order": sort_order,
            }
            for parent_slug, slug, name, sort_order in children_spec
        ],
    )


def downgrade() -> None:
    op.drop_table("project_categories")
    op.drop_index(op.f("ix_categories_parent_id"), table_name="categories")
    op.drop_table("categories")
