import asyncio
import hashlib
import importlib.util
import json
import subprocess
import urllib.request
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest

from chitti.brand_profiles import BrandProfile
from chitti.job_types import POSTER_POLICY
from chitti.plans import PlanDocument, PlanRevision
from chitti.provider import ModelCompletion
from chitti.worker import (
    DockerSandboxDispatcher,
    FixedOperation,
    VisualReviewInconclusive,
    WorkerLimits,
    _model_tool_progress_detail,
    _parse_visual_verdict,
    _task_done_checks,
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


def _visual_png() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
        b"\x1f\x15\xc4\x89"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _visual_verdict(digest: str, verdict: str = "pass") -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "observations": {
                "background": "A dark background.",
                "imagery": "A generated image is visible.",
                "imagery_edges": "No visible rectangular edges surround the generated image.",
                "text_blocks": "Fixture text is visible.",
                "colour_use": "Brand colours are visible.",
            },
            "image_sha256": digest,
            "criteria": {
                "fixture_text": "pass",
                "visual_hierarchy": "pass",
                "readability": verdict,
                "generated_imagery": "pass",
                "composite_integrity": "pass",
                "brand_constraints": "pass",
            },
            "findings": (
                []
                if verdict == "pass"
                else [
                    {
                        "criterion": "readability",
                        "issue": "Text is too small.",
                        "action": "Increase the fixture text size.",
                    }
                ]
            ),
            "summary": "Visual review completed.",
            "evidence_limitations": [],
        }
    )


def test_visual_verdict_is_digest_bound_and_strict() -> None:
    digest = hashlib.sha256(b"poster").hexdigest()
    parsed = _parse_visual_verdict(
        json.loads(_visual_verdict(digest)),
        digest,
    )
    assert parsed["image_sha256"] == digest
    assert parsed["observations"]["imagery_edges"].startswith("No visible")
    with pytest.raises(VisualReviewInconclusive, match="digest"):
        _parse_visual_verdict(json.loads(_visual_verdict(digest)), "different")
    malformed = json.loads(_visual_verdict(digest))
    del malformed["criteria"]
    with pytest.raises(VisualReviewInconclusive, match="incomplete"):
        _parse_visual_verdict(malformed, digest)


def test_visual_verdict_accepts_string_evidence_limitation() -> None:
    digest = hashlib.sha256(b"poster").hexdigest()
    value = json.loads(_visual_verdict(digest))
    value["evidence_limitations"] = "Screenshot-only review."
    parsed = _parse_visual_verdict(value, digest)
    assert parsed["evidence_limitations"] == ["Screenshot-only review."]


@pytest.mark.parametrize("field", ["observations", "criteria"])
def test_visual_verdict_rejects_malformed_visual_fields(field: str) -> None:
    digest = hashlib.sha256(b"poster").hexdigest()
    value = json.loads(_visual_verdict(digest))
    if field == "observations":
        value[field]["imagery_edges"] = 1
    else:
        del value[field]["composite_integrity"]
    with pytest.raises(VisualReviewInconclusive, match="incomplete"):
        _parse_visual_verdict(value, digest)


def test_visual_verdict_requires_fail_when_composite_integrity_fails() -> None:
    digest = hashlib.sha256(b"poster").hexdigest()
    value = json.loads(_visual_verdict(digest))
    value["criteria"]["composite_integrity"] = "fail"
    with pytest.raises(VisualReviewInconclusive, match="failed criteria"):
        _parse_visual_verdict(value, digest)


def test_visual_pass_cannot_rescue_deterministic_poster_gates() -> None:
    assert not _task_done_checks(
        {"visual-review"},
        POSTER_POLICY.required_gates,
    )


