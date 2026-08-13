"""add explicit worker run job types"""

from alembic import op
import sqlalchemy as sa

revision = "0027_run_job_types"
down_revision = "0026_brand_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "worker_runs",
        sa.Column(
            "job_type",
            sa.String(length=32),
            nullable=False,
            server_default="website",
        ),
    )
    op.add_column(
        "worker_runs",
        sa.Column(
            "job_config",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.create_check_constraint(
        "worker_runs_job_type_ck",
        "worker_runs",
        "job_type IN ('website', 'poster')",
    )


def downgrade() -> None:
    op.drop_constraint("worker_runs_job_type_ck", "worker_runs", type_="check")
    op.drop_column("worker_runs", "job_config")
    op.drop_column("worker_runs", "job_type")
