"""Read local, ignored environment settings without overriding process values."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import MutableMapping


def load_local_env(path: Path | None = None, environ: MutableMapping[str, str] | None = None) -> MutableMapping[str, str]:
    target = path or Path(__file__).resolve().parents[2] / ".env"
    values = os.environ if environ is None else environ
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'"}:
            value = value[1:-1]
        else:
            value = re.sub(r"\s+#.*$", "", value).strip()
        values.setdefault(name, value)
    return values


def resolve_supabase_key(preferred: str = "SUPABASE_STORAGE_API_KEY", environ: MutableMapping[str, str] | None = None) -> str:
    """Resolve either supported write-authorized Supabase Storage key name."""
    values = os.environ if environ is None else environ
    for name in (preferred, "SUPABASE_STORAGE_API_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY"):
        value = values.get(name, "")
        if value:
            return value
    return ""