@pytest.mark.asyncio
async def test_visual_critique_persists_reference_without_base64_and_rebinds_digest(
    tmp_path: Path,
) -> None:
    image = tmp_path / "artifacts" / "poster.png"
    image.parent.mkdir()
    image.write_bytes(_visual_png())
    generated = tmp_path / "out" / "generated"
    generated.mkdir(parents=True)
    (generated / "stadium.png").write_bytes(_visual_png())
    prompts: list[str] = []

    class Provider:
        async def agent_completion(self, messages, role, tools=None, tool_choice=None):
            content = messages[1]["content"]
            text = content[0]["text"]
            digest = text.split("IMAGE SHA-256: ", 1)[1].splitlines()[0]
            return ModelCompletion(
                content=_visual_verdict(digest),
                model="fake:vision",
                prompt_tokens=10,
                completion_tokens=10,
                total_tokens=20,
                cost_usd=0.001,
            )

    dispatcher = object.__new__(DockerSandboxDispatcher)
    dispatcher.model_provider = Provider()
    dispatcher._event = lambda *args, **kwargs: asyncio.sleep(0)

    async def record(*args, **kwargs):
        prompts.append(kwargs["prompt"])

    dispatcher._record_model_call = record
    state = {
        "cycles": 0,
        "cost": 0.0,
        "tokens": 0,
        "accounted_cost": 0.0,
        "accounted_tokens": 0,
        "last_failed_digest": None,
    }
    profile = BrandProfile(
        namespace="shared",
        brand_colors=("#112233",),
        typography="Inter",
        poster_formats=("1080x1350",),
        audience="private audience",
        voice="private voice",
        do_not_use=("real player likenesses",),
        updated_by="owner",
        updated_at=datetime.now(UTC),
    )
    first = await dispatcher._visual_critique(
        1, "task", 1, tmp_path, WorkerLimits(), state, "fixture brief", profile
    )
    image.write_bytes(_visual_png() + b"changed")
    second = await dispatcher._visual_critique(
        1, "task", 2, tmp_path, WorkerLimits(), state, "fixture brief", profile
    )
    assert first[0].startswith("VISUAL_REVIEW_PASS")
    assert second[0].startswith("VISUAL_REVIEW_PASS")
    assert len(prompts) == 2
    assert all("base64" not in prompt for prompt in prompts)
    assert prompts[0] != prompts[1]
    assert "#112233" in prompts[0]
    assert "Inter" in prompts[0]
    assert "1080x1350" in prompts[0]
    assert "real player likenesses" in prompts[0]
    assert "private audience" not in prompts[0]
    assert "private voice" not in prompts[0]
    assert state["cycles"] == 2


@pytest.mark.asyncio
async def test_visual_critique_caps_two_failing_cycles(tmp_path: Path) -> None:
    image = tmp_path / "artifacts" / "poster.png"
    image.parent.mkdir()
    image.write_bytes(_visual_png())
    generated = tmp_path / "out" / "generated"
    generated.mkdir(parents=True)
    (generated / "stadium.png").write_bytes(_visual_png())

    class Provider:
        async def agent_completion(self, messages, role, tools=None, tool_choice=None):
            text = messages[1]["content"][0]["text"]
            digest = text.split("IMAGE SHA-256: ", 1)[1].splitlines()[0]
            return ModelCompletion(
                content=_visual_verdict(digest, "fail"),
                model="fake:vision",
                prompt_tokens=10,
                completion_tokens=10,
                total_tokens=20,
                cost_usd=0.001,
            )

    dispatcher = object.__new__(DockerSandboxDispatcher)
    dispatcher.model_provider = Provider()
    dispatcher._event = lambda *args, **kwargs: asyncio.sleep(0)
    dispatcher._record_model_call = lambda *args, **kwargs: asyncio.sleep(0)
    state = {
        "cycles": 0,
        "cost": 0.0,
        "tokens": 0,
        "accounted_cost": 0.0,
        "accounted_tokens": 0,
        "last_failed_digest": None,
    }
    await dispatcher._visual_critique(
        1, "task", 1, tmp_path, WorkerLimits(), state, "fixture brief", None
    )
    image.write_bytes(_visual_png() + b"changed")
    with pytest.raises(VisualReviewInconclusive, match="maximum repair cycles"):
        await dispatcher._visual_critique(
            1, "task", 2, tmp_path, WorkerLimits(), state, "fixture brief", None
        )
    assert state["cycles"] == 2


