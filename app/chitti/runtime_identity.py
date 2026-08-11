from __future__ import annotations

import hashlib
import importlib
import json
import marshal
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from types import CodeType, FunctionType, ModuleType
from typing import Any

IDENTITY_MODULES = (
    "chitti.model_tools",
    "chitti.provider",
    "chitti.runner",
    "chitti.worker",
)
IDENTITY_PATH = Path(
    os.environ.get("CHITTI_CODE_IDENTITY_PATH", "/run/chitti-worker/loaded-code.json")
)


def _code_objects(value: Any, seen: set[int]) -> list[CodeType]:
    identifier = id(value)
    if identifier in seen:
        return []
    seen.add(identifier)
    if isinstance(value, CodeType):
        return [value]
    if isinstance(value, FunctionType):
        return [value.__code__]
    values: Iterable[Any]
    if isinstance(value, ModuleType):
        values = value.__dict__.values()
    elif isinstance(value, type):
        values = value.__dict__.values()
    else:
        return []
    objects: list[CodeType] = []
    for item in values:
        objects.extend(_code_objects(item, seen))
    return objects


def loaded_code_digest() -> str:
    digest = hashlib.sha256()
    seen: set[int] = set()
    modules = [
        (name, importlib.import_module(name))
        for name in IDENTITY_MODULES
    ]
    for name, module in modules:
        digest.update(name.encode())
        for code in _code_objects(module, seen):
            digest.update(marshal.dumps(code))
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
