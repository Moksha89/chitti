import json
import os
import sys

from chitti.runtime_identity import IDENTITY_MODULES, loaded_code_digest, write_loaded_code_identity


def test_loaded_code_digest_is_stable_and_covers_worker_modules() -> None:
    assert set(IDENTITY_MODULES) <= set(sys.modules)
    assert loaded_code_digest() == loaded_code_digest()


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
