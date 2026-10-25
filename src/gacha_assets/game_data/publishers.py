"""Immutable revision publishers. Publication never changes an active pointer."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .. import publish as image_publish
from ..validate import decode_and_normalize
from .contract import encode, digest, entity_hash, validate_entity


class Publisher(Protocol):
    def publish(self, release: dict, directory: Path, *, dry_run: bool, verify_only: bool) -> dict: ...


def descriptor(release: dict) -> dict:
    return {"schemaVersion": 1, "syncId": release["syncId"], "game": release["game"],
            "locale": release["locale"], "sourceVersion": release["sourceVersion"],
            "entities": [{"canonicalId": e["canonicalId"], "contentHash": e["contentHash"]} for e in release["entities"]],
            "assets": [{**asset, "storage": "supabase"} for asset in release["assets"]]}


def revision_key(entity: dict) -> str:
    return digest([entity["canonicalId"], entity["contentHash"]])


def entity_batches(entities):
    batch, size = [], 0
    for entity in entities:
        length = len(encode(entity))
        if batch and (len(batch) >= 100 or size + length > 4_000_000):
            yield batch
            batch, size = [], 0
        batch.append(entity)
        size += length
    if batch:
        yield batch


def retry(operation, *, attempts=4, sleep=time.sleep):
    for attempt in range(attempts):
        try:
            return operation()
        except Exception:
            if attempt + 1 == attempts:
                raise
            sleep(min(4, 0.5 * 2 ** attempt))


class MongoPublisher:
    def __init__(self, collection, releases, *, operation_factory=None, sleep=time.sleep, batch_size=100):
        if not 1 <= batch_size <= 500:
            raise ValueError("invalid Mongo batch size")
        self.collection, self.releases = collection, releases
        self.operation_factory, self.sleep, self.batch_size = operation_factory, sleep, batch_size

    def publish(self, release, directory, *, dry_run=False, verify_only=False):
        if dry_run:
            return {"status": "dry-run", "entities": len(release["entities"])}
        # _id is the server-enforced unique index on (canonical identity, content hash).
        # No index mutation is necessary in verify-only mode.
        operation_factory = self.operation_factory
        if not verify_only:
            retry(lambda: self.collection.create_index([("game", 1), ("locale", 1), ("canonicalId", 1), ("contentHash", 1)],
                  unique=True, name="game_data_revision_identity"), sleep=self.sleep)
        if operation_factory is None and not verify_only:
            from pymongo import UpdateOne
            operation_factory = UpdateOne
        written = 0
        for start in range(0, len(release["entities"]), self.batch_size):
            batch = release["entities"][start:start + self.batch_size]
            keys = [revision_key(e) for e in batch]
            existing = {row["_id"]: row for row in self.collection.find({"_id": {"$in": keys}})}
            operations = []
            for entity, key in zip(batch, keys):
                current = existing.get(key)
                if current and (current.get("entity", {}).get("contentHash") != entity["contentHash"] or entity_hash(current["entity"]) != entity["contentHash"]):
                    raise ValueError("Mongo immutable revision conflict")
                if current is None and not verify_only:
                    operations.append(operation_factory({"_id": key}, {"$setOnInsert": {"entity": entity,
                        "game": release["game"], "locale": release["locale"], "canonicalId": entity["canonicalId"],
                        "contentHash": entity["contentHash"]}}, upsert=True))
            if operations:
                retry(lambda: self.collection.bulk_write(operations, ordered=False), sleep=self.sleep)
                written += len(operations)
            verified = {row["_id"]: row for row in self.collection.find({"_id": {"$in": keys}})}
            if any(k not in verified or entity_hash(verified[k]["entity"]) != e["contentHash"] or verified[k]["entity"].get("contentHash") != e["contentHash"] for k, e in zip(keys, batch)):
                raise ValueError("Mongo readback verification failed")
        release_descriptor = descriptor(release)
        if not verify_only:
            retry(lambda: self.releases.update_one({"_id": release["syncId"]},
                {"$setOnInsert": {"descriptor": release_descriptor}}, upsert=True), sleep=self.sleep)
        stored = self.releases.find_one({"_id": release["syncId"]})
        if not stored or stored.get("descriptor") != release_descriptor:
            raise ValueError("Mongo release verification failed")
        return {"status": "verified", "entities": len(release["entities"]), "written": written}


class SupabasePublisher:
    def __init__(self, url: str, bucket: str, key: str, *, request=None, rest=None, sleep=time.sleep):
        if not url.startswith("https://") or not bucket:
            raise ValueError("Supabase HTTPS URL and bucket required")
        self.url, self.bucket, self.key = url.rstrip("/"), bucket, key
        self.request = request or image_publish._request
        self.rest = rest or self._rest
        self.sleep = sleep

    def _rest(self, path, method="GET", payload=None):
        headers = {"apikey": self.key, "Content-Type": "application/json",
                   "Prefer": "resolution=ignore-duplicates,return=minimal"}
        if self.key and not self.key.startswith("sb_"):
            headers["Authorization"] = f"Bearer {self.key}"
        request = Request(f"{self.url}/rest/v1/{path}", method=method, headers=headers,
                          data=encode(payload) if payload is not None else None)
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read(20_000_001)
                if len(body) > 20_000_000:
                    raise ValueError("database response too large")
                return json.loads(body) if body else None
        except HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                raise RuntimeError(f"Supabase transient HTTP {exc.code}") from None
            raise ValueError(f"Supabase HTTP {exc.code}") from None
        except URLError:
            raise RuntimeError("Supabase transport unavailable") from None

    def api(self, path, method="GET", payload=None):
        return retry(lambda: self.rest(path, method, payload), sleep=self.sleep)

    def storage_request(self, *args):
        def perform():
            status, mime, body = self.request(*args)
            if status == 0 or status == 429 or status >= 500:
                raise RuntimeError(f"storage transient HTTP {status}")
            return status, mime, body
        return retry(perform, sleep=self.sleep)

    def object(self, path, data, *, verify_only=False, image=False, entity=False):
        url = image_publish._url(self.url, self.bucket, path)
        public = url.replace("/object/", "/object/public/", 1)
        status, _, remote = self.storage_request(public, "GET", self.key)
        expected = hashlib.sha256(data).hexdigest()
        def matches(content):
            if not entity:
                return hashlib.sha256(content).hexdigest() == expected
            parsed = json.loads(content)
            validate_entity(parsed)
            return parsed["contentHash"] == json.loads(data)["contentHash"]
        if status == 200:
            if not matches(remote):
                raise ValueError("immutable storage object conflict")
        elif not verify_only and image_publish._is_missing_object(status, remote):
            status, _, _ = self.storage_request(url, "POST", self.key, data, "image/webp" if image else "application/json")
            if not 200 <= status < 300:
                raise ValueError("storage upload failed")
            status, _, remote = self.storage_request(public, "GET", self.key)
        if status != 200 or not matches(remote):
            raise ValueError("storage readback verification failed")
        if image:
            decode_and_normalize(remote)

    def publish(self, release, directory, *, dry_run=False, verify_only=False):
        if dry_run:
            return {"status": "dry-run", "entities": len(release["entities"])}
        for asset in release["assets"]:
            local = (directory / "files" / asset["path"]).resolve()
            if not local.is_relative_to((directory / "files").resolve()):
                raise ValueError("unsafe release asset path")
            if local.is_file():
                data = local.read_bytes()
                if hashlib.sha256(data).hexdigest() != asset["sha256"]:
                    raise ValueError("local image checksum mismatch")
                decode_and_normalize(data)
                self.object(asset["path"], data, verify_only=verify_only, image=True)
            else:
                # Retained legacy variants already live in Storage: validate actual bytes.
                url = image_publish._url(self.url, self.bucket, asset["path"]).replace("/object/", "/object/public/", 1)
                status, _, data = self.storage_request(url, "GET", self.key)
                if status != 200 or hashlib.sha256(data).hexdigest() != asset["sha256"]:
                    raise ValueError("retained asset unavailable or checksum mismatch")
                decode_and_normalize(data)
        for batch in entity_batches(release["entities"]):
            rows = []
            for entity in batch:
                self.object(f"game-data/{release['game']}/entities/{entity['contentHash']}.json", encode(entity), verify_only=verify_only, entity=True)
                rows.append({"game": release["game"], "locale": release["locale"], "canonical_id": entity["canonicalId"],
                             "content_hash": entity["contentHash"], "entity": entity})
            query = urlencode({"game": f"eq.{release['game']}", "locale": f"eq.{release['locale']}",
                               "content_hash": "in.(" + ",".join(row["content_hash"] for row in rows) + ")"})
            remote = self.api(f"game_data_entities?{query}")
            existing = {row["content_hash"]: row for row in remote}
            missing = [row for row in rows if row["content_hash"] not in existing]
            if missing and not verify_only:
                self.api("game_data_entities?on_conflict=game,locale,canonical_id,content_hash", "POST", missing)
                remote = self.api(f"game_data_entities?{query}")
                existing = {row["content_hash"]: row for row in remote}
            for row in rows:
                current = existing.get(row["content_hash"], {})
                if current.get("canonical_id") != row["canonical_id"] or current.get("entity", {}).get("contentHash") != row["content_hash"] or entity_hash(current["entity"]) != row["content_hash"]:
                    raise ValueError("Supabase metadata readback failed")
        desc = descriptor(release)
        # Manifest last, after every required asset/revision has been verified.
        self.object(f"game-data/{release['game']}/releases/{release['syncId']}.json", encode(desc), verify_only=verify_only)
        row = {"sync_id": release["syncId"], "game": release["game"], "locale": release["locale"], "descriptor": desc}
        if not verify_only:
            self.api("game_data_releases?on_conflict=sync_id", "POST", [row])
        remote = self.api("game_data_releases?" + urlencode({"sync_id": f"eq.{release['syncId']}"}))
        if len(remote) != 1 or remote[0].get("descriptor") != desc:
            raise ValueError("Supabase release readback failed")
        return {"status": "verified", "entities": len(release["entities"])}

    def active(self, game, locale):
        rows = self.api("game_data_active?" + urlencode({"game": f"eq.{game}", "locale": f"eq.{locale}"}))
        return rows[0]["sync_id"] if rows else None

    def activate(self, release, expected, targets):
        # A single transaction in PostgreSQL is the authoritative release commit.
        self.api("rpc/activate_game_data_release", "POST", {"p_game": release["game"], "p_locale": release["locale"],
                 "p_sync_id": release["syncId"], "p_expected": expected, "p_targets": targets})
        if self.active(release["game"], release["locale"]) != release["syncId"]:
            raise ValueError("active release readback failed")
