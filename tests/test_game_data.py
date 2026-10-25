"""Offline contract, ingestion and distributed failure/retry integration tests."""
import copy
import hashlib
import json
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image

from gacha_assets.game_data.contract import atomic_json, digest, encode, read_json, validate_entity
from gacha_assets.game_data.http import FetchError, JsonClient, Response
from gacha_assets.game_data.ingest import ingest, load_release
from gacha_assets.game_data.provider import NanokaProvider, SourceConfig
from gacha_assets.game_data.publishers import MongoPublisher, SupabasePublisher
from gacha_assets.game_data.sync import synchronize
from gacha_assets.validate import decode_and_normalize


def image_bytes(color="red"):
    stream = BytesIO()
    Image.new("RGB", (3, 3), color).save(stream, "PNG")
    return stream.getvalue()


class Source:
    def __init__(self):
        self.calls = []
        self.documents = {"/manifest.json": {"zzz": {"latest": "test.1"}}}
        for kind in ("character", "weapon", "equipment", "shiyu", "boss", "simul", "monster"):
            row = {"en": "Example"}
            if kind == "character":
                row["icon"] = "UI/ActualIcon.png"
            self.documents[f"/zzz/test.1/{kind}.json"] = {"1001": row}
            if kind != "monster":
                self.documents[f"/zzz/test.1/en/{kind}/1001.json"] = {"id": 1001, "name": "Example", "stats": {"hp": 10}}
        self.image = image_bytes()
        self.image_status = 200

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers))
        path = urlsplit(url).path
        if path.startswith("/assets/zzz/"):
            return Response(self.image_status, {"content-type": "image/webp"}, self.image)
        if path not in self.documents:
            return Response(404, {}, b"")
        if headers.get("If-None-Match") == '"etag"':
            return Response(304, {}, b"")
        return Response(200, {"etag": '"etag"'}, encode(self.documents[path]))


@pytest.fixture
def setup(tmp_path):
    source = Source()
    config = SourceConfig.load(Path("config/sources/zzz-data.json"))
    client = JsonClient(tmp_path / "raw", config.base_url, request=source, sleep=lambda _: None)
    provider = NanokaProvider(config, client)
    normalized = decode_and_normalize(source.image)
    asset = {"assetId": "zzz.character.legacy-slug.icon", "game": "zzz", "entityType": "character",
             "entityId": "legacy-slug", "variant": "icon", "storage": "supabase", "path": "old/icon.webp",
             "source": {"provider": "legacy", "sourceId": "old/icon.webp"},
             "sha256": hashlib.sha256(normalized).hexdigest(), "status": "ready"}
    manifest, mapping = tmp_path / "old.json", tmp_path / "mapping.json"
    atomic_json(manifest, {"schemaVersion": 1, "assets": [asset]})
    atomic_json(mapping, {"schemaVersion": 1, "entities": {"character:1001": "legacy-slug"}, "images": {}})
    root = tmp_path / "data"
    return source, provider, root, manifest, mapping, asset


def stage(setup, **kwargs):
    source, provider, root, manifest, mapping, _ = setup
    return ingest(provider, root, manifest, mapping, **kwargs)


def test_version_and_reference_urls(setup):
    _, provider, *_ = setup
    assert provider.version() == "test.1"
    assert provider.image_url(r"UI\ActualIcon.png") == "https://static.nanoka.cc/assets/zzz/ActualIcon.webp"
    assert len(provider.discover("test.1")) == 7


def test_complete_ingest_reuses_identity_and_preserves_manifest(setup):
    source, _, _, manifest, _, asset = setup
    before = manifest.read_bytes()
    report = stage(setup)
    release = load_release(Path(report["release"]))
    assert report["status"] == "staged" and report["validated"] == 7
    character = next(e for e in release["entities"] if e["entityType"] == "character")
    assert character["sourceId"] == 1001 and isinstance(character["sourceId"], int)
    assert character["canonicalId"] == "zzz.character.source-1001"
    assert character["attributes"]["consumerId"] == "legacy-slug"
    assert character["assets"][0]["assetId"] == asset["assetId"]
    assert release["assets"] == [asset]
    assert manifest.read_bytes() == before
    assert any("/en/equipment/1001.json" in url for url, _ in source.calls)


