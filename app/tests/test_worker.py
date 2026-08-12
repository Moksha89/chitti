import asyncio
import importlib.util
import subprocess
import urllib.request
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest

from chitti.plans import PlanDocument, PlanRevision
from chitti.worker import (
    DockerSandboxDispatcher,
    FixedOperation,
    WorkerLimits,
    fixed_operations,
)


def revision() -> PlanRevision:
    document = PlanDocument(
        title="Sandbox fixture",
        summary="Deterministic fixture",
        tasks=[
            {
                "id": "one",
                "title": "Write fixture",
                "description": "Write a fixture.",
                "done_condition": "Fixture exists.",
            }
        ],
    )
    return PlanRevision(
        id=1,
        project="sandbox",
        brief="Build fixture.",
        revision=1,
        document=document,
        content_hash="a" * 64,
        created_at=datetime.now(UTC),
        parent_revision_id=None,
    )


def test_worker_limits_are_recorded_as_explicit_sandbox_contract() -> None:
    limits = WorkerLimits()
    values = limits.as_json()
    assert values["cpus"] == 1.0
    assert values["memory"] == "2g"
    assert values["pids"] == 512
    assert values["non_root_uid"] == 65532
    assert values["workspace_bytes"] == 4 * 1024 * 1024 * 1024
    assert values["output_bytes"] > 0
    assert WorkerLimits.from_json(values) == limits


def test_fixed_operations_are_deterministic_and_include_preview() -> None:
    operations = fixed_operations(revision())
    assert all(isinstance(operation, FixedOperation) for operation in operations)
    assert [operation.name for operation in operations] == [
        "git-init",
        "write-fixture",
        "install-node-dependencies",
        "next-build",
        "static-export",
        "browser-preview",
        "run-tests",
        "git-diff",
    ]
    assert operations[2].network == "bridge"
    assert all(operation.network == "none" for operation in operations[:2])
    assert all(operation.network == "none" for operation in operations[3:])
    assert "/workspace/artifacts" in operations[1].command[-1]
    assert "node_modules" in operations[-1].command[-1]
    assert ".next" in operations[-1].command[-1]
    assert ".npm-cache" in operations[-1].command[-1]


def _capture_module():
    script = Path(__file__).parents[2] / "sandbox" / "next_screenshot.py"
    spec = importlib.util.spec_from_file_location("next_screenshot", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_static_capture_serves_produced_export_without_model_server(tmp_path) -> None:
    module = _capture_module()
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "index.html").write_text("<html><body>export</body></html>")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    served = {}
    original_serve = module._serve_export

    def serve(workspace):
        process = original_serve(workspace)
        served["process"] = process
        return process

    module._serve_export = serve

    class Page:
        def on(self, *_args) -> None:
            pass

        def goto(self, url, **_kwargs) -> None:
            with urllib.request.urlopen(url, timeout=5) as response:
                assert response.status == 200

        def wait_for_timeout(self, _milliseconds) -> None:
            pass

        def locator(self, _selector):
            return self

        def inner_text(self) -> str:
            return "export"

        def evaluate(self, _script):
            return []

        def screenshot(self, path, **_kwargs) -> None:
            Path(path).write_bytes(b"png")

        def close(self) -> None:
            pass

    class Browser:
        def new_page(self, **_kwargs):
            return Page()

        def close(self) -> None:
            pass

    class Playwright:
        class Chromium:
            def launch(self):
                return Browser()

        chromium = Chromium()

    class PlaywrightContext:
        def __enter__(self):
            return Playwright()

        def __exit__(self, *_args) -> None:
            pass

    module.capture(tmp_path, playwright_factory=lambda: PlaywrightContext())
    assert (artifacts / "phone.png").exists()
    assert (artifacts / "desktop.png").exists()
    assert (artifacts / "browser-errors.json").read_text() == "[]"
    assert served["process"].poll() is not None


def test_static_capture_module_imports_without_playwright() -> None:
    _capture_module()


def test_workspace_mount_verifies_artifact_write_access(monkeypatch, tmp_path) -> None:
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 0, "/dev/loop0 ext4 rw,nosuid,nodev\n", ""
        )

    monkeypatch.setattr("chitti.worker.subprocess.run", run)
    DockerSandboxDispatcher._verify_worker_mount(tmp_path)
    assert "write_probe.write_bytes" in commands[0][-1]
    assert "worker artifacts directory is missing" in commands[0][-1]


