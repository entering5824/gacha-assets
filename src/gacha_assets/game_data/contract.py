"""Stable JSON encoding and the Game Data Contract v1."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, TypedDict

_directory_lock = threading.Lock()


class Entity(TypedDict):
    schemaVersion: int
    game: str
    entityType: str
    sourceId: int | str
    canonicalId: str
    names: dict
    attributes: dict
    assets: list
    source: dict
    contentHash: str
    syncedAt: str


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf8")


def digest(value: Any) -> str:
    return hashlib.sha256(encode(value)).hexdigest()


def entity_hash(value: dict) -> str:
    # The observation time does not represent a content change.
    return digest({k: v for k, v in value.items() if k not in {"contentHash", "syncedAt"}})


def atomic_json(path: Path, value: Any) -> None:
    with _directory_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encode(value))
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path: Path, default=None):
    return json.loads(path.read_text("utf8")) if path.is_file() else default


@lru_cache(maxsize=1)
def _validator():
    from jsonschema import Draft202012Validator
    from importlib.resources import files
    schema = json.loads(files(__package__).joinpath("contract-v1.schema.json").read_text("utf8"))
    return Draft202012Validator(schema)


def validate_entity(entity: dict) -> None:
    _validator().validate(entity)
    if entity_hash(entity) != entity["contentHash"]:
        raise ValueError("entity content hash mismatch")


def diff_entities(before: list[dict], after: list[dict]) -> dict:
    old = {r["canonicalId"]: r for r in before}
    new = {r["canonicalId"]: r for r in after}
    return {
        "added": sorted(new.keys() - old.keys()),
        "changed": sorted(k for k in new.keys() & old.keys() if new[k]["contentHash"] != old[k]["contentHash"]),
        "unchanged": sorted(k for k in new.keys() & old.keys() if new[k]["contentHash"] == old[k]["contentHash"]),
        "missingCandidates": sorted(old.keys() - new.keys()),
    }