def test_repeat_run_no_changes_and_no_endpoint_download(setup):
    first = stage(setup)
    setup[0].calls.clear()
    second = stage(setup)
    assert first["syncId"] == second["syncId"]
    assert second["new"] == second["changed"] == 0 and second["unchanged"] == 7
    assert len(setup[0].calls) == 1  # Fresh manifest only; immutable version cache hits.


def test_changed_json_and_refresh_conditional_get(setup):
    first = stage(setup)
    source = setup[0]
    path = "/zzz/test.1/en/character/1001.json"
    source.documents[path]["stats"]["hp"] = 99
    # Simulate an endpoint returning a changed ETag.
    original = source.__call__
    def changed(url, headers, timeout):
        if urlsplit(url).path == path:
            return Response(200, {"etag": "changed"}, encode(source.documents[path]))
        return original(url, headers, timeout)
    setup[1].client.request = changed
    second = stage(setup, refresh=True)
    assert first["syncId"] != second["syncId"]
    assert second["changed"] == 1 and second["unchanged"] == 6


def test_unresolved_mapping_never_guesses_asset_slug(setup):
    atomic_json(setup[4], {"schemaVersion": 1, "entities": {}, "images": {}})
    result = stage(setup)
    release = load_release(Path(result["release"]))
    assert not release["assets"]
    assert any(r["reason"] == "asset_mapping_required" for r in result["unresolved"])
    assert next(e for e in release["entities"] if e["entityType"] == "character")["assets"][0]["status"] == "unresolved"


@pytest.mark.parametrize("status,body", [(404, b""), (200, b"bad"), (200, b"")])
def test_required_image_failure_preserves_last_stage(setup, status, body):
    first = stage(setup)
    setup[0].image_status, setup[0].image = status, body
    failed = stage(setup, refresh=True)
    pointer = read_json(setup[2] / "zzz/en/staged.json")
    assert failed["status"] == "failed" and pointer["syncId"] == first["syncId"]
    assert load_release(Path(first["release"]))


def test_required_detail_failure_never_stages_partial(setup):
    first = stage(setup)
    del setup[0].documents["/zzz/test.1/en/boss/1001.json"]
    failed = stage(setup, refresh=True)
    assert failed["status"] == "failed" and failed["reason"] == "source_http_404"
    assert read_json(setup[2] / "zzz/en/staged.json")["syncId"] == first["syncId"]


def test_invalid_index_and_detail_schema(setup):
    source, provider, *_ = setup
    source.documents["/zzz/test.1/character.json"] = {"1001": []}
    with pytest.raises(ValueError, match="index"):
        provider.discover("test.1")
    source.documents["/zzz/test.1/character.json"] = {"1001": {}}
    source.documents["/zzz/test.1/en/character/1001.json"] = {"id": 9999}
    indexes = provider.discover("test.1", refresh=True)
    with pytest.raises(ValueError, match="identity"):
        provider.records("test.1", indexes)


@pytest.mark.parametrize("failure", [429, 500, 503, TimeoutError()])
def test_retry_transients_and_timeout(tmp_path, failure):
    calls, waits = [], []
    def transport(url, headers, timeout):
        calls.append(url)
        if len(calls) < 3:
            if isinstance(failure, Exception):
                raise failure
            return Response(failure, {"retry-after": "1"}, b"")
        return Response(200, {}, b'{"zzz":{"latest":"x"}}')
    client = JsonClient(tmp_path, "https://test.example", request=transport, sleep=waits.append)
    assert client.fetch("zzz", "latest", "en", "manifest")["zzz"]["latest"] == "x"
    assert len(calls) == 3 and len(waits) == 2
    assert waits[1] >= waits[0]