@pytest.mark.asyncio
async def test_visual_critique_reasks_once_for_noncompliant_fail(tmp_path: Path) -> None:
    image = tmp_path / "artifacts" / "poster.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(_visual_png())
    generated = tmp_path / "out" / "generated"
    generated.mkdir(parents=True)
    (generated / "stadium.png").write_bytes(_visual_png())
    calls = 0

    class Provider:
        async def agent_completion(self, messages, role, tools=None, tool_choice=None):
            nonlocal calls
            calls += 1
            text = messages[1]["content"][0]["text"]
            digest = text.split("IMAGE SHA-256: ", 1)[1].splitlines()[0]
            verdict = json.loads(_visual_verdict(digest, "fail"))
            if calls == 1:
                verdict["findings"] = []
            return ModelCompletion(
                content=json.dumps(verdict),
                model="fake:vision",
                prompt_tokens=10,
                completion_tokens=10,
                total_tokens=20,
                cost_usd=0.001,
            )

    dispatcher = object.__new__(DockerSandboxDispatcher)
    dispatcher.model_provider = Provider()
    dispatcher._event = lambda *args, **kwargs: asyncio.sleep(0)
    dispatcher._record_model_call = lambda *args, **kwargs: asyncio.sleep(0)
    state = {
        "cycles": 0,
        "cost": 0.0,
        "tokens": 0,
        "accounted_cost": 0.0,
        "accounted_tokens": 0,
        "last_failed_digest": None,
    }

    result = await dispatcher._visual_critique(
        1, "task", 1, tmp_path, WorkerLimits(), state, "fixture brief", None
    )

    assert result[0].startswith("VISUAL_REVIEW_FAIL")
    assert calls == 2
    assert state["cycles"] == 1


@pytest.mark.asyncio
async def test_visual_critique_output_limit_is_inconclusive_with_diagnostics(
    tmp_path: Path,
) -> None:
    image = tmp_path / "artifacts" / "poster.png"
    image.parent.mkdir()
    image.write_bytes(_visual_png())
    generated = tmp_path / "out" / "generated"
    generated.mkdir(parents=True)
    (generated / "stadium.png").write_bytes(_visual_png())
    recorded: list[dict[str, object]] = []

    class Provider:
        async def agent_completion(self, messages, role, tools=None, tool_choice=None):
            return ModelCompletion(
                content="",
                model="fake:vision",
                prompt_tokens=10,
                completion_tokens=4096,
                total_tokens=4106,
                cost_usd=0.001,
                finish_reason="length",
                message_fields=("response_failure_class=output limit",),
                response_diagnostics=(
                    "response_finish_reason=length",
                    "response_tail={}",
                ),
            )

    dispatcher = object.__new__(DockerSandboxDispatcher)
    dispatcher.model_provider = Provider()
    dispatcher._event = lambda *args, **kwargs: asyncio.sleep(0)

    async def record(*args, **kwargs):
        recorded.append(kwargs)

    dispatcher._record_model_call = record
    state = {
        "cycles": 0,
        "cost": 0.0,
        "tokens": 0,
        "accounted_cost": 0.0,
        "accounted_tokens": 0,
        "last_failed_digest": None,
    }

    with pytest.raises(
        VisualReviewInconclusive, match="exceeded the output limit"
    ) as raised:
        await dispatcher._visual_critique(
            1, "task", 1, tmp_path, WorkerLimits(), state, "fixture brief", None
        )
    assert "response_finish_reason=length" in str(raised.value)
    assert recorded


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
    poster_operations = fixed_operations(
        revision(),
        POSTER_POLICY,
        {"artifact": "poster.html", "width": 1080, "height": 1350, "scale": 1},
    )
    capture_command = poster_operations[3].command[-1]
    assert 'test -f "out/$CHITTI_POSTER_ARTIFACT"' in capture_command
    assert "grep -F -- \"$CHITTI_POSTER_ARTIFACT\" out/index.html" in capture_command
    assert "--artifact poster.html" in capture_command


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


