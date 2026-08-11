"""record model-loop calls and bounded audit transcripts"""

from alembic import op
import sqlalchemy as sa

revision = "0008_worker_model_loop"
down_revision = "0007_worker_payload_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("worker_run_event_status_ck", "worker_run_events", type_="check")
    op.create_check_constraint(
        "worker_run_event_status_ck",
        "worker_run_events",
        "status IN ('queued','running','operation_running','passed','failed',"
        "'cancel_requested','cancelled','task_finished','model_tool_failed',"
        "'model_route_switched','review_complete')",
    )
    op.create_table(
        "worker_model_calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(80), nullable=False),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("route", sa.String(80), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["worker_runs.id"]),
        sa.CheckConstraint("iteration > 0", name="worker_model_call_iteration_ck"),
        sa.CheckConstraint("cost_usd >= 0", name="worker_model_call_cost_ck"),
    )
    for function, message in (
        ("reject_worker_model_call_mutation", "worker model calls are append-only"),
    ):
        op.execute(
            f"""CREATE OR REPLACE FUNCTION {function}() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION '{message}'; END;
            $$ LANGUAGE plpgsql"""
        )
        op.execute(
            f"""CREATE TRIGGER {function}_trigger BEFORE UPDATE OR DELETE ON worker_model_calls
            FOR EACH ROW EXECUTE FUNCTION {function}()"""
        )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS reject_worker_model_call_mutation_trigger "
        "ON worker_model_calls"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_worker_model_call_mutation")
    op.drop_table("worker_model_calls")
    op.drop_constraint("worker_run_event_status_ck", "worker_run_events", type_="check")
    op.create_check_constraint(
        "worker_run_event_status_ck",
        "worker_run_events",
        "status IN ('queued','running','operation_running','passed','failed',"
        "'cancel_requested','cancelled')",
    )
