RUN_EVENT_STATUSES = frozenset(
    {
        "queued",
        "running",
        "operation_running",
        "operation_complete",
        "passed",
        "failed",
        "cancel_requested",
        "cancelled",
        "interrupted",
        "task_finished",
        "model_tool_running",
        "model_tool_failed",
        "model_route_switched",
        "model_context_compacted",
        "live_output_degraded",
        "review_complete",
        "visual_review_failed",
        "visual_review_passed",
        "visual_review_inconclusive",
        "preview_failed",
        "preview_blocked",
    }
)

TERMINAL_RUN_STATUSES = frozenset(
    {
        "passed",
        "failed",
        "cancelled",
        "interrupted",
        "preview_failed",
        "preview_blocked",
        "visual_review_inconclusive",
    }
)


def validate_run_event_status(status: str) -> str:
    if status not in RUN_EVENT_STATUSES:
        raise ValueError(f"unrecognized worker run event status: {status}")
    return status
