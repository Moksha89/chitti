"""record successful runner housekeeping sweeps"""

from alembic import op
import sqlalchemy as sa


revision = "0021_runner_health_success"
down_revision = "0020_runner_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runner_health",
        sa.Column("last_succeeded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runner_health", "last_succeeded_at")