def test_permanent_http_error_no_retry(tmp_path):
    calls = []
    def transport(*args):
        calls.append(1)
        return Response(404, {}, b"")
    client = JsonClient(tmp_path, "https://test.example", request=transport)
    with pytest.raises(FetchError) as exc:
        client.fetch("zzz", "x", "en", "character")
    assert exc.value.status == 404 and len(calls) == 1


def test_cache_separates_version_locale_source_and_checksum(tmp_path):
    calls = []
    def transport(*args):
        calls.append(1)
        return Response(200, {}, b'{"ok":1}')
    client = JsonClient(tmp_path, "https://a.example", request=transport)
    for version, locale in [("1", "en"), ("1", "en"), ("2", "en"), ("1", "vi")]:
        client.fetch("zzz", version, locale, "character")
    JsonClient(tmp_path, "https://b.example", request=transport).fetch("zzz", "1", "en", "character")
    assert len(calls) == 4
    cached = next(tmp_path.rglob("character.json"))
    data = read_json(cached)
    data["hash"] = "invalid"
    atomic_json(cached, data)
    # All cache entries remain isolated and tampered entry is refetched.
    for version, locale in [("1", "en"), ("2", "en"), ("1", "vi")]:
        client.fetch("zzz", version, locale, "character")
    JsonClient(tmp_path, "https://b.example", request=transport).fetch("zzz", "1", "en", "character")
    assert len(calls) == 5


def test_hash_validation_and_schema_reject_tampering(setup):
    report = stage(setup)
    release = load_release(Path(report["release"]))
    invalid = copy.deepcopy(release["entities"][0])
    invalid["sourceId"] = []
    with pytest.raises(Exception):
        validate_entity(invalid)
    invalid = copy.deepcopy(release["entities"][0])
    invalid["names"] = {"en": "tampered"}
    with pytest.raises(ValueError, match="hash"):
        validate_entity(invalid)
    release["sourceVersion"] = "tampered"
    atomic_json(Path(report["release"]), release)
    with pytest.raises(ValueError, match="checksum"):
        load_release(Path(report["release"]))


def test_ingest_dry_run_does_not_create_release_or_touch_legacy(setup):
    original = setup[3].read_bytes()
    result = stage(setup, dry_run=True)
    assert result["status"] == "dry-run" and "release" not in result
    assert not (setup[2] / "zzz/en/staged.json").exists()
    assert setup[3].read_bytes() == original


class Operation:
    def __init__(self, selector, update, upsert):
        self.selector, self.update = selector, update
        assert upsert


class Collection:
    def __init__(self):
        self.rows, self.writes, self.fail = {}, 0, False

    def find(self, selector):
        return [copy.deepcopy(self.rows[key]) for key in selector["_id"]["$in"] if key in self.rows]

    def create_index(self, fields, unique, name):
        assert unique and fields == [("game", 1), ("locale", 1), ("canonicalId", 1), ("contentHash", 1)]

    def bulk_write(self, operations, ordered):
        for operation in operations:
            self.update_one(operation.selector, operation.update, upsert=True)
        if self.fail:
            self.fail = False
            raise RuntimeError("timeout AFTER server commit")

    def update_one(self, selector, update, upsert):
        key = selector["_id"]
        if key not in self.rows:
            self.rows[key] = {"_id": key, **copy.deepcopy(update["$setOnInsert"])}
            self.writes += 1

    def find_one(self, selector):
        return copy.deepcopy(self.rows.get(selector["_id"]))


def mongo_publisher():
    return MongoPublisher(Collection(), Collection(), operation_factory=Operation, sleep=lambda _: None)


def test_mongo_bulk_upsert_idempotent_retry_and_verify_only(setup):
    result = stage(setup)
    path = Path(result["release"])
    release = load_release(path)
    mongo = mongo_publisher()
    mongo.collection.fail = True
    assert mongo.publish(release, path.parent)["status"] == "verified"
    writes = mongo.collection.writes, mongo.releases.writes
    assert writes == (7, 1)
    assert mongo.publish(release, path.parent)["written"] == 0
    assert mongo.publish(release, path.parent, verify_only=True)["status"] == "verified"
    assert writes == (mongo.collection.writes, mongo.releases.writes)
    assert mongo_publisher().publish(release, path.parent, dry_run=True)["status"] == "dry-run"


