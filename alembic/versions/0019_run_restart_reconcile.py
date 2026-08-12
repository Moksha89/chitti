"""track worker execution heartbeats and interrupted terminal runs"""

from alembic import op
import sqlalchemy as sa


revision = "0019_run_restart_reconcile"
down_revision = "0018_reminders_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_run_heartbeats",
        sa.Column("run_id", sa.Integer(), primary_key=True),
        sa.Column("runner_id", sa.String(128), nullable=False),
        sa.Column(
            "heartbeat_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["run_id"], ["worker_runs.id"]),
    )
    op.drop_constraint(
        "worker_run_event_status_ck", "worker_run_events", type_="check"
    )
    op.create_check_constraint(
        "worker_run_event_status_ck",
        "worker_run_events",
        "status IN ('queued','running','operation_running','operation_complete',"
        "'passed','failed','cancel_requested','cancelled','interrupted','task_finished',"
        "'model_tool_running','model_tool_failed','model_route_switched',"
        "'model_context_compacted','live_output_degraded','review_complete')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "worker_run_event_status_ck", "worker_run_events", type_="check"
    )
    op.create_check_constraint(
        "worker_run_event_status_ck",
        "worker_run_events",
        "status IN ('queued','running','operation_running','operation_complete',"
        "'passed','failed','cancel_requested','cancelled','task_finished',"
        "'model_tool_running','model_tool_failed','model_route_switched',"
        "'model_context_compacted','live_output_degraded','review_complete')",
    )
    op.drop_table("worker_run_heartbeats")
