"""persist unresolved memory contradictions"""

from alembic import op
import sqlalchemy as sa

revision = "0002_conflicts"
down_revision = "0001_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_conflicts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("decision_key", sa.String(255), nullable=False),
        sa.Column("existing_decision_id", sa.Integer(), nullable=False),
        sa.Column("proposed_value", sa.Text(), nullable=False),
        sa.Column("proposed_rationale", sa.Text(), nullable=True),
        sa.Column("proposed_project", sa.String(255), nullable=True),
        sa.Column("proposed_source", sa.String(32), nullable=False),
        sa.Column("resolution_decision_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["existing_decision_id"], ["decisions.id"]),
        sa.ForeignKeyConstraint(["resolution_decision_id"], ["decisions.id"]),
    )


def downgrade() -> None:
    op.drop_table("memory_conflicts")
