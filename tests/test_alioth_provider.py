from gacha_assets.providers import alioth


def card(name, url):
    return f'<button aria-label="{name}"><p>{name}</p><img alt="{name}" src="{url}"></button>'


def test_alioth_discovery_uses_exact_card_name_and_declared_image_url(monkeypatch):
    pages = [
        card(
            "Aberrant Core of the Deep Shadow",
            "https://transform.alioth.wiki/i/gi/ItemIcon/UI_ItemIcon_112138.png?w=256&f=auto",
        ),
        "",
    ]
    urls = []

    def fetch(url):
        urls.append(url)
        return alioth._parse_page(pages.pop(0))

    monkeypatch.setattr(alioth, "_fetch_page", fetch)
    result = alioth.discover_material_asset_candidates(
        [
            {"key": "AberrantCoreOfTheDeepShadow", "name": "Aberrant Core of the Deep Shadow"},
            {"key": "UnlistedMaterial", "name": "Unlisted Material"},
        ],
        max_pages=3,
    )

    assert result["pagesFetched"] == 2
    assert result["matchedRecords"] == 1
    assert result["unmatchedKeys"] == ["UnlistedMaterial"]
    assert result["candidates"] == [{
        "materialKey": "AberrantCoreOfTheDeepShadow",
        "provider": "alioth",
        "sourceId": "UI_ItemIcon_112138",
        "mappingProvider": "alioth-inventory",
        "sourceCatalog": "https://alioth.wiki/en/gi/item",
        "sourceName": "Aberrant Core of the Deep Shadow",
        "imageUrl": "https://transform.alioth.wiki/i/gi/ItemIcon/UI_ItemIcon_112138.png?w=256&f=auto",
    }]
    assert urls == ["https://alioth.wiki/en/gi/item", "https://alioth.wiki/en/gi/item?page=2"]


def test_alioth_discovery_rejects_ambiguous_names(monkeypatch):
    pages = [
        card("Known Material", "https://transform.alioth.wiki/i/gi/ItemIcon/UI_ItemIcon_10001.png")
        + card("Known Material", "https://transform.alioth.wiki/i/gi/ItemIcon/UI_ItemIcon_10002.png"),
        "",
    ]
    monkeypatch.setattr(alioth, "_fetch_page", lambda _url: alioth._parse_page(pages.pop(0)))
    result = alioth.discover_material_asset_candidates([{"key": "Known", "name": "Known Material"}])
    assert result["candidates"] == []
    assert result["ambiguousKeys"] == ["Known"]


def test_alioth_discovery_ignores_non_catalog_image_hosts(monkeypatch):
    html = card("Known Material", "https://evil.example/UI_ItemIcon_10001.png")
    monkeypatch.setattr(alioth, "_fetch_page", lambda _url: alioth._parse_page(html))
    result = alioth.discover_material_asset_candidates([{"key": "Known", "name": "Known Material"}])
    assert result["candidates"] == []
    assert result["unmatchedKeys"] == ["Known"]


def test_alioth_name_normalization_is_stable_for_punctuation_and_diacritics():
    assert alioth._normalized_name("Frostnight's Glory") == alioth._normalized_name("Frostnight’s Glory")
    assert alioth._normalized_name("Café Hero") == alioth._normalized_name("Cafe Hero")
