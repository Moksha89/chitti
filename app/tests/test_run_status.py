from chitti.run_status import (
    RUN_EVENT_STATUSES,
    TERMINAL_RUN_STATUSES,
    validate_run_event_status,
)


def test_visual_review_inconclusive_is_a_terminal_event_status() -> None:
    assert "visual_review_inconclusive" in RUN_EVENT_STATUSES
    assert "visual_review_inconclusive" in TERMINAL_RUN_STATUSES
    assert validate_run_event_status("visual_review_inconclusive") == (
        "visual_review_inconclusive"
    )


def test_unknown_run_event_status_fails_closed() -> None:
    try:
        validate_run_event_status("future_status")
    except ValueError as exc:
        assert str(exc) == "unrecognized worker run event status: future_status"
    else:
        raise AssertionError("unknown run event status was accepted")
