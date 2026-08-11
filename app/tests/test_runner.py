import asyncio
from pathlib import Path
from types import SimpleNamespace

from chitti import runner


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

    def __iter__(self):
        return iter(self._rows or [])


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
