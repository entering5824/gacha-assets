"""Build complete immutable local releases without altering legacy manifests."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from ..models import Asset
from ..pipeline import run as run_assets
from ..validate import decode_and_normalize
from .contract import Entity, atomic_json, diff_entities, digest, entity_hash, now, read_json, validate_entity
from .http import FetchError
from .errors import safe_error
from .provider import NanokaProvider, image_fields


def load_mapping(path: Path, assets: list[dict]) -> dict:
    mapping = read_json(path)
    if not isinstance(mapping, dict) or mapping.get("schemaVersion") != 1:
        raise ValueError("mapping must use schemaVersion 1")
    if not isinstance(mapping.get("entities"), dict) or not isinstance(mapping.get("images"), dict):
        raise ValueError("invalid mapping sections")
    seen = set()
    for key, value in mapping["entities"].items():
        if not re.fullmatch(r"[a-z]+:[A-Za-z0-9_-]+", key) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", value):
            raise ValueError("invalid explicit entity mapping")
        identity = (key.split(":")[0], value)
        if identity in seen:
            raise ValueError("ambiguous entity mapping")
        seen.add(identity)
    known = {a["assetId"] for a in assets}
    for value in mapping["images"].values():
        if value not in known:
            raise ValueError("explicit image mapping must reference an existing asset ID")
    return mapping


def _image(provider, icon: str, cache: Path, *, refresh: bool):
    url = provider.image_url(icon)
    path = cache / "images" / digest(url)
    metadata = read_json(path.with_suffix(".json"))
    if not refresh and metadata and path.is_file():
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() == metadata.get("sha256"):
            decode_and_normalize(data)
            return url, path, metadata["sha256"]
    headers = {"Accept": "image/*"}
    if metadata and metadata.get("etag") and path.is_file():
        headers["If-None-Match"] = metadata["etag"]
    response = provider.client.get(url, headers)
    if response.status == 304 and path.is_file():
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != metadata["sha256"]:
            raise ValueError("cached image checksum mismatch")
        decode_and_normalize(data)
        return url, path, metadata["sha256"]
    if response.status != 200:
        raise FetchError(response.status)
    data = decode_and_normalize(response.body)
    checksum = hashlib.sha256(data).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_bytes(data)
    temp.replace(path)
    atomic_json(path.with_suffix(".json"), {"sha256": checksum, "etag": response.headers.get("etag"), "url": url})
    return url, path, checksum


def ingest(provider: NanokaProvider, root: Path, manifest: Path, mapping_path: Path,
           *, refresh=False, dry_run=False) -> dict:
    root = Path(root)
    game = provider.config.game
    root.mkdir(parents=True, exist_ok=True)
    lock = root / f"{game}.ingest-lock"
    try:
        lock.mkdir()
    except FileExistsError:
        raise RuntimeError("another ingest is running; inspect lock before manual recovery") from None
    try:
        attempt = {"syncId": uuid.uuid4().hex, "game": game, "locale": provider.config.locale,
                   "sourceVersion": None, "startedAt": now(), "validated": 0,
                   "new": 0, "changed": 0, "unchanged": 0, "failed": 1, "status": "failed"}
        try:
            return _ingest(provider, root, manifest, mapping_path, refresh=refresh, dry_run=dry_run, attempt=attempt)
        except Exception as exc:
            report = {**attempt, **safe_error(exc), "finishedAt": now()}
            atomic_json(root / game / provider.config.locale / "last-attempt.json", report)
            return report
    finally:
        lock.rmdir()


def _ingest(provider, root, manifest, mapping_path, *, refresh, dry_run, attempt):
    started = now()
    game, locale = provider.config.game, provider.config.locale
    legacy = read_json(manifest)
    if legacy.get("schemaVersion") != 1:
        raise ValueError("legacy manifest must use Asset Contract v1")
    for asset in legacy["assets"]:
        Asset(**asset).validate()
    mapping = load_mapping(mapping_path, legacy["assets"])
    assets_by_id = {a["assetId"]: a for a in legacy["assets"] if a["game"] == game}
    previous_pointer = read_json(root / game / locale / "staged.json", {})
    previous = read_json(root / game / locale / "releases" / previous_pointer.get("syncId", "none") / "release.json", {})
    entities, unresolved, errors, candidates, reused = [], [], [], [], {}
    version = provider.version()
    attempt["sourceVersion"] = version
    indexes = provider.discover(version, refresh=refresh)
    records = provider.records(version, indexes, refresh=refresh)
    seen_images = {}
    for kind, key, index, detail in records:
        source_id = detail.get("id", index.get("id", key))
        if isinstance(source_id, bool) or not isinstance(source_id, (str, int)) or str(source_id) != key:
            raise ValueError("source ID does not agree with index key")
        # JSON object keys remain strings unless the upstream supplies a typed id.
        slug = mapping["entities"].get(f"{kind}:{key}")
        identity = f"{game}.{kind}.source-{key}"
        if not slug and kind in {"character", "weapon", "equipment"}:
            unresolved.append({"entity": identity, "reason": "canonical_mapping_required", "sourceId": source_id})
        references = []
        image_rows = dict(image_fields({"index": index, "detail": detail}))
        for field, icon in image_rows.items():
            target = mapping["images"].get(f"{kind}:{key}:{field}")
            asset_kind = "artifact" if kind == "equipment" else kind
            if not target and slug and field == "index.icon" and asset_kind in {"character", "weapon", "artifact"}:
                target = f"{game}.{asset_kind}.{slug}.icon"
            ref = {"field": field, "status": "unresolved", "sourceIcon": icon}
            try:
                image_url = provider.image_url(icon)
                if image_url not in seen_images:
                    seen_images[image_url] = _image(provider, icon, root / game / version / locale, refresh=refresh)
                url, image_path, checksum = seen_images[image_url]
                ref.update({"url": url, "sha256": checksum})
                if target:
                    old = assets_by_id.get(target)
                    parts = target.split(".")
                    if len(parts) != 4 or parts[0] != game:
                        raise ValueError("invalid mapped asset identity")
                    if old and old["sha256"] == checksum and old["status"] == "ready":
                        reused[target] = old
                        relative = old["path"]
                    else:
                        relative = f"game-data/images/{game}/{checksum}.webp"
                    candidates.append({"game": game, "entityType": parts[1], "entityId": parts[2],
                                       "variant": parts[3], "storage": old["storage"] if old else "supabase",
                                       "path": relative, "provider": old["source"]["provider"] if target in reused else "nanoka",
                                       "sourceId": old["source"]["sourceId"] if target in reused else key,
                                       "sourceFile": str(image_path)})
                    ref.update({"assetId": target, "status": "ready"})
                else:
                    unresolved.append({"entity": identity, "field": field, "reason": "asset_mapping_required", "url": url})
            except Exception as exc:
                ref["status"] = "missing" if isinstance(exc, FetchError) and exc.status == 404 else "failed"
                issue = {"entity": identity, "field": field, "reason": ref["status"], "errorType": type(exc).__name__}
                unresolved.append(issue)
                if target:
                    errors.append(issue)
            references.append(ref)
        # Preserve all known variants for an explicitly mapped entity.
        if slug:
            for asset in assets_by_id.values():
                if asset["entityType"] == ("artifact" if kind == "equipment" else kind) and asset["entityId"] == slug:
                    if asset["status"] == "ready" and not any(r.get("assetId") == asset["assetId"] for r in references):
                        reused[asset["assetId"]] = asset
                        references.append({"field": f"existing.{asset['variant']}", "status": "ready", "assetId": asset["assetId"]})
        language_keys = {"en", "ja", "jp", "ko", "kr", "zh", "chs", "cht", "ru", "fr", "de", "es", "pt", "vi", "th", "id"}
        names = {k: v for k, v in index.items() if k in language_keys and isinstance(v, (str, dict))}
        if isinstance(detail.get("name"), str):
            names[locale] = detail["name"]
        entity: Entity = {"schemaVersion": 1, "game": game, "entityType": kind, "sourceId": source_id,
                  "canonicalId": identity, "names": names, "attributes": {"index": index, "detail": detail, "consumerId": slug},
                  "assets": references, "source": {"provider": "nanoka", "version": version, "locale": locale,
                    "endpoint": f"{kind}/{key}"}, "syncedAt": started}
        entity["contentHash"] = entity_hash(entity)
        validate_entity(entity)
        entities.append(entity)
    if len({r["canonicalId"] for r in entities}) != len(entities):
        raise ValueError("duplicate canonical identity")
    changes = diff_entities(previous.get("entities", []), entities)
    # Missing discovery is a review candidate, never automatic deletion.
    absent = set(changes["missingCandidates"])
    entities.extend(r for r in previous.get("entities", []) if r["canonicalId"] in absent)
    entities.sort(key=lambda row: row["canonicalId"])
    # Retain prior asset dependencies for carried entities.
    for asset in previous.get("assets", []):
        if any(r.get("assetId") == asset["assetId"] for e in entities if e["canonicalId"] in absent for r in e["assets"]):
            reused.setdefault(asset["assetId"], asset)
    report = {"game": game, "locale": locale, "sourceVersion": version, "startedAt": started,
              "syncId": attempt["syncId"],
              "validated": len(records), "new": len(changes["added"]), "changed": len(changes["changed"]),
              "unchanged": len(changes["unchanged"]), "failed": len(errors), "diff": changes,
              "unresolved": unresolved, "errors": errors, "status": "failed" if errors else "staged"}
    if errors:
        report["finishedAt"] = now()
        atomic_json(root / game / locale / "last-attempt.json", report)
        return report
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        stage = Path(temporary)
        # Deduplicate repeated icon fields and detect ambiguous explicit mappings.
        by_id = {}
        for candidate in candidates:
            key = f"{candidate['game']}.{candidate['entityType']}.{candidate['entityId']}.{candidate['variant']}"
            old = by_id.setdefault(key, candidate)
            if old["sourceFile"] != candidate["sourceFile"]:
                raise ValueError("multiple source images mapped to one asset")
        asset_result = run_assets(list(by_id.values()), stage / "assets.json", stage / "files")
        if asset_result["rejected"]:
            raise ValueError("image pipeline rejected staged assets")
        all_assets = {**reused, **{a["assetId"]: a for a in asset_result["assets"]}}
        asset_rows = sorted(all_assets.values(), key=lambda row: row["assetId"])
        stable = {"game": game, "locale": locale, "sourceVersion": version,
                  "entities": [{k: v for k, v in r.items() if k != "syncedAt"} for r in entities], "assets": asset_rows}
        sync_id = digest(stable)
        report["syncId"] = sync_id
        release = {**report, "schemaVersion": 1, "entities": entities, "assets": asset_rows,
                   "mappingHash": digest(mapping), "sourceBase": provider.client.base}
        atomic_json(stage / "release.json", release)
        atomic_json(stage / "assets.json", {"schemaVersion": 1, "assets": asset_rows})
        if not dry_run:
            destination = root / game / locale / "releases" / sync_id
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                # Same filesystem rename: a crashed copy can never look like a completed release.
                ready = root / f".stage-{sync_id}"
                if ready.exists():
                    raise RuntimeError("orphaned release stage requires inspection")
                shutil.copytree(stage, ready)
                os.replace(ready, destination)
                # The pointer is updated only after the complete snapshot exists.
            atomic_json(root / game / locale / "staged.json", {"syncId": sync_id})
            report["release"] = str(destination / "release.json")
    report["status"] = "dry-run" if dry_run else "staged"
    report["finishedAt"] = now()
    atomic_json(root / game / locale / "last-attempt.json", report)
    return report


def load_release(path: Path) -> dict:
    release = read_json(path)
    if not isinstance(release, dict) or release.get("schemaVersion") != 1 or release.get("status") != "staged" or release.get("failed"):
        raise ValueError("only complete staged releases can be published")
    if not release.get("entities"):
        raise ValueError("empty releases are not publishable")
    for entity in release["entities"]:
        validate_entity(entity)
        if entity["game"] != release["game"] or entity["source"]["locale"] != release["locale"]:
            raise ValueError("release entity scope mismatch")
    assets = {}
    paths = {}
    for asset in release["assets"]:
        Asset(**asset).validate()
        if asset["game"] != release["game"] or asset["status"] != "ready" or asset["assetId"] in assets:
            raise ValueError("invalid release asset identity/status")
        assets[asset["assetId"]] = asset
        if paths.setdefault(asset["path"], asset["sha256"]) != asset["sha256"]:
            raise ValueError("release storage path conflict")
        local = (path.parent / "files" / asset["path"]).resolve()
        if not local.is_relative_to((path.parent / "files").resolve()):
            raise ValueError("unsafe release asset")
        if local.is_file():
            data = local.read_bytes()
            if hashlib.sha256(data).hexdigest() != asset["sha256"]:
                raise ValueError("local image checksum mismatch")
            decode_and_normalize(data)
    for entity in release["entities"]:
        for ref in entity["assets"]:
            if ref["status"] == "ready" and ref.get("assetId") not in assets:
                raise ValueError("dangling ready asset reference")
    if len({r["canonicalId"] for r in release["entities"]}) != len(release["entities"]):
        raise ValueError("duplicate release entity")
    stable = {"game": release["game"], "locale": release["locale"], "sourceVersion": release["sourceVersion"],
              "entities": [{k: v for k, v in r.items() if k != "syncedAt"} for r in release["entities"]], "assets": release["assets"]}
    if digest(stable) != release["syncId"]:
        raise ValueError("release checksum mismatch")
    return release
