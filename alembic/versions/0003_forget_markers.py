"""append-only markers for forgotten decisions"""

from alembic import op
import sqlalchemy as sa

revision = "0003_forget_markers"
down_revision = "0002_conflicts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "decision_forgets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("decision_id", sa.Integer(), nullable=False, unique=True),
        sa.ForeignKeyConstraint(["decision_id"], ["decisions.id"]),
    )


def downgrade() -> None:
    op.drop_table("decision_forgets")
