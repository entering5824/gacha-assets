"""Bounded HTTP transport with conditional GET and validated raw JSON cache."""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .contract import atomic_json, digest, read_json


class FetchError(RuntimeError):
    def __init__(self, status: int = 0):
        self.status = status
        super().__init__(f"source HTTP {status}" if status else "source transport unavailable")


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict
    body: bytes


def transport(url: str, headers: dict, timeout: float) -> Response:
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as response:
            body = response.read(20_000_001)
            if len(body) > 20_000_000:
                raise ValueError("response exceeds size limit")
            return Response(response.status, {k.lower(): v for k, v in response.headers.items()}, body)
    except HTTPError as exc:
        return Response(exc.code, {k.lower(): v for k, v in exc.headers.items()}, b"")


def token(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", value) or ".." in value:
        raise ValueError("unsafe source identifier")
    return value


class JsonClient:
    def __init__(self, root: Path, base_url: str, *, timeout=30, attempts=4,
                 backoff=0.5, request=transport, sleep=time.sleep):
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("source base URL must use HTTPS without credentials/query")
        if not 0 < timeout <= 120 or not 1 <= attempts <= 8 or not 0 <= backoff <= 10:
            raise ValueError("invalid transport limits")
        self.root, self.base = Path(root), base_url.rstrip("/")
        self.timeout, self.attempts, self.backoff = timeout, attempts, backoff
        self.request, self.sleep = request, sleep

    def get(self, url: str, headers=None) -> Response:
        for attempt in range(self.attempts):
            try:
                response = self.request(url, headers or {}, self.timeout)
            except (URLError, TimeoutError, OSError):
                response = Response(0, {}, b"")
            if response.status and response.status != 429 and response.status < 500:
                return response
            if attempt + 1 < self.attempts:
                delay = self.backoff * 2 ** attempt
                retry_after = response.headers.get("retry-after", "")
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    try:
                        delay = max(delay, parsedate_to_datetime(retry_after).timestamp() - time.time())
                    except (ValueError, TypeError):
                        pass
                self.sleep(min(30, delay))
        raise FetchError(response.status)

    def fetch(self, game: str, version: str, locale: str, endpoint: str, *, fresh=False, validator=None) -> dict:
        path = "/".join((token(game), token(version), token(locale), token(endpoint)))
        # Include source identity to prevent cache poisoning when configuration changes.
        cache = self.root / hashlib.sha256(self.base.encode()).hexdigest()[:16] / f"{path}.json"
        try:
            previous = read_json(cache)
        except (ValueError, UnicodeError):
            previous = None
        if not isinstance(previous, dict) or "raw" not in previous or digest(previous["raw"]) != previous.get("hash"):
            previous = None
        if previous and validator:
            try:
                validator(previous["raw"])
            except (ValueError, TypeError, KeyError):
                previous = None
        if previous and not fresh:
            return previous["raw"]
        relative = "manifest.json" if endpoint == "manifest" else f"{game}/{version}/{endpoint}.json"
        if "/" in endpoint:
            raise ValueError("endpoint must be a cache token")
        if endpoint.startswith("detail-"):
            _, kind, source_id = endpoint.split("-", 2)
            relative = f"{game}/{version}/{locale}/{kind}/{source_id}.json"
        headers = {"Accept": "application/json", "User-Agent": "gacha-assets/0.1"}
        if previous:
            for field, header in (("etag", "If-None-Match"), ("last-modified", "If-Modified-Since")):
                if previous.get(field):
                    headers[header] = previous[field]
        response = self.get(f"{self.base}/{relative}", headers)
        if response.status == 304 and previous:
            return previous["raw"]
        if response.status != 200:
            raise FetchError(response.status)
        raw = json.loads(response.body)
        if not isinstance(raw, dict):
            raise ValueError("expected JSON object")
        if validator:
            validator(raw)
        atomic_json(cache, {"raw": raw, "hash": digest(raw), "etag": response.headers.get("etag"),
                            "last-modified": response.headers.get("last-modified")})
        return raw
