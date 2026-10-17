"""Publish validated canonical assets to public Supabase Storage."""
from __future__ import annotations

import hashlib
import json
import mimetypes
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .models import Asset
from .resolve import canonical_id
from .validate import decode_and_normalize


def merge_contracts(input_paths: list[Path], output_path: Path, game: str) -> dict:
    """Merge product manifests into one per-game canonical view without path/hash clobbering."""
    if not input_paths:
        raise ValueError("At least one input contract is required")
    by_id: dict[str, dict] = {}
    for input_path in input_paths:
        document = json.loads(input_path.read_text(encoding="utf-8"))
        if document.get("schemaVersion") != 1 or not isinstance(document.get("assets"), list):
            raise ValueError(f"Input must use Asset Contract v1: {input_path}")
        for raw in document["assets"]:
            record = dict(raw)
            Asset(**record).validate()
            if record["game"] != game:
                continue
            previous = by_id.get(record["assetId"])
            if previous is None:
                by_id[record["assetId"]] = record
                continue
            old_hash, new_hash = previous["sha256"], record["sha256"]
            if old_hash and new_hash and old_hash != new_hash:
                raise ValueError(f"Asset ID has conflicting SHA-256 values: {record['assetId']}")
            rank = {"supabase": 3, "public": 2, "bundled": 1}
            current_score = (bool(new_hash), record["status"] == "ready", rank.get(record["storage"], 0), record["path"])
            previous_score = (bool(old_hash), previous["status"] == "ready", rank.get(previous["storage"], 0), previous["path"])
            if current_score > previous_score:
                by_id[record["assetId"]] = record

    records = sorted(by_id.values(), key=lambda item: item["assetId"])
    hashes_by_path: dict[str, str | None] = {}
    for record in records:
        previous = hashes_by_path.get(record["path"])
        if previous and record["sha256"] and previous != record["sha256"]:
            raise ValueError(f"Different asset hashes claim the same storage path: {record['path']}")
        if record["sha256"]:
            hashes_by_path[record["path"]] = record["sha256"]
        elif record["path"] not in hashes_by_path:
            hashes_by_path[record["path"]] = None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps({"schemaVersion": 1, "assets": records}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(output_path)
    return {"game": game, "assets": len(records), "manifest": str(output_path)}


def merge_all_contracts(input_paths: list[Path], output_path: Path) -> dict:
    """Build one cross-game manifest for consumers that load a single contract."""
    if not input_paths:
        raise ValueError("At least one input contract is required")
    by_id: dict[str, dict] = {}
    hashes_by_path: dict[str, str | None] = {}
    storage_rank = {"supabase": 3, "public": 2, "bundled": 1}
    for input_path in input_paths:
        document = json.loads(input_path.read_text(encoding="utf-8"))
        if document.get("schemaVersion") != 1 or not isinstance(document.get("assets"), list):
            raise ValueError(f"Input must use Asset Contract v1: {input_path}")
        for raw in document["assets"]:
            record = dict(raw)
            Asset(**record).validate()
            previous = by_id.get(record["assetId"])
            if previous is not None:
                if any(previous[key] != record[key] for key in ("game", "entityType", "entityId", "variant", "path")):
                    raise ValueError(f"Asset ID has conflicting canonical identity: {record['assetId']}")
                if previous["sha256"] and record["sha256"] and previous["sha256"] != record["sha256"]:
                    raise ValueError(f"Asset ID has conflicting SHA-256 values: {record['assetId']}")
                old_rank = (previous["status"] == "ready", storage_rank.get(previous["storage"], 0), bool(previous["sha256"]))
                new_rank = (record["status"] == "ready", storage_rank.get(record["storage"], 0), bool(record["sha256"]))
                if new_rank > old_rank:
                    by_id[record["assetId"]] = record
            else:
                by_id[record["assetId"]] = record
            previous_hash = hashes_by_path.get(record["path"])
            if previous_hash and record["sha256"] and previous_hash != record["sha256"]:
                raise ValueError(f"Different asset hashes claim the same storage path: {record['path']}")
            if record["sha256"]:
                hashes_by_path[record["path"]] = record["sha256"]
            elif record["path"] not in hashes_by_path:
                hashes_by_path[record["path"]] = None

    records = sorted(by_id.values(), key=lambda item: item["assetId"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps({"schemaVersion": 1, "assets": records}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(output_path)
    return {"games": sorted({record["game"] for record in records}), "assets": len(records), "manifest": str(output_path)}


def _url(base: str, bucket: str, object_path: str) -> str:
    encoded_path = "/".join(quote(part, safe="") for part in object_path.split("/"))
    return f"{base.rstrip('/')}/storage/v1/object/{quote(bucket, safe='')}/{encoded_path}"


def _request(url: str, method: str, key: str, data: bytes | None = None, content_type: str | None = None):
    headers = {}
    if key:
        headers["apikey"] = key
        # Opaque sb_* API keys are not JWT bearer tokens.
        if not key.startswith("sb_"):
            headers["Authorization"] = f"Bearer {key}"
    if content_type:
        # Manifests are mutable pointers to the current inventory, so keep
        # their CDN TTL short. Image paths are stable but may be republished.
        cache_control = "60" if content_type == "application/json" else "86400"
        headers.update({"Content-Type": content_type, "x-upsert": "true", "cache-control": cache_control})
    request = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            return response.status, response.headers.get_content_type(), response.read(20_000_001)
    except HTTPError as error:
        return error.code, error.headers.get_content_type(), error.read(4096)
    except URLError as error:
        return 0, "", str(error.reason).encode("utf-8")


def _is_missing_object(status: int, body: bytes) -> bool:
    if status == 404:
        return True
    if status != 400:
        return False
    try:
        error = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return error.get("code") == "NoSuchKey" or error.get("error") == "not_found"


def import_contract(input_path: Path, assets_root: Path, output_path: Path, storage: str | None = None) -> dict:
    """Bootstrap a canonical manifest from an existing v1 consumer contract after revalidation."""
    document = json.loads(input_path.read_text(encoding="utf-8"))
    if document.get("schemaVersion") != 1 or not isinstance(document.get("assets"), list):
        raise ValueError("Input must be an Asset Contract v1 manifest")
    output = []
    seen = set()
    root = assets_root.resolve()
    for source in document["assets"]:
        record = dict(source)
        canonical_games = {"genshin-impact": "genshin", "star-rail": "hsr", "honkai-star-rail": "hsr", "zenless-zone-zero": "zzz", "wuthering-waves": "wuwa"}
        if record.get("game") in canonical_games:
            record["game"] = canonical_games[record["game"]]
            record["assetId"] = f"{record['game']}.{record['entityType']}.{record['entityId']}.{record['variant']}"
        if storage:
            record["storage"] = storage
        Asset(**record).validate()
        if record["assetId"] in seen:
            raise ValueError(f"Duplicate assetId: {record['assetId']}")
        seen.add(record["assetId"])
        if record["status"] == "ready":
            relative = PurePosixPath(record["path"])
            source_file = (root / Path(*relative.parts)).resolve()
            if not source_file.is_relative_to(root) or not source_file.is_file():
                raise ValueError(f"Missing source bytes for {record['assetId']}")
            content = source_file.read_bytes()
            if not content or hashlib.sha256(content).hexdigest() != record["sha256"]:
                raise ValueError(f"Source SHA-256 mismatch for {record['assetId']}")
            decode_and_normalize(content)
        output.append(record)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp = output_path.with_suffix(output_path.suffix + ".tmp")
    temp.write_text(json.dumps({"schemaVersion": 1, "assets": output}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(output_path)
    return {"assets": len(output), "ready": sum(item["status"] == "ready" for item in output), "manifest": str(output_path)}


def import_genentering_contract(input_path: Path, assets_root: Path, output_path: Path,
                                materials_path: Path | None = None) -> dict:
    """Merge the offline product subset with canonical GameData material results."""
    staged_base = output_path.with_suffix(output_path.suffix + ".base.tmp")
    import_contract(input_path, assets_root, staged_base, "bundled")
    base = json.loads(staged_base.read_text(encoding="utf-8"))
    logical_manifest_path = assets_root / "manifest.json"
    if logical_manifest_path.is_file():
        logical_manifest = json.loads(logical_manifest_path.read_text(encoding="utf-8"))
        identities: dict[str, list[tuple[str, str]]] = {}
        for entity_id, variants in logical_manifest.get("characters", {}).items():
            if not isinstance(variants, dict):
                continue
            for variant, relative_path in variants.items():
                if isinstance(relative_path, str):
                    identities.setdefault(PurePosixPath(relative_path).as_posix(), []).append((entity_id, variant))
        remapped = []
        for record in base["assets"]:
            aliases = identities.get(record.get("path")) if record.get("entityType") == "character" else None
            for entity_id, variant in aliases or [(record["entityId"], record["variant"])]:
                item = dict(record)
                item["entityId"] = canonical_id(entity_id)
                item["variant"] = "icon" if canonical_id(variant) == "portrait" else canonical_id(variant)
                item["assetId"] = f"{item['game']}.{item['entityType']}.{item['entityId']}.{item['variant']}"
                item["source"] = {**item["source"], "sourceId": entity_id}
                remapped.append(item)
        unique = {}
        for item in remapped:
            Asset(**item).validate()
            previous = unique.get(item["assetId"])
            if previous is not None and previous != item:
                raise ValueError(f"Conflicting GenEntering logical asset ID: {item['assetId']}")
            unique[item["assetId"]] = item
        base["assets"] = list(unique.values())

    if materials_path is None or not materials_path.is_file():
        records = base["assets"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp = output_path.with_suffix(output_path.suffix + ".tmp")
        temp.write_text(json.dumps({"schemaVersion": 1, "assets": records}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temp.replace(output_path)
        staged_base.unlink(missing_ok=True)
        return {"assets": len(records), "ready": sum(item["status"] == "ready" for item in records), "manifest": str(output_path)}

    extra = json.loads(materials_path.read_text(encoding="utf-8"))
    if extra.get("schemaVersion") != 1 or not isinstance(extra.get("assets"), list):
        raise ValueError("Material manifest must use Asset Contract v1")
    # GameData is authoritative for the material key set. Remove the older
    # imported material aliases before applying its exact 517 canonical rows.
    records = {record["assetId"]: record for record in base["assets"] if record["entityType"] != "material"}
    root = assets_root.resolve()
    for record in extra["assets"]:
        Asset(**record).validate()
        if record["entityType"] != "material" or record["game"] != "genshin":
            raise ValueError("GenEntering material manifest contains an unsupported record")
        current = records.get(record["assetId"])
        if current and (current["path"] != record["path"] or current["sha256"] != record["sha256"]):
            raise ValueError(f"Conflicting material asset record: {record['assetId']}")
        if record["status"] == "ready":
            source = (root / Path(*PurePosixPath(record["path"]).parts)).resolve()
            if not source.is_relative_to(root) or not source.is_file():
                raise ValueError(f"Missing GenEntering material bytes: {record['assetId']}")
            data = source.read_bytes()
            if not data or hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise ValueError(f"GenEntering material hash mismatch: {record['assetId']}")
            decode_and_normalize(data)
        records[record["assetId"]] = record
    base["assets"] = sorted(records.values(), key=lambda item: item["assetId"])
    digests_by_path: dict[str, str | None] = {}
    for record in base["assets"]:
        digest = record["sha256"]
        previous = digests_by_path.setdefault(record["path"], digest)
        if previous != digest:
            raise ValueError(f"Different asset hashes claim the same storage path: {record['path']}")
    temp = output_path.with_suffix(output_path.suffix + ".tmp")
    temp.write_text(json.dumps(base, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(output_path)
    staged_base.unlink(missing_ok=True)
    result = {"assets": len(base["assets"]), "ready": sum(item["status"] == "ready" for item in base["assets"]), "manifest": str(output_path)}
    return result


def publish_supabase(manifest_path: Path, assets_root: Path, base_url: str, bucket: str, key: str,
                     *, manifest_object_path: str | None = None, dry_run: bool = False,
                     verify_only: bool = False) -> dict:
    """Upload ready bytes, verify public GET/hash/decode, then publish the manifest last."""
    if not dry_run and (not base_url.startswith("https://") or not bucket or (not key and not verify_only)):
        raise ValueError("HTTPS Supabase URL and bucket are required; upload mode also requires a write-authorized service key")
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    if document.get("schemaVersion") != 1 or not isinstance(document.get("assets"), list):
        raise ValueError("Canonical manifest must use Asset Contract v1")
    # The remote contract describes the destination adapter, even when the
    # source manifest was created from local/public bytes.
    for record in document["assets"]:
        if record.get("status") == "ready":
            record["storage"] = "supabase"
    root = assets_root.resolve()
    digests_by_path: dict[str, str] = {}
    for record in document["assets"]:
        if record["status"] != "ready":
            continue
        previous = digests_by_path.setdefault(record["path"], record["sha256"])
        if previous != record["sha256"]:
            raise ValueError(f"Different asset hashes claim the same storage path: {record['path']}")
    prepared = []
    for record in document["assets"]:
        Asset(**record).validate()
        if record["status"] != "ready":
            continue
        relative = PurePosixPath(record["path"])
        local = (root / Path(*relative.parts)).resolve()
        if not local.is_relative_to(root) or not local.is_file():
            prepared.append((record, None, None, "source_missing"))
            continue
        content = local.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if not content or digest != record["sha256"]:
            prepared.append((record, None, None, "source_hash_mismatch"))
            continue
        try:
            decode_and_normalize(content)
        except Exception:
            prepared.append((record, None, None, "source_decode_failed"))
            continue
        prepared.append((record, local, content, None))

    def process(item):
        record, local, content, preflight_error = item
        if preflight_error:
            return {"assetId": record["assetId"], "status": preflight_error}
        digest = hashlib.sha256(content).hexdigest()
        if dry_run:
            return {"assetId": record["assetId"], "status": "validated"}
        object_url = _url(base_url, bucket, record["path"])
        public_url = object_url.replace("/object/", "/object/public/", 1)
        auth_key = key or ""
        if verify_only:
            # A GET is required to verify content anyway; avoid an extra HEAD
            # request for every object in read-only coverage scans.
            get_status, get_type, remote = _request(public_url, "GET", auth_key)
            if _is_missing_object(get_status, remote):
                return {"assetId": record["assetId"], "status": "missing", "reason": "NoSuchKey", "httpStatus": get_status}
            if get_status != 200 or not get_type.startswith("image/"):
                return {"assetId": record["assetId"], "status": "verify_failed", "httpStatus": get_status or None}
            if hashlib.sha256(remote).hexdigest() != digest:
                return {"assetId": record["assetId"], "status": "hash_mismatch", "httpStatus": get_status}
            try:
                decode_and_normalize(remote)
            except Exception:
                return {"assetId": record["assetId"], "status": "remote_decode_failed"}
            return {"assetId": record["assetId"], "status": "ready", "sha256": digest}
        head_status, head_type, _ = _request(public_url, "HEAD", auth_key)
        if head_status == 200 and head_type.startswith("image/"):
            get_status, get_type, remote = _request(public_url, "GET", auth_key)
            if get_status == 200 and get_type.startswith("image/") and hashlib.sha256(remote).hexdigest() == digest:
                try:
                    decode_and_normalize(remote)
                except Exception:
                    return {"assetId": record["assetId"], "status": "remote_decode_failed"}
                return {"assetId": record["assetId"], "status": "ready", "sha256": digest}
        mime = "image/webp" if local.suffix.lower() == ".webp" else (mimetypes.guess_type(local.name)[0] or "application/octet-stream")
        upload_status, _, body = _request(object_url, "POST", key, content, mime)
        if not 200 <= upload_status < 300:
            return {"assetId": record["assetId"], "status": "upload_failed", "httpStatus": upload_status}
        get_status, get_type, remote = _request(public_url, "GET", key)
        if get_status != 200 or not get_type.startswith("image/") or hashlib.sha256(remote).hexdigest() != digest:
            return {"assetId": record["assetId"], "status": "verify_failed", "httpStatus": get_status}
        try:
            decode_and_normalize(remote)
        except Exception:
            return {"assetId": record["assetId"], "status": "remote_decode_failed"}
        return {"assetId": record["assetId"], "status": "ready", "sha256": digest}

    if dry_run or len(prepared) < 2:
        results = [process(item) for item in prepared]
    else:
        with ThreadPoolExecutor(max_workers=min(16, len(prepared))) as pool:
            results = list(pool.map(process, prepared))

    ready_count = sum(item["status"] == "ready" for item in results)
    manifest_url = None
    manifest_verified = False
    manifest_status = None
    manifest_object_path = manifest_object_path or f"manifests/{manifest_path.name}"
    manifest_bytes = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    public_manifest_url = _url(base_url, bucket, manifest_object_path).replace("/object/", "/object/public/", 1)
    if verify_only:
        manifest_status, content_type, remote = _request(public_manifest_url, "GET", key or "")
        manifest_verified = (
            manifest_status == 200
            and content_type == "application/json"
            and hashlib.sha256(remote).hexdigest() == hashlib.sha256(manifest_bytes).hexdigest()
        )
        if manifest_verified:
            manifest_url = public_manifest_url
    elif not dry_run and ready_count == sum(item["status"] == "ready" for item in document["assets"]):
        status, _, _ = _request(_url(base_url, bucket, manifest_object_path), "POST", key, manifest_bytes, "application/json")
        manifest_status = status
        if 200 <= status < 300:
            get_status, content_type, remote = _request(public_manifest_url, "GET", key)
            manifest_status = get_status
            if get_status == 200 and content_type == "application/json" and hashlib.sha256(remote).hexdigest() == hashlib.sha256(manifest_bytes).hexdigest():
                manifest_url = public_manifest_url
                manifest_verified = True
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "supabaseWriteAttempted": not dry_run and not verify_only,
        "manifest": manifest_path.name, "bucket": bucket, "expected": sum(item["status"] == "ready" for item in document["assets"]),
        "ready": ready_count, "coverage": ready_count / max(1, sum(item["status"] == "ready" for item in document["assets"])),
        "manifestPublished": not verify_only and manifest_url is not None,
        "manifestVerified": manifest_verified, "manifestStatus": manifest_status,
        "manifestUrl": manifest_url, "assets": results,
    }

