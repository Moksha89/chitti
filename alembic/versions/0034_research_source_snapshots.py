"""add durable host-side research source snapshots"""

from alembic import op
import sqlalchemy as sa

revision = "0034_research_source_snapshots"
down_revision = "0033_research_packages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_source_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.UniqueConstraint("content_digest", name="research_snapshots_digest_uq"),
    )


def downgrade() -> None:
    op.drop_table("research_source_snapshots")
