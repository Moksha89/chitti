import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from chitti.main import (
    _diff_body,
    _prepare_files_view,
    _prepare_workspace_run,
    _run_event_stream,
    _tree_nodes,
    humanize_belief_key,
    project_from_brief,
    render_markdown,
    templates,
    workspace_diff_file,
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
    assert humanize_belief_key("newExtractorKey-v2") == "New extractor key v2"


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
    assert unapproved.count('action="/plans/3/runs"') == 1

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
    assert rendered.count('action="/plans/3/runs"') == 1


@pytest.mark.parametrize(
    ("actor", "expected"),
    [("owner", "owner"), ("agent", "agent"), (None, "unknown")],
)
def test_plan_result_approval_displays_attribution_and_reason(actor, expected) -> None:
    run = _finished_run(
        {
            "manifest_id": 4,
            "approval_id": 5,
            "decision": "approved",
            "approved_by": actor,
            "reason": "Checked the result.",
        }
    )
    rendered = _render_plan_run(run)

    assert f"Result approved by {expected}" in rendered
    assert "Checked the result." in rendered


def test_workspace_result_approval_displays_unknown_historical_attribution() -> None:
    run = _finished_run(
        {
            "manifest_id": 4,
            "approval_id": 5,
            "decision": "approved",
            "approved_by": None,
            "reason": None,
        }
    )
    rendered = _workspace_context(run)

    assert "approved · unknown" in rendered


def test_approved_plan_keeps_model_run_action_available() -> None:
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
            "reason": None,
            "content_hash": "a" * 64,
        },
        task_statuses={},
        runs=[],
    )

    assert 'action="/plans/3/runs"' in rendered
    assert "Run model coding loop" in rendered


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
    assert "Live operation output" in rendered
    assert "No diff artifact was recorded for this run." in rendered
    assert "Live operation output" in rendered


def test_workspace_surfaces_latest_capture_per_path() -> None:
    run = _finished_run()
    run["artifacts"] = [
        {
            "id": 10,
            "kind": "screenshot",
            "path": "artifacts/desktop.png",
            "byte_size": 3,
        },
        {
            "id": 12,
            "kind": "screenshot",
            "path": "artifacts/desktop.png",
            "byte_size": 5,
        },
        {
            "id": 11,
            "kind": "screenshot",
            "path": "artifacts/phone.png",
            "byte_size": 4,
        },
        {
            "id": 13,
            "kind": "browser_evidence",
            "path": "artifacts/browser-errors.json",
            "byte_size": 2,
        },
    ]

    prepared = _prepare_workspace_run(run)

    assert [artifact["id"] for artifact in prepared["screenshots"]] == [12, 11]
    assert prepared["browser_errors"]["id"] == 13


def test_diff_tree_parses_authored_and_generated_files() -> None:
    payload = (
        b"diff --git a/app/page.js b/app/page.js\n"
        b"--- /dev/null\n+++ b/app/page.js\n@@ -0,0 +1 @@\n+page\n"
        b"diff --git a/out/index.html b/out/index.html\n"
        b"--- /dev/null\n+++ b/out/index.html\n@@ -0,0 +1 @@\n+html\n"
        b"diff --git a/package-lock.json b/package-lock.json\n"
        b"--- /dev/null\n+++ b/package-lock.json\n@@ -0,0 +1 @@\n+lock\n"
    )

    view = _prepare_files_view(payload, {"id": 41, "byte_size": len(payload)})

    assert view["state"] == "available"
    assert view["authored_count"] == 1
    assert view["generated_count"] == 2
    assert {entry["path"] for entry in view["entries"]} == {
        "app/page.js",
        "out/index.html",
        "package-lock.json",
    }


def test_workspace_files_tab_renders_bounded_loading_and_manifest_trees() -> None:
    run = _finished_run()
    payload = (
        b"diff --git a/app/page.js b/app/page.js\n"
        b"--- /dev/null\n+++ b/app/page.js\n@@ -0,0 +1 @@\n+page\n"
    )
    run["files_view"] = _prepare_files_view(
        payload, {"id": 41, "byte_size": len(payload)}
    )
    run["files_view"]["artifact_url"] = "/runs/7/artifacts/41"
    run["export_view"] = {
        "state": "available",
        "file_count": 1,
        "total_bytes": 4,
        "digest": "d" * 64,
        "tree": _tree_nodes(
            [
                {
                    "path": "index.html",
                    "size": 4,
                    "sha256": "e" * 64,
                    "kind": "manifest",
                    "role": "generated",
                    "summary": "4 bytes",
                    "index": 0,
                }
            ]
        ),
    }

    rendered = _workspace_context(run)

    assert "Source diff" in rendered
    assert "Authored source" in rendered
    assert "Select a file to load its bounded diff body." in rendered
    assert "Static export manifest" in rendered
    assert "Open full diff artifact" in rendered
    assert "eeeeeeeeeeeeeeee" in rendered


