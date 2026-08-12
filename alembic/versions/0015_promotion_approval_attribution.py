"""record promotion approval attribution"""

from alembic import op
import sqlalchemy as sa

revision = "0015_promotion_approval_attribution"
down_revision = "0014_live_output_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "promotion_approvals",
        sa.Column("approved_by", sa.String(16), nullable=True),
    )
    op.create_check_constraint(
        "promotion_approval_actor_ck",
        "promotion_approvals",
        "approved_by IS NULL OR approved_by IN ('owner', 'agent', 'system')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "promotion_approval_actor_ck",
        "promotion_approvals",
        type_="check",
    )
    op.drop_column("promotion_approvals", "approved_by")