class SupabaseMock:
    def __init__(self):
        self.objects, self.rows, self.calls = {}, {}, []
        self.current, self.fail_image, self.fail_metadata = None, False, False

    def request(self, url, method, key, data=None, content_type=None):
        self.calls.append((method, url))
        url = url.replace("/object/public/", "/object/")
        if method == "POST":
            self.objects[url] = data
            return 200, content_type, b"{}"
        if url in self.objects:
            body = b"corrupt" if self.fail_image and url.endswith(".webp") else self.objects[url]
            return 200, "application/json", body
        return 404, "application/json", b"{}"

    def rest(self, path, method="GET", payload=None):
        self.calls.append((method, path))
        if path == "rpc/activate_game_data_release":
            if self.current != payload["p_expected"]:
                raise ValueError("concurrent release")
            self.current = payload["p_sync_id"]
            return None
        if path == "rpc/record_game_data_sync_run":
            return None
        table, _, query = path.partition("?")
        if table == "game_data_active":
            return [{"sync_id": self.current}] if self.current else []
        rows = self.rows.setdefault(table, {})
        if method == "POST":
            for row in payload:
                key = row.get("sync_id") or row["content_hash"]
                rows.setdefault(key, copy.deepcopy(row))
            return []
        query = parse_qs(query)
        def match(row, key, value):
            if value.startswith("in.("):
                return row[key] in value[4:-1].split(",")
            return str(row[key]) == value[3:]
        result = [row for row in rows.values() if all(match(row, k, values[0]) for k, values in query.items())]
        if self.fail_metadata and table == "game_data_entities":
            return []
        return result


def supabase_publisher():
    mock = SupabaseMock()
    return SupabasePublisher("https://supabase.example", "bucket", "test-key", request=mock.request, rest=mock.rest,
                             sleep=lambda _: None), mock


def test_supabase_publish_verify_manifest_last_and_incremental(setup):
    result = stage(setup)
    path = Path(result["release"])
    release = load_release(path)
    supabase, mock = supabase_publisher()
    assert supabase.publish(release, path.parent)["status"] == "verified"
    object_posts = [url for method, url in mock.calls if method == "POST" and "/object/" in url]
    assert "/releases/" in object_posts[-1]
    writes = len(object_posts)
    supabase.publish(release, path.parent)
    supabase.publish(release, path.parent, verify_only=True)
    assert len([1 for method, url in mock.calls if method == "POST" and "/object/" in url]) == writes


@pytest.mark.parametrize("failure", ["image", "metadata"])
def test_supabase_failure_does_not_publish_release_manifest(setup, failure):
    result = stage(setup)
    path = Path(result["release"])
    supabase, mock = supabase_publisher()
    if failure == "metadata":
        mock.fail_metadata = True
    else:
        # Simulate an existing conflicting remote object: never overwrite it.
        mock.objects["https://supabase.example/storage/v1/object/bucket/old/icon.webp"] = b"bad"
    with pytest.raises(ValueError):
        supabase.publish(load_release(path), path.parent)
    assert not any("/releases/" in url for url in mock.objects)


def test_partial_destination_failure_keeps_active_then_retry_succeeds(setup):
    result = stage(setup)
    path = Path(result["release"])
    supabase, mock = supabase_publisher()
    mock.current = "previous"
    class Broken:
        def publish(self, *args, **kwargs):
            raise RuntimeError("must never expose mongodb://secret:password@host")
    failed = synchronize(path, {"supabase": supabase, "mongodb": Broken()}, dry_run=False, activate=True, confirmed=True)
    assert failed["status"] == "failed" and mock.current == "previous"
    assert "password" not in json.dumps(failed)
    mongo = mongo_publisher()
    success = synchronize(path, {"supabase": supabase, "mongodb": mongo}, dry_run=False, activate=True, confirmed=True)
    assert success["status"] == "active" and mock.current == result["syncId"]
    count = mongo.collection.writes
    synchronize(path, {"supabase": supabase, "mongodb": mongo}, dry_run=False, activate=True, confirmed=True)
    assert mongo.collection.writes == count