def test_static_capture_rejects_missing_export_informatively(tmp_path) -> None:
    module = _capture_module()
    with pytest.raises(RuntimeError, match="static export directory is missing"):
        module._serve_export(tmp_path)
    (tmp_path / "out").mkdir()
    with pytest.raises(RuntimeError, match="static export directory is empty"):
        module._serve_export(tmp_path)


@pytest.mark.parametrize("network", ["chitti_net", "host"])
def test_fixed_operations_never_join_application_network(network: str) -> None:
    assert network not in {operation.network for operation in fixed_operations(revision())}


def test_workspace_mount_refuses_underlying_filesystem() -> None:
    with pytest.raises(RuntimeError, match="workspace quota mount verification failed"):
        DockerSandboxDispatcher._assert_quota_mount(
            "/dev/vda3", "ext4", "rw,nodev,nosuid"
        )


def test_mount_operations_target_host_mount_namespace() -> None:
    command = DockerSandboxDispatcher._host_command(["umount", "/workspace"])
    assert command[:3] == [
        "nsenter",
        "--mount=/proc/1/ns/mnt",
        "--",
    ]
    assert command[3:] == ["umount", "/workspace"]


def test_mount_inspection_uses_host_mount_namespace(monkeypatch, tmp_path) -> None:
    command = []

    def run(actual, **_kwargs):
        command.append(actual)
        return subprocess.CompletedProcess(
            actual, 0, "/dev/loop0 ext4 rw,nosuid,nodev\n", ""
        )

    monkeypatch.setattr("chitti.worker.subprocess.run", run)
    details = DockerSandboxDispatcher._mounted_details(tmp_path)

    assert details == ("/dev/loop0", "ext4", "rw,nosuid,nodev")
    assert command[0][:3] == ["nsenter", "--mount=/proc/1/ns/mnt", "--"]
    assert command[0][3] == "findmnt"


def test_worker_mount_verification_refuses_unverified_bind(monkeypatch, tmp_path) -> None:
    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1, "", "worker mount was not visible")

    monkeypatch.setattr("chitti.worker.subprocess.run", run)
    with pytest.raises(RuntimeError, match="worker quota mount verification failed"):
        DockerSandboxDispatcher._verify_worker_mount(tmp_path)


def test_unmount_surfaces_status_and_stderr(monkeypatch, tmp_path) -> None:
    dispatcher = DockerSandboxDispatcher(None)  # type: ignore[arg-type]
    workspace = tmp_path / "chitti-run-1"
    commands = []
    monkeypatch.setattr(dispatcher, "_mounted_source", lambda _workspace: "/dev/loop0")

    def run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 32, "", "target is busy")

    monkeypatch.setattr("chitti.worker.subprocess.run", run)
    with pytest.raises(RuntimeError, match="target is busy"):
        asyncio.run(dispatcher._unmount_workspace(workspace))
    assert commands[0][:3] == ["nsenter", "--mount=/proc/1/ns/mnt", "--"]
    assert commands[0][3:5] == ["umount", str(workspace)]


def test_already_detached_loop_is_cleanup_success(monkeypatch, tmp_path) -> None:
    dispatcher = DockerSandboxDispatcher(None)  # type: ignore[arg-type]
    workspace = tmp_path / "chitti-run-1"
    mounted = iter(["/dev/loop0", None])
    loops = iter([("/dev/loop0",), ()])
    monkeypatch.setattr(dispatcher, "_mounted_source", lambda _workspace: next(mounted))
    monkeypatch.setattr(dispatcher, "_workspace_loops", lambda _image: next(loops))

    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1, "", "No such device or address")

    monkeypatch.setattr("chitti.worker.subprocess.run", run)
    asyncio.run(dispatcher._unmount_workspace(workspace))


def test_surviving_loop_keeps_cleanup_failed_with_diagnostics(
    monkeypatch, tmp_path
) -> None:
    dispatcher = DockerSandboxDispatcher(None)  # type: ignore[arg-type]
    workspace = tmp_path / "chitti-run-1"
    mounted = iter(["/dev/loop0", None])
    monkeypatch.setattr(dispatcher, "_mounted_source", lambda _workspace: next(mounted))
    monkeypatch.setattr(
        dispatcher, "_workspace_loops", lambda _image: ("/dev/loop0",)
    )

    async def no_sleep(_seconds) -> None:
        return None

    monkeypatch.setattr("chitti.worker.asyncio.sleep", no_sleep)

    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1, "", "device is busy")

    monkeypatch.setattr("chitti.worker.subprocess.run", run)
    with pytest.raises(RuntimeError, match="device is busy"):
        asyncio.run(dispatcher._unmount_workspace(workspace))


