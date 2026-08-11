"""record model prompt history compaction events"""

from alembic import op

revision = "0010_model_context_compaction"
down_revision = "0009_model_artifact_sizes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("worker_run_event_status_ck", "worker_run_events", type_="check")
    op.create_check_constraint(
        "worker_run_event_status_ck",
        "worker_run_events",
        "status IN ('queued','running','operation_running','passed','failed',"
        "'cancel_requested','cancelled','task_finished','model_tool_failed',"
        "'model_route_switched','model_context_compacted','review_complete')",
    )


def downgrade() -> None:
    op.drop_constraint("worker_run_event_status_ck", "worker_run_events", type_="check")
    op.create_check_constraint(
        "worker_run_event_status_ck",
        "worker_run_events",
        "status IN ('queued','running','operation_running','passed','failed',"
        "'cancel_requested','cancelled','task_finished','model_tool_failed',"
        "'model_route_switched','review_complete')",
    )
