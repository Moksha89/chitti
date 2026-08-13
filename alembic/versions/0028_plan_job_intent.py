"""persist planning job intent and poster configuration"""

from alembic import op
import sqlalchemy as sa

revision = "0028_plan_job_intent"
down_revision = "0027_run_job_types"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table_name in ("plan_jobs", "plan_revisions"):
        op.add_column(
            table_name,
            sa.Column(
                "job_type",
                sa.String(length=32),
                nullable=False,
                server_default="website",
            ),
        )
        op.add_column(
            table_name,
            sa.Column(
                "job_config",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'::json"),
            ),
        )
        op.create_check_constraint(
            f"{table_name}_job_type_ck",
            table_name,
            "job_type IN ('website', 'poster')",
        )


def downgrade() -> None:
    for table_name in ("plan_revisions", "plan_jobs"):
        op.drop_constraint(f"{table_name}_job_type_ck", table_name, type_="check")
        op.drop_column(table_name, "job_config")
        op.drop_column(table_name, "job_type")
