import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from chitti import runner
from chitti.previews import build_manifest, copy_export
from chitti.provider import GatewayMisconfigurationError, GatewayTransientError
from chitti.worker import RunBudgetExceeded


class _Result:
    def __init__(self, rows=None, one=None):
        self._rows = rows
        self._one = one

    def mappings(self):
        return self

    def one(self):
        return self._one

    def one_or_none(self):
        return self._one

    def scalar_one(self):
        return self._one if isinstance(self._one, bool) else False

    def __iter__(self):
        return iter(self._rows or [])


def test_run_refuses_gateway_misconfiguration_before_workspace(monkeypatch) -> None:
    dispatched = False
    events: list[str] = []

    class Provider:
        async def validate_gateway(self) -> None:
            raise GatewayMisconfigurationError("gateway routes unavailable: reviewer")

    class Dispatcher:
        async def dispatch(self, *_args) -> None:
            nonlocal dispatched
            dispatched = True

    async def record_event(_database, _run_id, status, detail) -> None:
        events.append(f"{status}: {detail}")

    async def trim_payloads(_database) -> None:
        return

    monkeypatch.setattr("chitti.runner.record_event", record_event)
    monkeypatch.setattr("chitti.runner.trim_payloads", trim_payloads)
    asyncio.run(
        runner.execute_run(
            None,  # type: ignore[arg-type]
            Dispatcher(),  # type: ignore[arg-type]
            {"id": 1, "revision_id": 1, "limits": runner.WorkerLimits().as_json()},
            Provider(),  # type: ignore[arg-type]
        )
    )

    assert dispatched is False
    assert events == ["failed: gateway misconfiguration: gateway routes unavailable: reviewer"]


def test_run_distinguishes_transient_gateway_failure_before_workspace(monkeypatch) -> None:
    dispatched = False
    events: list[str] = []

    class Provider:
        async def validate_gateway(self) -> None:
            raise GatewayTransientError("gateway did not respond during preflight")

    class Dispatcher:
        async def dispatch(self, *_args) -> None:
            nonlocal dispatched
            dispatched = True

    async def record_event(_database, _run_id, status, detail) -> None:
        events.append(f"{status}: {detail}")

    async def trim_payloads(_database) -> None:
        return

    monkeypatch.setattr("chitti.runner.record_event", record_event)
    monkeypatch.setattr("chitti.runner.trim_payloads", trim_payloads)
    asyncio.run(
        runner.execute_run(
            None,  # type: ignore[arg-type]
            Dispatcher(),  # type: ignore[arg-type]
            {"id": 1, "revision_id": 1, "limits": runner.WorkerLimits().as_json()},
            Provider(),  # type: ignore[arg-type]
        )
    )

    assert dispatched is False
    assert events == [
        "failed: gateway temporarily unavailable: gateway did not respond during preflight"
    ]


def test_run_budget_exhaustion_is_terminal_without_retryable_tool_failures(monkeypatch) -> None:
    events: list[tuple[str, str]] = []
    dispatched = False

    class Provider:
        async def validate_gateway(self) -> None:
            return

    class Dispatcher:
        async def dispatch(self, *_args) -> None:
            nonlocal dispatched
            dispatched = True
            raise RunBudgetExceeded("model tool-call")

        async def cancel(self, *_args) -> None:
            raise AssertionError("terminal budget failure should not be retried or cancelled")

    async def approved(_session, _revision_id):
        return object()

    async def record_event(_database, _run_id, status, detail, **_kwargs) -> None:
        events.append((status, detail))

    async def trim_payloads(_database) -> None:
        return

    monkeypatch.setattr("chitti.runner.approved_revision", approved)
    monkeypatch.setattr("chitti.runner.record_event", record_event)
    monkeypatch.setattr("chitti.runner.trim_payloads", trim_payloads)
    asyncio.run(
        runner.execute_run(
            _Database(),  # type: ignore[arg-type]
            Dispatcher(),  # type: ignore[arg-type]
            {"id": 1, "revision_id": 1, "limits": runner.WorkerLimits().as_json()},
            Provider(),  # type: ignore[arg-type]
        )
    )

    assert dispatched
    assert events == [("failed", "model tool-call budget exceeded")]
    assert all(status != "model_tool_failed" for status, _detail in events)


