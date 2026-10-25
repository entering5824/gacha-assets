from __future__ import annotations

from threading import Lock

from gacha_assets.providers import genshin_dev


def test_material_discovery_requires_a_unique_key_and_available_icon(monkeypatch) -> None:
    active = 0
    maximum_active = 0
    lock = Lock()

    def fake_json(url: str):
        nonlocal active, maximum_active
        if url.endswith("/materials/all?lang=en"):
            return {
                "boss": {"name": "A Flower Yet to Bloom"},
                "ambiguous-one": {"name": "Shared Name"},
                "ambiguous-two": {"name": "Shared Name"},
                "no-icon": {"name": "No Icon Material"},
            }
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            if url.endswith("/no-icon/list"):
                return ["card"]
            return ["icon.webp"]
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(genshin_dev, "_json", fake_json)
    rows = [
        {"key": "Boss", "name": "Something else"},
        {"key": "SharedName", "name": "Shared Name"},
        {"key": "NoIcon", "name": "No Icon Material"},
        {"key": "Unknown", "name": "Unknown Material"},
    ]

    result = genshin_dev.discover_material_candidates(rows, "https://assets.example")

    assert result["matchedRecords"] == 2
    assert result["ambiguousKeys"] == ["SharedName"]
    assert result["missingIconVariants"] == ["NoIcon"]
    assert result["unmatchedKeys"] == ["Unknown"]
    assert result["candidates"] == [{
        "materialKey": "Boss",
        "provider": "genshin.dev",
        "sourceId": "boss",
        "imageUrl": "https://assets.example/materials/boss/icon",
    }]
    assert 1 <= maximum_active <= 8


def test_material_discovery_propagates_upstream_errors(monkeypatch) -> None:
    def fake_json(url: str):
        if url.endswith("/materials/all?lang=en"):
            return {"boss": {"name": "Boss"}}
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr(genshin_dev, "_json", fake_json)

    try:
        genshin_dev.discover_material_candidates([{"key": "boss", "name": "Boss"}])
    except RuntimeError as error:
        assert str(error) == "upstream unavailable"
    else:
        raise AssertionError("upstream errors must fail discovery")
