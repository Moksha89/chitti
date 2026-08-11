import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from chitti.main import (
    _prepare_workspace_run,
    _run_event_stream,
    humanize_belief_key,
    project_from_brief,
    render_markdown,
    templates,
    workspace_index,
    workspace_run_events,
)


def _finished_run(promotion=None):
    return {
        "run": {"id": 7},
        "events": [{"status": "passed", "detail": "complete"}],
        "token_totals": 0,
        "reasoning_token_totals": 0,
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


def test_finished_run_displays_reasoning_token_usage() -> None:
    run = _finished_run()
    run["token_totals"] = 1234
    run["reasoning_token_totals"] = 456
    rendered = _render_plan_run(run)

    assert "Model calls: 1234 tokens (456 reasoning)" in rendered


def test_plan_approval_reason_form_round_trips_to_plan_display() -> None:
    unapproved = _render_plan_run(_finished_run())
    assert 'name="reason"' in unapproved
    assert 'action="/plans/3/approve"' in unapproved

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
    rendered = templates.get_template("plan.html").render(
        csrf_token="csrf",
        revision=revision,
        approval={
            "decision": "approved",
            "reason": "Approved on the owner's behalf.",
            "content_hash": "a" * 64,
        },
        task_statuses={},
        runs=[],
    )

    assert "Approved on the owner&#39;s behalf." in rendered


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


def _workspace_context(run=None):
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
            tasks=[
                SimpleNamespace(
                    id="T1",
                    title="Build",
                    description="Build it.",
                    dependencies=[],
                    done_condition="build passes",
                )
            ],
        ),
    )
    run = run or {
        "run": {"id": 7, "revision_id": 3},
        "events": [
            {"id": 10, "status": "running", "detail": "started", "task_id": "T1"}
        ],
        "operations": [],
        "artifacts": [],
        "model_calls": [],
        "token_totals": 0,
        "reasoning_token_totals": 0,
        "cost_total_usd": 0.0,
        "promotion": None,
        "reviewer_verdict": None,
    }
    return templates.get_template("workspace.html").render(
        csrf_token="csrf",
        revision=revision,
        run=_prepare_workspace_run(run),
        run_links=[{"id": 7, "project": "demo", "revision": 1, "status": "running", "is_open": True}],
        task_statuses={"T1": "running"},
        current_task="T1",
    )


def test_workspace_cold_load_contains_durable_state_and_safe_empty_panels() -> None:
    rendered = _workspace_context()

    assert "Run 7" in rendered
    assert "Build" in rendered
    assert "started" in rendered
    assert "No screenshots have been captured for this run yet." in rendered
    assert "No reviewer verdict has been recorded yet." in rendered
    assert "No completed operations yet." in rendered


def test_run_event_stream_replays_after_last_event_id() -> None:
    class Request:
        async def is_disconnected(self):
            return False

    class Manager:
        async def events_after(self, _run_id, event_id):
            return [
                {"id": 1, "status": "queued", "detail": "queued"},
                {"id": 2, "status": "running", "detail": "running"},
                {"id": 3, "status": "operation_running", "detail": "working"},
            ][event_id:]

    stream = _run_event_stream(Request(), Manager(), 7, 2)
    first = asyncio.run(stream.__anext__())

    assert first.startswith("id: 3\n")
    assert '"status":"operation_running"' in first
    asyncio.run(stream.aclose())


def test_run_event_stream_stops_cleanly_when_client_disconnects() -> None:
    class Request:
        async def is_disconnected(self):
            return True

    class Manager:
        async def events_after(self, _run_id, _event_id):
            raise AssertionError("disconnected clients must not query state")

    stream = _run_event_stream(Request(), Manager(), 7, 0)

    with pytest.raises(StopAsyncIteration):
        asyncio.run(stream.__anext__())


def test_run_event_stream_closes_after_terminal_event() -> None:
    class Request:
        async def is_disconnected(self):
            return False

    class Manager:
        async def events_after(self, _run_id, _event_id):
            return [{"id": 8, "status": "failed", "detail": "finished"}]

    stream = _run_event_stream(Request(), Manager(), 7, 7)
    terminal = asyncio.run(stream.__anext__())

    assert '"terminal":true' in terminal
    with pytest.raises(StopAsyncIteration):
        asyncio.run(stream.__anext__())


def test_run_event_stream_requires_authentication() -> None:
    class Auth:
        def get_session(self, _token):
            return None

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(auth=Auth())),
        cookies={},
        headers={},
    )

    with pytest.raises(HTTPException, match="authentication required"):
        asyncio.run(workspace_run_events(7, request))


def test_workspace_index_honors_forced_password_change() -> None:
    class Auth:
        must_change_password = True

        def get_session(self, _token):
            return SimpleNamespace(username="akirah")

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(auth=Auth())),
        cookies={"chitti_session": "session"},
        headers={},
    )

    response = asyncio.run(workspace_index(request))

    assert response.status_code == 303
    assert response.headers["location"] == "/change-password"