def test_failure_after_owner_cancellation_is_recorded_as_cancelled(monkeypatch) -> None:
    events: list[tuple[str, str]] = []

    class Provider:
        async def validate_gateway(self) -> None:
            return

    class Dispatcher:
        async def dispatch(self, *_args) -> None:
            raise RuntimeError("late model failure")

        async def cancel(self, *_args) -> None:
            return

    async def approved(_session, _revision_id):
        return object()

    checks = 0

    async def cancelled(_database, _run_id) -> bool:
        nonlocal checks
        checks += 1
        if checks == 1:
            return False
        events.append(("cancelled", "cancelled by owner"))
        return True

    async def record_event(_database, _run_id, status, detail, **_kwargs) -> None:
        events.append((status, detail))

    async def trim_payloads(_database) -> None:
        return

    monkeypatch.setattr("chitti.runner.approved_revision", approved)
    monkeypatch.setattr("chitti.runner.record_cancelled_if_requested", cancelled)
    monkeypatch.setattr("chitti.runner.record_event", record_event)
    monkeypatch.setattr("chitti.runner.trim_payloads", trim_payloads)
    asyncio.run(
        runner.execute_run(
            _Database(),  # type: ignore[arg-type]
            Dispatcher(),  # type: ignore[arg-type]
            {"id": 1, "revision_id": 1, "limits": runner.WorkerLimits().as_json()},
            Provider(),  # type: ignore[arg-type]
        )
    )

    assert events == [("cancelled", "cancelled by owner")]


class _Session:
    def __init__(self):
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, statement, *_args, **_kwargs):
        query = str(statement)
        self.calls += 1
        if "promotion_approvals" in query:
            return _Result(rows=[{"approval_id": 1, "run_id": 11}, {"approval_id": 2, "run_id": 22}])
        return _Result(one={"total": 0, "count": 0})


class _Database:
    def sessions(self):
        return _Session()


class _EventSession:
    def __init__(self, database):
        self.database = database

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, statement, parameters=None, **_kwargs):
        query = str(statement)
        if query.startswith("SELECT status, detail"):
            latest = self.database.events[-1] if self.database.events else None
            return _Result(one=latest)
        self.database.events.append(
            {
                "status": parameters["status"],
                "detail": parameters["detail"],
            }
        )
        return _Result()

    async def commit(self):
        return None


class _EventDatabase:
    def __init__(self):
        self.events = []

    def sessions(self):
        return _EventSession(self)


def test_publish_failures_are_recorded_and_do_not_stop_later_approvals(
    monkeypatch, tmp_path: Path
) -> None:
    attempts = []
    events = []

    async def publish_one(_database, _settings, _root, approval):
        attempts.append(approval["approval_id"])
        if approval["approval_id"] == 1:
            raise RuntimeError("staging output is missing")
        return 3

    async def record_event(_database, run_id, status, detail):
        events.append((run_id, status, detail))

    monkeypatch.setattr(runner, "_evict_expired_preview_directories", _noop)
    monkeypatch.setattr(runner, "_publish_one_preview", publish_one)
    monkeypatch.setattr(runner, "_record_preview_event", record_event)
    settings = SimpleNamespace(
        preview_root=str(tmp_path),
        preview_max_count=4,
        preview_max_bytes=200,
    )

    asyncio.run(runner.publish_approved_previews(_Database(), settings))

    assert attempts == [1, 2]
    assert events == [(11, "preview_failed", "staging output is missing")]


def test_preview_quota_block_is_durable_and_does_not_stop_later_approvals(
    monkeypatch, tmp_path: Path
) -> None:
    attempts = []
    events = []

    async def publish_one(_database, _settings, _root, approval):
        attempts.append(approval["approval_id"])
        if approval["approval_id"] == 1:
            raise runner.PreviewBlockedError("preview count quota exhausted")
        return 3

    async def record_event(_database, run_id, status, detail):
        events.append((run_id, status, detail))

    monkeypatch.setattr(runner, "_evict_expired_preview_directories", _noop)
    monkeypatch.setattr(runner, "_publish_one_preview", publish_one)
    monkeypatch.setattr(runner, "_record_preview_event", record_event)
    settings = SimpleNamespace(
        preview_root=str(tmp_path),
        preview_max_count=4,
        preview_max_bytes=200,
    )

    asyncio.run(runner.publish_approved_previews(_Database(), settings))

    assert attempts == [1, 2]
    assert events == [(11, "preview_blocked", "preview count quota exhausted")]


async def _noop(*_args, **_kwargs):
    return None


def test_preview_failure_event_is_not_duplicated() -> None:
    database = _EventDatabase()

    asyncio.run(
        runner._record_preview_event(
            database, 11, "preview_failed", "approved preview staging output is missing"
        )
    )
    asyncio.run(
        runner._record_preview_event(
            database, 11, "preview_failed", "approved preview staging output is missing"
        )
    )

    assert database.events == [
        {
            "status": "preview_failed",
            "detail": "approved preview staging output is missing",
        }
    ]


def test_publish_verifies_manifest_from_destination_after_copy(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.html").write_text("approved")
    destination = tmp_path / "destination"
    approved = build_manifest(source)
    real_copy_export = copy_export

    def copy_then_alter(source_path, destination_path):
        result = real_copy_export(source_path, destination_path)
        (destination_path / "index.html").write_text("altered")
        return result

    monkeypatch.setattr(runner, "copy_export", copy_then_alter)

    with pytest.raises(RuntimeError, match="changed while publishing"):
        runner._copy_and_verify_export(source, destination, approved)
