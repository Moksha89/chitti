"""immutable worker runs and artifacts"""

from alembic import op
import sqlalchemy as sa

revision = "0006_worker_cage"
down_revision = "0005_plan_approval_spine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revision_id", sa.Integer(), nullable=False),
        sa.Column("limits", sa.JSON(), nullable=False),
        sa.Column("workspace_id", sa.String(128), nullable=False),
        sa.ForeignKeyConstraint(["revision_id"], ["plan_revisions.id"]),
    )
    op.create_table(
        "worker_run_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("operation_index", sa.Integer(), nullable=True),
        sa.Column("task_id", sa.String(80), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["worker_runs.id"]),
        sa.CheckConstraint(
            "status IN ('queued','running','operation_running','passed','failed','cancel_requested','cancelled')",
            name="worker_run_event_status_ck",
        ),
    )
    op.create_table(
        "worker_operations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(80), nullable=False),
        sa.Column("operation_index", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("stdout", sa.Text(), nullable=False),
        sa.Column("stderr", sa.Text(), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["worker_runs.id"]),
    )
    op.create_table(
        "worker_artifacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["worker_runs.id"]),
        sa.ForeignKeyConstraint(["operation_id"], ["worker_operations.id"]),
        sa.CheckConstraint("byte_size >= 0", name="worker_artifact_size_ck"),
    )
    for table, function, message in (
        ("worker_runs", "reject_worker_run_mutation", "worker runs are immutable"),
        ("worker_run_events", "reject_worker_event_mutation", "worker run events are append-only"),
        ("worker_operations", "reject_worker_operation_mutation", "worker operations are append-only"),
        ("worker_artifacts", "reject_worker_artifact_mutation", "worker artifacts are immutable"),
    ):
        op.execute(
            f"""CREATE OR REPLACE FUNCTION {function}() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION '{message}'; END;
            $$ LANGUAGE plpgsql"""
        )
        op.execute(
            f"""CREATE TRIGGER {function}_trigger BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION {function}()"""
        )


def downgrade() -> None:
    for table, function in (
        ("worker_artifacts", "reject_worker_artifact_mutation"),
        ("worker_operations", "reject_worker_operation_mutation"),
        ("worker_run_events", "reject_worker_event_mutation"),
        ("worker_runs", "reject_worker_run_mutation"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {function}_trigger ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {function}")
    op.drop_table("worker_artifacts")
    op.drop_table("worker_operations")
    op.drop_table("worker_run_events")
    op.drop_table("worker_runs")
