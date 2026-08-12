"""surface persistent host runner housekeeping failures"""

from alembic import op
import sqlalchemy as sa


revision = "0020_runner_health"
down_revision = "0019_run_restart_reconcile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runner_health",
        sa.Column("component", sa.String(64), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("first_failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('failed', 'healthy')", name="runner_health_status_ck"),
        sa.CheckConstraint(
            "consecutive_failures >= 0", name="runner_health_failure_count_ck"
        ),
    )


def downgrade() -> None:
    op.drop_table("runner_health")