def test_remote_write_requires_confirmation_and_dry_run_is_offline(setup):
    result = stage(setup)
    path = Path(result["release"])
    supabase, mock = supabase_publisher()
    with pytest.raises(ValueError, match="confirm"):
        synchronize(path, {"supabase": supabase}, required=("supabase",), dry_run=False)
    report = synchronize(path, {"supabase": supabase, "mongodb": mongo_publisher()}, dry_run=True, activate=True)
    assert report["status"] == "dry-run" and not mock.calls


def test_missing_discovery_retains_old_entity_and_reports_candidate(setup):
    first = stage(setup)
    source = setup[0]
    source.documents["/zzz/test.1/character.json"] = {"2002": {"en": "Another"}}
    source.documents["/zzz/test.1/en/character/2002.json"] = {"id": 2002, "name": "Another"}
    previous = source.__call__
    def refresh(url, headers, timeout):
        return previous(url, {}, timeout)
    setup[1].client.request = refresh
    second = stage(setup, refresh=True)
    release = load_release(Path(second["release"]))
    assert second["diff"]["missingCandidates"] == ["zzz.character.source-1001"]
    assert len(release["entities"]) == 8
    assert release["assets"] == load_release(Path(first["release"]))["assets"]


def test_packaged_schema_matches_public_contract():
    assert read_json(Path("schema/game-data-contract-v1.schema.json")) == read_json(Path("src/gacha_assets/game_data/contract-v1.schema.json"))


def test_no_json_cache_promotion_for_invalid_source(tmp_path):
    client = JsonClient(tmp_path, "https://test.example", request=lambda *args: Response(200, {}, b"[]"))
    with pytest.raises(ValueError, match="JSON object"):
        client.fetch("zzz", "v1", "en", "weapon")
    assert not list(tmp_path.rglob("*.json"))


def test_refresh_bad_json_keeps_previous_valid_cache(tmp_path):
    client = JsonClient(tmp_path, "https://test.example", request=lambda *args: Response(200, {}, b'{"ok":1}'))
    client.fetch("zzz", "v1", "en", "weapon")
    client.request = lambda *args: Response(200, {}, b"{invalid")
    with pytest.raises(json.JSONDecodeError):
        client.fetch("zzz", "v1", "en", "weapon", fresh=True)
    assert client.fetch("zzz", "v1", "en", "weapon") == {"ok": 1}


def test_refresh_bad_image_keeps_valid_image_cache(setup):
    first = stage(setup)
    cached = list((setup[2] / "zzz/test.1/en/images").iterdir())
    before = {p: p.read_bytes() for p in cached}
    setup[0].image = b"bad image"
    assert stage(setup, refresh=True)["status"] == "failed"
    assert before == {p: p.read_bytes() for p in cached}
    assert load_release(Path(first["release"]))


def test_local_image_checksum_rejected_before_remote_or_dry_run(setup):
    result = stage(setup)
    path = Path(result["release"])
    release = load_release(path)
    local = path.parent / "files" / release["assets"][0]["path"]
    local.write_bytes(b"tampered")
    supabase, mock = supabase_publisher()
    with pytest.raises(ValueError, match="checksum"):
        synchronize(path, {"supabase": supabase}, required=("supabase",), dry_run=True)
    assert not mock.calls


def test_existing_variants_survive_ingest_and_remote_verification(setup):
    doc = read_json(setup[3])
    variant = {**doc["assets"][0], "assetId": "zzz.character.legacy-slug.artwork", "variant": "artwork", "path": "old/artwork.webp"}
    doc["assets"].append(variant)
    atomic_json(setup[3], doc)
    result = stage(setup)
    path = Path(result["release"])
    release = load_release(path)
    assert {a["variant"] for a in release["assets"]} == {"icon", "artwork"}
    supabase, mock = supabase_publisher()
    with pytest.raises(ValueError, match="retained asset"):
        supabase.publish(release, path.parent)
    mock.objects["https://supabase.example/storage/v1/object/bucket/old/artwork.webp"] = decode_and_normalize(setup[0].image)
    assert supabase.publish(release, path.parent)["status"] == "verified"


