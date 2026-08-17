"""add immutable host-side research packages for design facts"""

from alembic import op
import sqlalchemy as sa

revision = "0033_research_packages"
down_revision = "0032_run_event_status_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_packages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("namespace", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("facts", sa.JSON(), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.String(length=120), nullable=True),
        sa.Column("reference_assets", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.UniqueConstraint("content_digest", name="research_packages_digest_uq"),
        sa.CheckConstraint(
            "json_typeof(facts) = 'object'",
            name="research_packages_facts_object_ck",
        ),
        sa.CheckConstraint(
            "json_typeof(sources) = 'array'",
            name="research_packages_sources_array_ck",
        ),
    )
    op.create_index(
        "research_packages_namespace_idx",
        "research_packages",
        ["namespace", "created_at"],
    )
    op.add_column(
        "plan_revisions",
        sa.Column("research_package_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "plan_revisions_research_package_fk",
        "plan_revisions",
        "research_packages",
        ["research_package_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "plan_revisions_research_package_fk",
        "plan_revisions",
        type_="foreignkey",
    )
    op.drop_column("plan_revisions", "research_package_id")
    op.drop_index("research_packages_namespace_idx", table_name="research_packages")
    op.drop_table("research_packages")
