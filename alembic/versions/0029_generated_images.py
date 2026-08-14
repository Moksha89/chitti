"""record host-generated poster images"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0029_generated_images"
down_revision = "0028_plan_job_intent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_image_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("cache_digest", sa.String(64), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("negative_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("parameters", postgresql.JSONB(), nullable=False),
        sa.Column("workflow_template_id", sa.String(120), nullable=False),
        sa.Column("endpoint_id", sa.String(120), nullable=False),
        sa.Column("worker_image", sa.String(255), nullable=False),
        sa.Column("delay_time_ms", sa.Integer(), nullable=False),
        sa.Column("execution_time_ms", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("cache_hit", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["run_id"], ["worker_runs.id"]),
    )
    op.create_index("worker_image_jobs_run_id_idx", "worker_image_jobs", ["run_id"])
    op.execute(
        """CREATE OR REPLACE FUNCTION reject_worker_image_job_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'worker image jobs are append-only'; END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE TRIGGER reject_worker_image_job_mutation_trigger
        BEFORE UPDATE OR DELETE ON worker_image_jobs
        FOR EACH ROW EXECUTE FUNCTION reject_worker_image_job_mutation()"""
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS reject_worker_image_job_mutation_trigger "
        "ON worker_image_jobs"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_worker_image_job_mutation")
    op.drop_index("worker_image_jobs_run_id_idx", table_name="worker_image_jobs")
    op.drop_table("worker_image_jobs")
