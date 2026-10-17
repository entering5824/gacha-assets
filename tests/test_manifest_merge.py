import hashlib
import json
from pathlib import Path

import pytest

from gacha_assets.publish import merge_all_contracts


def _record(game: str, entity_id: str, path: str, digest: str) -> dict:
    entity_type = "character"
    return {
        "assetId": f"{game}.{entity_type}.{entity_id}.icon",
        "game": game,
        "entityType": entity_type,
        "entityId": entity_id,
        "variant": "icon",
        "storage": "supabase",
        "path": path,
        "source": {"provider": "fixture", "sourceId": entity_id},
        "sha256": digest,
        "status": "ready",
    }


def _write(path: Path, records: list[dict]) -> None:
    path.write_text(json.dumps({"schemaVersion": 1, "assets": records}), encoding="utf-8")


def test_merge_all_game_manifests_preserves_shared_path_aliases(tmp_path):
    digest = hashlib.sha256(b"same image bytes").hexdigest()
    genshin = tmp_path / "genshin.json"
    hsr = tmp_path / "hsr.json"
    _write(genshin, [
        _record("genshin", "traveler-anemo", "characters/aether.webp", digest),
        _record("genshin", "traveler-dendro", "characters/aether.webp", digest),
    ])
    _write(hsr, [_record("hsr", "march-7th", "hsr/characters/march-7th.webp", digest)])
    output = tmp_path / "merged.json"

    result = merge_all_contracts([genshin, hsr], output)

    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert result["assets"] == 3
    assert result["games"] == ["genshin", "hsr"]
    assert [record["assetId"] for record in manifest["assets"]] == sorted(record["assetId"] for record in manifest["assets"])
    assert len({record["path"] for record in manifest["assets"]}) == 2


def test_merge_all_game_manifests_rejects_conflicting_hashes_without_replacing_output(tmp_path):
    path = "shared/character.webp"
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _write(first, [_record("genshin", "one", path, "a" * 64)])
    _write(second, [_record("hsr", "two", path, "b" * 64)])
    output = tmp_path / "merged.json"
    output.write_text("preserve this", encoding="utf-8")

    with pytest.raises(ValueError, match="Different asset hashes"):
        merge_all_contracts([first, second], output)

    assert output.read_text(encoding="utf-8") == "preserve this"
