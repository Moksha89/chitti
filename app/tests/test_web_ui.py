from types import SimpleNamespace

from chitti.main import (
    humanize_belief_key,
    project_from_brief,
    render_markdown,
    templates,
)


def _finished_run(promotion=None):
    return {
        "run": {"id": 7},
        "events": [{"status": "passed", "detail": "complete"}],
        "token_totals": 0,
        "cost_total_usd": 0.0,
        "promotion": promotion,
        "promotion_event": None,
        "reviewer_verdict": None,
        "operations": [],
        "artifacts": [],
    }


def _render_plan_run(run):
    revision = SimpleNamespace(
        id=3,
        project="demo",
        revision=1,
        brief="Build a demo.",
        content_hash="a" * 64,
        document=SimpleNamespace(
            title="Demo",
            summary="A demo.",
            memory_decisions=[],
            tasks=[],
        ),
    )
    return templates.get_template("plan.html").render(
        csrf_token="csrf",
        revision=revision,
        approval=None,
        task_statuses={},
        runs=[run],
    )


def test_markdown_output_formats_code_and_escapes_html() -> None:
    rendered = render_markdown(
        "**bold**\n\n`inline()`\n\n```python\nprint('<unsafe>')\n```\n\n<script>alert('x')</script>"
    )

    assert "<strong>bold</strong>" in rendered
    assert "<code>inline()</code>" in rendered
    assert "<pre><code class=\"language-python\">" in rendered
    assert "&lt;unsafe&gt;" in rendered
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_belief_presentation_keeps_keys_consistent_and_values_readable() -> None:
    assert humanize_belief_key("hard_rules_meeting_start_time") == "Hard rules meeting start time"
    assert humanize_belief_key("hard_rules.meeting_start_time") == "Hard rules meeting start time"


def test_project_brief_is_detected_without_starting_execution() -> None:
    assert project_from_brief("VSports", True) == "vsports"
    assert project_from_brief("VSports", False) is None
    assert project_from_brief(None, True) is None
    assert project_from_brief("!!!", True) is None


def test_finished_run_without_export_manifest_is_not_promotable() -> None:
    rendered = _render_plan_run(_finished_run())

    assert "This run is not promotable: static export evidence was not produced." in rendered
    assert "Approve result" not in rendered


def test_finished_run_with_manifest_without_approval_can_be_approved() -> None:
    rendered = _render_plan_run(_finished_run({"manifest_id": 4, "approval_id": None}))

    assert "Approve result" in rendered
    assert "Preview is queued for the host runner." not in rendered


def test_finished_run_with_approval_is_pending_or_published() -> None:
    pending = _render_plan_run(
        _finished_run({"manifest_id": 4, "approval_id": 5, "decision": "approved"})
    )
    published = _render_plan_run(
        _finished_run(
            {
                "manifest_id": 4,
                "approval_id": 5,
                "decision": "approved",
                "preview_id": "preview",
                "expires_at": "tomorrow",
            }
        )
    )

    assert "Preview is queued for the host runner." in pending
    assert "Approve result" not in pending
    assert "Open preview" in published
    assert "Approve result" not in published