def test_diff_body_is_bounded_with_explicit_clip_marker() -> None:
    payload = (
        b"diff --git a/app/page.js b/app/page.js\n"
        b"--- /dev/null\n+++ b/app/page.js\n@@ -1 +1 @@\n+"
        + b"x" * 20_000
    )

    body = _diff_body(payload, 0)

    assert body is not None
    assert body["clipped"] is True
    assert len(str(body["body"]).encode()) <= 12_000


def test_expired_diff_payload_is_explicit() -> None:
    view = _prepare_files_view(None, {"id": 41, "byte_size": 100})

    assert view["state"] == "expired"
    rendered = _workspace_context(
        {
            **_finished_run(),
            "files_view": view,
            "export_view": {"state": "empty", "tree": [], "entries": []},
        }
    )
    assert "diff evidence has aged out of payload retention" in rendered


def test_workspace_diff_body_loads_on_demand() -> None:
    class Auth:
        def get_session(self, _token):
            return SimpleNamespace(username="akirah")

    class Result:
        def mappings(self):
            return self

        def one_or_none(self):
            return {
                "id": 41,
                "content": (
                    b"diff --git a/app/page.js b/app/page.js\n"
                    b"--- /dev/null\n+++ b/app/page.js\n@@ -0,0 +1 @@\n+page\n"
                ),
            }

    class SessionContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, *_args, **_kwargs):
            return Result()

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                auth=Auth(),
                database=SimpleNamespace(sessions=lambda: SessionContext()),
            )
        ),
        cookies={"chitti_session": "session"},
        headers={},
    )

    body = asyncio.run(workspace_diff_file(7, 0, request))

    assert body["path"] == "app/page.js"
    assert body["clipped"] is False
    assert "full_artifact_url" in body


def test_workspace_diff_body_requires_authentication() -> None:
    class Auth:
        def get_session(self, _token):
            return None

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(auth=Auth())),
        cookies={},
        headers={},
        url=SimpleNamespace(path="/workspace/runs/7/diff/0", query=""),
    )

    with pytest.raises(HTTPException, match="authentication required"):
        asyncio.run(workspace_diff_file(7, 0, request))


def test_run_event_stream_replays_after_last_event_id() -> None:
    class Request:
        async def is_disconnected(self):
            return False

    class Manager:
        async def latest_status(self, _run_id):
            return "running"

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


def test_run_event_stream_replays_output_after_separate_chunk_cursor() -> None:
    class Request:
        async def is_disconnected(self):
            return False

    class Manager:
        async def latest_status(self, _run_id):
            return "running"

        async def output_chunks_after(self, _run_id, chunk_id):
            assert chunk_id == 4
            return [
                {
                    "id": 5,
                    "operation_index": 2,
                    "stream": "stdout",
                    "sequence": 1,
                    "byte_offset": 10,
                    "content": "next\n",
                }
            ]

        async def events_after(self, _run_id, event_id):
            assert event_id == 3
            return []

    stream = _run_event_stream(Request(), Manager(), 7, 3, 4)
    output = asyncio.run(stream.__anext__())

    assert output.startswith("event: output\n")
    assert '"id":5' in output
    assert '"content":"next\\n"' in output
    asyncio.run(stream.aclose())


def test_run_event_stream_stops_cleanly_when_client_disconnects() -> None:
    class Request:
        async def is_disconnected(self):
            return True

    class Manager:
        async def latest_status(self, _run_id):
            raise AssertionError("disconnected clients must not query state")

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
        async def latest_status(self, _run_id):
            return "running"

        async def events_after(self, _run_id, _event_id):
            return [{"id": 8, "status": "failed", "detail": "finished"}]

    stream = _run_event_stream(Request(), Manager(), 7, 7)
    terminal = asyncio.run(stream.__anext__())

    assert '"terminal":true' in terminal
    with pytest.raises(StopAsyncIteration):
        asyncio.run(stream.__anext__())


def test_finished_run_stream_closes_before_polling() -> None:
    class Request:
        async def is_disconnected(self):
            return False

    class Manager:
        async def latest_status(self, _run_id):
            return "passed"

        async def events_after(self, _run_id, _event_id):
            raise AssertionError("finished runs must not poll events")

    stream = _run_event_stream(Request(), Manager(), 7, 999)

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
