"""add project_relations

Revision ID: a9c3d71f4e22
Revises: f7c91a2b5e01
Create Date: 2026-04-19 18:00:00.000000

Typed directed edges between projects. Source-owner declares; target
doesn't opt in. Unique on (source, target, type) so a project can be
both inspired_by AND depends_on the same target, but not declare the
same relation twice. CHECK forbids self-links.

`fork_of` is included in the enum from day one so the Forks feature
can land without an ALTER TYPE step. User routes reject it at the
application layer; only the server-side Forks flow creates those rows.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a9c3d71f4e22"
down_revision: Union[str, None] = "f7c91a2b5e01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_relations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column(
            "relation_type",
            sa.Enum(
                "inspired_by",
                "related_to",
                "depends_on",
                "fork_of",
                name="relation_type",
            ),
            nullable=False,
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
            ["source_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["target_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id",
            "target_id",
            "relation_type",
            name="uq_project_relation_triple",
        ),
        sa.CheckConstraint(
            "source_id <> target_id",
            name="ck_project_relation_no_self",
        ),
    )
    op.create_index(
        op.f("ix_project_relations_source_id"),
        "project_relations",
        ["source_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_project_relations_target_id"),
        "project_relations",
        ["target_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_project_relations_target_id"), table_name="project_relations"
    )
    op.drop_index(
        op.f("ix_project_relations_source_id"), table_name="project_relations"
    )
    op.drop_table("project_relations")
    sa.Enum(name="relation_type").drop(op.get_bind(), checkfirst=False)
