"""Repository-wide pytest policy."""

import pytest


_ALLOWED_SKIP_REASON = "set RUN_DB_TESTS=1"
_unexpected_environment_skips: list[tuple[str, str]] = []


def pytest_configure(config: pytest.Config) -> None:
    _unexpected_environment_skips.clear()


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.outcome != "skipped":
        return
    reason = str(report.longrepr)
    if _ALLOWED_SKIP_REASON not in reason:
        _unexpected_environment_skips.append((report.nodeid, reason))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    unexpected = _unexpected_environment_skips
    if not unexpected:
        return
    terminal = session.config.pluginmanager.get_plugin("terminalreporter")
    if terminal is not None:
        terminal.write_sep("!", "unexpected environment-dependent test skips")
        for nodeid, reason in unexpected:
            terminal.write_line(f"{nodeid}: {reason}")
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