def test_output_reader_stops_at_budget_without_buffering_unbounded_output() -> None:
    async def exercise() -> tuple[str, bool]:
        dispatcher = DockerSandboxDispatcher(None)  # type: ignore[arg-type]
        return dispatcher._read_limited(BytesIO(b"x" * 4096), 1024)

    output, exceeded = asyncio.run(exercise())
    assert len(output) == 1024
    assert exceeded


def test_failed_unmount_keeps_workspace_image(monkeypatch, tmp_path) -> None:
    dispatcher = DockerSandboxDispatcher(None)  # type: ignore[arg-type]
    workspace = tmp_path / "chitti-run-1"
    image = dispatcher._workspace_image(workspace)
    image.write_bytes(b"backing image")

    async def fail_unmount(_workspace):
        raise RuntimeError("workspace mount remains active")

    monkeypatch.setattr(dispatcher, "_unmount_workspace", fail_unmount)

    with pytest.raises(RuntimeError, match="workspace mount remains active"):
        asyncio.run(dispatcher._cleanup_workspace(workspace))

    assert image.exists()


def test_cancelled_run_still_cleans_up_workspace(monkeypatch, tmp_path) -> None:
    dispatcher = DockerSandboxDispatcher(None, workspace_root=tmp_path)  # type: ignore[arg-type]
    dispatcher._cancelled.add(1)
    events: list[tuple[str, str]] = []
    cleaned: list[Path] = []

    async def mount(_workspace, _limits) -> None:
        return None

    async def event(_run_id, status, detail, **_kwargs) -> None:
        events.append((status, detail))

    async def cleanup(workspace) -> None:
        cleaned.append(workspace)

    monkeypatch.setattr(dispatcher, "_mount_workspace", mount)
    monkeypatch.setattr(dispatcher, "_event", event)
    monkeypatch.setattr(dispatcher, "_cleanup_workspace", cleanup)

    asyncio.run(dispatcher._dispatch_one(object(), 1, WorkerLimits()))  # type: ignore[arg-type]

    assert events == [("running", "run started"), ("cancelled", "cancelled before operation")]
    assert cleaned == [tmp_path / "chitti-run-1"]


def test_stale_cleanup_attempts_every_workspace_before_failing(monkeypatch, tmp_path) -> None:
    dispatcher = DockerSandboxDispatcher(None)  # type: ignore[arg-type]
    dispatcher.workspace_root = tmp_path
    workspaces = {tmp_path / "chitti-run-proof-a", tmp_path / "chitti-run-proof-b"}
    attempted: list[str] = []
    recorded: list[tuple[str, str]] = []

    monkeypatch.setattr(dispatcher, "_backing_loops", lambda _root: {})
    monkeypatch.setattr(dispatcher, "_mounted_workspaces", lambda _root: workspaces)

    async def remove_container(_container):
        return None

    async def fail_cleanup(workspace):
        attempted.append(workspace.name)
        raise RuntimeError("mount remains")

    async def record_failure(run_id, detail):
        recorded.append((run_id, detail))

    monkeypatch.setattr(dispatcher, "_remove_container", remove_container)
    monkeypatch.setattr(dispatcher, "_cleanup_workspace", fail_cleanup)
    monkeypatch.setattr(dispatcher, "_record_cleanup_failure", record_failure)

    with pytest.raises(RuntimeError, match="proof-a|proof-b"):
        asyncio.run(dispatcher.cleanup_stale_workspaces())

    assert set(attempted) == {workspace.name for workspace in workspaces}
    assert {run_id for run_id, _detail in recorded} == {"proof-a", "proof-b"}


def test_cleanup_failure_recorder_logs_non_run_id_without_raising(caplog) -> None:
    dispatcher = DockerSandboxDispatcher(None)  # type: ignore[arg-type]

    with caplog.at_level("ERROR"):
        asyncio.run(
            dispatcher._record_cleanup_failure(
                "proof-quota", "workspace mount remains active"
            )
        )

    assert "proof-quota" in caplog.text
    assert "workspace mount remains active" in caplog.text