def _poster_capture_factory(layout_errors, scripts=None):
    class Page:
        def on(self, *_args) -> None:
            pass

        def goto(self, _url, **_kwargs) -> None:
            pass

        def wait_for_timeout(self, _milliseconds) -> None:
            pass

        def locator(self, _selector):
            return self

        def inner_text(self) -> str:
            return "poster"

        def evaluate(self, script):
            if scripts is not None:
                scripts.append(script)
            return layout_errors

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

    return lambda: PlaywrightContext()


def test_poster_capture_rejects_measured_horizontal_overflow(tmp_path, capsys) -> None:
    module = _capture_module()
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "poster.html").write_text("<html><body>poster</body></html>")
    (tmp_path / "artifacts").mkdir()

    with pytest.raises(SystemExit):
        module.capture(
            tmp_path,
            playwright_factory=_poster_capture_factory(
                [
                    {
                        "kind": "poster-overflow",
                        "axis": "horizontal",
                        "overflow": 8,
                        "message": "poster overflow: horizontal by 8 CSS pixels",
                    }
                ]
            ),
            width=1080,
            height=1350,
            artifact="poster.html",
        )

    assert "horizontal by 8 CSS pixels" in capsys.readouterr().err


def test_poster_capture_accepts_fitting_content(tmp_path) -> None:
    module = _capture_module()
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "poster.html").write_text("<html><body>poster</body></html>")
    (tmp_path / "artifacts").mkdir()

    module.capture(
        tmp_path,
        playwright_factory=_poster_capture_factory([]),
        width=1080,
        height=1350,
        artifact="poster.html",
    )

    assert (tmp_path / "artifacts" / "poster.png").exists()


def test_poster_capture_uses_named_poster_layout_script(tmp_path) -> None:
    module = _capture_module()
    scripts = []
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "poster.html").write_text("<html><body>poster</body></html>")
    (tmp_path / "artifacts").mkdir()

    module.capture(
        tmp_path,
        playwright_factory=_poster_capture_factory([], scripts),
        width=1080,
        height=1350,
        artifact="poster.html",
    )

    assert scripts == [module.POSTER_LAYOUT_SCRIPT]


def test_poster_capture_ignores_decorative_wrapper_but_rejects_clipped_text(
    tmp_path, capsys
) -> None:
    module = _capture_module()
    (tmp_path / "out").mkdir()
    poster = tmp_path / "out" / "poster.html"
    (tmp_path / "artifacts").mkdir()
    poster.write_text(
        "<div id='poster' style='position:absolute;left:-40px;width:1160px'>"
        "<h1 style='position:absolute;left:500px'>Safe</h1></div>"
    )

    module.capture(
        tmp_path,
        playwright_factory=_poster_capture_factory([]),
        width=1080,
        height=1350,
        artifact="poster.html",
    )

    poster.write_text("<h1 style='position:absolute;left:-20px'>Clipped</h1>")
    with pytest.raises(SystemExit):
        module.capture(
            tmp_path,
            playwright_factory=_poster_capture_factory(
                [
                    {
                        "kind": "poster-overflow",
                        "axis": "horizontal",
                        "overflow": 20,
                        "message": "poster overflow: horizontal by 20 CSS pixels",
                    }
                ]
            ),
            width=1080,
            height=1350,
            artifact="poster.html",
        )

    assert "horizontal by 20 CSS pixels" in capsys.readouterr().err
    assert "childNodes" in module.POSTER_LAYOUT_SCRIPT
    assert "Node.TEXT_NODE" in module.POSTER_LAYOUT_SCRIPT


@pytest.mark.parametrize(
    ("overflow", "should_refuse"),
    [(0.99, False), (1.01, True)],
)
def test_poster_capture_tolerance_does_not_hide_real_overflow(
    tmp_path, overflow, should_refuse
) -> None:
    module = _capture_module()
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "poster.html").write_text("<html><body>poster</body></html>")
    (tmp_path / "artifacts").mkdir()

    errors = (
        [
            {
                "kind": "poster-overflow",
                "axis": "horizontal",
                "overflow": overflow,
                "message": f"poster overflow: horizontal by {overflow} CSS pixels",
            }
        ]
        if should_refuse
        else []
    )
    if should_refuse:
        with pytest.raises(SystemExit):
            module.capture(
                tmp_path,
                playwright_factory=_poster_capture_factory(errors),
                width=1080,
                height=1350,
                artifact="poster.html",
            )
    else:
        module.capture(
            tmp_path,
            playwright_factory=_poster_capture_factory(errors),
            width=1080,
            height=1350,
            artifact="poster.html",
        )


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


