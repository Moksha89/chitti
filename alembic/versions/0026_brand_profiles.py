"""add namespace-scoped owner brand profiles"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0026_brand_profiles"
down_revision = "0025_conflict_declined"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "brand_profiles",
        sa.Column("namespace", sa.String(length=64), primary_key=True),
        sa.ForeignKeyConstraint(["namespace"], ["memory_namespaces.slug"]),
        sa.Column(
            "brand_colors",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("typography", sa.String(length=120), nullable=False),
        sa.Column(
            "poster_formats",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("audience", sa.Text(), nullable=False),
        sa.Column("voice", sa.Text(), nullable=False),
        sa.Column(
            "do_not_use",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("updated_by", sa.String(length=255), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_table(
        "brand_profile_history",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("namespace", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["namespace"], ["memory_namespaces.slug"]),
        sa.Column(
            "profile",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("changed_by", sa.String(length=255), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "brand_profile_history_ns_idx",
        "brand_profile_history",
        ["namespace", "changed_at"],
    )


def downgrade() -> None:
    op.drop_index("brand_profile_history_ns_idx", table_name="brand_profile_history")
    op.drop_table("brand_profile_history")
    op.drop_table("brand_profiles")
