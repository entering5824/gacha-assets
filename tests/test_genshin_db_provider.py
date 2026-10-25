from __future__ import annotations

from gacha_assets.providers import genshin_db


def test_material_candidates_use_exact_catalog_names_and_declared_icon_filenames(monkeypatch) -> None:
    monkeypatch.setattr(genshin_db, "_catalog", lambda _: [
        {"id": 112138, "name": "Aberrant Core of the Deep Shadow", "images": {"filename_icon": "UI_ItemIcon_112138"}},
        {"id": 100001, "name": "Shared Name", "images": {"filename_icon": "UI_ItemIcon_100001"}},
        {"id": 100002, "name": "Shared Name", "images": {"filename_icon": "UI_ItemIcon_100002"}},
        {"id": 100003, "name": "No Icon", "images": {}},
        {"id": 100004, "name": "Unrelated Item", "images": {"filename_icon": "UI_ItemIcon_100004"}},
    ])

    result = genshin_db.discover_material_asset_candidates([
        {"key": "AberrantCoreOfTheDeepShadow", "name": "Aberrant Core of the Deep Shadow"},
        {"key": "SharedName", "name": "Shared Name"},
        {"key": "NoIcon", "name": "No Icon"},
        {"key": "Unknown", "name": "Unknown Item"},
    ])

    assert result["matchedRecords"] == 2
    assert result["ambiguousKeys"] == ["SharedName"]
    assert result["missingIconVariants"] == ["NoIcon"]
    assert result["unmatchedKeys"] == ["Unknown"]
    assert result["candidates"] == [{
        "materialKey": "AberrantCoreOfTheDeepShadow",
        "provider": "alioth",
        "sourceId": "genshin-db:112138:UI_ItemIcon_112138",
        "mappingProvider": "genshin-db",
        "sourceCatalog": "https://github.com/theBowja/genshin-db",
        "imageUrl": "https://transform.alioth.wiki/i/gi/ItemIcon/UI_ItemIcon_112138.png?f=auto&w=256",
    }]


def test_empty_catalog_does_not_invent_candidates(monkeypatch) -> None:
    monkeypatch.setattr(genshin_db, "_catalog", lambda _: [])
    result = genshin_db.discover_material_asset_candidates([])
    assert result["catalogItems"] == 0
    assert result["candidates"] == []
