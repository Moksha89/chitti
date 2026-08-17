"""derive the run-event database constraint from the application status set"""

from alembic import op

from chitti.run_status import RUN_EVENT_STATUSES

revision = "0032_run_event_status_contract"
down_revision = "0031_google_email_actions"
branch_labels = None
depends_on = None


def _status_constraint_sql() -> str:
    values = ", ".join(
        "'" + status.replace("'", "''") + "'"
        for status in sorted(RUN_EVENT_STATUSES)
    )
    return f"status IN ({values})"


def upgrade() -> None:
    op.drop_constraint("worker_run_event_status_ck", "worker_run_events", type_="check")
    op.create_check_constraint(
        "worker_run_event_status_ck",
        "worker_run_events",
        _status_constraint_sql(),
    )


def downgrade() -> None:
    op.drop_constraint("worker_run_event_status_ck", "worker_run_events", type_="check")
    op.create_check_constraint(
        "worker_run_event_status_ck",
        "worker_run_events",
        "status IN ('queued','running','operation_running','operation_complete',"
        "'passed','failed','cancel_requested','cancelled','interrupted','task_finished',"
        "'model_tool_running','model_tool_failed','model_route_switched',"
        "'model_context_compacted','live_output_degraded','review_complete',"
        "'visual_review_failed','visual_review_passed','visual_review_inconclusive',"
        "'preview_failed','preview_blocked')",
    )