def test_two_destination_verification_is_read_only(setup):
    result = stage(setup)
    path = Path(result["release"])
    supabase, mock = supabase_publisher()
    mongo = mongo_publisher()
    synchronize(path, {"supabase": supabase, "mongodb": mongo}, dry_run=False, confirmed=True)
    mock.calls.clear()
    writes = mongo.collection.writes, mongo.releases.writes
    report = synchronize(path, {"supabase": supabase, "mongodb": mongo}, dry_run=False, verify_only=True)
    assert report["status"] == "verified" and not report["active"]
    assert all(method == "GET" for method, _ in mock.calls)
    assert writes == (mongo.collection.writes, mongo.releases.writes)


def test_single_destination_cannot_activate(setup):
    result = stage(setup)
    with pytest.raises(ValueError, match="both"):
        synchronize(Path(result["release"]), {"mongodb": mongo_publisher()}, required=("mongodb",), activate=True)


def test_activation_lost_response_does_not_claim_old_pointer_is_preserved(setup):
    result = stage(setup)
    supabase, mock = supabase_publisher()
    original = supabase.activate
    def lost_response(*args):
        original(*args)
        raise TimeoutError("lost response after commit")
    supabase.activate = lost_response
    report = synchronize(Path(result["release"]), {"supabase": supabase, "mongodb": mongo_publisher()},
                         dry_run=False, activate=True, confirmed=True)
    assert mock.current == result["syncId"]
    assert report["status"] == "activation-unknown" and report["active"] is None


def test_source_retry_exhaustion(tmp_path):
    calls, waits = [], []
    def unavailable(*args):
        calls.append(1)
        return Response(503, {}, b"")
    client = JsonClient(tmp_path, "https://test.example", attempts=3, request=unavailable, sleep=waits.append)
    with pytest.raises(FetchError) as error:
        client.fetch("zzz", "v1", "en", "character")
    assert error.value.status == 503 and len(calls) == 3 and waits == [0.5, 1.0]


def test_concurrency_never_exceeds_configured_bound(setup):
    import threading
    import time
    source, provider, *_ = setup
    active, peak = 0, 0
    lock = threading.Lock()
    def bounded(*args):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.01)
            return source(*args)
        finally:
            with lock:
                active -= 1
    provider.client.request = bounded
    indexes = provider.discover("test.1")
    provider.records("test.1", indexes)
    assert 1 < peak <= provider.config.concurrency


def test_bad_mapping_rejected_before_source_requests(setup):
    atomic_json(setup[4], {"schemaVersion": 1, "entities": {"character:1": "same", "character:2": "same"}, "images": {}})
    failed = stage(setup)
    assert failed["status"] == "failed" and failed["reason"] == "ambiguous_entity_mapping"
    assert not setup[0].calls


def test_cli_publish_defaults_to_offline_dry_run(setup, monkeypatch):
    from argparse import Namespace
    from gacha_assets.game_data.cli import execute
    result = stage(setup)
    args = Namespace(data_command="publish-mongodb", source=Path("config/sources/zzz-data.json"),
                     cache_root=setup[2], asset_manifest=setup[3], mapping=setup[4], release=Path(result["release"]),
                     dry_run=False, verify_only=False, confirm=False, target="both", refresh=False)
    # No pymongo dependency or credentials are needed for an offline publication plan.
    assert execute(args)["status"] == "dry-run"


def test_cli_legacy_and_data_commands(capsys, monkeypatch):
    import sys
    from gacha_assets.cli import main
    monkeypatch.setattr(sys, "argv", ["gacha-assets", "--help"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    output = capsys.readouterr().out
    for name in ("data", "run", "publish-supabase", "import-genentering", "merge-manifests"):
        assert name in output
    monkeypatch.setattr(sys, "argv", ["gacha-assets", "data", "--help"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0 and "publish-mongodb" in capsys.readouterr().out