def test_model_tool_progress_detail_is_bounded_and_names_target() -> None:
    assert _model_tool_progress_detail("write_file", {"path": "app/page.js"}) == (
        "write_file: app/page.js"
    )
    assert len(
        _model_tool_progress_detail("run_command", {"name": "x" * 5000})
    ) <= 1000


def test_live_chunk_persistence_failure_degrades_without_failing_reader() -> None:
    dispatcher = DockerSandboxDispatcher(None)  # type: ignore[arg-type]
    degraded: list[int] = []
    persisted: list[bytes] = []

    async def fail_persist(*_args) -> None:
        raise RuntimeError("database unavailable")

    async def record_degraded(run_id, _status, _detail, **_kwargs) -> None:
        degraded.append(run_id)

    async def exercise() -> tuple[str, bool]:
        dispatcher._append_output_chunk = fail_persist  # type: ignore[method-assign]
        dispatcher._event = record_degraded  # type: ignore[method-assign]
        return await dispatcher._read_stream_live_async(
            7, 2, "stdout", BytesIO(b"operation output\n"), 1024
        )

    output, exceeded = asyncio.run(exercise())
    assert output == "operation output\n"
    assert not exceeded
    assert degraded == [7]
    assert persisted == []


def test_live_reader_keeps_multibyte_boundary_intact(monkeypatch) -> None:
    class OneByteStream:
        def __init__(self, content: bytes) -> None:
            self.content = content

        def read(self, _size: int) -> bytes:
            if not self.content:
                return b""
            chunk, self.content = self.content[:1], self.content[1:]
            return chunk

    dispatcher = DockerSandboxDispatcher(None)  # type: ignore[arg-type]
    chunks: list[bytes] = []

    async def persist(_run, _operation, _stream, _sequence, _offset, content) -> None:
        chunks.append(content)

    async def exercise() -> str:
        dispatcher._append_output_chunk = persist  # type: ignore[method-assign]
        return (
            await dispatcher._read_stream_live_async(
                7, 2, "stdout", OneByteStream("é".encode()), 1024
            )
        )[0]

    monkeypatch.setattr("chitti.worker.LIVE_OUTPUT_FLUSH_BYTES", 1)
    assert asyncio.run(exercise()) == "é"
    assert b"".join(chunks).decode("utf-8") == "é"


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


def test_model_critique_failure_records_terminal_run_event(monkeypatch, tmp_path) -> None:
    class Result:
        def mappings(self):
            return self

        def one(self):
            return {"job_type": "website", "job_config": {}}

    class Session:
        async def execute(self, _statement, _params):
            return Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Database:
        def sessions(self):
            return Session()

    dispatcher = DockerSandboxDispatcher(Database(), workspace_root=tmp_path)
    dispatcher.model_provider = object()  # type: ignore[assignment]
    events: list[tuple[str, str]] = []

    async def event(_run_id, status, detail, **_kwargs):
        events.append((status, detail))

    async def mount(_workspace, _limits):
        return None

    async def cleanup(_workspace):
        return None

    async def fail_dispatch(*_args):
        raise VisualReviewInconclusive("visual critique observations were incomplete")

    monkeypatch.setattr(dispatcher, "_event", event)
    monkeypatch.setattr(dispatcher, "_mount_workspace", mount)
    monkeypatch.setattr(dispatcher, "_cleanup_workspace", cleanup)
    monkeypatch.setattr(dispatcher, "_dispatch_model_one", fail_dispatch)

    with pytest.raises(VisualReviewInconclusive):
        asyncio.run(dispatcher._dispatch_one(revision(), 1, WorkerLimits()))

    assert events[-1] == (
        "failed",
        "visual critique observations were incomplete",
    )


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
