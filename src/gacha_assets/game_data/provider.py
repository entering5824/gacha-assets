"""Nanoka ZZZ API adapter, matching zzz-server-tool's published API rules."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .http import JsonClient, token
from .contract import read_json


@dataclass(frozen=True)
class SourceConfig:
    game: str
    base_url: str
    locale: str
    endpoints: tuple[str, ...]
    details: tuple[str, ...]
    concurrency: int = 4
    timeout: float = 30
    attempts: int = 4

    @classmethod
    def load(cls, path: Path):
        value = read_json(path)
        if value is None:
            raise FileNotFoundError(path)
        result = cls(**{**value, "endpoints": tuple(value["endpoints"]), "details": tuple(value["details"])})
        for item in (result.game, result.locale, *result.endpoints, *result.details):
            token(item)
        if not 1 <= result.concurrency <= 16 or not set(result.details) <= set(result.endpoints):
            raise ValueError("invalid source configuration")
        return result


class NanokaProvider:
    def __init__(self, config: SourceConfig, client: JsonClient):
        if config.game != "zzz":
            raise ValueError("Nanoka adapter currently supports ZZZ only")
        self.config, self.client = config, client

    def version(self) -> str:
        def validate(manifest):
            token(manifest["zzz"]["latest"])
        manifest = self.client.fetch("zzz", "discovery", "global", "manifest", fresh=True, validator=validate)
        return token(manifest["zzz"]["latest"])

    def discover(self, version: str, *, refresh=False) -> dict:
        def fetch(kind):
            def validate(index):
                if not index or any(not isinstance(row, dict) for row in index.values()):
                    raise ValueError(f"invalid or empty {kind} index")
                for key, row in index.items():
                    token(key)
                    if "icon" in row and not isinstance(row["icon"], str):
                        raise ValueError("invalid source icon")
                    if "en" in row and not isinstance(row["en"], (dict, str)):
                        raise ValueError("invalid source name")
            index = self.client.fetch("zzz", version, "global", kind, fresh=refresh, validator=validate)
            return kind, index
        with ThreadPoolExecutor(max_workers=self.config.concurrency) as pool:
            return dict(pool.map(fetch, self.config.endpoints))

    def records(self, version: str, indexes: dict, *, refresh=False) -> list[tuple]:
        def fetch(item):
            kind, key, index = item
            detail = {}
            if kind in self.config.details:
                def validate(detail):
                    if not detail:
                        raise ValueError("empty required detail")
                    if "id" in detail and str(detail["id"]) != key:
                        raise ValueError("detail identity mismatch")
                detail = self.client.fetch("zzz", version, self.config.locale, f"detail-{kind}-{key}", fresh=refresh, validator=validate)
            return kind, key, index, detail
        work = [(kind, key, row) for kind, index in indexes.items() for key, row in sorted(index.items())]
        with ThreadPoolExecutor(max_workers=self.config.concurrency) as pool:
            return list(pool.map(fetch, work))

    def image_url(self, icon: str) -> str:
        # zzz-api.ts:getAssetUrl; no filename/ID is invented.
        filename = icon.replace("\\", "/").split("/")[-1]
        filename = re.sub(r"\.[^.]+$", "", filename)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", filename):
            raise ValueError("unsafe icon path")
        return f"{self.client.base}/assets/zzz/{quote(filename)}.webp"


def image_fields(value, prefix=""):
    """Only source-declared image fields, including endgame room/monster icons."""
    if isinstance(value, dict):
        for key, item in sorted(value.items()):
            path = f"{prefix}.{key}".strip(".")
            if key in {"icon", "image", "monster_icon"} and isinstance(item, str) and item:
                yield path, item
            elif isinstance(item, (dict, list)):
                yield from image_fields(item, path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from image_fields(item, f"{prefix}.{index}")
