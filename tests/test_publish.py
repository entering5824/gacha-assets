import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from gacha_assets import publish
from gacha_assets.env import load_local_env
from gacha_assets.publish import import_contract, import_genentering_contract, merge_contracts, publish_supabase


def _webp(path: Path) -> bytes:
    Image.new("RGB", (5, 4), (12, 34, 56)).save(path, "WEBP", lossless=True)
    return path.read_bytes()


def _contract(tmp_path: Path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    image = source_root / "assets" / "games" / "genshin-impact" / "characters" / "one.webp"
    image.parent.mkdir(parents=True)
    content = _webp(image)
    record = {
        "assetId": "genshin.character.one.icon", "game": "genshin", "entityType": "character",
        "entityId": "one", "variant": "icon", "storage": "public", "path": "assets/games/genshin-impact/characters/one.webp",
        "source": {"provider": "enka", "sourceId": "one"}, "sha256": hashlib.sha256(content).hexdigest(), "status": "ready",
    }
    manifest = tmp_path / "input.json"
    manifest.write_text(json.dumps({"schemaVersion": 1, "assets": [record]}), encoding="utf-8")
    return source_root, image, manifest, record



def test_local_env_loader_preserves_process_values_and_parses_comments(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('''# local settings
SUPABASE_URL="https://local.example" # ignored comment
SUPABASE_BUCKET=GachaCounter
SUPABASE_STORAGE_API_KEY='opaque-value'
invalid line
''', encoding="utf-8")
    values = {"SUPABASE_URL": "https://process.example"}

    load_local_env(env_file, values)

    assert values == {
        "SUPABASE_URL": "https://process.example",
        "SUPABASE_BUCKET": "GachaCounter",
        "SUPABASE_STORAGE_API_KEY": "opaque-value",
    }


def test_secret_supabase_api_key_is_sent_only_in_apikey_header(monkeypatch):
    captured = {}

    class Headers:
        def get_content_type(self):
            return "application/json"

    class Response:
        status = 200
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b"ok"

    def open_url(request, timeout):
        captured["apikey"] = request.get_header("Apikey")
        captured["authorization"] = request.get_header("Authorization")
        return Response()

    monkeypatch.setattr(publish, "urlopen", open_url)
    assert publish._request("https://storage.example/object", "GET", "sb_secret_test") == (
        200, "application/json", b"ok"
    )
    assert captured["apikey"] == "sb_secret_test"
    assert captured["authorization"] is None


def test_supabase_upload_uses_shorter_cache_ttl_for_mutable_manifests(monkeypatch):
    cache_controls = []

    class Headers:
        def get_content_type(self):
            return "application/json"

    class Response:
        status = 200
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b"ok"

    def open_url(request, timeout):
        cache_controls.append(request.get_header("Cache-control"))
        return Response()

    monkeypatch.setattr(publish, "urlopen", open_url)
    publish._request("https://storage.example/image", "POST", "sb_test", b"image", "image/webp")
    publish._request("https://storage.example/manifest", "POST", "sb_test", b"{}", "application/json")

    assert cache_controls == ["86400", "60"]

def test_import_contract_revalidates_and_can_set_consumer_storage(tmp_path):
    root, _, source, record = _contract(tmp_path)
    output = tmp_path / "canonical.json"
    result = import_contract(source, root, output, "supabase")
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert result["ready"] == 1
    assert loaded["assets"][0]["storage"] == "supabase"
    assert loaded["assets"][0]["sha256"] == record["sha256"]


def test_import_contract_rejects_hash_mismatch_without_replacing_manifest(tmp_path):
    root, image, source, _ = _contract(tmp_path)
    output = tmp_path / "canonical.json"
    output.write_text("previous", encoding="utf-8")
    image.write_bytes(b"changed")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        import_contract(source, root, output)
    assert output.read_text(encoding="utf-8") == "previous"


def test_supabase_publish_verifies_files_then_publishes_manifest_last(tmp_path, monkeypatch):
    root, image, source, record = _contract(tmp_path)
    canonical = tmp_path / "canonical.json"
    import_contract(source, root, canonical, "supabase")
    uploaded = []
    content = image.read_bytes()
    manifest_bytes = (json.dumps(json.loads(canonical.read_text(encoding="utf-8")), indent=2, ensure_ascii=False) + "\n").encode()

    def request(url, method, key, data=None, content_type=None):
        assert key == "service-key"
        if method == "HEAD":
            return 404, "application/json", b""
        if method == "POST":
            uploaded.append((url, content_type, data))
            return 200, "application/json", b"{}"
        if "/manifests/" in url:
            return 200, "application/json", manifest_bytes
        return 200, "image/webp", content

    monkeypatch.setattr(publish, "_request", request)
    report = publish_supabase(canonical, root, "https://assets.example", "bucket", "service-key")
    assert report["ready"] == report["expected"] == 1
    assert report["supabaseWriteAttempted"] is True
    assert report["generatedAt"].endswith("Z")
    assert report["manifestPublished"] is True
    assert len(uploaded) == 2
    assert uploaded[0][1] == "image/webp"
    assert uploaded[1][1] == "application/json"


def test_supabase_remote_manifest_uses_supabase_storage_for_ready_assets(tmp_path, monkeypatch):
    root, _, source, _ = _contract(tmp_path)
    remote_manifests = []

    def request(url, method, key, data=None, content_type=None):
        if method == "HEAD":
            return 404, "application/json", b""
        if method == "POST" and content_type == "application/json":
            remote_manifests.append(json.loads(data))
            return 200, "application/json", b"{}"
        if "/manifests/" in url:
            payload = (json.dumps(remote_manifests[-1], indent=2, ensure_ascii=False) + "\n").encode()
            return 200, "application/json", payload
        image = next(iter((root / "assets").rglob("*.webp"))).read_bytes()
        return 200, "image/webp", image

    monkeypatch.setattr(publish, "_request", request)
    report = publish_supabase(source, root, "https://assets.example", "bucket", "service-key")
    assert report["manifestPublished"] is True
    assert remote_manifests[0]["assets"][0]["storage"] == "supabase"
    assert json.loads(source.read_text(encoding="utf-8"))["assets"][0]["storage"] == "public"


def test_supabase_dry_run_never_makes_network_requests(tmp_path, monkeypatch):
    root, _, source, _ = _contract(tmp_path)
    canonical = tmp_path / "canonical.json"
    import_contract(source, root, canonical, "supabase")
    monkeypatch.setattr(publish, "_request", lambda *args, **kwargs: pytest.fail("network called in dry run"))
    report = publish_supabase(canonical, root, "", "", "", dry_run=True)
    assert report["ready"] == 0
    assert report["assets"][0]["status"] == "validated"


def test_supabase_verify_only_classifies_missing_storage_objects(tmp_path, monkeypatch):
    root, _, source, _ = _contract(tmp_path)
    canonical = tmp_path / "canonical.json"
    import_contract(source, root, canonical, "supabase")
    calls = []

    def request(url, method, key, data=None, content_type=None):
        calls.append(method)
        if method == "HEAD":
            return 400, "application/json", b""
        return 400, "application/json", b'{"statusCode":"404","error":"not_found","message":"Object not found","code":"NoSuchKey"}'

    monkeypatch.setattr(publish, "_request", request)
    report = publish_supabase(canonical, root, "https://assets.example", "bucket", "", verify_only=True)

    assert report["supabaseWriteAttempted"] is False
    assert report["generatedAt"].endswith("Z")
    assert calls == ["GET", "GET"]
    assert report["assets"] == [{"assetId": "genshin.character.one.icon", "status": "missing", "reason": "NoSuchKey", "httpStatus": 400}]
    assert report["manifestVerified"] is False
    assert report["manifestStatus"] == 400


def test_supabase_verify_only_confirms_existing_canonical_manifest(tmp_path, monkeypatch):
    root, image, source, _ = _contract(tmp_path)
    canonical = tmp_path / "canonical.json"
    import_contract(source, root, canonical, "supabase")
    record = json.loads(canonical.read_text(encoding="utf-8"))["assets"][0]
    content = image.read_bytes()
    manifest_bytes = (json.dumps(json.loads(canonical.read_text(encoding="utf-8")), indent=2, ensure_ascii=False) + "\n").encode()

    def request(url, method, key, data=None, content_type=None):
        if "/manifests/" in url:
            return 200, "application/json", manifest_bytes
        if method == "HEAD":
            return 200, "image/webp", b""
        return 200, "image/webp", content

    monkeypatch.setattr(publish, "_request", request)
    report = publish_supabase(canonical, root, "https://assets.example", "bucket", "", verify_only=True)

    assert report["assets"][0]["assetId"] == record["assetId"]
    assert report["assets"][0]["status"] == "ready"
    assert report["manifestPublished"] is False
    assert report["manifestVerified"] is True


def test_genentering_import_replaces_stale_material_aliases_with_exact_game_data_keys(tmp_path):
    root = tmp_path / "assets"
    old_file = root / "materials" / "old.webp"
    old_file.parent.mkdir(parents=True)
    old_bytes = _webp(old_file)
    source = tmp_path / "local-contract.json"
    base_record = {
        "assetId": "genshin.material.old-name.icon", "game": "genshin", "entityType": "material",
        "entityId": "old-name", "variant": "icon", "storage": "bundled", "path": "materials/old.webp",
        "source": {"provider": "legacy", "sourceId": "old"}, "sha256": hashlib.sha256(old_bytes).hexdigest(), "status": "ready",
    }
    source.write_text(json.dumps({"schemaVersion": 1, "assets": [base_record]}), encoding="utf-8")
    new_file = root / "materials" / "new.webp"
    new_bytes = _webp(new_file)
    material_record = {**base_record, "assetId": "genshin.material.new-name.icon", "entityId": "new-name", "path": "materials/new.webp", "sha256": hashlib.sha256(new_bytes).hexdigest(), "source": {"provider": "GameData", "sourceId": "NewName"}}
    materials = tmp_path / "materials.json"
    materials.write_text(json.dumps({"schemaVersion": 1, "assets": [material_record]}), encoding="utf-8")
    output = tmp_path / "canonical.json"

    import_genentering_contract(source, root, output, materials)

    records = json.loads(output.read_text(encoding="utf-8"))["assets"]
    assert [record["assetId"] for record in records] == ["genshin.material.new-name.icon"]


def test_genentering_import_uses_logical_camel_case_character_ids_and_keeps_aliases(tmp_path):
    root = tmp_path / "assets"
    image = root / "characters" / "shared.webp"
    image.parent.mkdir(parents=True)
    content = _webp(image)
    (root / "manifest.json").write_text(json.dumps({"characters": {
        "HuTao": {"portrait": "characters/shared.webp", "artwork": "characters/shared.webp"},
        "TravelerAnemo": {"portrait": "characters/shared.webp"},
        "TravelerCryo": {"portrait": "characters/shared.webp"},
    }}), encoding="utf-8")
    source_record = {
        "assetId": "genshin.character.shared.icon", "game": "genshin", "entityType": "character",
        "entityId": "shared", "variant": "icon", "storage": "bundled", "path": "characters/shared.webp",
        "source": {"provider": "legacy", "sourceId": "shared"}, "sha256": hashlib.sha256(content).hexdigest(), "status": "ready",
    }
    source = tmp_path / "legacy.json"
    source.write_text(json.dumps({"schemaVersion": 1, "assets": [source_record]}), encoding="utf-8")
    output = tmp_path / "canonical.json"

    import_genentering_contract(source, root, output)

    records = json.loads(output.read_text(encoding="utf-8"))["assets"]
    assert {record["assetId"] for record in records} == {
        "genshin.character.hu-tao.icon", "genshin.character.hu-tao.artwork",
        "genshin.character.traveler-anemo.icon", "genshin.character.traveler-cryo.icon",
    }


def test_genentering_import_deduplicates_repeated_base_alias_paths(tmp_path):
    root = tmp_path / "assets"
    image = root / "characters" / "traveler.webp"
    image.parent.mkdir(parents=True)
    content = _webp(image)
    (root / "manifest.json").write_text(json.dumps({"characters": {
        "TravelerAnemo": {"portrait": "characters/traveler.webp"},
        "TravelerCryo": {"portrait": "characters/traveler.webp"},
    }}), encoding="utf-8")
    records = []
    for element in ("anemo", "cryo"):
        records.append({
            "assetId": f"genshin.character.traveler-{element}.icon", "game": "genshin", "entityType": "character",
            "entityId": f"traveler-{element}", "variant": "icon", "storage": "bundled", "path": "characters/traveler.webp",
            "source": {"provider": "legacy", "sourceId": f"Traveler{element.title()}"},
            "sha256": hashlib.sha256(content).hexdigest(), "status": "ready",
        })
    source = tmp_path / "legacy.json"
    source.write_text(json.dumps({"schemaVersion": 1, "assets": records}), encoding="utf-8")
    output = tmp_path / "canonical.json"

    import_genentering_contract(source, root, output)

    output_records = json.loads(output.read_text(encoding="utf-8"))["assets"]
    ids = [record["assetId"] for record in output_records]
    assert len(ids) == len(set(ids)) == 2
    assert set(ids) == {"genshin.character.traveler-anemo.icon", "genshin.character.traveler-cryo.icon"}


def test_supabase_publish_rejects_conflicting_hashes_for_same_path_before_network(tmp_path, monkeypatch):
    root, _, source, record = _contract(tmp_path)
    second = {**record, "assetId": "genshin.character.two.icon", "entityId": "two", "sha256": "0" * 64}
    manifest = tmp_path / "collision.json"
    manifest.write_text(json.dumps({"schemaVersion": 1, "assets": [record, second]}), encoding="utf-8")
    monkeypatch.setattr(publish, "_request", lambda *args, **kwargs: pytest.fail("publisher made a network request"))
    with pytest.raises(ValueError, match="same storage path"):
        publish_supabase(manifest, root, "https://assets.example", "bucket", "service-key")


def test_merge_contracts_prefers_central_storage_without_changing_asset_identity(tmp_path):
    _, _, _, record = _contract(tmp_path)
    central = tmp_path / "central.json"
    local = tmp_path / "local.json"
    remote = {**record, "storage": "supabase"}
    central.write_text(json.dumps({"schemaVersion": 1, "assets": [remote]}), encoding="utf-8")
    bundled = {**record, "storage": "bundled", "path": "characters/one.webp"}
    local.write_text(json.dumps({"schemaVersion": 1, "assets": [bundled]}), encoding="utf-8")
    output = tmp_path / "genshin.json"

    result = merge_contracts([local, central], output, "genshin")

    merged = json.loads(output.read_text(encoding="utf-8"))["assets"]
    assert result["assets"] == 1
    assert merged[0]["assetId"] == record["assetId"]
    assert merged[0]["storage"] == "supabase"
    assert merged[0]["sha256"] == record["sha256"]


def test_merge_contracts_rejects_two_hashes_for_the_same_path_before_replace(tmp_path):
    _, _, _, record = _contract(tmp_path)
    other = {**record, "assetId": "genshin.character.two.icon", "entityId": "two", "sha256": "0" * 64}
    source = tmp_path / "input.json"
    source.write_text(json.dumps({"schemaVersion": 1, "assets": [record, other]}), encoding="utf-8")
    output = tmp_path / "manifest.json"
    output.write_text("previous", encoding="utf-8")

    with pytest.raises(ValueError, match="same storage path"):
        merge_contracts([source], output, "genshin")
    assert output.read_text(encoding="utf-8") == "previous"

