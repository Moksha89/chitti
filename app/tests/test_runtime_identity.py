import json
import os
import shutil
import sys
from pathlib import Path

from chitti.runtime_identity import (
    IDENTITY_MODULES,
    loaded_code_digest,
    write_loaded_code_identity,
)


def test_loaded_code_digest_is_stable_and_covers_worker_modules() -> None:
    first_digest = loaded_code_digest()
    assert set(IDENTITY_MODULES) <= set(sys.modules)
    assert first_digest == loaded_code_digest()


def test_loaded_code_digest_changes_when_worker_source_changes(tmp_path) -> None:
    import chitti.worker as worker

    source = Path(str(worker.__file__))
    copied_source = tmp_path / source.name
    shutil.copyfile(source, copied_source)
    original_digest = loaded_code_digest()
    try:
        copied_source.write_text(copied_source.read_text() + "\n")
        worker.__file__ = str(copied_source)
        changed_digest = loaded_code_digest()
    finally:
        worker.__file__ = str(source)
    assert changed_digest != original_digest


def test_loaded_code_identity_records_current_process(tmp_path, monkeypatch) -> None:
    path = tmp_path / "loaded-code.json"
    monkeypatch.setenv("CHITTI_CODE_IDENTITY_PATH", str(path))
    import chitti.runtime_identity as runtime_identity

    monkeypatch.setattr(runtime_identity, "IDENTITY_PATH", path)
    identity = write_loaded_code_identity()
    saved = json.loads(path.read_text())
    assert saved == identity
    assert saved["pid"] == os.getpid()
    assert saved["digest"] == loaded_code_digest()
