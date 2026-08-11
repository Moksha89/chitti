from __future__ import annotations

import hashlib
import importlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

IDENTITY_MODULES = (
    "chitti.model_tools",
    "chitti.provider",
    "chitti.runner",
    "chitti.worker",
)
IDENTITY_PATH = Path(
    os.environ.get("CHITTI_CODE_IDENTITY_PATH", "/run/chitti-worker/loaded-code.json")
)


def loaded_code_digest() -> str:
    digest = hashlib.sha256()
    modules = [
        (name, importlib.import_module(name))
        for name in IDENTITY_MODULES
    ]
    for name, module in modules:
        digest.update(name.encode())
        source = Path(str(module.__file__)).read_bytes()
        digest.update(hashlib.sha256(source).digest())
    return digest.hexdigest()


def write_loaded_code_identity() -> dict[str, object]:
    identity = {
        "pid": os.getpid(),
        "started_at": datetime.now(UTC).isoformat(),
        "digest": loaded_code_digest(),
        "modules": list(IDENTITY_MODULES),
    }
    IDENTITY_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = IDENTITY_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(identity, sort_keys=True) + "\n")
    temporary.replace(IDENTITY_PATH)
    return identity
